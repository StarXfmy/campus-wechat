#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
鲸鲸 · 微信 4.x 桥接（不注入、不修改微信）

原理：
  - 读消息：解密微信 4.x 本地 SQLite 数据库（只读，需要微信处于登录状态）
  - 发消息：驱动微信窗口（UI 自动化 + 剪贴板），相当于有人替你打字发送

前置：
  1. WeChat 4.x 已登录（建议用小号）
  2. 已装依赖：pip install wechatauto-replica requests
  3. 鲸鲸服务已启动（双击 启动鲸鱼娘.bat）
  4. config.json 里 gateway.wechat.enabled = true（脚本会读 token）

用法：
  python examples/wechatauto_bridge.py

注意：
  - 发送消息会抢微信窗口焦点，这是 4.x 自动化的正常表现。
  - 微信必须保持登录；退出登录后数据库密钥会失效。
  - 自动化不是微信官方接口，仍有账号风险，只用小号 + 测试群。
"""

import json
import hashlib
import random
import shutil
import urllib.parse
import os
# 关掉 wechatauto 的「拟人节流」（默认 natural：120 秒内 6 次写就冷却 30~75 秒）
os.environ.setdefault("WECHATAUTO_RHYTHM", "off")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
import sys
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
LOG_FILE = os.path.join(ROOT, "data", "wechat-bridge-4x.log")

# 只回复这些会话（留空 = 全部会话都交给鲸鲸判断）
# 例如 ["文件传输助手", "深海鱼塘"]
ONLY_CHATS = ["AAA电子厂对口（1005）", "2026 Vibe Ai Lab", "木鱼测试群"]  # 只在这些群响应；空列表=所有群

# 白名单未命中的群，每个只提示一次，方便照着日志把准确群名补进来
_BLOCKED_LOGGED = set()

# 距离启动多久之前的消息不处理（秒）
SKIP_OLDER_THAN = 10
DIGEST_MINUTES = 30
MASTER_WXID = ''
# 超过这么多秒的旧消息直接不回，避免“回上一条”。
# 现在冷却期内是排队补答（一条一条来），等待可能超过几秒，所以放宽到 3 分钟；
# 重启刷屏由 SKIP_OLDER_THAN 兜住。
STALE_AFTER = float(os.environ.get("WHALE_STALE_AFTER", "180"))
try:
    _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
    with open(_cfg_path, "r", encoding="utf-8-sig") as _f:
        _cfg = json.load(_f)
    _stale = (_cfg.get("wechat") or {}).get("staleAfterSec")
    if isinstance(_stale, (int, float)) and _stale > 0:
        STALE_AFTER = float(_stale)
    _wechat = _cfg.get("wechat") or {}
    GAME_TOKENS = [str(x).lower() for x in (_wechat.get("gameWhitelist") or []) if str(x).strip()]
    AWAY_NOTICE = bool(_wechat.get("awayNotice", True))
    RETURN_NOTICE = bool(_wechat.get("returnNotice", True))
    QUEUED_MAX_AGE = float(_wechat.get("queuedMaxAgeSec", 300))
    QUEUED_MAX_CHATS = int(_wechat.get("queuedMaxChats", 2))
    QUOTE_REPLY = bool(_wechat.get("quoteReply", True))
    LOW_BATTERY_PCT = float(_wechat.get("lowBatteryPercent", 5))
    GOODNIGHT_TEXT = str(_wechat.get("goodnightText", "晚安，电量快没了，我先睡啦，明天见～"))
    _share = _cfg.get("share") or {}
    SHARE_ENABLED = bool(_share.get("enabled", True))
    SHARE_EVERY = float(_share.get("everyMinutes", 120))
    _digest = _wechat.get("digestMinutes", 30)
    DIGEST_MINUTES = float(_digest) if isinstance(_digest, (int, float)) else 30.0
    MASTER_WXID = (((_cfg.get("persona") or {}).get("master") or {}).get("wxids") or [''])[0]
    _brain = _cfg.get("brain") or {}
    BRAIN_BASE = str(_brain.get("baseUrl") or "").rstrip("/")
    BRAIN_TOKEN = str(_brain.get("token") or "")
    PUSH_ENABLED = bool(_brain.get("push", True)) and bool(BRAIN_BASE) and bool(MASTER_WXID)
    PUSH_EVERY = float(_brain.get("pushPollSeconds") or 60)
except Exception:
    BRAIN_BASE = ""
    BRAIN_TOKEN = ""
    PUSH_ENABLED = False
    PUSH_EVERY = 60.0

START_TS = time.time()

# 系统 / 公众号通知：只用来记录，不接话（否则会跟「微信团队」吵起来）
SYSTEM_SENDER_NAMES = {
    "微信团队", "微信支付", "服务通知", "订阅号消息", "文件传输助手",
    "微信运动", "微信公众平台", "腾讯客服", "微信安全中心", "微信广告",
}
try:
    SYSTEM_SENDER_NAMES |= {str(x).strip() for x in (_wechat.get("ignoreSenders") or []) if str(x).strip()}
except Exception:
    pass
_SYSTEM_TEXT_RE = __import__("re").compile(r"<a\s+href=|weixin://|&lt;a\s+href=", __import__("re").I)
# 同一会话同一句话的重复回复保护（秒）与每分钟回复上限
DUP_REPLY_WINDOW = 45
MAX_REPLIES_PER_MIN = 8
# 单实例锁端口：被占用就说明已经有桥接在跑，直接退出
BRIDGE_LOCK_PORT = 47821

# —— 语音识别（语音消息 → 文字）——
# 默认用阿里云百炼的 qwen3-asr-flash；Key 依次取 voice.apiKey → data/settings.json → deepseek.apiKey
VOICE_ENABLED = True
VOICE_BASE_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
VOICE_MODEL = "qwen3-asr-flash"
VOICE_API_KEY = ""
VOICE_TIMEOUT = 90
VOICE_MAX_SECONDS = 120
VOICE_SAMPLE_RATE = 16000
# 本地 whisper：用系统 Python 的 faster-whisper（tiny 模型已缓存），零 API 费用。
# 优先走它，失败了再退云端 ASR（如果配了 Key）。
VOICE_LOCAL = True
VOICE_LOCAL_PY = "py -3"
try:
    _voice_cfg = (_cfg.get("voice") or {})
    VOICE_LOCAL = bool(_voice_cfg.get("local", True))
    VOICE_LOCAL_PY = str(_voice_cfg.get("localPython") or VOICE_LOCAL_PY) or "py -3"
    # 本地模式不需要 Key，enabled=false 也照样跑
    VOICE_ENABLED = bool(_voice_cfg.get("enabled", True)) or VOICE_LOCAL
    VOICE_BASE_URL = str(_voice_cfg.get("baseUrl") or VOICE_BASE_URL)
    VOICE_MODEL = str(_voice_cfg.get("model") or VOICE_MODEL)
    VOICE_API_KEY = str(_voice_cfg.get("apiKey") or "")
    VOICE_TIMEOUT = float(_voice_cfg.get("timeoutMs", 90000)) / 1000.0
    VOICE_MAX_SECONDS = float(_voice_cfg.get("maxSeconds", 120))
    if not VOICE_API_KEY:
        try:
            _settings_path = os.path.join(ROOT, "data", "settings.json")
            with open(_settings_path, "r", encoding="utf-8-sig") as _sf:
                _st = json.load(_sf)
            VOICE_API_KEY = str(_st.get("apiKey") or "")
        except Exception:
            VOICE_API_KEY = ""
except Exception:
    pass


def asr_transcribe(wav_bytes, api_key, model, base_url, timeout=90):
    """把 wav 字节交给语音识别接口，返回文字；失败抛异常或返回空串。"""
    import base64
    import urllib.request

    if not api_key:
        raise RuntimeError("没有可用的语音识别 Key")
    payload = {
        "model": model,
        "input": {
            "messages": [{
                "role": "user",
                "content": [{"audio": "data:audio/wav;base64," + base64.b64encode(wav_bytes).decode()}],
            }]
        },
    }
    req = urllib.request.Request(
        base_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    try:
        text = data["output"]["choices"][0]["message"]["content"][0]["text"]
    except Exception:
        text = ""
    return str(text or "").strip()


def clean_transcript(text):
    """去掉只剩标点的转写结果。"""
    t = str(text or "").strip()
    t = t.strip("，,。.！!？?…~～、 ")
    return t if len(t) >= 1 else ""

_recent_dup = {}
_reply_stamps = {}
_guard_lock = threading.Lock()


def is_system_notice(username, nickname, content):
    """系统号 / 通知类消息：带 weixin:// 链接、HTML 链接、或已知系统号名字。"""
    if _SYSTEM_TEXT_RE.search(str(content or "")):
        return True
    name = str(nickname or "").strip()
    if name in SYSTEM_SENDER_NAMES:
        return True
    uid = str(username or "")
    if uid.startswith("gh_"):
        return True
    return False


def duplicate_guard(chat_username, sender, content):
    """同一人 45 秒内重复同一句 → 不再回（微信重投/通知刷屏）。"""
    now = time.time()
    key = (str(chat_username or ""), str(sender or ""), str(content or "").strip())
    with _guard_lock:
        last = _recent_dup.get(key, 0)
        _recent_dup[key] = now
        if len(_recent_dup) > 500:
            for k, v in list(_recent_dup.items()):
                if now - v > 600:
                    _recent_dup.pop(k, None)
        return now - last < DUP_REPLY_WINDOW


BOOT_LOG = os.path.join(ROOT, "data", "bridge-boot.log")


def split_long_text(text: str, limit: int = 1200) -> list:
    """按空行/换行切长文本，尽量让每段不超过 limit 字，不切断句子。"""
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks = []
    current = ""
    for para in text.split("\n"):
        candidate = (current + "\n" + para) if current else para
        if len(candidate) > limit and current:
            chunks.append(current)
            current = para
        else:
            current = candidate
    if current:
        chunks.append(current)
    # 单段还超长（比如一大段没换行）→ 硬切
    out = []
    for piece in chunks:
        while len(piece) > limit:
            out.append(piece[:limit])
            piece = piece[limit:]
        if piece:
            out.append(piece)
    return out or [text[:limit]]


def boot(msg):
    """启动诊断：只写文件，不依赖控制台（控制台被选中时 print 会卡住）"""
    try:
        os.makedirs(os.path.dirname(BOOT_LOG), exist_ok=True)
        with open(BOOT_LOG, "a", encoding="utf-8") as f:
            f.write("%s [pid %s] %s\n" % (time.strftime("%H:%M:%S"), os.getpid(), msg))
    except Exception:
        pass


boot("脚本开始执行")


def log(*args):
    line = time.strftime("[%H:%M:%S] ") + " ".join(str(a) for a in args)
    # 先写文件，再打印：这样即使控制台卡住，日志也不会丢
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


_win_problem_ts = {}


def log_window_problem(where, err):
    """窗口操作失败两分钟内只报一次。

    挪不动/置顶不了窗口不该让整条发送失败（UIA 还能把字打进去），
    更不该让看门狗以为“发送卡住”把桥接反复重启——那会一直弹「拒绝访问」。
    """
    now = time.time()
    if now - _win_problem_ts.get(where, 0) < 120:
        return
    _win_problem_ts[where] = now
    log(where + "：", repr(err))


def read_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except Exception as e:
            log("读取 config.json 失败：", e)
    return cfg


def build_bot_url(cfg):
    port = (cfg.get("server") or {}).get("port", 8787)
    return os.environ.get("WHALE_BOT_URL", "http://127.0.0.1:%s/webhook/wechat" % port)


