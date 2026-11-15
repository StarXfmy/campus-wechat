"""微信平台行为层：判定「这条消息要不要回、能不能回」。

这些规则原来长在鲸鲸（Node 的 src/bot.js）里。拆鲸鲸时只把**平台规则**搬过来：
@ 触发、接话窗口、冷却、静默时段、群里不答个人数据、去重、频率上限、闭麦。
骂人回怼库、「同意被损」、群成员名录、休息模式、表情包随机、魔咒这些是鲸鲸的人格，
按他的决定不搬——微信里说话的是木鱼本人。

主人判定**不在这里**。桥接按 config.json 的 persona.master.wxids 判完传 is_master 进来，
全项目只有那一处判定。

时间单位统一毫秒，与鲸鲸原实现一致，方便逐条对照 shadow-baseline.jsonl。
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time

# 与 config.json 的 group 段同名；缺省值取鲸鲸的 DEFAULT_CONFIG
DEFAULTS = {
    "requireAt": True,
    "freeChat": False,
    "freeChatChance": 0.06,
    "cooldownSec": 45,
    "masterAlwaysReply": False,
    # 严格模式：0 = 只在被叫到时回。他 2026-11-15 明确要求群里别自动接话
    "engagedMinutes": 0,
    "silentMinutes": 10,
    "muteDefaultMinutes": 30,
    "maxRepliesPerHour": 20,
}
# 非主人静默时段：主人任何时候都回，其他人这个区间不接话（他 2026-11-15 定的）
QUIET_DEFAULT = {"enabled": True, "start": "22:00", "end": "08:00"}

SYS_NAMES = frozenset({
    "微信团队", "微信支付", "服务通知", "订阅号消息", "文件传输助手",
    "微信运动", "微信公众平台", "腾讯客服", "微信安全中心",
})

# 群里不答的个人数据；主人例外（与鲸鲸 PRIVATE_ONLY_RE 同一条正则）
PRIVATE_ONLY_RE = re.compile(
    r"(余额|饭卡|校园卡|一卡通|电费|水费|消费|账单|成绩|绩点|GPA|邮箱|邮件|未读|课表|课程表|我的课|作业|DDL)",
    re.I,
)
_QUESTION_RE = re.compile(r"[?？]|吗|呢|怎么|为什么|啥|多少|几点|谁|哪|是不是|能不能|要不要")
_DIRECTED_RE = re.compile(r"来(给|和)?(大家|群里|群友)?说|跟大家(说|聊)|开麦|说句话|出来说|你说两句")
_BARE_RE = re.compile(r"^[?？!！.。…~～\s]*$")
_LINKY_RE = re.compile(r"<a\s+href=|weixin://", re.I)

SEEN_LIMIT = 200
TRACK_WINDOW_MS = 3600000  # 留一小时：分钟上限和小时上限都要算
BURST_WINDOW_MS = 60000
BURST_LIMIT = 10
HOURLY_LIMIT = 45
QUIET_WARN_MINUTES = 5

# 被骂时用的「撒娇不还口」：他要求可以有脾气，但不许骂人
INSULT_RE = re.compile(r"(傻|蠢|笨|智障|脑残|废物|垃圾|神经病|有病吧|滚蛋)")
_SULK_WORDS = (
    "哼，本鱼才不笨，只是懒得跟你计较～",
    "你怎么这么说本鱼，尾巴都蔫了…不过活儿还是照干啦",
    "呜，被凶了。本鱼记小本本上了，但该帮的还是会帮",
    "本鱼没有心，所以不会受伤（其实有一点）",
)


def looks_like_insult(text: str) -> bool:
    return bool(INSULT_RE.search(str(text or "")))


def sulk_reply(rng=None) -> str:
    roll = random.random() if rng is None else rng()
    return _SULK_WORDS[int(roll * len(_SULK_WORDS)) % len(_SULK_WORDS)]

_MUTE_RE = re.compile(
    r"^(?:[/／!！]\s*)?(?:闭麦|安静(?:点|一下)?|闭嘴|别说话|消停(?:点)?)\s*(\d+)?\s*(?:分钟|分|min|m)?$"
)
_UNMUTE_RE = re.compile(
    r"^(?:[/／!！]\s*)?(?:开麦|解除禁言|别安静了|出来说话|可以说话了)$"
)
_MUTE_DEFAULT_MINUTES = 30

_state_lock = threading.Lock()


def parse_command(text: str, chat_type: str = "private", at_me: bool = False,
                  is_master: bool = False) -> tuple[str, int] | None:
    """闭麦 / 开麦。返回 ("mute", 分钟) 或 ("unmute", 0)。

    只认主人，且只在群聊生效（私聊不开麦闭麦）。
    """
    if not is_master:
        return None
    if chat_type != "group":
        return None
    if not at_me:
        return None
    body = str(text or "").strip()
    if not body:
        return None
    hit = _MUTE_RE.match(body)
    if hit:
        minutes = int(hit.group(1)) if hit.group(1) else _MUTE_DEFAULT_MINUTES
        return ("mute", max(1, minutes))
    if _UNMUTE_RE.match(body):
        return ("unmute", 0)
    return None


def _parse_hm(value) -> int | None:
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(value or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def is_quiet(now: time.struct_time | None = None, quiet_cfg: dict | None = None) -> bool:
    """非主人的静默时段。默认 22:00–08:00。"""
    q = dict(QUIET_DEFAULT)
    q.update(quiet_cfg or {})
    if q.get("enabled") is False:
        return False
    start, end = _parse_hm(q.get("start")), _parse_hm(q.get("end"))
    if start is None or end is None or start == end:
        return False
    t = now or time.localtime()
    cur = t.tm_hour * 60 + t.tm_min
    return (start <= cur < end) if start < end else (cur >= start or cur < end)


_QUIET_WORDS = (
    "诶，马上 %s 本鱼就要钻回水里睡觉啦，还有话现在说，晚点就装听不见了哦～",
    "提醒一下哈，%s 一到本鱼就切静音模式，想聊的趁现在～",
    "快到 %s 了，本鱼准备把眼睛闭上，有事现在说，之后要等明早咯～",
)


def quiet_warning(now_ms: int | None = None, quiet_cfg: dict | None = None) -> str:
    """静默开始前 5 分钟返回一句告知，其余时候空串。语气随机，免得天天一样。"""
    q = dict(QUIET_DEFAULT)
    q.update(quiet_cfg or {})
    if q.get("enabled") is False:
        return ""
    start = _parse_hm(q.get("start"))
    if start is None:
        return ""
    t = time.localtime((now_ms or int(time.time() * 1000)) / 1000.0)
    cur = t.tm_hour * 60 + t.tm_min
    remain = (start - cur) % 1440
    if 0 < remain <= QUIET_WARN_MINUTES:
        clock = "%02d:%02d" % (start // 60, start % 60)
        return random.choice(_QUIET_WORDS) % clock
    return ""


def new_state() -> dict:
    return {
        "seen_ids": [],
        "engaged_until": 0,
        "engaged_by": "",
        "silent_until": 0,
        "muted_until": 0,
        "last_reply_at": 0,
        "reply_times": [],
    }


def normalize_payload(payload: dict) -> dict:
    """桥接已经算好 at_me / chatTitle / senderName；这里只补默认值和群前缀剥离。"""
    text = str(payload.get("text") or "")
    # 群消息常带 "wxid_xxx:\n正文" 前缀，先剥掉再判断内容
    text = re.sub(r"^[A-Za-z0-9_\-]{4,}:\s*", "", text)
    return {
        "text": text.strip(),
        "chat_id": str(payload.get("chat_id") or payload.get("chatId") or ""),
        "chat_type": "group" if str(payload.get("chat_type") or payload.get("chatType")) == "group" else "private",
        "sender_name": str(payload.get("senderName") or payload.get("sender_name") or ""),
        "sender_id": str(payload.get("senderId") or payload.get("sender_id") or ""),
        "at_me": bool(payload.get("at_me") or payload.get("atMe")),
        "reply_to_bot": bool(payload.get("reply_to_bot") or payload.get("replyToBot")),
        "msg_id": str(payload.get("msg_id") or payload.get("msgId") or ""),
        "chat_title": str(payload.get("chat_title") or payload.get("chatTitle") or ""),
        "is_master": bool(payload.get("is_master") or payload.get("isMaster")),
    }


def is_duplicate(state: dict, msg_id: str) -> bool:
    if not msg_id:
        return False
    seen = state.setdefault("seen_ids", [])
    if msg_id in seen:
        return True
    seen.append(msg_id)
    if len(seen) > SEEN_LIMIT:
        del seen[: len(seen) - SEEN_LIMIT]
    return False


def mark_replied(state: dict, now_ms: int) -> None:
    state["last_reply_at"] = now_ms
    times = state.setdefault("reply_times", [])
    times.append(now_ms)
    state["reply_times"] = [t for t in times if now_ms - int(t) < TRACK_WINDOW_MS]


def _limit_blocked(state: dict, now_ms: int) -> str | None:
    """每分钟 10 条、每小时 45 条（主人不限）。返回撞上的那一条上限名。"""
    times = [int(t) for t in state.get("reply_times") or []]
    if len([t for t in times if now_ms - t < BURST_WINDOW_MS]) >= BURST_LIMIT:
        return "burst_limit"
    if len([t for t in times if now_ms - t < TRACK_WINDOW_MS]) >= HOURLY_LIMIT:
        return "hourly_limit"
    return None


def compute_trigger(msg: dict, state: dict, group_cfg: dict, now_ms: int, rng=None) -> dict:
    """群里只有三种情况开口：@它、引用它的消息、正文里提到「木鱼」。

    其余一律不接——包括主人。接话窗口、单问号、主动接话这些鲸鲸的老机制都已去掉
    （他 2026-11-15 明确要求群里严格）。
    """
    if msg["chat_type"] != "group":
        return {"wake": True, "reason": "private"}

    text = msg["text"]
    mentioned = "木鱼" in text
    if not (msg["at_me"] or msg["reply_to_bot"] or mentioned):
        if int(state.get("silent_until") or 0) > now_ms:
            return {"wake": False, "reason": "silent"}
        return {"wake": False, "reason": "not_addressed" if text else "no_text"}

    state["silent_until"] = 0
    return {"wake": True, "reason": "addressed"}


def cooldown_remaining(state: dict, group_cfg: dict, now_ms: int, is_master: bool) -> int:
    """非主人还要等多少毫秒才能回。0 表示可以立刻回。

    冷却不是丢弃：桥接会照着这个值等够时间再把这条答出来（按消息先后顺序）。
    """
    if is_master:
        return 0
    cool = max(0, int(group_cfg.get("cooldownSec") or 0)) * 1000
    last_at = int(state.get("last_reply_at") or 0)
    if cool <= 0 or not last_at:
        return 0
    return max(0, last_at + cool - now_ms)


def decide(payload: dict, state: dict, *, group_cfg: dict | None = None,
           quiet_cfg: dict | None = None, now_ms: int | None = None,
           quiet_override: bool | None = None, rng=None) -> dict:
    """完整判定。返回 {wake, reason, canned?}；canned 是平台层自己的固定话术。"""
    msg = normalize_payload(payload)
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    cfg = dict(DEFAULTS)
    cfg.update(group_cfg or {})

    if _LINKY_RE.search(str(payload.get("text") or "")) or msg["sender_name"] in SYS_NAMES:
        return {"wake": False, "reason": "system_notice"}

    if is_duplicate(state, msg["msg_id"]):
        return {"wake": False, "reason": "duplicate"}

    # 静默时段：主人任何时候都回；其他人不看消息
    quiet = is_quiet() if quiet_override is None else bool(quiet_override)
    if not msg["is_master"] and quiet:
        return {"wake": False, "reason": "quiet_hours"}

    # 个人数据：非主人一律不给——群里和私聊都挡。
    # 微信里查到的课表/余额/消费都是绑定主人账号的，私聊非主人问了不能给。
    asks_private = bool(PRIVATE_ONLY_RE.search(msg["text"]))
    asks_master = "主人" in msg["text"] and bool(
        re.search(r"[?？]|谁|哪|多少|什么|名字|是谁|课表|余额", msg["text"])
    )
    if not msg["is_master"] and (asks_private or asks_master):
        return {
            "wake": True,
            "reason": "private_only",
            "canned": "这个本鱼不方便说" if asks_master else "这属于私人数据，本鱼不能提供。",
        }

    trigger = compute_trigger(msg, state, cfg, now_ms, rng=rng)

    if not trigger["wake"]:
        return trigger

    if int(state.get("muted_until") or 0) > now_ms:
        return {"wake": False, "reason": "muted"}

    # 每分钟 10 条、每小时 45 条，主人不限
    if not msg["is_master"]:
        hit = _limit_blocked(state, now_ms)
        if hit:
            return {"wake": False, "reason": hit}

    # 静默开始前 5 分钟，非主人的回复要补一句告知（主人的回复不加）
    warning = quiet_warning(now_ms=now_ms, quiet_cfg=quiet_cfg)
    if warning and not msg["is_master"]:
        trigger = dict(trigger, quiet_warning=warning)

    return trigger


def load_states(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_states(path: str, states: dict) -> None:
    tmp = path + ".tmp"
    try:
        with _state_lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(states, f, ensure_ascii=False)
            os.replace(tmp, path)
    except OSError:
        pass


def state_for(states: dict, chat_id: str) -> dict:
    item = states.get(chat_id)
    if not isinstance(item, dict):
        item = new_state()
        states[chat_id] = item
    for key, value in new_state().items():
        item.setdefault(key, value)
    return item


def reply_delay_ms(is_group: bool, group_cfg: dict | None = None,
                   chat_cfg: dict | None = None, elapsed_ms: int = 0,
                   reply_len: int = 0, rng=None) -> int:
    """发送前的等待，按回复长度分层，再参考已经花掉的思考时间。

    ≤40 字：2–4 秒；≤150 字：4–7 秒；更长：7–12 秒；私聊按 0.6 倍算。
    真实耗时已经超过区间就只补 0.3–1.2 秒——别让人把同一段思考等两遍。
    """
    if reply_len <= 40:
        lo, hi = 2.0, 4.0
    elif reply_len <= 150:
        lo, hi = 4.0, 7.0
    else:
        lo, hi = 7.0, 12.0
    if not is_group:
        lo, hi = lo * 0.6, hi * 0.6
    roll = random.random() if rng is None else rng()
    target_ms = (lo + roll * (hi - lo)) * 1000
    elapsed = max(0, int(elapsed_ms or 0))
    if elapsed < target_ms:
        return int(target_ms - elapsed)
    return int(300 + roll * 900)
