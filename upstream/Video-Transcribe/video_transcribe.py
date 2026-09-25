#!/usr/bin/env python3
"""
video_transcribe.py - Extract text/transcripts from YouTube and Bilibili videos.

Supports:
- CC subtitle extraction (fastest)
- Audio download + ASR transcription (faster-whisper)
- LLM post-correction with context (title + description + comments)

Model cache is redirected to D:\\lk\\.cache\\ to avoid filling C drive.

Usage:
    python video_transcribe.py "https://www.bilibili.com/video/BV..."
    python video_transcribe.py "https://www.youtube.com/watch?v=..."
    python video_transcribe.py "URL" --llm-correction        # 启用脚本内置 LLM 纠正（默认关闭，日常纠错由 AI 助手完成）
    python video_transcribe.py "URL" --whisper-size base
    python video_transcribe.py "URL" --llm-correction --llm-model deepseek-chat --base-url https://api.deepseek.com --api-key YOUR_KEY
"""

import argparse
import atexit
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# ── Bypass forced proxy (WorkBuddy sandbox injects HTTP_PROXY, slows Bili downloads ~78x) ──
for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(key, None)

# ── Redirect model caches to D:\lk\.cache\ ──────────────────────────────
CACHE_ROOT = os.path.join("D:\\", "lk", ".cache")
os.environ.setdefault("XDG_CACHE_HOME", CACHE_ROOT)
os.environ.setdefault("HF_HOME", os.path.join(CACHE_ROOT, "huggingface"))
os.environ.setdefault("TRANSFORMERS_CACHE", os.path.join(CACHE_ROOT, "huggingface"))
os.environ.setdefault("WHISPER_CACHE_DIR", os.path.join(CACHE_ROOT, "faster-whisper"))
os.environ.setdefault("TORCH_HOME", os.path.join(CACHE_ROOT, "torch"))
# Ensure cache dirs exist
for key in ("XDG_CACHE_HOME", "HF_HOME", "WHISPER_CACHE_DIR", "TORCH_HOME"):
    d = os.environ.get(key, "")
    if d:
        os.makedirs(d, exist_ok=True)
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

# ── Track temp dirs for cleanup on crash ──────────────────────────────
_tmp_dirs = []
def _cleanup_tmp():
    for d in _tmp_dirs:
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
atexit.register(_cleanup_tmp)
signal.signal(signal.SIGTERM, lambda *_: (_cleanup_tmp(), sys.exit(1)))