# 影子期基准：把鲸鲸每次的判定落盘，供 Python 行为层逐条对照「该不该回、回了什么」。
# 只追加写文件，不影响发送；出任何错都吞掉，别让埋点拖垮主链。
SHADOW_LOG = os.path.join(ROOT, "data", "shadow-baseline.jsonl")
SHADOW_STATE = os.path.join(ROOT, "data", "wx-state-shadow.json")
_shadow = {"states": None, "group_cfg": None}


def _shadow_decide(payload, is_master, replied=False):
    """跑一遍 Python 行为层，只返回结论，不发消息。"""
    global _shadow
    try:
        import wx_behavior as WB

        if _shadow["states"] is None:
            _shadow["states"] = WB.load_states(SHADOW_STATE)
        if _shadow["group_cfg"] is None:
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                    _shadow["group_cfg"] = (json.load(f).get("group") or {})
            except Exception:
                _shadow["group_cfg"] = {}
        chat_id = str(payload.get("chat_id") or "")
        state = WB.state_for(_shadow["states"], chat_id)
        now_ms = int(time.time() * 1000)
        got = WB.decide(
            dict(payload, is_master=bool(is_master)),
            state,
            group_cfg=_shadow["group_cfg"],
            now_ms=now_ms,
        )
        # 鲸鲸只有真回了才更新 lastReplyAt；这里对齐同一语义，否则接话窗口判得不一样
        if replied:
            WB.mark_replied(state, now_ms)
        WB.save_states(SHADOW_STATE, _shadow["states"])
        return {"wake": bool(got.get("wake")), "reason": got.get("reason")}
    except Exception as exc:
        return {"error": repr(exc)}


