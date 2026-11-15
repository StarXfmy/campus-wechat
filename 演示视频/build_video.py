"""校园木鱼微信端演示视频合成。

以旁白音频为主时间轴：每幕取源录屏的一个区间，画面比旁白短就冻尾帧补齐。
画面统一裁出微信窗口放大到 1920×1080，底部留 90 像素字幕带。
字幕和卡片都用 Pillow 渲染成 PNG 再叠加——这台机器的 ffmpeg 里 fontconfig 坏了
（Cannot load default config file），libass 取不到中文字体，subtitles 滤镜画出来是空白。

跑法：py -3 build_video.py            全量并拼接
      py -3 build_video.py m03 m00    只重做指定幕（不拼接）
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent
SRC = "C:/Users/lenovo/Desktop/微信木鱼.mp4"
W, H, FPS = 1920, 1080, 30
BAND = 90                       # 底部字幕带高度
BG = "#F3EEE3"                  # 暖米色
INK = "#3B3229"                 # 正文深棕灰
ACCENT = "#8A6D3B"
FONT_PATH = "C:/Windows/Fonts/msyh.ttc"

BUILD = ROOT / "build"
BUILD.mkdir(exist_ok=True)
DUR = json.loads((ROOT / "audio" / "durations.json").read_text(encoding="utf-8"))

# 每幕的源视频区间（秒）；卡片幕用 card 标记
SCENES = [
    ("m00", {"card": "title"}),
    ("m01", {"src": (1.0, 27.0)}),
    ("m02", {"src": (27.0, 44.0)}),
    ("m03", {"src": (51.8, 68.0)}),
    ("m04", {"src": (75.0, 93.5)}),
    ("m05", {"src": (101.5, 118.5)}),
    ("m06", {"src": (133.0, 156.0)}),
    ("m07", {"src": (162.0, 194.0)}),
    ("m08", {"src": (200.0, 232.0)}),
    ("m09", {"src": (244.5, 275.0)}),
    ("m10", {"src": (275.0, 297.0)}),
    ("m11", {"card": "outro"}),
]

TAIL = 0.6  # 旁白结束后保留的画面余量
BASE_VF = (
    "crop=1470:1004:450:254,scale=-2:%d,pad=%d:%d:(%d-iw)/2:0:%s,setsar=1"
    % (H - BAND, W, H, W, BG.replace("#", "0x"))
)

CUE_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n(.+?)(?:\n\n|\Z)",
    re.S,
)


def run(cmd: list[str]) -> None:
    res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        print(res.stderr[-2500:])
        raise SystemExit("命令失败：%s" % " ".join(cmd[:8]))


def rel(p: pathlib.Path) -> str:
    return p.relative_to(ROOT).as_posix()


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH, size, index=0)


def text_png(path: pathlib.Path, text: str, size: int, color: str) -> None:
    """透明画布上居中一行字，超长自动缩字号。"""
    img = Image.new("RGBA", (W, BAND), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = font(size)
    while f.getlength(text) > W - 90 and size > 18:
        size -= 2
        f = font(size)
    d.text(((W - f.getlength(text)) / 2, (BAND - size) / 2 - 2),
           text, font=f, fill=color)
    img.save(path)


def cues(sid: str) -> list[tuple[float, float, str]]:
    """读该幕的句级 VTT。"""
    vtt = (ROOT / "audio" / ("%s.vtt" % sid)).read_text(encoding="utf-8")
    out = []
    for m in CUE_RE.finditer(vtt):
        g = m.groups()
        start = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3]) / 1000
        end = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7]) / 1000
        out.append((start, end, " ".join(g[8].split())))
    return out


def make_card(path: pathlib.Path, lines: list[tuple[str, int, str]]) -> None:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    total = sum(size + 70 for _, size, _ in lines)
    y = (H - total) // 2 - 30
    for i, (text, size, color) in enumerate(lines):
        f = font(size)
        d.text(((W - f.getlength(text)) / 2, y), text, font=f, fill=color)
        y += size + 70
        if i == 0:
            d.line([(W // 2 - 150, y - 30), (W // 2 + 150, y - 30)],
                   fill=ACCENT, width=3)
    img.save(path)


def scene_duration(sid: str, spec: dict) -> float:
    audio = DUR[sid] / 1000.0
    if "src" in spec:
        a, b = spec["src"]
        return max(b - a, audio + TAIL)
    return audio + (1.2 if sid == "m00" else 2.4)


def build_scene(sid: str, spec: dict) -> pathlib.Path:
    out = BUILD / ("%s.mp4" % sid)
    dur = scene_duration(sid, spec)
    cue_list = cues(sid)

    if "card" in spec:
        png = BUILD / ("%s.png" % sid)
        if spec["card"] == "title":
            make_card(png, [
                ("校园木鱼 · 微信端", 82, INK),
                ("跑在个人微信里的校园助手", 40, ACCENT),
                ("真实操作录屏 · 已剪去等待", 28, ACCENT),
            ])
        else:
            make_card(png, [
                ("谢谢观看", 82, INK),
                ("三进程链路 · 严格触发 · 本地语音与识图", 40, ACCENT),                ("慢，是它像人的代价", 28, ACCENT),
            ])
        main_in = ["-loop", "1", "-framerate", str(FPS), "-t", "%.3f" % dur,
                   "-i", rel(png)]
        vf = "scale=%d:%d,setsar=1" % (W, H)
    else:
        a, b = spec["src"]
        clip_len = b - a
        main_in = ["-ss", "%.3f" % a, "-t", "%.3f" % clip_len, "-i", SRC]
        vf = BASE_VF
        gap = dur - clip_len
        if gap > 0.02:
            vf += ",tpad=stop_mode=clone:stop_duration=%.3f" % gap

    # 每条字幕一个循环输入，用 enable 按句级时间戳开关
    sub_in: list[str] = []
    for i, (_s, _e, text) in enumerate(cue_list):
        png = BUILD / ("sub_%s_%02d.png" % (sid, i))
        text_png(png, text, 30, INK)
        sub_in += ["-loop", "1", "-framerate", str(FPS), "-t", "%.3f" % dur,
                   "-i", rel(png)]

    parts = ["[0:v]%s,fps=%d[base]" % (vf, FPS)]
    cur = "[base]"
    for i, (s, e, _t) in enumerate(cue_list):
        nxt = "[c%d]" % i
        stop = min(e - 0.03, dur)
        if stop <= s:
            continue
        parts.append("%s[%d:v]overlay=0:%d:enable='between(t,%.3f,%.3f)'%s"
                     % (cur, i + 2, H - BAND, s, stop, nxt))
        cur = nxt
    parts.append("%sformat=yuv420p[v]" % cur)
    parts.append("[1:a]aresample=44100,apad[a]")

    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + main_in
           + ["-i", rel(ROOT / "audio" / ("%s.mp3" % sid))] + sub_in
           + ["-filter_complex", ";".join(parts),
              "-map", "[v]", "-map", "[a]", "-t", "%.3f" % dur,
              "-c:v", "libx264", "-preset", "medium", "-crf", "20",
              "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
              "-ar", "44100", "-ac", "2", "-video_track_timescale", "30000",
              rel(out)])
    run(cmd)
    return out


def main() -> None:
    only = set(sys.argv[1:])
    names = []
    for sid, spec in SCENES:
        cached = BUILD / ("%s.mp4" % sid)
        if only and sid not in only:
            continue
        build_scene(sid, spec)
        print("built %s dur=%.2f" % (sid, scene_duration(sid, spec)), flush=True)
        names.append(cached.name)

    if only:
        print("单幕模式，不拼接")
        return

    (BUILD / "list.txt").write_text(
        "".join("file '%s'\n" % n for n in names), encoding="utf-8")
    final = ROOT / "out"
    final.mkdir(exist_ok=True)
    dst = final / "校园木鱼微信端_成片.mp4"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "concat", "-safe", "1", "-i", rel(BUILD / "list.txt"),
         "-c", "copy", "-movflags", "+faststart", rel(dst)])
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration:stream=width,height,codec_name",
                            "-of", "default=nw=1", str(dst)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
    print("成片", dst)
    print(probe.stdout)


if __name__ == "__main__":
    main()