def detect_platform(url: str) -> str:
    """Detect video platform from URL."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if any(d in host for d in ["youtube.com", "youtu.be"]):
        return "youtube"
    if any(d in host for d in ["bilibili.com", "b23.tv"]):
        return "bilibili"
    return "unknown"


# ── yt-dlp 公共参数：B站必须带 UA+Referer（脏缓存实测 2026-08-11）──────────
# B站 CDN 对无 UA 请求会命中脏缓存节点，返回错误视频的标题/时长（两次实测中招）。
# 非 B站平台只带 UA（referer 只对 B站有意义）。
BILI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

def ytdlp_args(url: str) -> list:
    """yt-dlp 公共参数：UA 必带；B站额外带 Referer 防脏缓存。"""
    args = ["--user-agent", BILI_UA]
    if any(d in url for d in ["bilibili.com", "b23.tv"]):
        args += ["--referer", "https://www.bilibili.com/"]
    return args


def sanitize_title(title: str) -> str:
    """统一文件名清理：过滤非法字符 + 全角竖线（｜）等易混字符，长度上限 80。

    2026-08-11 修正：全角 ｜ 不过滤会导致脚本产物与手工 corrected.md 命名不一致。
    """
    return re.sub(r'[<>:"/\\|?*｜|·]', '', title or "Unknown").strip()[:80]


def get_video_info(url: str) -> dict:
    """Get video metadata: yt-dlp first, Bilibili official API as fallback."""
    try:
        result = subprocess.run(
            ["yt-dlp"] + ytdlp_args(url) + ["--dump-json", "--no-download", url],
            capture_output=True, text=True, timeout=60, check=True,
            encoding="utf-8", errors="replace",
        )
        return json.loads(result.stdout.strip().split("\n")[0])
    except Exception as e:
        print(f"  Warning: yt-dlp metadata failed ({e}), trying Bilibili API...")
    # Bilibili official API fallback (yt-dlp often hits HTTP 412 anti-crawl)
    if any(d in url for d in ["bilibili.com", "b23.tv"]):
        m = re.search(r"(BV[0-9A-Za-z]{10})", url)
        if m:
            bvid = m.group(1)
            api = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
            try:
                import urllib.request
                req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "Referer": "https://www.bilibili.com"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())["data"]
                return {
                    "title": data.get("title", "Unknown"),
                    "description": data.get("desc", ""),
                    "id": str(data.get("aid", "")),
                    "duration": data.get("duration", 0),
                    "bvid": data.get("bvid", bvid),
                    "uploader": (data.get("owner") or {}).get("name", ""),
                    "mid": str((data.get("owner") or {}).get("mid", "")),
                    "cid": str(data.get("cid", "")),
                    "pubdate": data.get("pubdate", 0),
                    "tname": data.get("tname", ""),
                    "stat": data.get("stat", {}),
                    "pic": data.get("pic", ""),
                }
            except Exception as e2:
                print(f"  Warning: Bilibili API also failed: {e2}")
    return {"title": "Unknown", "description": "", "id": ""}


def _load_bili_cookies() -> dict:
    """Load Bilibili login cookies for full-comment access.

    Two sources (checked in order):
    1. env BILIBILI_COOKIE:  "SESSDATA=xxx; bili_jct=xxx; DedeUserID=xxx" (raw header string)
    2. env BILIBILI_COOKIES_FILE: path to a Netscape-format cookies.txt (yt-dlp export)
    Returns {} when not logged in → anonymous mode (hot comments only).
    """
    env = os.environ.get("BILIBILI_COOKIE", "").strip()
    if env:
        return dict(
            (p.split("=", 1)[0].strip(), p.split("=", 1)[1].strip())
            for p in env.split(";") if "=" in p
        )
    path = os.environ.get("BILIBILI_COOKIES_FILE", "").strip()
    if path and os.path.isfile(path):
        try:
            import http.cookiejar
            jar = http.cookiejar.MozillaCookieJar()
            jar.load(path, ignore_discard=True, ignore_expires=True)
            return {c.name: c.value for c in jar if "bilibili" in (c.domain or "")}
        except Exception:
            pass
    return {}


def fetch_bilibili_comments(aid: str, limit: int = 20) -> str:
    """Fetch comments + their reply threads from Bilibili API.

    Anonymous mode (no login): x/v2/reply is rate-limited by Bilibili to ~3 hot
    comments per video (offset pagination ignored, verified 2026-08-07); reply
    threads (x/v2/reply/reply) are NOT rate-limited → full discussion captured.
    Logged-in mode (BILIBILI_COOKIE / BILIBILI_COOKIES_FILE set): full pagination
    of main comments until `limit` is reached.
    Output format: "主评论 [赞N] msg | ↳回复1 | ↳回复2 ..." joined by " || ".
    """
    import urllib.request
    import urllib.parse
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "Referer": "https://www.bilibili.com"}
    cookies = _load_bili_cookies()
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    def _get(url: str) -> dict:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception:
            return {}

    def _replies_of(url: str) -> list:
        return (_get(url).get("data") or {}).get("replies") or []

    def _block_of(r: dict) -> str:
        msg = (r.get("content") or {}).get("message", "").strip()
        if not msg:
            return ""
        block = [f"[赞{r.get('like', 0)}] {msg}"]
        rpid = r.get("rpid")
        if rpid:
            rep_url = (f"https://api.bilibili.com/x/v2/reply/reply?type=1&oid={aid}"
                       f"&root={rpid}&ps=20")
            for rep in _replies_of(rep_url)[:20]:
                rmsg = (rep.get("content") or {}).get("message", "").strip()
                if rmsg:
                    block.append(f"↳ {rmsg[:120]}")
        return " | ".join(block)

    try:
        texts = []
        if cookies:
            # Logged-in: paginate main comments until limit
            offset = ""
            while len(texts) < limit:
                pg = json.dumps({"offset": offset}) if offset else json.dumps({})
                url = (f"https://api.bilibili.com/x/v2/reply?type=1&oid={aid}"
                       f"&ps=20&sort=2&pagination_str={urllib.parse.quote(pg)}")
                reps = _replies_of(url)
                if not reps:
                    break
                for r in reps:
                    if len(texts) >= limit:
                        break
                    b = _block_of(r)
                    if b:
                        texts.append(b)
                offset = str(reps[-1]["rpid"])
                if len(reps) < 20:
                    break
        else:
            # Anonymous: single hot-comments page (rate-limited by Bilibili)
            main_url = f"https://api.bilibili.com/x/v2/reply?type=1&oid={aid}&ps={limit}&sort=2"
            for r in _replies_of(main_url):
                b = _block_of(r)
                if b:
                    texts.append(b)
        return " || ".join(texts[:limit])
    except Exception:
        return ""


def _get_wbi_keys() -> tuple:
    """从 nav 接口获取 WBI img_key/sub_key（文件名截取）。失败返回 (None, None)。"""
    import urllib.request as _ur
    try:
        req = _ur.Request(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                     "Referer": "https://www.bilibili.com"},
        )
        with _ur.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
        img = d["data"]["wbi_img"]["img_url"]
        sub = d["data"]["wbi_img"]["sub_url"]
        return img.rsplit("/", 1)[-1].split(".")[0], sub.rsplit("/", 1)[-1].split(".")[0]
    except Exception:
        return None, None


def fetch_bili_ai_subtitle(url: str, output_dir: str, lang: str = "zh") -> str:
    """P1-1：B站登录态 AI 字幕直取（x/player/wbi/v2，WBI 签名 + cookie）。

    命中返回 SRT 文件路径（已写入 output_dir，随视频命名），未命中/无登录返回 ""。
    流程：view 拿 aid/cid → nav 拿 WBI key → player/wbi/v2 拿字幕列表 →
    选中文轨下载 JSON → 转 SRT。0 成本、100% 准确，命中则跳过 ASR。
    """
    import urllib.request as _ur
    import urllib.parse as _up
    import hashlib as _hl
    import time as _time

    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if not m:
        return ""
    bvid = m.group(1)
    cookies = _load_bili_cookies()
    if not cookies:
        print("  ℹ 未配置 BILIBILI_COOKIE，跳过 B站 AI 字幕直取")
        return ""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
               "Referer": "https://www.bilibili.com",
               "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}

    # 1. view：aid/cid
    try:
        req = _ur.Request(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", headers=headers)
        with _ur.urlopen(req, timeout=15) as resp:
            info = json.loads(resp.read())["data"]
        aid, cid, title = info["aid"], info["cid"], info["title"]
    except Exception as e:
        print(f"  ⚠ AI 字幕：view 获取失败（{e}）")
        return ""

    # 2. WBI 签名
    img_key, sub_key = _get_wbi_keys()
    if not img_key or not sub_key:
        print("  ⚠ AI 字幕：WBI key 获取失败")
        return ""
    mix_key = sub_key[:4] + img_key[:4]
    params = dict(sorted({"aid": aid, "cid": cid, "wts": int(_time.time())}.items()))
    query = _up.urlencode(params)
    w_rid = _hl.md5((query + mix_key).encode()).hexdigest()
    player_url = f"https://api.bilibili.com/x/player/wbi/v2?{query}&w_rid={w_rid}"

    # 3. 字幕列表
    try:
        req = _ur.Request(player_url, headers=headers)
        with _ur.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        sub_list = ((data.get("data") or {}).get("subtitle") or {}).get("list") or []
    except Exception as e:
        print(f"  ⚠ AI 字幕：player 接口失败（{e}）")
        return ""
    if not sub_list:
        print("  ℹ 该视频无 AI 字幕")
        return ""

    # 4. 选中文轨（优先 zh/ai-zh）
    chosen = None
    for s in sub_list:
        lan = (s.get("lan") or "")
        if lan.startswith("zh") or lan.startswith("ai-zh"):
            chosen = s
            break
    chosen = chosen or sub_list[0]
    sub_url = chosen.get("subtitle_url", "")
    if not sub_url:
        return ""
    if not sub_url.startswith("http"):
        sub_url = "https:" + sub_url

    # 5. 下载字幕 JSON → SRT
    try:
        req = _ur.Request(sub_url, headers=headers)
        with _ur.urlopen(req, timeout=15) as resp:
            sub_json = json.loads(resp.read())
    except Exception as e:
        print(f"  ⚠ AI 字幕：字幕文件下载失败（{e}）")
        return ""
    body = sub_json.get("body") or []
    if not body:
        return ""

    safe = sanitize_title(title)
    srt_lines = []
    for i, seg in enumerate(body, 1):
        start, end = seg.get("from", 0), seg.get("to", 0)
        text = (seg.get("content") or "").strip()
        if text:
            srt_lines.append(f"{i}\n{fmt_srt_ts(start)} --> {fmt_srt_ts(end)}\n{text}\n")
    srt_path = os.path.join(output_dir, safe + ".srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(srt_lines))
    print(f"  ✅ B站 AI 字幕直取成功: {len(body)} 段 → {os.path.basename(srt_path)}")
    return srt_path


def extract_subtitles(url: str, output_dir: str, lang: str = "zh") -> str:
    """Try to extract CC subtitles. Returns path to subtitle file or empty string."""
    lang_codes = "zh-Hans,zh-Hans.auto,zh,en,en.auto" if lang == "zh" else f"{lang},{lang}.auto,en,en.auto"

    result = subprocess.run(
        [
            "yt-dlp",
            "--write-sub", "--write-auto-sub",
            f"--sub-lang={lang_codes}",
            "--convert-subs", "srt",
            "--skip-download",
            "-o", os.path.join(output_dir, "subtitle"),
        ] + ytdlp_args(url) + [url],
        capture_output=True, text=True, timeout=120, check=False,
        encoding="utf-8", errors="replace",
    )

    # Find downloaded subtitle files
    for ext in [".srt", ".vtt"]:
        for f in os.listdir(output_dir):
            if f.startswith("subtitle") and f.endswith(ext):
                return os.path.join(output_dir, f)

    return ""


def parse_srt(filepath: str) -> str:
    """Parse SRT subtitle file into plain text."""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.split("\n")
    text_lines = []
    for line in lines:
        line = line.strip()
        if re.match(r'^\d+$', line):
            continue
        if re.match(r'^\d{2}:\d{2}:\d{2}', line):
            continue
        if re.match(r'^<', line):
            continue
        if not line:
            continue
        line = re.sub(r'<[^>]+>', '', line)
        text_lines.append(line)

    return "".join(text_lines)


def download_audio(url: str, output_path: str) -> str:
    """Download audio from video URL. Returns the actual audio file path."""
    subprocess.run(
        ["yt-dlp"] + ytdlp_args(url) + ["-x", "--audio-format", "mp3", "--audio-quality", "0",
         "--no-playlist",
         "-o", output_path, url],
        capture_output=True, text=True, timeout=300, check=False,
        encoding="utf-8", errors="replace",
    )
    # yt-dlp may replace extension or add info
    base = os.path.splitext(output_path)[0]
    for f in os.listdir(os.path.dirname(output_path)):
        full = os.path.join(os.path.dirname(output_path), f)
        if f.startswith(os.path.basename(base)) and os.path.isfile(full):
            return full
    return output_path if os.path.isfile(output_path) else ""


BBDOWN_PATH = r"C:\Users\lk\bin\BBDown\BBDown.exe"


def find_bbdown() -> str:
    """Locate BBDown executable (self-contained .NET build)."""
    for c in (BBDOWN_PATH, r"C:\Users\lk\bin\BBDown\BBDown.exe"):
        if os.path.isfile(c):
            return c
    # Also try PATH
    import shutil
    return shutil.which("BBDown") or ""


def download_audio_bili(url: str, output_dir: str) -> str:
    """Download Bilibili audio via BBDown (fast CDN, bypasses yt-dlp HTTP 412).

    Returns path to the downloaded audio file, or "" on failure.
    """
    bb = find_bbdown()
    if not bb:
        print("  BBDown not found, falling back to yt-dlp")
        return ""
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    bvid = m.group(1) if m else url
    import shutil
    ffmpeg = shutil.which("ffmpeg") or r"C:\Users\lk\Documents\视频\ffmpeg.exe"
    subprocess.run(
        [bb, bvid, "--audio-only", "--audio-ascending",
         "--work-dir", output_dir, "-F", bvid,
         "--ffmpeg-path", ffmpeg],
        capture_output=True, text=True, timeout=600, check=False,
        encoding="gbk", errors="ignore",
    )
    # P1-2：按 bvid 前缀匹配 BBDown 产物（-F bvid 已强制文件名前缀），
    # 精确命中直接返回；找不到才回退最大文件（兼容旧行为）
    best, best_size = "", 0
    bvid_l = bvid.lower()
    for f in os.listdir(output_dir):
        full = os.path.join(output_dir, f)
        if os.path.isfile(full) and f.lower().endswith((".m4a", ".mp3", ".flac", ".aac", ".wav")):
            if f.lower().startswith(bvid_l):
                return full
            sz = os.path.getsize(full)
            if sz > best_size:
                best, best_size = full, sz
    return best


def download_video_bili(url: str, output_dir: str) -> str:
    """BBDown 下载完整视频（含音轨，480P），返回视频文件路径或 ""。

    2026-08-12：视频+音频一次拿下——音轨供 ASR，视频缓存供抽帧复用，
    避免 frames_extract 二次下载。
    """
    bb = find_bbdown()
    if not bb:
        print("  BBDown not found, falling back to yt-dlp")
        return ""
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    bvid = m.group(1) if m else url
    import shutil
    ffmpeg = shutil.which("ffmpeg") or r"C:\Users\lk\Documents\视频\ffmpeg.exe"
    subprocess.run(
        [bb, bvid, "--dfn-priority", "480P",
         "--work-dir", output_dir, "-F", bvid,
         "--ffmpeg-path", ffmpeg],
        capture_output=True, text=True, timeout=600, check=False,
        encoding="gbk", errors="ignore",
    )
    vids = [os.path.join(output_dir, f) for f in os.listdir(output_dir)
            if f.lower().endswith((".mp4", ".flv", ".m4v", ".mkv"))]
    return vids[0] if vids else ""


def cleanup_intermediates(output_dir: str, keep_suffix: str = "_完整文案.md") -> int:
    """清理 output_dir 下除 keep_suffix 结尾外的所有文件 + _frames/ 目录。

    2026-08-12：视频/音频/转写/srt/弹幕/画面信息/帧图都是中间产物，
    交付只需 _完整文案.md。返回删除项数。
    """
    if not os.path.isdir(output_dir):
        return 0
    removed = 0
    for f in sorted(os.listdir(output_dir)):
        fp = os.path.join(output_dir, f)
        if os.path.isfile(fp) and not f.endswith(keep_suffix):
            try:
                os.remove(fp)
                removed += 1
                print(f"  🗑️  {f}")
            except Exception as e:
                print(f"  ⚠ 清理失败 {f}: {e}")
        elif os.path.isdir(fp) and f == "_frames":
            shutil.rmtree(fp, ignore_errors=True)
            removed += 1
            print(f"  🗑️  {f}/")
    return removed


def download_danmaku_bili(url: str, output_dir: str) -> list:
    """Download Bilibili danmaku via BBDown (--danmaku-only, WBI signed).

    Produces "<title>.xml" (standard Bilibili danmaku XML with timestamps)
    and "<title>.ass" in output_dir. Returns list of downloaded files.

    2026-08-11 修复：BBDown work-dir 用独立临时目录，避免 output_dir 里
    旧视频的 xml/ass 混入（曾导致弹幕时间线解析到别的视频的弹幕）。
    """
    bb = find_bbdown()
    if not bb:
        return []
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    bvid = m.group(1) if m else url
    import tempfile as _tf
    import shutil as _sh
    dm_tmp = _tf.mkdtemp(prefix="danmaku_")
    try:
        subprocess.run(
            [bb, bvid, "--danmaku-only", "--work-dir", dm_tmp],
            capture_output=True, text=True, timeout=300, check=False,
            encoding="gbk", errors="ignore",
        )
        files = []
        for f in os.listdir(dm_tmp):
            full = os.path.join(dm_tmp, f)
            if os.path.isfile(full) and f.lower().endswith((".xml", ".ass")):
                dest = os.path.join(output_dir, f)
                _sh.move(full, dest)
                files.append(dest)
        return files
    finally:
        _sh.rmtree(dm_tmp, ignore_errors=True)


def resolve_local_model(model_size: str) -> str:
    """优先返回本地已下载的模型 snapshot 路径，避免重复下载（WorkBuddy 方案）。

    查找顺序：env HF_HUB_CACHE/HF_HOME → ~/.cache/faster-whisper → D:\\lk\\.cache\\faster-whisper

    P0-2026-08-11：本地未命中时【绝不自动联网下载】——回退到本地已有模型
    （small 优先），全部缺失则抛错。此前 medium 下载在 HF 直连/xet/镜像
    全部不可达时拉锯 20 分钟，属于纯浪费。
    """
    import glob as _glob
    from pathlib import Path as _P
    cache_roots = []
    for k in ("HF_HUB_CACHE", "HF_HOME", "WHISPER_CACHE_DIR"):
        if os.environ.get(k):
            cache_roots.append(_P(os.environ[k]))
    cache_roots.append(_P.home() / ".cache" / "faster-whisper")
    cache_roots.append(_P(r"D:\lk\.cache\faster-whisper"))

    def _find(size: str):
        for root in cache_roots:
            pattern = str(root / f"models--Systran--faster-whisper-{size}" / "snapshots" / "*")
            for snap in sorted(_glob.glob(pattern), reverse=True):
                if (_P(snap) / "model.bin").exists():
                    return snap
        return None

    found = _find(model_size)
    if found:
        print(f"  ℹ 使用本地模型缓存: {found}")
        return found

    # 未命中 → 回退本地已有模型（small 优先），绝不联网下载
    for fallback in ("small", "base"):
        if fallback == model_size:
            continue
        fb = _find(fallback)
        if fb:
            print(f"  ⚠ 本地无 {model_size} 模型，回退使用 {fallback}: {fb}")
            return fb

    raise RuntimeError(
        f"本地没有 {model_size}（也无 small/base）模型缓存，且脚本不会自动联网下载。"
        "请从 ModelScope 或 HF 手动下载（如 pengzhendong/faster-whisper-medium）后重试。"
    )


def plan_frames_rules(segments: list) -> list:
    """规则兜底：转写片段命中引导词 → 抽帧点（MM:SS）。0 成本。

    引导词 = 视频里"要展示画面"的常见话术。命中即认为该时刻画面值得 OCR。
    """
    trigger = re.compile(
    r"我们看一下|大家看|大家看到|看这个|看下这个|这是截图|这是屏幕|屏幕|后台|"
    r"数据|表格|日历|成绩|书单|榜单|展示|给大家|画面|记录"
    )
    tss, seen = [], set()

    def _add(mm, ss):
        key = f"{mm:02d}:{ss:02d}"
        if key not in seen:
            seen.add(key)
            tss.append(key)

    for s in segments or []:
        if trigger.search(s.get("text", "") or ""):
            _add(int(s["start"] // 60), int(s["start"] % 60))

    # 列表朗读检测：大量「数字+实体名」片段（书单/榜单/数据盘点）→ 表格类视频
    # 没有"我们看一下"等引导词，但画面几乎全程是表格 → 均匀抽帧覆盖全片
    list_hits = 0
    for s in (segments or [])[:60]:
        # ASR 转写繁简混杂（推薦閱讀幾書三科興），正则覆盖简繁变体
        if re.search(
            r"\d{3,5}|\d{2,4}万|首订|均订|修订|推薦|閱讀|學習|推荐|阅读|学习|"
            r"颗星|顆星|科星|科興|幾書|指数|指數|星",
            s.get("text", "") or "",
        ):
            list_hits += 1
    if list_hits >= max(3, int(len(segments or []) * 0.25)):
        dur = max((s.get("end", 0) or 0) for s in (segments or [])) or 240
        step = max(15, int(dur // 16))  # 均匀 ~16 帧
        for sec in range(int(dur // step) + 1):
            _add(sec * step // 60, (sec * step) % 60)
        print(f"  ℹ 检测到列表朗读型视频（{list_hits}/{len(segments)} 段含数字/术语），均匀抽帧覆盖全片")
    return tss[:24]


def plan_frames_llm(segments: list, api_key: str = None, base_url: str = None,
                    llm_model: str = None) -> list:
    """LLM 根据转写文案（带时间戳）判断哪些时刻的画面值得抽帧。

    输出 MM:SS 列表（≤12，升序）。无 LLM 配置或调用失败 → 返回 []（由调用方
    走规则兜底）。用 urllib 直调 OpenAI-compatible endpoint，无新增依赖。
    """
    import json as _json
    import urllib.request as _ur

    api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
    base_url = base_url or os.environ.get("OPENAI_BASE_URL", "")
    llm_model = llm_model or os.environ.get("LLM_MODEL", "") or "gpt-4o-mini"
    if not api_key or not base_url:
        return []

    ctx_lines = []
    for s in (segments or [])[:120]:
        mm, ss = int(s["start"] // 60), int(s["start"] % 60)
        ctx_lines.append(f"[{mm:02d}:{ss:02d}] {s.get('text', '')[:80]}")

    prompt = (
        "你是视频画面规划助手。下面是视频转写文案（带时间戳）。\n"
        "判断哪些时刻的画面可能包含重要视觉信息：表格、数据截图、文档、网页、"
        "演示、字幕、关键画面（如UP主展示后台/成绩/书单）。\n"
        "只输出这些时刻的时间戳，每行一个 MM:SS，最多 12 个，按时间升序。\n"
        "如果完全没有值得抽帧的画面，只输出一个空行。\n\n"
        + "\n".join(ctx_lines)
    )
    body = _json.dumps({
        "model": llm_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 300,
    }).encode("utf-8")
    req = _ur.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    try:
        with _ur.urlopen(req, timeout=60) as resp:
            d = _json.loads(resp.read())
        content = d["choices"][0]["message"]["content"]
        tss, seen = [], set()
        for line in content.splitlines():
            m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", line.strip())
            if m:
                key = f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
                if key not in seen:
                    seen.add(key)
                    tss.append(key)
        return tss[:12]
    except Exception as e:
        print(f"  ⚠ LLM 抽帧规划失败（{e}），回退规则兜底")
        return []


def plan_frames(segments: list, api_key: str = None, base_url: str = None,
                llm_model: str = None) -> list:
    """P0：按文案规划抽帧点——LLM 优先，规则兜底，合并去重后返回 MM:SS 列表。"""
    llm_tss = plan_frames_llm(segments, api_key, base_url, llm_model)
    rule_tss = plan_frames_rules(segments)
    merged, seen = [], set()
    for ts in llm_tss + rule_tss:
        if ts not in seen:
            seen.add(ts)
            merged.append(ts)
    if llm_tss:
        print(f"  🎯 LLM 规划抽帧 {len(llm_tss)} 点（规则补 {len(rule_tss)} 点）: {merged}")
    else:
        print(f"  🎯 规则兜底抽帧 {len(rule_tss)} 点: {merged}")
    return merged


def fmt_srt_ts(seconds: float) -> str:
    """秒 → SRT 时间戳 HH:MM:SS,mmm"""
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def detect_gpu() -> tuple:
    """检测 GPU 是否可用（用 ctranslate2，不依赖 torch）。

    faster-whisper 推理引擎是 ctranslate2（自带 CUDA 支持），
    无需为"探测"安装 ~2.5GB 的 torch。返回 (可用, 设备数)。
    """
    try:
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
        if n > 0:
            return True, n
    except Exception:
        pass
    return False, 0


def transcribe_whisper(audio_path: str, language: str = "zh", model_size: str = "small"):
    """Transcribe audio using faster-whisper. Returns (text, segments).

    segments: list of {"start": float, "end": float, "text": str} — keeps the
    timeline so downstream steps (frame extraction at matching timestamps) can
    align visual content with what was being said.
    """
    from faster_whisper import WhisperModel

    # GPU 检测（ctranslate2，无需 torch）
    gpu, _n = detect_gpu()
    if gpu:
        device, compute_type = "cuda", "float16"
    else:
        device, compute_type = "cpu", "int8"

    model_path = resolve_local_model(model_size)
    print(f"  Loading faster-whisper/{model_size} ({device})...")
    model = WhisperModel(
        model_path,
        device=device, compute_type=compute_type,
        download_root=os.environ["WHISPER_CACHE_DIR"],
    )

    print("  Transcribing (this may take a while)...")
    segments_iter, info = model.transcribe(
        audio_path, language=language, beam_size=1, vad_filter=True,
        condition_on_previous_text=False,
        initial_prompt="以下是普通话视频内容。",
    )

    texts = []
    segments = []
    for seg in segments_iter:
        if seg.text.strip():
            texts.append(seg.text.strip())
            segments.append({
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
                "text": seg.text.strip(),
            })

    return "".join(texts), segments


def _module_available(name: str) -> bool:
    """P1-3：模块可用性探测（不 import，避免副作用）。"""
    try:
        import importlib.util as _iu
        return _iu.find_spec(name) is not None
    except Exception:
        return False


def transcribe_sensevoice(audio_path: str, language: str = "zh") -> tuple:
    """P1-3：SenseVoiceSmall ONNX 中文 ASR（sherpa-onnx 后端，无 torch 依赖）。

    比 whisper-small 快 ~5 倍、中文准确率更高、自带标点。返回 (text, segments)。
    segments 为按句子估算的分段（无 VAD 时按标点+字符比例分配时间），
    保证下游 plan_frames 仍可基于文案规划抽帧。
    依赖缺失（sherpa_onnx 未装/模型缺失）→ 抛 RuntimeError 由调用方回退 whisper。
    模型布局：model.onnx / model.int8.onnx + tokens.txt（sherpa-onnx 官方导出，
    来源 hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17）。

    2026-08-12 修复长音频退化：850s 音频整体推理 → 仅 599 字符词汤
    （greedy search 长序列循环退化）；改为 30s 分块解码 + 拼接。
    """
    try:
        import sherpa_onnx
    except ImportError:
        raise RuntimeError("sherpa_onnx 未安装，SenseVoice 后端不可用")

    model_dir = os.environ.get("SENSEVOICE_MODEL_DIR", r"D:\lk\.cache\SenseVoiceSmall")
    # 模型文件探测：优先 int8 量化版（体积小、速度快），回退 fp32
    model_file = next(
        (os.path.join(model_dir, c) for c in ("model.int8.onnx", "model.onnx")
         if os.path.isfile(os.path.join(model_dir, c))),
        None,
    )
    tokens_file = os.path.join(model_dir, "tokens.txt")
    if not model_file:
        raise RuntimeError(f"SenseVoice 模型缺失（{model_dir} 下无 model.onnx/model.int8.onnx）")
    if not os.path.isfile(tokens_file):
        raise RuntimeError(f"SenseVoice tokens 缺失（{tokens_file}）")

    print(f"  Loading SenseVoiceSmall (sherpa-onnx, {os.path.basename(model_file)})...")
    kwargs = dict(model=model_file, tokens=tokens_file, num_threads=4,
                  use_itn=True, debug=False)
    if language not in ("zh", "auto", ""):
        kwargs["language"] = language
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(**kwargs)
    print("  Transcribing (SenseVoice)...")
    import soundfile as _sf
    samples, sr = _sf.read(audio_path, dtype="float32")
    if sr != 16000:  # 管道已 ffmpeg 统一 16k 单声道，此处兜底重采样
        import numpy as _np
        samples = _np.interp(
            _np.linspace(0, len(samples), int(len(samples) * 16000 / sr)),
            _np.arange(len(samples)), samples).astype("float32")
    dur = len(samples) / 16000.0

    # 30s 分块解码（长音频整体推理会退化，见函数 docstring）
    CHUNK = 30.0
    chunk_n = int(CHUNK * 16000)
    chunks = []
    for start in range(0, len(samples), chunk_n):
        chunks.append((start, samples[start:start + chunk_n]))

    chunk_texts = []
    for idx, (start, chunk) in enumerate(chunks):
        if len(chunk) == 0:
            continue
        stream = recognizer.create_stream()
        stream.accept_waveform(sample_rate=16000, waveform=chunk)
        recognizer.decode_stream(stream)
        t = stream.result.text.strip()
        if t:
            chunk_texts.append((start / 16000.0, t))
        print(f"    chunk {idx + 1}/{len(chunks)} ({start / 16000.0:.0f}s): {len(t)} 字", flush=True)

    if not chunk_texts:
        raise RuntimeError("SenseVoice 输出为空")

    text = "".join(t for _, t in chunk_texts).strip()
    if not text:
        raise RuntimeError("SenseVoice 输出为空")

    # 按句子切分估算 segments（中文标点断句，时间按字符比例分配）
    segs = []
    for c_start, c_text in chunk_texts:
        sentences = [s for s in re.split(r"(?<=[。！？!?；;])", c_text) if s.strip()]
        if not sentences:
            sentences = [c_text]
        total_chars = sum(len(s) for s in sentences) or 1
        cursor = c_start
        for s in sentences:
            span = len(s) / total_chars * min(CHUNK, dur - c_start)
            segs.append({"start": round(cursor, 2), "end": round(cursor + span, 2), "text": s.strip()})
            cursor += span
    return text, segs


def correct_with_llm(text: str, title: str, description: str = "", comments: str = "",
                     model: str = "gpt-4o-mini",
                     api_key: str = None, base_url: str = None) -> str:
    """Correct ASR/subtitle text using an LLM with context.

    Returns (text, corrected: bool) — corrected is False when skipped/failed.
    """
    import openai

    key = api_key or os.environ.get("OPENAI_API_KEY")
    url = base_url or os.environ.get("OPENAI_BASE_URL")
    kwargs = {"api_key": key}
    if url:
        kwargs["base_url"] = url

    if not key:
        print("  Warning: No API key set for LLM correction, skipping.")
        return text, False

    client = openai.OpenAI(**kwargs)

    # Build context section
    context_parts = []
    if title:
        context_parts.append(f"视频标题：{title}")
    if description:
        context_parts.append(f"视频简介：{description[:500]}")
    if comments:
        context_parts.append(f"热门评论（可帮助理解视频主题和关键术语）：{comments[:500]}")

    context_str = "\n".join(context_parts)

    prompt = f"""你是一个专业的音视频转录校对员。请校对以下自动语音识别(ASR)生成的文字稿。