def shadow_baseline(payload, data, is_master=False):
    try:
        row = {
            "ts": int(time.time() * 1000),
            "chat_type": payload.get("chat_type"),
            "chat_id": payload.get("chat_id"),
            "sender": payload.get("senderName"),
            "text": str(payload.get("text") or "")[:200],
            "at_me": bool(payload.get("at_me")),
            "action": data.get("action"),
            "reason": data.get("reason"),
            "reply": str(data.get("reply") or "")[:400],
            "py": _shadow_decide(payload, is_master, replied=(data.get("action") == "reply")),
        }
        with open(SHADOW_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


# 引擎：python = 桥接自己做平台判定、直接调木鱼大脑（鲸鲸下线）；
#       whale  = 仍走鲸鲸 8787（回退用，需要鲸鲸进程在跑）。
ENGINE = (os.environ.get("WX_ENGINE") or "python").strip().lower()
BRAIN_CHAT_URL = (os.environ.get("WX_BRAIN_URL") or "").strip()
BRAIN_CHAT_TOKEN = (os.environ.get("WX_BRAIN_TOKEN") or "").strip()
STATE_PATH = os.path.join(ROOT, "data", "wx-state.json")
_wx = {"states": None, "group_cfg": None, "chat_cfg": None}


def _load_cfg_sections():
    if _wx["group_cfg"] is None:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
            _wx["group_cfg"] = cfg.get("group") or {}
            _wx["chat_cfg"] = cfg.get("chat") or {}
            _wx.setdefault("quiet_cfg", cfg.get("quiet") or {})
        except Exception:
            _wx["group_cfg"] = {}
            _wx["chat_cfg"] = {}
    return _wx["group_cfg"], _wx["chat_cfg"]


def local_command(payload, is_master):
    """闭麦/开麦这类平台命令：本地执行，不走大脑。"""
    try:
        import wx_behavior as WB

        return WB.parse_command(
            str(payload.get("text") or ""),
            str(payload.get("chat_type") or "private"),
            bool(payload.get("at_me")),
            bool(is_master),
        )
    except Exception:
        return None


def apply_command(chat_id, kind, minutes):
    try:
        import wx_behavior as WB

        if _wx["states"] is None:
            _wx["states"] = WB.load_states(STATE_PATH)
        state = WB.state_for(_wx["states"], str(chat_id or ""))
        if kind == "mute":
            state["muted_until"] = int(time.time() * 1000) + minutes * 60000
            state["silent_until"] = 0
        else:
            state["muted_until"] = 0
        WB.save_states(STATE_PATH, _wx["states"])
    except Exception as exc:
        log("命令状态写入失败：", repr(exc))


def local_decide(payload, is_master):
    """正式判定：该不该回。出异常就放行，让人先收到回复，别在本地环节卡死。"""
    try:
        import wx_behavior as WB

        if _wx["states"] is None:
            _wx["states"] = WB.load_states(STATE_PATH)
        group_cfg, chat_cfg = _load_cfg_sections()
        state = WB.state_for(_wx["states"], str(payload.get("chat_id") or ""))
        got = WB.decide(
            dict(payload, is_master=bool(is_master)),
            state,
            group_cfg=group_cfg,
            quiet_cfg=_wx.get("quiet_cfg"),
        )
        WB.save_states(STATE_PATH, _wx["states"])
        return got
    except Exception as exc:
        log("平台判定异常，放行：", repr(exc))
        return {"wake": True, "reason": "decide_error"}


def mark_replied(chat_id):
    """真发出去了才记这一笔：接话窗口、冷却、频率上限都靠它。"""
    try:
        import wx_behavior as WB

        if _wx["states"] is None:
            _wx["states"] = WB.load_states(STATE_PATH)
        state = WB.state_for(_wx["states"], str(chat_id or ""))
        WB.mark_replied(state, int(time.time() * 1000))
        WB.save_states(STATE_PATH, _wx["states"])
    except Exception:
        pass


def queue_push(queue, item, max_per_chat=10):
    """入队；同一会话超过上限就把最早一条并进当前条，返回被合并的那条。"""
    same = [i for i in queue if i["username"] == item["username"]]
    merged = None
    if len(same) >= max_per_chat:
        merged = same[0]
        queue.remove(merged)
        note = "（另外你还问过：%s）" % "；".join(str(b) for b in merged["bubbles"])[:120]
        item["bubbles"] = [note] + list(item["bubbles"])
    queue.append(item)
    return merged


def brain_chat_url():
    if BRAIN_CHAT_URL:
        return BRAIN_CHAT_URL
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            base = str(((json.load(f).get("brain") or {}).get("baseUrl") or "")).rstrip("/")
    except Exception:
        base = ""
    return (base or "http://127.0.0.1:8790") + "/chat"


def brain_chat_token():
    if BRAIN_CHAT_TOKEN:
        return BRAIN_CHAT_TOKEN
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            token = str(((json.load(f).get("brain") or {}).get("token") or "")).strip()
    except Exception:
        token = ""
    return token or os.environ.get("WX_BRIDGE_TOKEN", "dev-token")


def vision_describe(b64, mime="image/jpeg", prompt="", timeout=120):
    """看图：走 Python 侧的 wx_vision（从鲸鲸搬来的同一套 Key 与提示词），不再敲 8787。"""
    import wx_vision

    return wx_vision.describe(b64, mime=mime, prompt=prompt, timeout=timeout)


def ensure_ffmpeg_env():
    """微信 4.x 的普通图片是 HEVC(wxgf) 容器，要 ffmpeg 才能转成 jpg。

    media 库用 shutil.which("ffmpeg") 找，很多机器装了 ffmpeg 但没加进 PATH，
    结果图片一直转不出来、看图全是「图片输入格式/解析错误」。
    这里先自己找一遍常见位置，找到就塞进 PATH。
    """
    import shutil
    if shutil.which("ffmpeg"):
        return True
    cands = [
        os.path.join(ROOT, "runtime", "ffmpeg", "bin"),
        os.path.join(ROOT, "tools", "ffmpeg", "bin"),
        r"C:\ffmpeg\bin", r"D:\ffmpeg\bin", r"E:\ffmpeg\bin", r"F:\ffmpeg\bin",
        os.path.expanduser(r"~\ffmpeg\bin"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\ffmpeg\bin"),
        r"C:\Program Files\ffmpeg\bin", r"D:\Program Files\ffmpeg\bin",
    ]
    for d in cands:
        exe = os.path.join(d, "ffmpeg.exe")
        if os.path.isfile(exe):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            log("找到 ffmpeg：", exe, "（微信图片是 wxgf/HEVC，靠它转 jpg）")
            return True
    log("[!] 没找到 ffmpeg：微信 4.x 的图片多是 wxgf/HEVC，没有它就看不了图。")
    log("    装一个（解压到 C:\\ffmpeg 即可）或把 ffmpeg.exe 放进 runtime\\ffmpeg\\bin")
    return False


def main():
    boot("进入 main()")
    ensure_ffmpeg_env()
    # 微信的 Qt accessibility 只在「纯英文路径」下能热激活成功；路径里带中文时，
    # UIA 认不出主窗（ensure_window 超时 37s），之后每次发送都退到 OCR 并报
    # error(5, 'SetWindowPos', '拒绝访问')，日志刷屏、消息发不出去。
    if any(ord(ch) > 127 for ch in ROOT):
        log("[!] 路径里有中文/非英文字符：", ROOT)
        log("    微信 UI 自动化在这种路径下会失败（发送一直报「拒绝访问」）。")
        log("    请把整个文件夹移到纯英文路径再启动，例如 D:\\whale-girl-3.0")
    # 单实例锁：多个桥接连同一份微信数据库会导致同一条消息被回好几遍
    _lock_sock = None
    try:
        import socket as _socket
        _lock_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _lock_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 0)
        _lock_sock.bind(("127.0.0.1", BRIDGE_LOCK_PORT))
        _lock_sock.listen(1)
        boot("单实例锁已获取（端口 %d）" % BRIDGE_LOCK_PORT)
    except OSError as e:
        log("已经有一个桥接在跑了（端口 %d 被占用），本进程退出：%r" % (BRIDGE_LOCK_PORT, e))
        try:
            if _lock_sock:
                _lock_sock.close()
        except Exception:
            pass
        return 0
    except Exception:
        _lock_sock = None
    try:
        import faulthandler

        try:
            _hang_file = open(os.path.join(ROOT, "data", "bridge-hang.log"), "a", encoding="utf-8")
            faulthandler.dump_traceback_later(40, repeat=True, file=_hang_file)
            boot("已开启卡死堆栈转储（40s）")
        except Exception:
            pass

        boot("开始 import requests")
        import requests

        boot("开始 import wechatauto（可能较慢，10~20 秒）")
        from wechatauto import MediaDownloader, WeChatDB, WeChatGUI
        from wechatauto.db import Listener

        # 小版本兼容补丁：本机微信上 UIA 控件树其实已经物化，但顶层窗口类名不是
        # mmui::MainWindow，wechatauto 自带的判断会把它当成「没物化」，于是每次
        # 发送都跳过 UIA、掉进会卡死在截屏里的 OCR 兜底。补丁见 uia_compat.py。
        # 包里自带的 Python 开了 safe_path（sys.flags.safe_path=True，sys.path[0]
        # 是 python312.zip 而不是脚本目录），所以同目录的 uia_compat 直接 import 会
        # ModuleNotFoundError。这里显式把 examples 目录塞进 sys.path；再不行就按
        # 文件路径加载（双保险）。
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        # 打开每一步的耗时日志（uia step xxx -> X.XXs），下次卡住能直接看出停在哪步
        os.environ.setdefault("WHALE_STEPLOG", "1")
        boot("应用 uia_compat 补丁")
        try:
            import uia_compat
            boot("uia_compat.apply() = %r" % (uia_compat.apply(),))
        except Exception as _compat_err:
            try:
                import importlib.util as _ilu
                _spec = _ilu.spec_from_file_location(
                    "uia_compat", os.path.join(_here, "uia_compat.py"))
                _mod = _ilu.module_from_spec(_spec)
                sys.modules["uia_compat"] = _mod
                _spec.loader.exec_module(_mod)
                _mod.apply()
                boot("uia_compat 已按文件路径加载并生效")
            except Exception:                # noqa: BLE001
                boot("uia_compat 未生效: %r" % (_compat_err,))

        boot("import 完成")
    except Exception as e:
        boot("导入失败: %r" % (e,))
        log("缺少依赖或导入失败：", e)
        log("请先运行：\"%LOCALAPPDATA%\\Programs\\Python\\Python312\\python.exe\" -m pip install wechatauto-replica requests")
        return 1

    cfg = read_config()
    gw = (cfg.get("gateway") or {}).get("wechat") or {}
    token = gw.get("inboundToken", "")
    bot_url = build_bot_url(cfg)
    aliases = ((cfg.get("persona") or {}).get("aliases") or []) + [
        ((cfg.get("persona") or {}).get("name") or "鲸鲸")
    ]

    log("连接微信 4.x 数据库（只读）…")
    # token 一定要读到：读不到就是「她不理人」最隐蔽的原因（服务端会直接 401）
    if token:
        log("webhook token：已读取（%s…）" % token[:6])
    else:
        log("⚠️ webhook token 是空的！检查 config.json 的 gateway.wechat.inboundToken（文件要能正常解析）")
    db = WeChatDB()
    _media_box = {"downloader": None}

    def _get_media():
        """按需创建媒体下载器；图片和表情识别共用一个实例。"""
        if _media_box["downloader"] is None:
            save_dir = os.path.join(ROOT, "data", "media-cache")
            os.makedirs(save_dir, exist_ok=True)
            _media_box["downloader"] = MediaDownloader(db, save_dir=save_dir)
        return _media_box["downloader"]

    me = db.wxid
    try:
        info = db.get_self_info() or {}
        log("当前登录账号：", info.get("nick_name") or info.get("remark") or me)
    except Exception:
        log("当前登录账号 wxid：", me)
    log("数据库目录：", db.db_dir)

    sessions = {}
    try:
        for s in db.get_sessions(limit=800):
            sessions[s["username"]] = s
    except Exception as e:
        log("读取会话列表失败：", e)

    name_cache = {}

    def display_name(username):
        if username in name_cache:
            return name_cache[username]
        name = ""
        try:
            if username.endswith("@chatroom"):
                name = db.group_id_to_name(username) or ""
                if not name:
                    s = sessions.get(username) or {}
                    name = s.get("last_sender") or ""
            else:
                name = db.get_nickname(username) or ""
        except Exception:
            name = ""
        name = name or username
        name_cache[username] = name
        return name

    alias_cache = {}
    # 上次把这个会话切到哪一行名字（微信里没命名的群，行名是成员名）
    open_cache = {}

    def aliases_for(username, shown=""):
        """会话列表里那一行可能叫什么（微信里没命名的群，显示的是成员名）。

        实测：群 45063658812@chatroom 在数据库里 nick_name 是空的，会话列表里显示
        「宇火之于、云白『世一区』」—— 按群名/open_chat 都匹配不上，所以这里把
        「成员名用、连起来」也当候选名。
        """
        key = (username or "", shown or "")
        if key in alias_cache:
            return alias_cache[key]
        out = []
        if shown and shown != username and not str(shown).endswith("@chatroom"):
            out.append(shown)
        if username and str(username).endswith("@chatroom"):
            try:
                nm = db.group_id_to_name(username) or ""
            except Exception:
                nm = ""
            if nm and nm != username:
                out.append(nm)
            try:
                nicks = []
                for m in (db.get_group_members(username) or []):
                    if m.get("username") == me:
                        continue
                    n = (m.get("remark") or "").strip() or (m.get("nick_name") or "").strip()
                    if n:
                        nicks.append(n)
                # 只剩一个成员时别拿它当候选行名：那串就是那个人的私聊会话名，
                # 按它 open_chat 会打开私聊窗口，群里的回复就发到私聊去了。
                # 两人群正好是这种情况（2026-11-15 16:39 实测踩到）。
                if len(nicks) >= 2:
                    out.append("、".join(nicks))
            except Exception:
                pass
        if not out:
            out.append(shown or username)
        out = [x for x in dict.fromkeys(out) if x]
        alias_cache[key] = out
        return out

    def chat_latest_seq(username):
        try:
            rows = db.get_messages(username, limit=1)
            return rows[0].get("sort_seq") or 0 if rows else 0
        except Exception:
            return 0

    gui = None
    gui_lock = threading.Lock()
    send_lock = threading.Lock()
    dead_chats = set()
    fail_streak = {}
    self_wxid = db.wxid
    pending_replies = {}
    # slow=True 表示当前在发图片（要截图 + 定位输入框，实测 23-39 秒），
    # 看门狗要按另一套阈值算，不能拿文字的 40 秒去卡它
    SEND_STATE = {"active": False, "start": 0.0, "slow": False}
    pending_lock = threading.Lock()
    AWAY = {"on": False}
    NOTIFIED = []

    def away_active():
        """主人在微信里发了「我去玩了」→ 写 data/away.json，这里据此静默排队。"""
        try:
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "away.json")
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            return float(d.get("until") or 0) > time.time()
        except Exception:
            return False

    def game_running():
        if not GAME_TOKENS:
            return False
        try:
            import psutil
            for p in psutil.process_iter(["name"]):
                n = (p.info.get("name") or "").lower()
                if any(t in n for t in GAME_TOKENS):
                    return True
        except Exception:
            pass
        return False

    def foreground_is_fullscreen():
        """前台是不是全屏程序（比如游戏）——这时别抢焦点，先把回复排着。"""
        try:
            import win32gui, win32api, win32process, psutil
            h = win32gui.GetForegroundWindow()
            if not h:
                return False
            _, pid = win32process.GetWindowThreadProcessId(h)
            name = psutil.Process(pid).name().lower()
            if name.startswith("weixin"):
                return False
            r = win32gui.GetWindowRect(h)
            sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
            return r[0] <= 0 and r[1] <= 0 and r[2] >= sw - 2 and r[3] >= sh - 2
        except Exception:
            return False

    def group_is_alive(chat_username):
        """被踢出群后，群会话还在，但发不出去；先查成员列表。"""
        if not str(chat_username).endswith("@chatroom"):
            return True
        try:
            members = db.get_group_members(chat_username) or []
            ids = {str(m.get("wxid") or m.get("username")) for m in members}
            return self_wxid in ids
        except Exception:
            return True

    # ---------- 微信窗口置顶（关键：别的窗口挡在微信上面会导致打字落空） ----------
    def find_weixin_hwnd():
        try:
            import win32gui
            import win32con
            import win32process
            import psutil
        except Exception:
            return 0
        found = []
        # 微信（4.x / Qt）会额外建一个「托盘图标隐藏消息窗」：它算可见窗口，
        # 而且 GetWindowRect 出来的面积常常比主窗还大（实测 1320x900 > 主窗 896x776）。
        # 以前按「面积最大」挑，就会挑中它 —— 对它 SetWindowPos 直接 error(5) 拒绝访问，
        # 于是每次发送都失败、走 OCR 兜底、再被看门狗重启，一直循环报「拒绝访问」。
        BAD_CLASS = ("trayicon", "messagewindow", "msctf", "sopy", "sogou", "hintwnd", "default ime", "notifyicon")

        def cb(h, _):
            try:
                title = win32gui.GetWindowText(h) or ""
                _, pid = win32process.GetWindowThreadProcessId(h)
                if pid <= 0:
                    return
                cls = win32gui.GetClassName(h) or ""
                low = cls.lower()
                if any(bad in low for bad in BAD_CLASS):
                    return
                rect = win32gui.GetWindowRect(h)
                area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
                if win32gui.IsIconic(h):
                    # 最小化后 GetWindowRect 是 (-16000,-16000,…) 的退化矩形，
                    # 换算成「还原后」的尺寸再判大小
                    try:
                        rc = win32gui.GetWindowPlacement(h)[4]
                        area = max(0, rc[2] - rc[0]) * max(0, rc[3] - rc[1]) or area
                    except Exception:
                        pass
                # 不再要求 IsWindowVisible：微信缩在托盘里时主窗 visible=0，以前这里
                # 直接 return，于是整条「置顶 + 调窗口」都被跳过。主窗至少 300x250，
                # 托盘提示窗（260x85）和辅助窗（173x196）顺手被排除。
                if area < 300 * 250 and title.strip() != "微信":
                    return
                try:
                    name = psutil.Process(pid).name().lower()
                except Exception:
                    return
                if not name.startswith("weixin"):
                    return
                if "发送" in title or "助手" in title:
                    return
                try:
                    style = win32gui.GetWindowLong(h, win32con.GWL_STYLE)
                    has_min_box = bool(style & win32con.WS_MINIMIZEBOX)
                except Exception:
                    has_min_box = False
                is_main_title = title.strip() == "微信"
                looks_main_class = ("qwindowicon" in low) or ("mmui::mainwindow" in low)
                # 真主窗的三条特征都各加一个大权重，面积只在同类里起作用，
                # 这样托盘消息窗再“大”也排不到前面
                score = area
                if is_main_title:
                    score += 10 ** 9
                if looks_main_class:
                    score += 10 ** 8
                if has_min_box:
                    score += 10 ** 7
                found.append((score, 1 if is_main_title else 0, h))
            except Exception:
                pass

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            return 0
        if not found:
            return 0
        found.sort(reverse=True)
        return found[0][2]

    def normalize_window(hwnd):
        """微信窗口太小 / 超出屏幕 / 被挪走都会让 UI 自动化点空，统一调成稳定尺寸。"""
        try:
            import win32gui
            import win32con
            import win32api
            sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
            rect = win32gui.GetWindowRect(hwnd)
            x, y, w, h = rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]
            tw, th = min(1320, sw - 160), min(900, sh - 160)
            need = w < 1080 or h < 760 or w > sw - 80 or h > sh - 80 or x < -20 or y < -20 or x + w > sw + 20 or y + h > sh + 20
            if need:
                win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 40, 20, tw, th, win32con.SWP_SHOWWINDOW)
                log("微信窗口调整：%dx%d @(%d,%d) → %dx%d（保证自动发送稳定）" % (w, h, x, y, tw, th))
                time.sleep(0.8)
        except Exception as e:
            log_window_problem("调整微信窗口失败（继续发送）", e)

    def with_window_on_top(fn, who="", text=""):
        """发送前确保微信在前台；游戏/全屏时不碰窗口。"""
        if game_running():
            return False
        hwnd = find_weixin_hwnd()
        was_topmost = False
        touched = False
        SEND_STATE["active"] = True
        SEND_STATE["start"] = time.time()
        # 卡住时留给看门狗写日志用：知道卡在哪个会话、发的是什么
        SEND_STATE["who"] = who
        SEND_STATE["text"] = (text or "")[:40]
        try:
            import win32gui
            import win32con
            if hwnd:
                try:
                    if not win32gui.IsWindowVisible(hwnd):
                        # 微信在托盘里时主窗是隐藏的，先把窗口拉出来再打字
                        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                        log("微信在托盘里，已把主窗口拉出来再发送")
                        time.sleep(0.4)
                except Exception as e:
                    log("拉出微信窗口失败：", repr(e))
                normalize_window(hwnd)
                try:
                    fg = win32gui.GetForegroundWindow()
                except Exception:
                    fg = 0
                if fg != hwnd:
                    touched = True
                    force_foreground(hwnd)
                    ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                    was_topmost = bool(ex & win32con.WS_EX_TOPMOST)
                    # 置顶失败不该让整条发送失败（比如挑到了不能定位的窗口），
                    # 顶多是“可能被别的窗口挡住”，打字还有 UIA 兜着
                    try:
                        win32gui.SetWindowPos(
                            hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
                        )
                    except Exception as e:
                        log_window_problem("窗口置顶失败（继续发送）", e)
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    time.sleep(0.2)
            return fn()
        finally:
            SEND_STATE["active"] = False
            try:
                if touched and hwnd and not was_topmost:
                    import win32gui
                    import win32con
                    win32gui.SetWindowPos(
                        hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
                    )
            except Exception:
                pass

    def force_foreground(hwnd):
        """把微信强制拉到前台（SetForegroundWindow 单用会被系统拒绝）。"""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            if user32.GetForegroundWindow() == hwnd:
                return True
            fg = user32.GetForegroundWindow()
            tid_fg = user32.GetWindowThreadProcessId(fg, None)
            tid = user32.GetWindowThreadProcessId(hwnd, None)
            try:
                user32.AttachThreadInput(tid_fg, tid, True)
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            finally:
                try:
                    user32.AttachThreadInput(tid_fg, tid, False)
                except Exception:
                    pass
            time.sleep(0.15)
            return True
        except Exception:
            return False

    UIA_CACHE = {"u": None}

    def _input_value(u):
        """尽量读出输入框当前文字；读不到返回空串。"""
        try:
            e = u._chat_input()
            if e is None:
                return ''
            for name in ('get_value', 'value'):
                try:
                    v = getattr(e, name)
                    v = v() if callable(v) else v
                    if v is not None:
                        return str(v)
                except Exception:
                    pass
            try:
                return str(e.iface_value.Value)
            except Exception:
                pass
        except Exception:
            pass
        return ''

    def press_enter(with_ctrl=False):
        """用底层按键补发 Enter（UIA 的 SendKeys 有时不触发发送）。"""
        try:
            import win32api
            import win32con
            hwnd = find_weixin_hwnd()
            if hwnd:
                force_foreground(hwnd)
            time.sleep(0.10)
            if with_ctrl:
                win32api.keybd_event(0x11, 0, 0, 0)
            win32api.keybd_event(0x0D, 0, 0, 0)
            win32api.keybd_event(0x0D, 0, win32con.KEYEVENTF_KEYUP, 0)
            if with_ctrl:
                win32api.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.25)
            return True
        except Exception as e:
            log("手动回车失败：", repr(e))
            return False

    def uia_send(text, who, username=""):
        """优先用 UIA 发送：不依赖 OCR 找发送按钮，成功后返回 True。"""
        u = UIA_CACHE["u"]
        if u is None:
            try:
                with gui_lock:
                    u = gui._get_uia() if gui is not None else None
                UIA_CACHE["u"] = u
            except Exception:
                u = None
        if u is None:
            return False
        keys = aliases_for(username, who) if username else [who]
        try:
            cur = None
            try:
                cur = u.current_chat()
            except Exception:
                pass
            if username and str(username).endswith("@chatroom"):
                # 群聊判「已经在群里」只认真群名。keys 里的成员拼接名、
                # open_cache 里上次切到的行名，都可能正好是某个人的私聊会话名——
                # 拿它们认命中，私聊窗口开着就会被当成群，群回复发进私聊。
                hit_cur = bool(cur) and cur == who
            else:
                hit_cur = (cur == who) or any(cur == k for k in keys if k)
                if not hit_cur and username:
                    hit_cur = bool(cur) and open_cache.get(username) == cur
            if not hit_cur:
                if not u.open_chat(keys):
                    try:
                        u.back_to_chat_tab()
                    except Exception:
                        pass
                    if not u.open_chat(keys):
                        return False
                if username:
                    try:
                        open_cache[username] = u.current_chat() or ""
                        log("UIA 已切到会话：", open_cache[username])
                    except Exception:
                        pass
            t_open = time.time()
            sent_ok = False
            try:
                sent_ok = bool(u.send_text(text))
            except Exception:
                sent_ok = False
            if not sent_ok:
                try:
                    u.back_to_chat_tab()
                    sent_ok = bool(u.send_text(text))
                except Exception:
                    sent_ok = False
            log("UIA 发送耗时 %.1fs（打开会话+输入）" % (time.time() - t_open))
            if sent_ok:
                # 多按几次：Enter 两次 + Ctrl+Enter 两次（兼容“按 Ctrl+Enter 发送”的设置）
                press_enter(False)
                press_enter(False)
                press_enter(True)
                press_enter(True)
                return True
        except Exception as e:
            log("UIA 发送异常：", repr(e))
        return False

    def send_text(text, chat_display_name, chat_username="", force=False, quote_text=None):
        nonlocal gui
        # 大脑用 [[IMG:路径]] 标记「先发这张图，再说话」。
        # 放在最前面，此时还没拿 send_lock，send_sticker 自己会去拿。
        import re as _img_re
        _imgs = _img_re.findall(r"\[\[IMG:(.+?)\]\]", text or "")
        if _imgs:
            text = _img_re.sub(r"\[\[IMG:.+?\]\]", "", text or "").strip()
            for _p in _imgs:
                try:
                    if send_sticker(_p, chat_display_name):
                        log("已发图：", _p)
                    else:
                        log("图没发出去：", _p)
                except Exception as e:
                    log("发图异常：", repr(e))
        # 超长内容按段发：一次性粘贴容易超时/被截断，拆开每段单独校验
        if len(text or "") > 1500:
            chunks = split_long_text(text, 1200)
            log("消息太长（%d 字），拆成 %d 段发" % (len(text), len(chunks)))
            all_ok = True
            for i, chunk in enumerate(chunks):
                if i:
                    time.sleep(0.8)
                ok = send_text(
                    chunk, chat_display_name, chat_username, force=force,
                    quote_text=quote_text if i == 0 else None,
                )
                all_ok = all_ok and bool(ok)
            return all_ok
        if chat_username and chat_username in dead_chats:
            log("跳过已失效的会话：", chat_display_name)
            return False
        if chat_username and not group_is_alive(chat_username):
            dead_chats.add(chat_username)
            log("群里已经没有我了（被移出？），以后跳过：", chat_display_name)
            return False
        if not force and (AWAY["on"] or away_active() or game_running() or foreground_is_fullscreen()):
            with pending_lock:
                pending_replies[chat_username or chat_display_name] = (text, chat_display_name, chat_username, time.time())
            log("你在游戏/全屏程序里，回复先排队，等你退出再发：", chat_display_name)
            return True
        with send_lock:
            if not force and (AWAY["on"] or away_active() or game_running() or foreground_is_fullscreen()):
                with pending_lock:
                    pending_replies[chat_username or chat_display_name] = (text, chat_display_name, chat_username, time.time())
                log("游戏/全屏中，先排队：", chat_display_name)
                return True
            with gui_lock:
                if gui is None:
                    log("初始化微信窗口驱动…（会抢一下焦点，约 5~10 秒）")
                    gui = WeChatGUI()
            # ⓪ 引用回复（引用对方那条消息）——比普通发送慢，失败自动回退
            if quote_text and QUOTE_REPLY:
                try:
                    if with_window_on_top(lambda: gui.quote_msg(text, who=chat_display_name, target_text=quote_text, verify=False), chat_display_name, text):
                        log("引用回复成功 →", chat_display_name)
                        return True
                except Exception as e:
                    log("引用回复失败，回退普通发送：", repr(e))
            # ① 首选 UIA：稳定、快，不依赖 OCR 找发送按钮
            t_start = time.time()
            uia_available = False
            try:
                uia_available = gui._get_uia() is not None
            except Exception:
                uia_available = False
            for attempt in (1, 2):
                t_att = time.time()
                try:
                    if with_window_on_top(lambda: uia_send(text, chat_display_name, chat_username), chat_display_name, text):
                        fail_streak.pop(chat_username, None)
                        log("发送成功（%.1fs）→ %s" % (time.time() - t_start, chat_display_name))
                        return True
                    log("UIA 发送未成功（第 %d 次，%.1fs），准备重试" % (attempt, time.time() - t_att))
                except Exception as e:
                    log("UIA 发送异常（第 %d 次，%.1fs）：%r" % (attempt, time.time() - t_att, e))
                time.sleep(0.5)
            if uia_available:
                # UIA 可用但没发出去：直接放弃，别走 75 秒的 OCR 兜底拖死后面消息
                log("UIA 发不出去，跳过 OCR 兜底：", chat_display_name)
            else:
                log("UIA 不可用，走 OCR 兜底")
            # ② 兜底：仅当 UIA 不可用时才走 OCR。
            #    注意：OCR 这条路在这台机器上会卡死在 PIL.ImageGrab.grab 截屏里
            #    （data/bridge-hang.log 有栈），以前直接拖到 40 秒看门狗把整个桥接
            #    os._exit(2) 重启；这里给它 25 秒，超时就放弃这一条。
            if not uia_available:
                try:
                    with gui_lock:
                        try:
                            gui = WeChatGUI()
                        except Exception as e:
                            log("重建窗口驱动失败：", repr(e))
                    _box = {}

                    def _ocr_send():
                        try:
                            _box["r"] = with_window_on_top(
                                lambda: gui.send_msg(text, chat_display_name, True),
                                chat_display_name, text)
                        except Exception as _e:      # noqa: BLE001
                            _box["e"] = _e

                    _th = threading.Thread(target=_ocr_send, daemon=True)
                    _th.start()
                    _th.join(25)
                    if _th.is_alive():
                        log("OCR 兜底 25 秒没返回（卡在截屏），放弃这条，等下次消息")
                        resp = False
                    else:
                        if "e" in _box:
                            raise _box["e"]
                        resp = _box.get("r")
                    if bool(resp):
                        fail_streak.pop(chat_username, None)
                        return True
                    log("兜底发送失败：%s" % resp)
                except Exception as e:
                    log("兜底发送异常：%r" % e)
            if chat_username:
                fail_streak[chat_username] = fail_streak.get(chat_username, 0) + 1
                if fail_streak[chat_username] >= 2:
                    dead_chats.add(chat_username)
                    log("连续发送失败，先停掉这个会话：", chat_display_name)
            log("发送最终失败，请把微信窗口保持在桌面、别最小化；内容：", text[:30])
            return False

    def send_sticker(path, chat_display_name):
        """发一张本地图片（表情包）。失败只记日志，不影响后面的文字。"""
        nonlocal gui
        if not path or not os.path.isfile(path):
            log("表情包文件不存在：", path)
            return False
        with send_lock:
            with gui_lock:
                if gui is None:
                    try:
                        gui = WeChatGUI()
                    except Exception as e:
                        log("初始化微信窗口驱动失败：", repr(e))
                        return False
                try:
                    # 必须先把微信真正拉到前台：只设 TOPMOST 不够，
                    # 实测终端盖在上面时点击落点跑到终端里，粘贴直接失效
                    try:
                        gui.ensure_visible()
                    except Exception as e:
                        log("拉微信到前台失败（继续尝试）：", repr(e))
                    # 发图要截图 + 定位输入框，实测一张 23-39 秒；
                    # 不标一下，看门狗会按 40 秒的发送阈值把它当卡死杀掉
                    SEND_STATE["slow"] = True
                    try:
                        r = with_window_on_top(
                            lambda: gui.send_image(path, who=chat_display_name, verify=True),
                            chat_display_name, str(path))
                    finally:
                        SEND_STATE["slow"] = False
                    if r:
                        return True
                    try:
                        msg = str(r.get("message") or "")
                    except Exception:
                        msg = str(r)
                    log("表情包发送未确认：", msg[:60])
                    return False
                except Exception as e:
                    log("表情包发送异常：", repr(e))
                    return False

    def send_at(member, text, chat_display_name, chat_username=""):
        """在群里真 @ 某个成员（UI 选人），失败返回 False 让调用方退回普通发送。"""
        nonlocal gui
        if not member or not text:
            return False
        if chat_username and (chat_username in dead_chats or not group_is_alive(chat_username)):
            return False
        if AWAY["on"] or away_active() or game_running() or foreground_is_fullscreen():
            with pending_lock:
                pending_replies[chat_username or chat_display_name] = (text, chat_display_name, chat_username, time.time())
            log("你在游戏/全屏程序里，@ 回复先排队：", chat_display_name)
            return True
        with send_lock:
            with gui_lock:
                if gui is None:
                    log("初始化微信窗口驱动…（会抢一下焦点，约 5~10 秒）")
                    gui = WeChatGUI()
            try:
                ok = bool(with_window_on_top(lambda: gui.at_member(member, text, who=chat_display_name, verify=False), chat_display_name, text))
                if ok:
                    log("已 @ %s → %s" % (member, chat_display_name))
                    return True
                log("@ 成员失败（%s），改用普通发送" % member)
            except Exception as e:
                log("@ 成员异常：", repr(e))
        return False

    def describe_image(msg):
        try:
            import base64
            user = msg.get("username") or ""
            lid = msg.get("local_id")
            shard = msg.get("shard")
            if not user or lid is None:
                return ""
            # 先用完整图（原图 > 压缩版），别拿缩略图去识别；两档都没有才退回 best
            path = None
            for _tier in ("full", "best"):
                try:
                    path = _get_media().download_image(user, int(lid), tier=_tier, shard=shard)
                except Exception as _e:
                    log("图片下载失败（tier=%s）：%r" % (_tier, _e))
                    path = None
                if path:
                    log("图片档位：%s → %d KB" % (_tier, os.path.getsize(path) // 1024))
                    break
            if not path:
                log("图片取不到本地文件")
                return ""
            with open(path, "rb") as f:
                raw = f.read()
            if len(raw) > 8 * 1024 * 1024:
                log("图片太大，跳过")
                return ""
            # 关键：按文件头判断真实格式。微信 4.x 的普通图片是 wxgf(HEVC) 容器，
            # 老代码只看扩展名，于是把 wxgf 原样当 image/jpeg 发出去，
            # 视觉接口就回「图片输入格式/解析错误」——表现就是「有时能认、有时不能」。
            if raw[:4] == b"wxgf":
                try:
                    conv = _get_media()._wxgf_to_jpg(raw)
                except Exception as _e:
                    conv = None
                    log("wxgf 转码异常：", repr(_e))
                if not conv:
                    log("这张图是 wxgf/HEVC，本机 ffmpeg 没能转出来，跳过（装 ffmpeg 到 C:\\ffmpeg 即可）")
                    return ""
                raw = conv
                ext = ".jpg"
            elif raw[:8] == b"\x89PNG\r\n\x1a\n":
                ext = ".png"
            elif raw[:3] == b"\xff\xd8\xff":
                ext = ".jpg"
            elif raw[:3] == b"GIF":
                ext = ".gif"
            else:
                ext = os.path.splitext(path)[1].lower()
                if ext not in (".jpg", ".jpeg", ".png", ".gif"):
                    log("图片格式不认识（%s），跳过" % raw[:4])
                    return ""
            mime = "image/png" if ext == ".png" else ("image/gif" if ext == ".gif" else "image/jpeg")
            base = bot_url.rsplit("/webhook", 1)[0]

            # 长截图（竖着很长）整张丢给模型，字会被缩糊；切成 2~3 段分别识别再拼起来
            parts = [raw]
            try:
                from PIL import Image
                import io as _io
                img = Image.open(_io.BytesIO(raw))
                img.load()
                w, h = img.size
                if w > 0 and h / float(w) > 2.2 and h > 1200:
                    n = 3 if h / float(w) > 4.0 else 2
                    overlap = int(h * 0.08)
                    parts = []
                    for i in range(n):
                        top = max(0, int(i * h / n) - (overlap if i else 0))
                        bottom = min(h, int((i + 1) * h / n) + overlap)
                        seg = img.crop((0, top, w, bottom)).convert("RGB")
                        buf = _io.BytesIO()
                        seg.save(buf, format="JPEG", quality=88)
                        parts.append(buf.getvalue())
                    log("长截图 %dx%d，切成 %d 段分别识别" % (w, h, len(parts)))
                else:
                    # 普通图：统一转 RGB JPEG 再发，规避 webp/heic 之类的格式坑
                    img2 = img.convert("RGB")
                    buf = _io.BytesIO()
                    img2.save(buf, format="JPEG", quality=88)
                    parts = [buf.getvalue()]
                    mime = "image/jpeg"
            except Exception as _e:
                log("图片预处理跳过（用原图）：", repr(_e))
                parts = [raw]

            texts = []
            for idx, part in enumerate(parts):
                prompt = ""
                if len(parts) > 1:
                    prompt = "这是一张长截图从上往下的第 %d/%d 段。逐条转写这一段里的文字（分清左边对方/右边本人），不要省略。" % (idx + 1, len(parts))
                try:
                    piece = vision_describe(base64.b64encode(part).decode(), mime, prompt).strip()
                except Exception as _ve:
                    piece = ""
                    log("第 %d 段识别失败：%s" % (idx + 1, repr(_ve)))
                if piece:
                    texts.append(piece)
            text = "\n".join(texts).strip()
            if len(text) > 1200:
                text = text[:1200] + "…"
            log("图片识别：", text[:80])
            return text
        except Exception as e:
            log("图片识别失败：", repr(e))
            return ""

    def _local_transcribe(wav_path):
        """本地 whisper 转写：调系统 Python 跑 transcribe_once.py，零费用。"""
        script = os.path.join(ROOT, "tools", "transcribe_once.py")
        if not os.path.isfile(script):
            log("本地转写脚本不在：", script)
            return ""
        cmd = VOICE_LOCAL_PY.split() + [script, wav_path]
        try:
            r = subprocess.run(
                cmd, capture_output=True, timeout=180,
                encoding="utf-8", errors="replace",
            )
        except Exception as e:
            log("本地转写调用失败：", repr(e))
            return ""
        out = (r.stdout or "").strip()
        if not out and r.returncode:
            log("本地转写失败，退出码 %s：%s" % (r.returncode, (r.stderr or "")[-120:]))
        return out

    def transcribe_voice(msg):
        """语音消息 → 文字；失败返回空串。"""
        if not VOICE_ENABLED:
            log("语音识别已关闭（voice.enabled=false）")
            return ""
        try:
            import io
            import wave
            user = msg.get("username") or ""
            lid = msg.get("local_id")
            shard = msg.get("shard")
            if not user or lid is None:
                return ""
            path = _get_media().download_voice(user, int(lid), shard=shard)
            if not path:
                log("语音没落盘（这条微信没缓存到本地），跳过")
                return ""
            try:
                import pysilk
            except Exception:
                log("缺少 pysilk，装一下：pip install silk-python")
                return ""
            pcm = io.BytesIO()
            with open(path, "rb") as f:
                pysilk.decode(f, pcm, VOICE_SAMPLE_RATE)
            raw = pcm.getvalue()
            seconds = len(raw) / float(VOICE_SAMPLE_RATE * 2)
            if seconds < 0.3:
                log("语音太短（%.1fs），跳过" % seconds)
                return ""
            if seconds > VOICE_MAX_SECONDS:
                log("语音太长（%.0fs > %.0fs），跳过" % (seconds, VOICE_MAX_SECONDS))
                return ""
            wav_path = os.path.splitext(path)[0] + ".wav"
            with wave.open(wav_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(VOICE_SAMPLE_RATE)
                w.writeframes(raw)
            text = ""
            if VOICE_LOCAL:
                text = _local_transcribe(wav_path)
            if not text and VOICE_API_KEY:
                with open(wav_path, "rb") as f:
                    text = asr_transcribe(
                        f.read(), VOICE_API_KEY, VOICE_MODEL, VOICE_BASE_URL, VOICE_TIMEOUT
                    )
            text = clean_transcript(text)
            log("语音识别（%.1fs，%s）：%s"
                % (seconds, "本地" if VOICE_LOCAL else "云端", text[:60] or "（空）"))
            return text
        except Exception as e:
            log("语音识别失败：", repr(e))
            return ""

    def _account_root():
        try:
            for rel, path, _ in db._db_files:
                if os.path.basename(path).startswith("message_"):
                    p = os.path.dirname(path)
                    while p and not os.path.basename(p).startswith(("wxid", "all_users")):
                        parent = os.path.dirname(p)
                        if parent == p:
                            break
                        p = parent
                    return p
        except Exception:
            pass
        return ""

    def _find_sticker_file(md5):
        root = _account_root()
        if not root or not md5:
            return ""
        roots = [os.path.join(root, "cache"), os.path.join(root, "msg", "attach")]
        for base in roots:
            for dirpath, _dirs, files in os.walk(base):
                for name in files:
                    if md5 in name:
                        return os.path.join(dirpath, name)
        return ""

    def _sticker_label(content):
        """从表情 XML 的 desc（base64 protobuf）里提取表情名，比如「鲸鱼娘」"""
        try:
            import base64
            import re
            m = re.search(r'desc\s*=\s*"([^"]*)"', content or "")
            if not m:
                return ""
            raw = base64.b64decode(m.group(1))
            txt = raw.decode("utf-8", "ignore")
            parts = [x for x in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9_]{2,}", txt) if x.lower() != "default"]
            return " ".join(parts[:3])
        except Exception:
            return ""

    def screenshot_sticker(who):
        """截图当前聊天窗口，让视觉模型找最新那条表情包（缓存解不开时的兜底）"""
        try:
            import io
            import base64
            import win32gui
            from PIL import ImageGrab
            hwnd = find_weixin_hwnd()
            if not hwnd:
                return ""
            try:
                u = UIA_CACHE.get("u") or (gui._get_uia() if gui is not None else None)
                if u is not None and who:
                    u.open_chat(who)
                    time.sleep(0.8)
            except Exception:
                pass
            force_foreground(hwnd)
            time.sleep(0.4)
            r = win32gui.GetWindowRect(hwnd)
            # 只截聊天区最底部一条消息，避免看到上面的旧表情包；左边留出 38% 跳过对方头像
            box = (int(r[0] + (r[2] - r[0]) * 0.38), int(r[1] + (r[3] - r[1]) * 0.45), r[2] - 8, r[3] - 120)
            img = ImageGrab.grab(bbox=box)
            # 放大一点，避免小表情包被视觉模型看糊
            img = img.resize((int(img.width * 1.3), int(img.height * 1.3)), 1)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=90)
            text = vision_describe(
                base64.b64encode(buf.getvalue()).decode(),
                "image/jpeg",
                "这是微信聊天窗口下半部分截图，最新消息在最底部、紧贴输入框。只看最底部那一条消息："
                "如果它是表情包/图片，描述画面内容（人物/动物/文字梗）；如果最底部不是表情包，回答「看不清」。"
                "不要描述它上面的任何历史消息。",
                timeout=60,
            ).strip()
            log("表情包截图识别：", text[:40])
            if "看不清" in text or "模糊" in text:
                return ""
            return text
        except Exception as e:
            log("表情包截图识别失败：", repr(e))
            return ""

    def describe_sticker(msg):
        """表情包：按 XML 里的 md5 找本地缓存，转成 jpg 再交给视觉模型。"""
        try:
            import base64
            import re
            import subprocess
            content = str(msg.get("content") or "")
            who = display_name(msg.get("username") or "")
            m = re.search(r'md5="([0-9a-fA-F]{32})"', content)
            md5 = m.group(1).lower() if m else ""
            if not md5:
                shot = screenshot_sticker(who)
                return shot or _sticker_label(content)
            path = _find_sticker_file(md5)
            if not path:
                shot = screenshot_sticker(who)
                if shot:
                    return shot
                return _sticker_label(content)
            with open(path, "rb") as f:
                raw = f.read()
            jpg = None
            if raw[:4] == b"wxgf":
                jpg = _get_media()._wxgf_to_jpg(raw)
            elif raw[:3] == b"\xff\xd8\xff" or raw[:8] == b"\x89PNG\r\n\x1a\n":
                jpg = raw
            else:
                try:
                    exe = ""
                    try:
                        import imageio_ffmpeg
                        exe = imageio_ffmpeg.get_ffmpeg_exe()
                    except Exception:
                        exe = shutil.which("ffmpeg") or ""
                    if not exe:
                        raise RuntimeError("没有可用的 ffmpeg")
                    out = path + ".jpg"
                    subprocess.run([exe, "-y", "-i", path, "-frames:v", "1", out],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                    if os.path.exists(out):
                        with open(out, "rb") as f:
                            jpg = f.read()
                except Exception as e:
                    log("表情包转码失败：", repr(e))
            if not jpg:
                shot = screenshot_sticker(who)
                if shot:
                    return shot
                return _sticker_label(content)
            ext = "image/png" if jpg[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            text = vision_describe(base64.b64encode(jpg).decode(), ext, timeout=60).strip()
            log("表情包识别：", text[:40])
            return text
        except Exception as e:
            log("表情包识别异常：", repr(e))
            return _sticker_label(str(msg.get("content") or ""))

    _redpacket_last = {}

    # 群里什么时候被 @ 过：只有 @ 前后三分钟内发的图才认，
    # 其余一律不调视觉模型（一张约 4 厘、15 秒，被刷表情包就是白烧）
    # 表情/图片按「条」算，不按时间：只认 @ 的前一条和后一条。
    # 线程池只有 1 个 worker，一张图 11-15 秒且串行，认多了后面全卡。
    _probe_last = {}    # 群 -> (@ 之前紧挨着的那条媒体消息) 只留一条
    _next_media = set()  # 刚刚被 @ 过、下一条媒体要认的群

    def announce_red_packet(username):
        """群里来了红包，喊一句。只提醒，不点、不领。

        同一群 90 秒内只喊一次；23 点后到早上 8 点不喊（那会儿群也睡了，
        主动发消息的痕迹越少越好）。
        """
        now = time.time()
        if now - _redpacket_last.get(username, 0) < 90:
            return
        _redpacket_last[username] = now

        hour = time.localtime().tm_hour
        if hour >= 23 or hour < 8:
            log("深夜有红包，不喊了：", display_name(username))
            return

        text = random.choice((
            "群里有红包，手快的先上",
            "红包来了，本鱼喊一声",
            "有红包，快去抢",
        ))

        def _say():
            try:
                send_text(text, display_name(username), username)
                log("群里喊红包 →", display_name(username))
            except Exception as e:
                log("红包提醒发送失败：", repr(e))

        timer = threading.Timer(random.uniform(1.0, 3.0), _say)
        timer.daemon = True
        timer.start()

    def on_message_inner(msg, listener):
        try:
            username = msg.get("username") or ""
            if not username:
                return
            mtype = msg.get("type") or ""
            content = (msg.get("content") or "").strip()
            # 红包：只在群里喊一嗓子，不进大脑（不分析、不追问）
            if mtype == "红包":
                if username.endswith("@chatroom"):
                    try:
                        allowed = not ONLY_CHATS or display_name(username) in ONLY_CHATS
                    except Exception:
                        allowed = not ONLY_CHATS
                    if allowed:
                        announce_red_packet(username)
                return
            # 群里的表情/图片：只认 @ 的前一条和后一条，其余一律跳过。
            # 线程池只有 1 个 worker，一张要 11-15 秒且串行，认多了后面全卡。
            if username.endswith("@chatroom") and mtype in ("图片", "表情", "动画表情"):
                if username in _next_media:
                    # 这是 @ 之后的紧跟一条 → 认，然后关掉这个名额
                    _next_media.discard(username)
                else:
                    # @ 之前的最后一条 → 存着等 @ 时补认；被新的媒体消息覆盖
                    _probe_last[username] = (msg, mtype)
                    return
            if mtype in ("表情", "动画表情"):
                desc = describe_sticker(msg)
                if not desc:
                    log("表情包没认出来，先不说话")
                    return
                content = "（对方刚刚新发了一个表情包，内容是：%s）" % desc
            if mtype == "图片":
                desc = describe_image(msg)
                if not desc:
                    return
                content = "（对方发了一张图片，图里是：%s）" % desc
            if mtype == "语音":
                said = transcribe_voice(msg)
                if not said:
                    log("语音没识别出内容，先不说话")
                    return
                content = "（对方发了一条语音，说的是：%s）" % said
            if mtype not in ("文本", "语音", "图片", "表情", "动画表情"):
                if "移出群聊" in content or "移出" in content:
                    dead_chats.add(username)
                    log("被移出群聊，停掉这个会话：", display_name(username))
                    return
                # 链接卡片 / 分享卡片：从 XML 里把 url 和 title 抠出来，当成一句带链接的话
                import re as _re
                mu = _re.search(r"<url>(.*?)</url>", content, _re.S)
                mt = _re.search(r"<title>(.*?)</title>", content, _re.S)
                if mu:
                    title = (mt.group(1).strip() if mt else "")[:60]
                    content = "分享了一个链接：%s %s" % (title, mu.group(1).strip())
                else:
                    return
            if ONLY_CHATS and username.endswith("@chatroom"):
                try:
                    shown = display_name(username)
                    if shown not in ONLY_CHATS:
                        if username not in _BLOCKED_LOGGED:
                            _BLOCKED_LOGGED.add(username)
                            log("白名单未命中，已忽略：群名=「%s」 ID=%s" % (shown, username))
                        return
                except Exception:
                    return
            # 图片/语音/表情的 content 已经被上面替换成描述，别再重置回原始 XML
            if mtype == "文本":
                content = (msg.get("content") or "").strip()
            if not content or content.startswith("<"):
                return
            sender = msg.get("sender_username") or ""
            if sender == me:
                return  # 自己发的
            create_time = float(msg.get("create_time") or 0)
            if create_time and create_time < START_TS - SKIP_OLDER_THAN:
                return
            # 已经放了太久的消息（比如前面堵着几条），不再回
            if create_time and (time.time() - create_time) > STALE_AFTER:
                log("跳过过期消息（%.0fs 前）：%s" % (time.time() - create_time, content[:20]))
                return

            is_group = username.endswith("@chatroom")
            nickname = ""
            try:
                nickname = db.get_nickname(sender) or ""
            except Exception:
                nickname = ""
            if not nickname:
                nickname = "群友" if is_group else "微信好友"

            # 系统号 / 通知不接话：之前会跟「微信团队」对骂，还把通知当成人来问
            if is_system_notice(username, nickname, content):
                if ONLY_CHATS:
                    return
                log("跳过系统通知：", nickname, "：", content[:24])
                return
            # 同一人短时间内重复同一句 → 不再回（防刷屏 / 防通知重投）
            if duplicate_guard(username, sender, content):
                log("跳过重复消息：", nickname, "：", content[:20])
                return


            # 「拍了拍」这类系统提示不是人在说话，别接
            if "拍了拍" in content and "<url>" not in content:
                log("跳过拍一拍提示：", content[:24])
                return
            # 只把真正的 @ 当作 at_me；普通句子里提到「鲸鲸」不能误触发。
            # WeChat 会在 @昵称 后插入 U+2005，先换成普通空格再判定。
            import re as _mention_re
            _at_text = content.replace("\u2005", " ")
            addressed = any(
                a and _mention_re.search(
                    r"@\s*" + _mention_re.escape(str(a)) + r"(?=$|[\s，,：:！!~～…?？。\-—])",
                    _at_text,
                )
                for a in aliases
            )
            # 群里按「上下各一条」算：@ 后紧接一条媒体认，@ 前紧挨一条补认
            if is_group and mtype == "文本":
                if addressed:
                    _next_media.add(username)
                    _prev = _probe_last.pop(username, None)
                    if _prev:
                        _pm, _pmt = _prev
                        try:
                            _d = describe_sticker(_pm) if _pmt != "图片" else describe_image(_pm)
                        except Exception:
                            _d = ""
                        if _d:
                            content = "（@ 之前紧挨着的那条%s是：%s）%s" % (
                                "图片" if _pmt == "图片" else "表情包", _d, content,
                            )
                            log("补认 @ 之前的那一条")
                else:
                    # 不是 @ 的话，上一条就别留着了：只认紧挨着的那条
                    _next_media.discard(username)
                    _probe_last.pop(username, None)
            _at_remainder = _mention_re.sub(r"^[A-Za-z0-9_\-]{4,}:\s*", "", _at_text).strip()
            for _alias in aliases:
                if _alias:
                    _at_remainder = _mention_re.sub(r"@\s*" + _mention_re.escape(str(_alias)), " ", _at_remainder)
            _bare_mention = not _at_remainder.strip(" \t\r\n，,。.!！?？…~～")
            # 群里先说话、再单独发一个 @ → 把上一条问题补回来
            if username.endswith("@chatroom") and addressed and _bare_mention:
                try:
                    # get_messages 按新到旧返回；直接遍历，拿离这个裸 @ 最近的上一条。
                    for m in db.get_messages(username, limit=12):
                        if m.get("type") != "文本":
                            continue
                        if (m.get("sender_username") or "") != sender:
                            continue
                        prev = (m.get("content") or "").strip()
                        prev = __import__("re").sub(r"^[A-Za-z0-9_\-]{4,}:\s*", "", prev).strip()
                        if prev and not any(a in prev for a in aliases):
                            content = prev
                            log("补上一条问题：", prev[:20])
                            break
                except Exception as e:
                    log("补问题失败：", repr(e))
            # 把群消息里的其他 @ 对象一并交给服务端，帮助模型分清“你”是在说谁。
            _mentioned_names = []
            if is_group:
                for _hit in _mention_re.findall(r"@\s*([^\s@，,：:！!~～…?？。—]+)", _at_text):
                    _name = str(_hit).strip()
                    if _name and _name not in aliases and _name not in _mentioned_names:
                        _mentioned_names.append(_name)
            _t_recv = time.time()
            log("收到", ("群" if is_group else "私聊"), display_name(username), "←", nickname, "：", content[:30])
            payload = {
                "chat_id": username,
                "chat_type": "group" if is_group else "private",
                "user_id": sender or "unknown",
                "senderId": sender or "unknown",
                "sender_id": sender or "unknown",
                "senderName": nickname,
                "nickname": nickname,
                "text": content,
                # msg_id 一定要带创建时间：微信重启或消息库重组后，同一个分片里的
                # local_id 会重新分配，只靠「分片+序号」会撞上 store.json 里持久化的
                # 旧 seenIds，新消息会被大脑当成重复直接静默丢掉（已读不回的真凶）。
                # 拿不到 create_time 时不能用 time.time() 兜底：那样同一条消息被重投
                # 两次会拿到两个不同 id，去重直接失效、还会回两遍。改用正文摘要——
                # 同一句话无论重投多少次都算得出同一个值，而旧 id 只有三段，格式上也撞不上。
                "msg_id": "%s:%s:%s:%s" % (
                    username, msg.get("shard") or "", msg.get("local_id"),
                    str(int(create_time)) if create_time else hashlib.md5(
                        content.encode("utf-8", "replace")
                    ).hexdigest()[:10],
                ),
                "at_me": addressed,
                "mentioned_names": _mentioned_names,
                "chat_title": display_name(username) if is_group else "",
            }


            log("准备转发：", ("群" if is_group else "私聊"), "chat_id=%s msg_id=%s" % (
                username, payload["msg_id"]))
            is_master = bool(MASTER_WXID) and username == MASTER_WXID
            _quiet_word = ""

            # python 引擎：平台判定在本地做，鲸鲸只作为可回退选项（WX_ENGINE=whale）
            if ENGINE != "whale":
                cmd = local_command(payload, is_master)
                if cmd:
                    kind, minutes = cmd
                    apply_command(username, kind, minutes)
                    word = ("好，先安静 %d 分钟" % minutes) if kind == "mute" else "我在了"
                    try:
                        send_text(word, display_name(username), username)
                        log("平台命令：", kind, minutes, "→", display_name(username))
                    except Exception as exc:
                        log("命令回执发送失败：", repr(exc))
                    return
                # 非主人冷却：不丢这条，等够时间再答（消息按先后顺序逐条处理）
                _quiet_word = ""
                if is_group and not is_master:
                    try:
                        import wx_behavior as _wb2

                        if _wx["states"] is None:
                            _wx["states"] = _wb2.load_states(STATE_PATH)
                        _g2, _c2 = _load_cfg_sections()
                        _st = _wb2.state_for(_wx["states"], username)
                        _wait_c = _wb2.cooldown_remaining(_st, _g2, int(time.time() * 1000), is_master)
                        if _wait_c > 0:
                            log("冷却中，等 %.0fs 再答：%s" % (_wait_c / 1000.0, content[:16]))
                            time.sleep(min(_wait_c, 120000) / 1000.0)
                    except Exception as _ce:
                        log("冷却等待异常：", repr(_ce))
                verdict = local_decide(payload, is_master)
                _quiet_word = str(verdict.get("quiet_warning") or "")
                # 被骂时撒娇不还口：不让模型自由发挥，免得真骂起来
                if verdict.get("wake"):
                    try:
                        import wx_behavior as _wb3

                        if _wb3.looks_like_insult(content):
                            send_text(_wb3.sulk_reply(), display_name(username), username)
                            mark_replied(username)
                            log("被骂了，撒娇不还口")
                            return
                    except Exception as _se:
                        log("撒娇判断异常：", repr(_se))
                if not verdict.get("wake"):
                    log("不接话：", verdict.get("reason"), "|", content[:24])
                    return
                canned = verdict.get("canned")
                if canned:
                    try:
                        send_text(canned, display_name(username), username)
                        mark_replied(username)
                    except Exception as exc:
                        log("固定话术发送失败：", repr(exc))
                    return

            # 群聊被叫到后先应一声：2.5 秒还没结果就先发，避免查数据时冷场 20 秒
            _preview = {"timer": None, "sent": False}
            if is_group and addressed:
                def _say_waiting():
                    if _preview["sent"]:
                        return
                    _preview["sent"] = True
                    try:
                        send_text("在，查一下", display_name(username), username)
                    except Exception as exc:
                        log("预回复失败：", repr(exc))
                _preview["timer"] = threading.Timer(2.5, _say_waiting)
                _preview["timer"].daemon = True
                _preview["timer"].start()

            try:
                if ENGINE == "whale":
                    r = requests.post(
                        bot_url,
                        json=payload,
                        headers={"x-bot-token": token} if token else {},
                        timeout=120,
                    )
                    data = r.json()
                    shadow_baseline(payload, data, is_master=is_master)
                    log("鲸鲸响应：HTTP %s action=%s reason=%s reply=%d字" % (
                        r.status_code, data.get("action"), data.get("reason"),
                        len(str(data.get("reply") or ""))))
                else:
                    # 发给大脑的正文要干净：群前缀「wxid_x:」和「@木鱼」都是协议噪音。
                    # 实测「wxid_xxx:\n@木鱼 好久不见」被知识库检索命中食堂条目——
                    # 2-gram 拿整串切片，脏字符会把不相干的问题拉出来。
                    brain_text = _at_text
                    for _a in aliases:
                        if _a:
                            brain_text = _mention_re.sub(
                                r"@\s*" + _mention_re.escape(str(_a)) + r"(?=$|[\s，,：:！!~～…?？。\-—])",
                                "", brain_text,
                            )
                    brain_text = _mention_re.sub(r"^[A-Za-z0-9_\-]{4,}:\s*", "", brain_text).strip()
                    # 木鱼大脑就是飞书那颗：同一个 /chat 入口、同一条 handlers 主链
                    r = requests.post(
                        brain_chat_url(),
                        json={
                            "text": brain_text or content,
                            "chatType": "group" if is_group else "private",
                            "chatId": username,
                            "senderName": nickname,
                            "isMaster": is_master,
                        },
                        headers={"authorization": "Bearer " + brain_chat_token()},
                        timeout=120,
                    )
                    body = r.json() or {}
                    reply_text = str(body.get("reply") or "").strip()
                    data = {
                        "action": "reply" if reply_text else "ignore",
                        "reason": "brain",
                        "reply": reply_text,
                        "bubbles": [reply_text] if reply_text else [],
                    }
                    log("大脑响应：HTTP %s action=%s reply=%d字" % (
                        r.status_code, data["action"], len(reply_text)))
            except Exception as e:
                log("请求鲸鲸失败：", repr(e))
                # 别装死：请求打不通或超时时至少回一句，否则主人只看到「已读不回」，
                # 连「机器人坏了」还是「我没发出去」都分不出来
                if username == MASTER_WXID:
                    try:
                        send_text("这个本鱼现在查不到，过会儿再问一次",
                                  display_name(username), username, force=True)
                    except Exception as e2:
                        log("兜底回执也没发出去：", repr(e2))
                return
            finally:
                if _preview["timer"]:
                    _preview["timer"].cancel()

            if data.get("action") != "reply" or not data.get("reply"):
                # 原来只有群聊才打这行，私聊被挡下时完全静默，排查时没有任何线索
                log("鲸鲸没接话：", nickname, "→", content[:20], "|", data.get("reason"))
                return

            target = display_name(username)
            bubbles = data.get("bubbles") or [data["reply"]]
            if _quiet_word:
                bubbles = list(bubbles) + [_quiet_word]
            mention = str(data.get("mention") or "").strip()
            # 发送交给发送线程：冷却和随机等待都在那边消化，
            # 这条处理完立刻回去读下一条消息（方案 A）。顺序由队列保证。
            _enqueue_reply({
                "username": username,
                "target": target,
                "bubbles": list(bubbles),
                "mention": mention,
                "is_group": is_group,
                "is_master": is_master,
                "nickname": nickname,
                "content": content,
                "t_recv": _t_recv,
            })
        except Exception as e:
            log("处理消息异常：", repr(e))

    log("开始监听新消息…（Ctrl+C 退出）")

    # ---------- 发送队列（方案 A）：读消息与发送解耦 ----------
    # 以前冷却等待直接 sleep 在处理链上，第 2、3 条要等前一条等完冷却才开始想。
    # 现在处理完就入队，发送线程按节奏一条条发；顺序不变，冷却照旧。
    _SEND_QUEUE = []
    _SEND_COND = threading.Condition()
    _MAX_QUEUE_PER_CHAT = 10

    def _enqueue_reply(item):
        with _SEND_COND:
            merged = queue_push(_SEND_QUEUE, item, _MAX_QUEUE_PER_CHAT)
            if merged:
                log("队列满了，把最早一条并进来：", (merged["bubbles"] or [""])[0][:20])
            _SEND_COND.notify()
        log("已排队待发：", item["target"], "| 队列 %d" % len(_SEND_QUEUE))

    def _deliver(item):
        import wx_behavior as _wb

        username = item["username"]
        target = item["target"]
        bubbles = item["bubbles"]
        mention = item["mention"]
        is_group = item["is_group"]
        is_master = item["is_master"]
        nickname = item["nickname"]
        content = item["content"]
        # 冷却：非主人群里按 30 秒一条排队等；在发送线程里等，不堵读消息
        if is_group and not is_master:
            try:
                _g, _c = _load_cfg_sections()
                if _wx["states"] is None:
                    _wx["states"] = _wb.load_states(STATE_PATH)
                _st = _wb.state_for(_wx["states"], username)
                _wait_c = _wb.cooldown_remaining(_st, _g, int(time.time() * 1000), is_master)
                if _wait_c > 0:
                    log("冷却中，等 %.0fs 再发" % (_wait_c / 1000.0))
                    time.sleep(min(_wait_c, 120000) / 1000.0)
            except Exception as _e:
                log("冷却等待异常：", repr(_e))
        # 防封号：按回复长度分层等待，并参考真实思考耗时
        try:
            _g, _c = _load_cfg_sections()
            _w = _wb.reply_delay_ms(
                is_group, _g, _c,
                elapsed_ms=int((time.time() - item["t_recv"]) * 1000),
                reply_len=sum(len(str(b)) for b in bubbles),
            )
            if _w > 0:
                log("发送前等待 %.1fs（随机间隔）" % (_w / 1000.0))
                time.sleep(_w / 1000.0)
        except Exception as _e:
            log("等待计算失败：", repr(_e))
        log("回复", ("群" if is_group else "私聊"), target, "→", nickname, "：", (bubbles[0] or "")[:40])
        mark_replied(username)
        first = True
        for line in bubbles:
            if not line:
                continue
            # 表情包：整条就是一个发图指令，不按文字处理
            if line.startswith("[[sticker:") and line.endswith("]]"):
                _stk = line[len("[[sticker:"):-2]
                if send_sticker(_stk, target):
                    log("表情包已发 →", target, "|", os.path.basename(_stk))
                time.sleep(0.8)
                first = False
                continue
            # 第一条如果是骂人/点名，用真 @（UI 里选成员），而不是纯文本 @名字
            if first and mention and is_group:
                body = line
                for prefix in ("@%s" % mention, "@ %s" % mention):
                    if body.startswith(prefix):
                        body = body[len(prefix):].lstrip(" ，,：:")
                        break
                if send_at(mention, body, target, username):
                    first = False
                    time.sleep(0.6)
                    continue
                log("真 @ 失败，退回普通文本：", mention)
                if not line.startswith("@"):
                    line = "@%s %s" % (mention, line)
            send_text(line, target, username, quote_text=(content if first and QUOTE_REPLY else None))
            first = False
            time.sleep(0.6)

    def _sender_worker():
        while True:
            with _SEND_COND:
                while not _SEND_QUEUE:
                    _SEND_COND.wait()
                item = _SEND_QUEUE.pop(0)
            try:
                _deliver(item)
            except Exception as _e:
                log("发送线程异常：", repr(_e))

    threading.Thread(target=_sender_worker, daemon=True, name="wx-sender").start()

    executor = ThreadPoolExecutor(max_workers=1)

    def on_message(msg, listener):
        # 每条消息丢线程池：发送慢不再阻塞后面消息的接收
        try:
            executor.submit(on_message_inner, msg, listener)
        except Exception as e:
            log("提交消息处理失败：", repr(e))

    listener = Listener(db, interval=0.4)
    listener.add_all(on_message)
    listener.start()
    def recent_chats(limit=2, within=1800):
        out = []
        try:
            for s in db.get_sessions(limit=20):
                u = s.get("username") or ""
                if not u or u == "filehelper":
                    continue
                try:
                    ms = db.get_messages(u, limit=1)
                except Exception:
                    continue
                if not ms:
                    continue
                t = float(ms[0].get("create_time") or 0)
                if t and (time.time() - t) > within:
                    continue
                out.append((u, display_name(u)))
                if len(out) >= limit:
                    break
        except Exception:
            pass
        return out

    def notify_away():
        for u, name in recent_chats():
            send_text("我先去休息一下，晚点回你～", name, u, force=True)
            NOTIFIED.append((u, name))
            time.sleep(0.3)
        if NOTIFIED:
            log("已通知最近的聊天：", "、".join(n for _, n in NOTIFIED))

    def share_watch():
        """每隔一段时间自己刷 B 站热门，挑一个没分享过的发到最近活跃的群/私聊。"""
        state_file = os.path.join(ROOT, "data", "share-state.json")
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                last = float(json.load(f).get("lastTs") or 0)
        except Exception:
            last = 0.0
        if not last:
            # 首次启动：5 分钟后再发，别一开机就刷屏
            last = time.time() - SHARE_EVERY * 60 + 60
        while True:
            time.sleep(30)
            if not SHARE_ENABLED:
                continue
            if time.time() - last < SHARE_EVERY * 60:
                continue
            if game_running() or away_active() or foreground_is_fullscreen():
                continue
            target = recent_chats(limit=1, within=7200)
            if not target:
                log("没有最近活跃的会话，这次先不分享")
                last = time.time()
                continue
            u, name = target[0]
            try:
                base = bot_url.rsplit("/webhook", 1)[0]
                r = requests.get(base + "/api/share?chat=" + urllib.parse.quote(u), timeout=40)
                data = r.json()
            except Exception as e:
                log("拉取视频失败：", repr(e))
                last = time.time()
                continue
            video = data.get("video")
            last = time.time()
            try:
                with open(state_file, "w", encoding="utf-8") as f:
                    json.dump({"lastTs": last}, f)
            except Exception:
                pass
            if not video:
                log("这次没挑到合适的视频")
                continue
            title = str(video.get("title") or "")[:60]
            link = str(video.get("url") or "")
            topic = str(data.get("topic") or "").strip()
            comment = str(data.get("comment") or "").strip()
            if not comment:
                comment = random.choice([
                    "这个看着还行，评论区比视频有意思",
                    "刚刷到，你们看看",
                    "有点意思，我接着刷了",
                ])
            text = "%s\n%s" % (link, comment)
            log("主动分享视频 →", name, str(video.get("title"))[:30])
            send_text(text, name, u)

    BATTERY_SENT = {"done": False}

    def battery_watch():
        while True:
            time.sleep(30)
            try:
                import psutil
                b = psutil.sensors_battery()
            except Exception:
                continue
            if not b or b.percent is None:
                continue
            pct = float(b.percent)
            if pct > 10:
                BATTERY_SENT["done"] = False
                continue
            if pct <= LOW_BATTERY_PCT and not BATTERY_SENT["done"]:
                BATTERY_SENT["done"] = True
                log("电量低（%.0f%%），给最近 3 个会话发晚安" % pct)
                try:
                    for u, name in recent_chats(limit=3, within=86400):
                        send_text(GOODNIGHT_TEXT, name, u, force=True)
                        time.sleep(0.4)
                except Exception as e:
                    log("发晚安失败：", repr(e))

    def notify_back():
        for u, name in list(NOTIFIED):
            send_text("我回来啦，刚在打游戏", name, u, force=True)
            time.sleep(0.3)
        NOTIFIED.clear()

    def game_watch():
        while True:
            time.sleep(4)
            running = game_running()
            if running and not AWAY["on"]:
                AWAY["on"] = True
                log("检测到游戏运行，进入休息模式（回复先排队）")
                if AWAY_NOTICE and not game_running():
                    try:
                        notify_away()
                    except Exception as e:
                        log("休息通知失败：", repr(e))
            elif not running and AWAY["on"]:
                AWAY["on"] = False
                log("游戏已退出，补发排队消息")
                with pending_lock:
                    items = list(pending_replies.values())
                    pending_replies.clear()
                now = time.time()
                fresh = [x for x in items if now - x[3] <= QUEUED_MAX_AGE]
                dropped = len(items) - len(fresh)
                fresh = fresh[-QUEUED_MAX_CHATS:]
                if dropped:
                    log("排队里有 %d 条已经太旧（>%ds），不补发" % (dropped, int(QUEUED_MAX_AGE)))
                for text, target, uname, _ts in fresh:
                    send_text(text, target, uname, force=True)
                if RETURN_NOTICE:
                    try:
                        notify_back()
                    except Exception as e:
                        log("回来通知失败：", repr(e))

    def digest_watch():
        last = time.time()
        while True:
            time.sleep(30)
            if DIGEST_MINUTES <= 0 or not MASTER_WXID:
                continue
            if time.time() - last < DIGEST_MINUTES * 60:
                continue
            if AWAY["on"] or away_active() or game_running() or foreground_is_fullscreen():
                continue
            try:
                base = bot_url.rsplit("/webhook", 1)[0]
                r = requests.get(base + "/api/digest", params={"since": int(last * 1000)}, timeout=30)
                data = r.json()
            except Exception as e:
                log("拉取消息汇总失败：", repr(e))
                continue
            if data.get("count") and data.get("text"):
                name = display_name(MASTER_WXID) or "主人"
                log("给主人发消息汇总：%d 条" % data["count"])
                if send_text(data["text"], name, MASTER_WXID, force=True):
                    last = time.time()
            else:
                last = time.time()

    def push_watch():
        """从大脑拉早报/二课新活动这类定时内容，发给主人私聊。

        只走主人私聊：群发定时消息是最容易被判定为批量操作的行为。
        """
        if not PUSH_ENABLED:
            log("定时推送未启用（brain.push=false 或没配 master）")
            return
        sess = requests.Session()
        headers = {"authorization": "Bearer " + BRAIN_TOKEN} if BRAIN_TOKEN else {}
        log("定时推送已启动，每 %.0f 秒问一次大脑" % PUSH_EVERY)
        while True:
            time.sleep(PUSH_EVERY)
            if AWAY["on"] or away_active() or game_running():
                continue
            # 深夜只收不发：消息留在 outbox，第二天早上再发
            if not (8 <= time.localtime().tm_hour < 23):
                continue
            try:
                r = sess.get(BRAIN_BASE + "/push/pending", headers=headers, timeout=20)
                items = (r.json() or {}).get("items") or []
            except Exception as e:
                log("拉取待推送失败：", repr(e))
                continue
            if not items:
                continue
            done = []
            for it in items:
                text = str(it.get("content") or "").strip()
                if not text:
                    done.append(it.get("id"))
                    continue
                # 按 outbox 里的 target 发：wxid 走私聊，xxx@chatroom 走群
                uname = str(it.get("target") or MASTER_WXID)
                # 大脑那边存的是 wx:private:wxid_xxx / wx:group:xxx@chatroom，
                # 前缀是它的会话命名，微信里搜不到，必须剥掉
                for _prefix in ("wx:private:", "wx:group:"):
                    if uname.startswith(_prefix):
                        uname = uname[len(_prefix):]
                        break
                name = display_name(uname) or ("主人" if uname == MASTER_WXID else uname)
                if send_text(text, name, uname, force=True):
                    done.append(it.get("id"))
                    log("推送 %s：%d 字" % (it.get("kind"), len(text)))
                time.sleep(2)
            if done:
                try:
                    sess.post(BRAIN_BASE + "/push/ack", json={"ids": done}, headers=headers, timeout=20)
                except Exception as e:
                    log("确认推送失败：", repr(e))

    def explain_uia_failure():
        """UIA 拿不到时把原因说清楚。

        「热激活 UIA 失败」有三种完全不同的原因，光看那一句没法修：
          A. 主窗所在进程里枚举不到 Weixin.dll（挑到子进程 / 权限 / 微信刚重启）
          B. 这个微信版本库还不支持（特征扫描没命中、版本表里也没有）
          C. 补丁写进去了但 mmui 控件没出现（校验失败，已回滚）
        """
        try:
            import win32process
            from wechatauto.accessibility import AccessibilityGate, QACCESSIBLE_ACTIVE_RVA_BY_VERSION
        except Exception as e:
            log("UIA 诊断不可用：", repr(e))
            return
        hwnd = find_weixin_hwnd()
        if not hwnd:
            log("UIA 诊断：找不到标题为「微信」的主窗（微信缩在托盘？先点开它）")
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            mods = list(AccessibilityGate._process_modules(pid) or [])
        except Exception as e:
            log("UIA 诊断：枚举模块失败：", repr(e))
            return
        dll = None
        for _b, _s, name, path in mods:
            if (name or "").lower() == "weixin.dll":
                dll = path
                break
        if not dll:
            log("UIA 诊断（原因A）：主窗进程 pid=%s 里只有 %d 个模块，枚举不到 Weixin.dll" % (pid, len(mods)))
            log("    → 多见于：微信刚重启、主窗属于子进程、或与微信权限不一致")
            return
        ver = os.path.basename(os.path.dirname(dll))
        try:
            cands = AccessibilityGate._qaccessible_candidate_rvas(dll)
        except Exception as e:
            log("UIA 诊断：取候选 RVA 失败：", repr(e))
            return
        known = QACCESSIBLE_ACTIVE_RVA_BY_VERSION.get(ver)
        if not cands:
            log("UIA 诊断（原因B）：微信 %s 没有任何可用 gate RVA，这个版本库还不支持" % ver)
            log("    → 升级 wechatauto-replica，或换回 4.1.13.65 / 4.1.15.53")
        else:
            log("UIA 诊断（原因C）：微信 %s，候选 RVA %d 个（%s），补丁写了但 mmui 没出现" % (
                ver, len(cands),
                "版本表命中" if known is not None else "靠特征扫描",
            ))
            log("    → 重启微信后立刻重试；仍失败说明该版本的保护变了，等库更新")

    def warmup():
        """启动后预热发送通道，避免第一条回复慢 5~10 秒。"""
        time.sleep(3)
        if game_running():
            log("游戏在运行，跳过发送通道预热")
            return
        try:
            nonlocal gui
            with gui_lock:
                if gui is None:
                    gui = WeChatGUI()
            u = gui._get_uia()
            UIA_CACHE["u"] = u
            if u is None:
                log("发送通道预热：UIA 不可用（只剩 OCR 兜底，先跑「一键修复并自检.bat」）")
                explain_uia_failure()
            else:
                win = getattr(u, "_win", None)
                try:
                    wcls = win.ClassName if win is not None else None
                    wname = win.Name if win is not None else None
                except Exception:                # noqa: BLE001
                    wcls, wname = None, None
                log("发送通道预热完成（UIA 可用，主窗类名=%r 名称=%r）" % (wcls, wname))
        except Exception as e:
            log("发送通道预热失败：", repr(e))

    def send_watchdog():
        """发送卡住就重启桥接，避免整个队列卡死。

        文字 40 秒算卡死；发图片另算 120 秒——截图 + 定位输入框本身就要
        二三十秒，拿文字的阈值去卡它，每发一次表情包就被杀一次。
        """
        while True:
            time.sleep(5)
            limit = 120 if SEND_STATE.get("slow") else 40
            if SEND_STATE["active"] and (time.time() - SEND_STATE["start"]) > limit:
                stuck = time.time() - SEND_STATE["start"]
                log("发送卡住 %.0f 秒（阈值 %d），强制重启桥接进程…" % (stuck, limit))
                log("  卡在会话：%s" % SEND_STATE.get("who", "?"))
                log("  内容开头：%s" % (SEND_STATE.get("text") or "?"))
                log("  常见原因：微信窗口被别的窗口挡住 / 缩到托盘 / 输入法弹窗卡住")
                time.sleep(0.3)
                os._exit(2)

    def flush_pending():
        while True:
            time.sleep(2)
            if AWAY["on"] or away_active() or foreground_is_fullscreen():
                continue
            with pending_lock:
                if not pending_replies:
                    continue
                items = list(pending_replies.values())
                pending_replies.clear()
            now = time.time()
            fresh = [x for x in items if now - x[3] <= QUEUED_MAX_AGE][-QUEUED_MAX_CHATS:]
            for text, target, uname, _ts in fresh:
                log("补发排队消息 →", target)
                send_text(text, target, uname, force=True)

    threading.Thread(target=flush_pending, daemon=True).start()
    threading.Thread(target=game_watch, daemon=True).start()
    # 群话题摘要：他 2026-11-15 明确不要这个功能，线程不再启动
    # threading.Thread(target=digest_watch, daemon=True).start()
    threading.Thread(target=push_watch, daemon=True).start()
    threading.Thread(target=warmup, daemon=True).start()
    threading.Thread(target=send_watchdog, daemon=True).start()
    threading.Thread(target=battery_watch, daemon=True).start()
    threading.Thread(target=share_watch, daemon=True).start()
    log("已启动。鲸鲸地址：", bot_url)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("正在退出…")
        listener.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
