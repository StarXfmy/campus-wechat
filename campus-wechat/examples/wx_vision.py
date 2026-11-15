"""看图：把图片交给视觉模型（默认智谱 GLM-4V-Flash），返回可读描述。

从鲸鲸的 src/vision.js 原样搬过来，保留了三件关键东西：
  1. Key 的查找顺序（网页设置里填的那把要认）；
  2. 以真实文件头判断图片格式（微信常给 HEIC 容器或 webp，扩展名不可信）；
  3. 默认提示词——微信截图要按气泡左右分说话人、逐条转写、不许省略。

没有 Key 时抛错由调用方兜底，不在这里瞎猜画面。
"""
from __future__ import annotations

import base64
import json
import os

import requests

DEFAULT_BASE = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-4v-flash"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
SETTINGS_PATH = os.path.join(ROOT, "data", "settings.json")

DEFAULT_PROMPT = "\n".join([
    "把这张图看清，用纯文本回答（不要 JSON、不要 markdown 代码块）：",
    "1. 如果是微信截图：只看气泡位置判断说话人——左边气泡一律写「对方：」，右边绿色气泡一律写「本人：」，"
    "不要根据内容猜谁是谁，也不要把气泡里的「主人」「鲸鲸」当成说话人标签。",
    "2. 逐条转写文字，顺序从上到下；顶部的群名、时间、未读数、电量也写出来。",
    "3. 如果是其他截图：把界面上的按钮、数字、标题按位置简单说清。照片：说清主体、场景、动作、画面里的字。",
    "4. 如果是表情包：先一句话说清画面，再说它想表达什么——注意中文谐音梗和网络流行梗，"
    "例如螃蟹=「蟹蟹」=谢谢、狗头=反讽、捂脸=无语、裂开=崩溃、竖大拇指=点赞、流泪=感动或委屈；"
    "优先给出「它想表达什么」，不要只描述画面。拿不准就写「大概是……的意思」，不要硬猜。",
    "最后一行用一句话总结这张图在说什么；表情包就总结它想表达什么情绪。",
    "忽略图里的头像、昵称、时间戳、未读数、电量这些界面元素，只关注消息内容和表情包本身。",
    "看不清的字写「模糊」，绝对不要编造。截图里每一条消息都要写到：不要省略、不要合并、不要只写大意。",
    "总长度控制在 900 字以内；宁可条目短一点，也不要漏掉人和话。",
])


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_settings() -> dict:
    cfg = _read_json(CONFIG_PATH)
    vision = cfg.get("vision") or {}
    settings = _read_json(SETTINGS_PATH)
    return {"vision": vision if isinstance(vision, dict) else {}, "settings": settings}


def resolve_key() -> str:
    """按鲸鲸的顺序找 Key：config.vision → 网页设置 → env。"""
    found = resolve_settings()
    vision = found["vision"]
    settings = found["settings"]
    keys = settings.get("keys") or {}
    for candidate in (
        vision.get("apiKey"),
        settings.get("visionApiKey"),
        keys.get("vision") if isinstance(keys, dict) else "",
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    env = os.environ.get("ZHIPU_API_KEY") or os.environ.get("VISION_API_KEY") or ""
    if env.strip():
        return env.strip()
    return str((keys.get("zhipu") if isinstance(keys, dict) else "") or "").strip()


def mask_key(key: str) -> str:
    s = str(key or "")
    if not s:
        return ""
    if len(s) <= 10:
        return s[:2] + "****"
    return s[:5] + "****" + s[-4:]


def sniff_mime(image_b64: str) -> str:
    """按真实文件头判断格式。识别不出返回空串，由调用方用原 mime。"""
    b64 = str(image_b64 or "")
    if b64.startswith("data:"):
        b64 = b64.split(",", 1)[-1]
    b64 = "".join(b64.split())
    try:
        head = base64.b64decode(b64[:32] + "=" * (-len(b64[:32]) % 4))
    except Exception:
        return ""
    if len(head) < 12:
        return ""
    if head[0:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[0:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[0:3] == b"GIF":
        return "image/gif"
    if head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[0:4] == b"wxgf":
        return "image/wxgf"  # 微信 HEVC 私有容器，视觉接口不收
    return ""


def describe(image_b64: str, mime: str = "image/jpeg", prompt: str = "", timeout: float | None = None) -> str:
    """调用视觉模型。失败抛异常，由调用方决定是回一句「看不了」还是重试。"""
    found = resolve_settings()
    vision = found["vision"]
    base_url = str(vision.get("baseUrl") or DEFAULT_BASE).rstrip("/")
    model = str(vision.get("model") or DEFAULT_MODEL).strip()
    key = resolve_key()
    if not image_b64:
        raise ValueError("没有图片数据")
    if not key:
        raise RuntimeError(
            "看图没有 Key：网页右上角「设置」→ 填「看图 API Key」"
            "（默认智谱 GLM-4V-Flash，Key 在 open.bigmodel.cn 申请；DeepSeek 的 Key 不能看图）"
        )
    real_mime = sniff_mime(image_b64) or mime or "image/jpeg"
    if real_mime == "image/wxgf":
        raise RuntimeError("这张图是微信的 wxgf/HEVC 私有格式，需要 ffmpeg 转码后才看得懂")
    clean = str(image_b64)
    if clean.startswith("data:"):
        clean = clean.split(",", 1)[-1]
    clean = "".join(clean.split())

    max_tokens = vision.get("maxTokens")
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        max_tokens = 1024 if "bigmodel.cn" in base_url else 1600

    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{real_mime};base64,{clean}"}},
                {"type": "text", "text": prompt or DEFAULT_PROMPT},
            ],
        }],
        "temperature": 0.1,
        # GLM-4V 系列上限就是 1024，超了直接 400
        "max_tokens": max_tokens,
    }
    resp = requests.post(
        base_url + "/chat/completions",
        json=body,
        headers={"Authorization": "Bearer " + key},
        timeout=timeout or float(vision.get("timeoutMs") or 30000) / 1000.0,
    )
    if resp.status_code != 200:
        detail = ""
        try:
            err = (resp.json() or {}).get("error") or {}
            detail = str(err.get("message") or err.get("code") or "")
        except Exception:
            detail = resp.text[:120]
        hint = ""
        if resp.status_code in (401, 403):
            hint = "（Key 无效或没有看图权限：这里要 open.bigmodel.cn 的智谱 Key，DeepSeek 的 Key 不支持看图）"
        raise RuntimeError(f"视觉模型返回：{detail or 'HTTP %s' % resp.status_code}{hint}")
    try:
        text = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError("视觉模型返回了空内容")
    return str(text or "").strip()


def status() -> dict:
    found = resolve_settings()
    vision = found["vision"]
    key = resolve_key()
    return {
        "configured": bool(key),
        "keyMasked": mask_key(key),
        "model": str(vision.get("model") or DEFAULT_MODEL),
        "baseUrl": str(vision.get("baseUrl") or DEFAULT_BASE),
    }