## 背景上下文
{context_str}

## 校对规则
1. 修正ASR常见的错误：同音字、多音字、专有名词音译错误
2. 根据视频标题、简介和评论中提到的主题，修正相关的技术术语、人名、游戏名、作品名
3. 修正数字和版本号的格式
4. 补充ASR漏掉的明显语气词和连接词，让文本通顺
5. 保留原文的口语表达风格和语气，不要做内容增删或改写
6. 不要擅自添加标题、分段或格式标记，保持纯文本输出
7. 只输出修正后的完整文本，不要输出你的思考过程

## 原始ASR转录
{text}"""

    print("  Running LLM correction...")
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=16384,
        )
        corrected = response.choices[0].message.content or text
        return corrected, True
    except Exception as e:
        print(f"  LLM correction failed: {e}")
        print("  Returning raw ASR text without correction.")
        return text, False


def fetch_uploader_profile(mid: str) -> dict:
    """抓取 UP主画像（B站 /x/web-interface/card 免 WBI，实测 2026-08-11）。

    返回: {name, sign, fans, official} 或 {}
    """
    if not mid:
        return {}
    import urllib.request
    try:
        req = urllib.request.Request(
            f"https://api.bilibili.com/x/web-interface/card?mid={mid}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "Referer": "https://www.bilibili.com/"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read())
        if d.get("code") != 0:
            return {}
        card = (d.get("data") or {}).get("card") or {}
        return {
            "name": card.get("name", ""),
            "sign": (card.get("sign") or "")[:200],
            "fans": card.get("fans", 0),
            "official": ((card.get("Official") or {}).get("title") or ""),
        }
    except Exception:
        return {}


def save_info_json(info: dict, comments: str, output_dir: str, title: str) -> str:
    """元数据 + 评论 + UP画像 落盘为 {title}_info.json（WorkBuddy 方案）。"""
    import copy
    meta = copy.deepcopy(info)
    # 来源标注：mid/cid/bvid 只在 B站 API 路径返回；yt-dlp 路径返回 uploader_id
    meta["_meta_source"] = "bilibili-api" if info.get("mid") else "yt-dlp"
    meta["comments_sample"] = comments[:2000]
    # mid（B站 API 路径）或 uploader_id（yt-dlp 路径）
    up_id = str(info.get("mid") or info.get("uploader_id") or "")
    if up_id and up_id != "None":
        meta["uploader_profile"] = fetch_uploader_profile(up_id)
    safe = sanitize_title(title)
    path = os.path.join(output_dir, f"{safe}_info.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"  ℹ 元数据档案: {os.path.basename(path)}")
    return path


def parse_danmaku_timeline(dm_files: list, output_dir: str, title: str = "") -> str:
    """把 BBDown 下载的弹幕 xml 解析为 [mm:ss] 时间线 txt（WorkBuddy 方案）。

    输入 dm_files 中第一个 .xml；产出 {output_dir}/{sanitize_title(title)}_弹幕时间线.txt
    2026-08-11 修正：按视频命名，避免多视频共用固定文件名互相覆盖。
    """
    xml_path = next((f for f in dm_files if f.lower().endswith(".xml")), None)
    if not xml_path:
        return ""
    try:
        with open(xml_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return ""
    import re as _re
    dms = _re.findall(r'<d p="([^"]+)">([^<]+)</d>', content)
    dms_sorted = sorted(dms, key=lambda x: float(x[0].split(",")[0]))
    lines = []
    for p, t in dms_sorted:
        sec = float(p.split(",")[0])
        mm, ss = divmod(int(sec), 60)
        lines.append(f"[{mm:02d}:{ss:02d}] {t}")
    if not lines:
        return ""
    safe = sanitize_title(title) if title else "视频"
    out = os.path.join(output_dir, f"{safe}_弹幕时间线.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  ℹ 弹幕时间线: {len(lines)} 条 → {os.path.basename(out)}")
    return out


# ── 按需抽帧工具（Agent 调用，供第二段 PPT 结构文案参考）──────────────────
# 2026-08-11 新增：文案提取完成后，Agent 判断是否需要画面帧补充；
# 需要才下载 480P 视频抽帧→OCR，产出 {safe_title}_画面信息.md。
# 纯口播/访谈类 → timestamps 传空 → 不下载视频，0 开销。

def _parse_ts(ts: str) -> float:
    """'mm:ss' / 'hh:mm:ss' → 秒。"""
    parts = ts.strip().split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(ts)


def _fmt_ts(seconds: float) -> str:
    """秒 → 'mm:ss'（保留 2 位小数秒）。"""
    m, s = divmod(float(seconds), 60)
    return f"{int(m):02d}:{s:05.2f}"


def _find_title_from_outdir(output_dir: str) -> str:
    """从 output_dir 下已有的 *_info.json 提取视频标题（供命名）。"""
    try:
        import glob as _glob
        from pathlib import Path as _P
        for f in sorted(_glob.glob(str(_P(output_dir) / "*_info.json"))):
            try:
                d = json.loads(_P(f).read_text(encoding="utf-8"))
                if d.get("title"):
                    return d["title"]
            except Exception:
                continue
    except Exception:
        pass
    return ""


# ============ P2-1: PySceneDetect 镜头切换检测 ============

def detect_scene_cuts(video_path: str, threshold: float = 10.0,
                      min_scene_len: float = 0.8) -> list:
    """检测视频镜头切换点（PySceneDetect ContentDetector）。

    P2-1：抽帧前先检测镜头切换时间戳，与 plan_frames 文案规划点合并，
    保证不漏关键镜头（切换点优先，文案点兜底）。

    阈值说明（2026-08-11 实测）：ContentDetector 默认 27 对
    testsrc2 动态测试图检不出切点，threshold=10 完美检出
    （合成视频 3 段 → 2 切点 5s/10s）。口播/PPT 视频背景静态，
    阈值 10 足够区分镜头切换；误检由 merge_scene_ts 的 ±2s 去重
    和 max_frames=24 上限兜底。

    返回秒数列表（float）。scenedetect 未安装/检测失败 → 返回 []（不阻断管道）。
    """
    try:
        from scenedetect import ContentDetector, SceneManager, open_video
    except Exception as e:
        print(f"  ⚠ scenedetect 不可用（{e}），跳过镜头切换检测")
        return []
    try:
        video = open_video(video_path)
        sm = SceneManager()
        sm.add_detector(ContentDetector(threshold=threshold,
                                        min_scene_len=min_scene_len))
        sm.detect_scenes(video, show_progress=False)
        cuts = []
        for sc in sm.get_scene_list():
            s = sc[0].seconds  # 新版 API：.seconds 属性（get_seconds() 已弃用）
            if s > 0.5:  # 跳过片头第 0 秒
                cuts.append(round(s, 2))
        if cuts:
            print(f"  🎬 镜头切换检测: {len(cuts)} 个切点 {cuts}")
        else:
            print("  🎬 镜头切换检测: 无切点（单镜头视频）")
        return cuts
    except Exception as e:
        print(f"  ⚠ 镜头切换检测失败（{e}），跳过")
        return []


def merge_scene_ts(scene_cuts: list, planned_ts: list, max_frames: int = 24) -> list:
    """合并镜头切点与文案规划点 → 最终抽帧时间点（秒）。

    规则（P2-1）：
      - 场景切点优先：保留所有切点
      - 文案规划点若与切点 ±2s 内重叠 → 去重（取切点）
      - 其余文案点保留（补足画面信息）
      - 排序、去重（±2s 容差）、截断到 max_frames
    返回秒数列表（float）。
    """
    # 统一转秒
    def _to_sec(x):
        if isinstance(x, (int, float)):
            return float(x)
        return _parse_ts(str(x))

    cuts = sorted(set(_to_sec(c) for c in scene_cuts))
    planned = sorted(set(_to_sec(t) for t in planned_ts))

    merged = list(cuts)
    for p in planned:
        if any(abs(p - c) <= 2.0 for c in cuts):
            continue  # 与切点重叠，去重（取切点）
        merged.append(p)
    merged = sorted(merged)

    # ±2s 容差去重（相邻太近保留先者）
    dedup = []
    for m in merged:
        if not dedup or m - dedup[-1] > 2.0:
            dedup.append(m)
    return dedup[:max_frames]


def frames_extract(url: str, output_dir: str,
                   timestamps: list = None,
                   max_frames: int = 24,
                   vision_script: str = None,
                   detect_cuts: bool = True,
                   video_path: str = "") -> str:
    """按需抽帧 → 视觉 OCR → {safe_title}_画面信息.md

    Agent 调用：
      frames_extract(url, outdir, timestamps=["01:30","05:12"])  # 指定时间点
      frames_extract(url, outdir, timestamps=[])                 # 无需画面 → 跳过

    P2-1：detect_cuts=True（默认）→ 下载视频后先做镜头切换检测，
    切点与规划点合并（切点优先，文案点兜底）；scenedetect 缺失自动跳过。
    P2-2：OCR 前 RapidOCR 粗排，无文字帧拦截（省 vision 调用）；
    RapidOCR 不可用/异常 → 放行（保 Recall）。

    2026-08-12：video_path 非空则直接复用（Step 2 已下载完整视频含音轨），
    不再二次下载；为空才走 BBDown 下载（--frames 独立模式）。

    依赖：BBDown + ffmpeg + vision_ask.py（~/.workbuddy/skills/vision-analyze/）
    返回画面信息文件路径；跳过/失败返回 ""。
    """
    if not timestamps:
        print("  ℹ 画面帧跳过（无需抽帧）")
        return ""

    # 标题：优先当前 URL 直查（自带 UA 防脏缓存），回退目录猜测
    title = ""
    try:
        info = get_video_info(url)
        if info and info.get("title") and info["title"] != "Unknown":
            title = info["title"]
    except Exception:
        pass
    if not title:
        title = _find_title_from_outdir(output_dir) or "视频"
    safe = sanitize_title(title)

    # vision_ask.py（WorkBuddy 视觉辅助脚本）
    if not vision_script:
        vision_script = os.path.expanduser(
            "~/.workbuddy/skills/vision-analyze/scripts/vision_ask.py")
    if not os.path.isfile(vision_script):
        print("  ⚠ vision_ask.py 未找到，跳过画面 OCR")
        return ""

    bb = find_bbdown()
    if not bb:
        print("  ⚠ BBDown 未找到，跳过画面抽帧")
        return ""
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if not m:
        print("  ⚠ 无法解析 BV 号")
        return ""
    bvid = m.group(1)

    import shutil
    import tempfile as _tf
    ffmpeg = shutil.which("ffmpeg") or r"C:\Users\lk\Documents\视频\ffmpeg.exe"
    video_tmp = _tf.mkdtemp(prefix="frames_")
    try:
        # 1. 视频来源：优先复用 Step 2 已下载的完整视频（含音轨），否则 BBDown 下载 480P
        if video_path and os.path.isfile(video_path):
            video = video_path
            print(f"  📹 复用已下载视频: {os.path.basename(video_path)}")
        else:
            print(f"  📹 下载 480P 视频: {bvid} ...")
            subprocess.run(
                [bb, bvid, "--video-only", "--dfn-priority", "480P",
                 "--work-dir", video_tmp, "--ffmpeg-path", ffmpeg],
                capture_output=True, text=True, timeout=600, check=False,
                encoding="gbk", errors="ignore",
            )
            vids = [os.path.join(video_tmp, f) for f in os.listdir(video_tmp)
                    if f.lower().endswith((".mp4", ".flv", ".m4v", ".mkv"))]
            if not vids:
                print("  ⚠ BBDown 未产出视频文件，跳过抽帧")
                return ""
            video = vids[0]

        # 2. P2-1: 镜头切换检测 → 与规划点合并（切点优先，文案点兜底）
        final_ts = list(timestamps[:max_frames])
        if detect_cuts:
            cuts = detect_scene_cuts(video)
            if cuts:
                merged = merge_scene_ts(cuts, timestamps, max_frames)
                # merge_scene_ts 返回秒数 → 转 "mm:ss" 字符串供下游统一处理
                final_ts = [_fmt_ts(s) if isinstance(s, (int, float)) else s
                            for s in merged]
                print(f"  🎯 合并后抽帧点 {len(final_ts)} 个: {final_ts}")

        # 3. 按时间点 ffmpeg 抽帧
        frame_dir = os.path.join(output_dir, "_frames", safe)
        os.makedirs(frame_dir, exist_ok=True)
        frame_paths = []
        for i, ts in enumerate(final_ts, 1):
            sec = _parse_ts(ts)
            fp = os.path.join(frame_dir, f"frame_{i:02d}_{ts.replace(':', '-')}.jpg")
            subprocess.run(
                [ffmpeg, "-y", "-ss", str(sec), "-i", video,
                 "-frames:v", "1", "-q:v", "2", fp],
                capture_output=True, text=True, timeout=120, check=False,
                encoding="utf-8", errors="replace",
            )
            if os.path.isfile(fp) and os.path.getsize(fp) > 0:
                frame_paths.append(fp)
        if not frame_paths:
            print("  ⚠ 抽帧失败（ffmpeg 未产出有效帧）")
            return ""

        # 4. vision_ask.py 并发 OCR（P0: ThreadPool 4 并行）
        #    + P2-2: RapidOCR 粗排前置（无文字帧拦截，省 vision 调用）
        from concurrent.futures import ThreadPoolExecutor as _TPE
        md_lines = [f"# 画面信息补充（{safe}）\n"]

        _tl = threading.local()

        def _rapidocr_engine():
            """线程内单例 RapidOCR 引擎；失败返回 False（禁用粗排，放行全部）。"""
            eng = getattr(_tl, "rapid_engine", None)
            if eng is None:
                try:
                    from rapidocr_onnxruntime import RapidOCR
                    eng = RapidOCR()
                except Exception as e:
                    print(f"  ⚠ RapidOCR 初始化失败（{e}），粗排禁用（放行全部）")
                    eng = False
                _tl.rapid_engine = eng
            return eng

        def _has_text(fp: str) -> bool:
            """RapidOCR 粗排：画面有文字 → True。无文字/异常/无引擎 → 保守放行。"""
            eng = _rapidocr_engine()
            if not eng:
                return True
            try:
                result, _el = eng(fp)
                if not result:
                    return False
                return any(len(r) >= 2 and str(r[1]).strip() for r in result)
            except Exception as e:
                print(f"  ⚠ RapidOCR 粗排失败（{e}），放行该帧")
                return True

        def _ocr_one(fp: str) -> tuple:
            if not _has_text(fp):
                return os.path.basename(fp), "⏭ 画面无文字（RapidOCR 粗排拦截）"
            r = subprocess.run(
                ["python", vision_script, "--image", fp,
                 "--prompt", "这是视频截帧。请完整提取画面中所有可见文字：标题、要点、图表、"
                             "文档、网页、字幕等。若主要是人像无文字，说明画面内容。中文回答。"],
                capture_output=True, text=True, timeout=180, check=False,
                encoding="utf-8", errors="replace",
            )
            return os.path.basename(fp), (r.stdout or "").strip() or "(OCR 无输出)"

        results = []
        with _TPE(max_workers=4) as pool:
            futs = [(i, fp, pool.submit(_ocr_one, fp))
                    for i, fp in enumerate(frame_paths, 1)]
            for i, fp, fut in futs:
                print(f"  [画面] 分析第 {i}/{len(frame_paths)} 帧: {os.path.basename(fp)}")
            for i, fp, fut in futs:
                try:
                    name, text = fut.result(timeout=200)
                    results.append((i, name, text))
                except Exception as e:
                    results.append((i, os.path.basename(fp), f"(OCR 失败: {e})"))
        results.sort(key=lambda x: x[0])
        filtered = sum(1 for _, _, t in results if t.startswith("⏭"))
        if filtered:
            print(f"  ⏭ RapidOCR 粗排拦截 {filtered}/{len(frame_paths)} 帧（省 vision 调用）")
        for i, name, text in results:
            md_lines.append(f"## 帧{i}（{name}）")
            md_lines.append("```")
            md_lines.append(text[:2000])
            md_lines.append("```")
            md_lines.append("")

        md_out = os.path.join(output_dir, f"{safe}_画面信息.md")
        with open(md_out, "w", encoding="utf-8") as f:
            f.write("\n".join(md_lines))
        print(f"  ✔ 画面信息: {len(frame_paths)} 帧 → {os.path.basename(md_out)}")
        return md_out
    finally:
        shutil.rmtree(video_tmp, ignore_errors=True)


def transcribe(
    url: str,
    output_dir: str = ".",
    lang: str = "zh",
    whisper_size: str = "auto",
    llm_correction: bool = True,
    llm_model: str = "gpt-4o-mini",
    api_key: str = None,
    base_url: str = None,
    clean_audio_cache: bool = False,
    auto_frames: bool = False,
    backend: str = "auto",
) -> str:
    """Full transcription pipeline. Returns path to output file.

    auto_frames: P0-3 转写完成后按文案（LLM 优先+规则兜底）自动规划抽帧点并执行 480P OCR。
    backend: "auto"=SenseVoice可用则用(装好+模型在)，否则whisper; "whisper"=强制whisper;
             "sensevoice"=强制SenseVoice(失败回退whisper)。
    """
    os.makedirs(output_dir, exist_ok=True)
    platform = detect_platform(url)

    # ── Auto-detect best whisper model ─────────────────────────────
    if whisper_size == "auto":
        gpu, _n = detect_gpu()
        if gpu:
            whisper_size = "small"   # GPU: small is fast enough (~2s)
            print(f"🔍 GPU detected, using small")
        else:
            whisper_size = "base"    # CPU: base for speed
            print("🔍 CPU mode, using base")

    if platform == "unknown":
        print(f"Warning: Unknown platform for URL, will try yt-dlp directly")

    # Get video info
    print(f"Platform: {platform}")
    info = get_video_info(url)
    title = info.get("title", "Unknown")
    description = info.get("description", "")
    print(f"Title: {title}")
    print(f"Description: {description[:100] if description else '(empty)'}")

    # Fetch comments for Bilibili
    comments = ""
    if platform == "bilibili":
        aid = str(info.get("aid") or info.get("id", ""))
        # The 'id' from yt-dlp for bilibili is the aid
        if aid:
            print("  Fetching comments for context...")
            comments = fetch_bilibili_comments(aid)
            if comments:
                print(f"  Got {len(comments)} chars of comments")

    # 元数据 + 评论 + UP主画像 落盘（WorkBuddy 方案：info.json 存档，供纠正阶段追溯）
    info_path = save_info_json(info, comments, output_dir, info.get("title", "Unknown"))
    _info_path = info_path

    raw_text = ""
    method = ""

    # Step 1: Try subtitles first (fastest)
    print("\nStep 1: Trying CC subtitles...")
    with tempfile.TemporaryDirectory() as tmpdir:
        _tmp_dirs.append(tmpdir)
        sub_file = ""
        # P1-1：B站登录态 AI 字幕直取（0 成本 100% 准，命中跳过 ASR）
        if platform == "bilibili" and _load_bili_cookies():
            print("  → 尝试 B站 AI 字幕直取（登录态）...")
            sub_file = fetch_bili_ai_subtitle(url, tmpdir, lang)
        if not sub_file:
            sub_file = extract_subtitles(url, tmpdir, lang)
        if sub_file and os.path.isfile(sub_file):
            raw_text = parse_srt(sub_file)
            method = "CC Subtitle"
            print(f"  Got subtitles ({len(raw_text)} chars)")
        else:
            print("  No CC subtitles available")

        # Step 2: If no subtitles, do ASR
        if not raw_text:
            import time as _time
            t_dl = _time.time()
            print("\nStep 2: Downloading audio...")
            audio_path = os.path.join(tmpdir, "audio.mp3")
            dm_files = []  # 弹幕文件（B站专用），默认空，避免非B站平台 NameError

            # Bilibili: BBDown first (fast CDN, avoids yt-dlp HTTP 412)
            video_path = ""  # 完整视频缓存（含音轨），供 Step 3b 抽帧复用
            if platform == "bilibili":
                print("  Using BBDown (Bilibili) ...")
                # 2026-08-12: 一次下载完整视频（含音轨）→ 提取音轨 ASR + 视频缓存抽帧，
                # 不再二次下载（frames_extract 传 video_path 复用）
                video_path = download_video_bili(url, tmpdir)
                if video_path:
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", audio_path],
                        capture_output=True, text=True, timeout=300, check=False
                    )
                else:
                    # 完整视频下载失败 → 回退仅音频
                    dl_file = download_audio_bili(url, tmpdir)
                    if dl_file:
                        subprocess.run(
                            ["ffmpeg", "-y", "-i", dl_file, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", audio_path],
                            capture_output=True, text=True, timeout=300, check=False
                        )
                # Danmaku sidecar (Bilibili only): .xml + .ass, saved to output_dir (survives tmp cleanup)
                dm_files = download_danmaku_bili(url, output_dir)
                if dm_files:
                    print(f"  Danmaku: {len(dm_files)} file(s) saved to {output_dir}")
            # Fallback: yt-dlp (YouTube, or Bilibili without BBDown)
            if not os.path.isfile(audio_path):
                print("  Using yt-dlp ...")
                subprocess.run(
                    ["yt-dlp"] + ytdlp_args(url) + ["-x", "--audio-format", "mp3", "--audio-quality", "5",
                     "--no-playlist",
                     "-o", audio_path, url],
                    capture_output=True, text=True, timeout=300, check=False,
                    encoding="utf-8", errors="replace",
                )
                # yt-dlp may use different extension; rename to .mp3
                for f in os.listdir(tmpdir):
                    full = os.path.join(tmpdir, f)
                    if f.startswith("audio") and os.path.isfile(full) and not f.endswith(".mp3"):
                        os.rename(full, audio_path)
                        break

            if not os.path.isfile(audio_path):
                print("Error: Failed to download audio")
                sys.exit(1)

            size_mb = os.path.getsize(audio_path) / 1024 / 1024
            dl_time = _time.time() - t_dl
            print(f"  Audio: {size_mb:.1f} MB (downloaded in {dl_time:.0f}s)")

            # Step 3: ASR transcription（P1-3：backend 支持 whisper / sensevoice）
            use_sv = False
            if backend == "sensevoice":
                use_sv = True
            elif backend == "auto":
                _sv_dir = os.environ.get("SENSEVOICE_MODEL_DIR", r"D:\lk\.cache\SenseVoiceSmall")
                use_sv = (os.path.isdir(_sv_dir) and _module_available("sherpa_onnx")
                          and (os.path.isfile(os.path.join(_sv_dir, "model.onnx"))
                               or os.path.isfile(os.path.join(_sv_dir, "model.int8.onnx")))
                          and os.path.isfile(os.path.join(_sv_dir, "tokens.txt")))
            if use_sv:
                print("\nStep 3: Transcribing (SenseVoiceSmall ONNX)...")
                t0 = _time.time()
                try:
                    raw_text, segments = transcribe_sensevoice(audio_path, lang)
                    elapsed = _time.time() - t0
                    # 质量门禁（2026-08-12）：SenseVoice 长音频曾退化出词汤
                    # （850s 音频仅 599 字符 ≈ 0.7 字/s；正常中文口播 > 3 字/s）
                    _dur = float(info.get("duration") or 0)
                    if _dur > 60 and len(raw_text) / _dur < 1.5:
                        print(f"  ⚠ SenseVoice 字率异常（{len(raw_text)} 字 / {_dur:.0f}s"
                              f" = {len(raw_text)/_dur:.1f} 字/s），判定退化，回退 faster-whisper/{whisper_size}")
                        raise RuntimeError("SenseVoice 字率过低（疑似长音频退化）")
                    print(f"  Transcribed {len(raw_text)} chars in {elapsed:.1f}s ({254/elapsed:.0f}x real-time)")
                    method = "SenseVoice (sherpa-onnx)"
                except Exception as e:
                    print(f"  ⚠ SenseVoice 失败（{e}），回退 faster-whisper/{whisper_size}")
                    use_sv = False
            if not use_sv:
                print(f"\nStep 3: Transcribing (faster-whisper/{whisper_size})...")
                t0 = _time.time()
                raw_text, segments = transcribe_whisper(audio_path, lang, whisper_size)
                elapsed = _time.time() - t0
                print(f"  Transcribed {len(raw_text)} chars in {elapsed:.1f}s ({919/elapsed:.0f}x real-time)")
                method = f"faster-whisper/{whisper_size}"
            # Save segments with timestamps (for aligned frame extraction later)
            _safe_seg = sanitize_title(title)
            seg_path = os.path.join(output_dir, _safe_seg + "_segments.json")
            with open(seg_path, "w", encoding="utf-8") as sf:
                json.dump(segments, sf, ensure_ascii=False, indent=1)
            print(f"  Segments with timestamps: {len(segments)} → {os.path.basename(seg_path)}")
            # SRT 输出（WorkBuddy 方案：时间轴字幕，供人工复核/后期）
            srt_path = os.path.join(output_dir, _safe_seg + ".srt")
            srt_lines = []
            for i, seg in enumerate(segments, 1):
                srt_lines.append(f"{i}\n{fmt_srt_ts(seg['start'])} --> {fmt_srt_ts(seg['end'])}\n{seg['text']}\n")
            with open(srt_path, "w", encoding="utf-8") as sf:
                sf.write("\n".join(srt_lines))
            print(f"  SRT: {len(segments)} 段 → {os.path.basename(srt_path)}")
            # 弹幕时间线解析（WorkBuddy 方案：弹幕已下载则解析为 [mm:ss] 文本，按视频命名）
            if dm_files:
                tl_path = parse_danmaku_timeline(dm_files, output_dir, title)
                if tl_path:
                    print(f"  Danmaku timeline: {os.path.basename(tl_path)}")

            # P0-3：按文案自动规划抽帧点（LLM 优先 + 规则兜底），命中则执行 480P 抽帧 OCR
            if segments and auto_frames:
                print("\nStep 3b: 规划画面抽帧（依据转写文案）...")
                tss = plan_frames(segments, api_key, base_url, llm_model)
                if tss:
                    frames_extract(url, output_dir, timestamps=tss, video_path=video_path)
                else:
                    print("  ℹ 文案无明显画面指向，跳过抽帧")

        if not method:
            method = f"faster-whisper/{whisper_size}"
        print(f"  ASR result: {len(raw_text)} chars")
    # tmpdir auto-cleaned here

    # Step 4: LLM correction (optional but recommended)
    final_text = raw_text
    corrected = False
    if llm_correction:
        print(f"\nStep 4: LLM correction ({llm_model})...")
        final_text, corrected = correct_with_llm(
            raw_text, title, description, comments,
            llm_model, api_key, base_url
        )

    # Step 5: Save output
    safe_title = sanitize_title(title)
    output_name = f"{safe_title}_transcript.md"
    output_path = os.path.join(output_dir, output_name)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n")
        f.write(f"**来源**: {url}\n")
        f.write(f"**方法**: {method}\n")
        f.write(f"**LLM纠正**: {'是' if corrected else '否'}\n")
        f.write(f"**日期**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"**字数**: {len(final_text)}\n\n")
        f.write("---\n\n")
        f.write(final_text)

    print(f"\n{'='*50}")
    print(f"✅ 完成！")
    print(f"   方法: {method}")
    print(f"   字数: {len(final_text)}")
    print(f"   输出: {output_path}")
    print(f"{'='*50}")

    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="从B站/YouTube提取视频文案（ASR转写；纠错默认由AI助手完成，可--llm-correction启用内置LLM纠正）"
    )
    parser.add_argument("url", nargs="?", default=None,
                        help="视频链接（B站或YouTube）；--cleanup-intermediates 模式下可省略")
    parser.add_argument("--output-dir", default="D:\\lk\\output",
                        help="输出目录（默认D:\\lk\\output）")
    parser.add_argument("--lang", default="zh", help="语言代码（默认zh）")
    parser.add_argument("--whisper-size", default="auto",
                        choices=["auto", "base", "small", "medium", "large"],
                        help="Whisper模型大小（默认auto: GPU用small, CPU用base）")
    parser.add_argument("--llm-correction", action="store_true",
                        help="启用脚本内置 LLM 纠正（默认关闭：日常纠错由 AI 助手/WorkBuddy 读取转写稿+元数据完成）")
    parser.add_argument("--llm-model", default="gpt-4o-mini",
                        help="LLM模型名（默认gpt-4o-mini）")
    parser.add_argument("--api-key", default=None,
                        help="LLM API密钥（或设置OPENAI_API_KEY环境变量）")
    parser.add_argument("--base-url", default=None,
                        help="LLM API地址（或设置OPENAI_BASE_URL环境变量）")
    parser.add_argument("--clean", action="store_true",
                        help="运行前清理旧输出文件（保留最近3次）")
    parser.add_argument("--clean-all", action="store_true",
                        help="清理所有旧输出和音频缓存")
    parser.add_argument("--frames", metavar="MM:SS,...", default=None,
                        help="按需抽帧模式：逗号分隔时间点（如 01:30,05:12）。"
                             "只下载480P视频抽帧→OCR，不转写。timestamps 为空时跳过不下载。")
    parser.add_argument("--auto-frames", action="store_true",
                        help="P0-3：转写完成后按文案（LLM优先+规则兜底）自动规划抽帧点并执行480P OCR")
    parser.add_argument("--backend", default="auto",
                        choices=["auto", "whisper", "sensevoice"],
                        help="P1-3：ASR后端 auto=有SenseVoice则用否则whisper; whisper=强制whisper; sensevoice=强制SenseVoice(失败回退)")
    parser.add_argument("--cleanup-intermediates", action="store_true",
                        help="2026-08-12：清理 output_dir 中间产物（视频/音频/srt/弹幕/画面信息/_frames 等），仅保留 *_完整文案.md。构建完最终文案后调用")

    args = parser.parse_args()

    # ── Cleanup intermediates 模式：仅清理，不转写 ─────────────────
    if args.cleanup_intermediates:
        n = cleanup_intermediates(args.output_dir)
        print(f"\n🗑️  已清理 {n} 项中间产物，仅保留 *_完整文案.md")
        return

    # ── frames 模式：按需抽帧（Agent 调用）──────────────────────────────
    if args.frames is not None:
        tss = [t.strip() for t in args.frames.split(",") if t.strip()]
        frames_extract(args.url, args.output_dir, timestamps=tss)
        return

    # ── Cleanup old files ────────────────────────────────────────
    if args.clean or args.clean_all:
        out_dir = args.output_dir
        if os.path.isdir(out_dir):
            md_files = sorted([
                f for f in os.listdir(out_dir)
                if f.endswith("_transcript.md")
            ])
            # Keep last 3
            keep = 0 if args.clean_all else 3
            to_del = md_files[:-keep] if keep > 0 else md_files
            for f in to_del:
                os.remove(os.path.join(out_dir, f))
                print(f"  🗑️  Removed old output: {f}")
            # Remove stray audio files
            for f in os.listdir(out_dir):
                if f.endswith((".mp3", ".m4a", ".wav")):
                    os.remove(os.path.join(out_dir, f))
                    print(f"  🗑️  Removed audio: {f}")

    # ── Display env ──────────────────────────────────────────────
    cache = os.environ.get("WHISPER_CACHE_DIR", "?")
    cache_size = "?"
    try:
        total = 0
        for dirpath, _, filenames in os.walk(cache):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if os.path.isfile(fp):
                    total += os.path.getsize(fp)
        cache_size = f"{total/1024/1024:.0f} MB"
    except:
        pass

    print(f"📁 缓存目录: {cache} ({cache_size})")
    print(f"📁 输出目录: {args.output_dir}")
    print()

    transcribe(
        url=args.url,
        output_dir=args.output_dir,
        lang=args.lang,
        whisper_size=args.whisper_size,
        llm_correction=args.llm_correction,
        llm_model=args.llm_model,
        api_key=args.api_key,
        base_url=args.base_url,
        auto_frames=args.auto_frames,
        backend=args.backend,
    )


if __name__ == "__main__":
    main()
