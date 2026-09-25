#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频文案提取工具（本地视频 + 在线链接双链路）
================================================
【本地视频链路】
  probe       : 探测视频流，给出建议路线
  subtitle    : 抽取内嵌字幕轨（软字幕，秒级零误差）
  transcribe  : faster-whisper 语音转写（无字幕视频主力）
  auto        : 自动决策（有字幕轨就抽，没有就转写）

【在线链接链路】（yt-dlp，B站/抖音/YouTube 等）
  fetch       : 只抓元数据（标题/简介/评论/标签），不下载视频
  subs        : 只下载中文字幕，不下载视频
  download    : 下载视频或纯音频（--audio-only）
  pipeline    : 一键全链路：元数据 → 字幕检索 → 无字幕则下音频 → 转写
  bili        : 【B站专用·推荐】调用开源 BBDown 下载器，绕过 yt-dlp 的 HTTP 412 反爬；
               自动选最低码率音频多线程下载（无需自备依赖），再本地转写

【大模型纠正环节】由 WorkBuddy 完成：读取元数据 + 原始转写稿，产出最终文案。

用法（Git Bash / 任意终端）：
  python video_text_extract.py <命令> <视频路径或链接> [选项]

重要环境事实（本机 2026-08-07 验证）：
  - 必须用装有 faster-whisper 的 Python 运行（本机: hermes venv）：
      C:\\Users\\lk\\AppData\\Local\\hermes\\hermes-agent\\venv\\Scripts\\python.exe
  - 本地模型缓存于 D:\\lk\\.cache\\faster-whisper\\（base + small 已下载），
    脚本自动优先使用本地模型，不重复下载。
  - yt-dlp / ffmpeg 可用；本机未装 ffprobe（转写前无需探测，不影响流程）。
  - B站下载依赖开源工具 BBDown（已装于 C:\\Users\\lk\\bin\\BBDown\\），自带多线程与 WBI 签名，
    无需自备依赖；hermes venv 的 PATH 不含 bin 目录，脚本用绝对路径常量定位它。

输出规范：
  本地视频 → outputs/srt、outputs/txt、outputs/json（{视频名}_{方法}_{语言}.*）
  在线链接 → outputs/{视频标题}/（最终文案.md、原始转写.srt、原始转写.txt、元数据.json）
  统一 UTF-8 无 BOM。
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- 基础工具

FFMPEG_FALLBACK_DIR = Path(r"C:\Users\lk\Documents\视频")
BBDown_PATH = Path(r"C:\Users\lk\bin\BBDown\BBDown.exe")  # 开源 B站下载器（自包含版，已安装）
LOCAL_MODEL_CACHE = Path(r"D:\lk\.cache\faster-whisper")  # 本机已下载模型的缓存根
KNOWN_MODELS = ("tiny", "base", "small", "medium", "large-v3")


def find_tool(name: str) -> str:
    """在 PATH 中查找工具，找不到则尝试本机已知的 ffmpeg 目录。"""
    p = shutil.which(name)
    if p:
        return p
    cand = FFMPEG_FALLBACK_DIR / (name + ".exe")
    if cand.exists():
        return str(cand)
    raise FileNotFoundError(
        f"找不到 {name}，请确认已安装并加入 PATH，或放置于 {FFMPEG_FALLBACK_DIR}"
    )


def run_cmd(cmd: list, quiet: bool = True) -> subprocess.CompletedProcess:
    """执行命令，失败时打印完整命令与错误信息后退出。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"[错误] 执行命令失败: {' '.join(cmd)}\n{e}")
        sys.exit(1)
    if r.returncode != 0:
        print(f"[错误] 命令执行失败(exit {r.returncode}): {' '.join(cmd)}")
        if r.stderr:
            print(r.stderr[-2000:])
        sys.exit(1)
    if not quiet and r.stdout:
        print(r.stdout)
    return r


def fmt_ts(seconds: float, srt_style: bool = True) -> str:
    """秒数转 SRT 时间戳 (HH:MM:SS,mmm)。"""
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    if srt_style:
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def write_utf8(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")  # Python 默认无 BOM
    print(f"  ✔ 已写出: {path}")


# ---------------------------------------------------------------- 模型解析

def resolve_model(model_arg: str) -> str:
    """解析模型参数：优先返回本机已下载的本地模型路径，避免重新下载。

    - 参数本身是目录 → 直接用
    - 参数是 tiny/base/small/medium/large-v3 → 在本地缓存里找对应 snapshot
    - 其他 → 原样返回（交给 faster-whisper 自行下载）
    """
    if os.path.isdir(model_arg):
        return model_arg
    if model_arg not in KNOWN_MODELS:
        return model_arg

    # 检查 HF_HUB_CACHE / 默认缓存 / 本机已知 D 盘缓存
    env_cache = os.environ.get("HF_HUB_CACHE") or os.environ.get("HF_HOME")
    cache_roots = []
    if env_cache:
        cache_roots.append(Path(env_cache))
    cache_roots.append(Path.home() / ".cache" / "faster-whisper")
    cache_roots.append(LOCAL_MODEL_CACHE)

    for root in cache_roots:
        pattern = str(root / f"models--Systran--faster-whisper-{model_arg}" / "snapshots" / "*")
        for snap in sorted(glob.glob(pattern), reverse=True):
            if (Path(snap) / "model.bin").exists():
                print(f"  ℹ 使用本地模型缓存: {snap}")
                return snap
    return model_arg  # 没找到 → 交给 faster-whisper 下载


# ---------------------------------------------------------------- 1. 本地视频：探测

def probe_video(video: Path) -> dict:
    """ffprobe 探测视频流信息，返回结构化结果并打印人类可读摘要。"""
    ffprobe = find_tool("ffprobe")
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries", "stream=index,codec_type,codec_name:stream_tags=language,title",
        "-of", "json", str(video),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"[错误] ffprobe 探测失败: {video}")
        print(r.stderr[-2000:])
        sys.exit(1)

    data = json.loads(r.stdout)
    streams = data.get("streams", [])
    info = {"video": video.name, "has_subtitle": False, "subtitle_tracks": [],
            "has_audio": False, "suggest_route": None, "streams": streams}

    print(f"\n📹 视频: {video.name}")
    for s in streams:
        kind = s.get("codec_type", "?")
        tags = s.get("tags", {})
        lang = tags.get("language", "-")
        title = tags.get("title", "-")
        if kind == "subtitle":
            info["has_subtitle"] = True
            info["subtitle_tracks"].append({"index": s.get("index"), "codec": s.get("codec_name"), "language": lang})
            print(f"  🎞 字幕轨 #{s.get('index')}: {s.get('codec_name')} 语言={lang} 标题={title}")
        elif kind == "audio":
            info["has_audio"] = True
            print(f"  🎵 音轨 #{s.get('index')}: {s.get('codec_name')} 语言={lang}")
        elif kind == "video":
            print(f"  🎬 视频轨 #{s.get('index')}: {s.get('codec_name')}")

    if info["has_subtitle"]:
        info["suggest_route"] = "subtitle"
        print("  ➡ 建议路线: A 抽取内嵌字幕轨（subtitle）")
    elif info["has_audio"]:
        info["suggest_route"] = "transcribe"
        print("  ➡ 建议路线: C 语音转写（transcribe）；画面有明显烧录字幕可考虑 OCR（未安装）")
    else:
        print("  ⚠ 无音轨也无字幕轨，无法提取文案")
    return info


# ---------------------------------------------------------------- 2. 本地视频：抽字幕轨

def extract_subtitle(video: Path, track: int, outdir: Path) -> Path:
    """抽取指定字幕轨为 SRT 文件。"""
    ffmpeg = find_tool("ffmpeg")
    out_srt = outdir / "srt" / f"{video.stem}_subtitle_track{track}.srt"
    print(f"\n🎞 抽取字幕轨 #{track} → {out_srt.name}")
    run_cmd([
        ffmpeg, "-y", "-i", str(video),
        "-map", f"0:s:{track}", "-c:s", "srt",
        str(out_srt),
    ])
    if out_srt.stat().st_size == 0:
        print("  ⚠ 抽取结果为空文件，可能该轨无实际字幕内容")
    else:
        print(f"  ✔ 抽取完成: {out_srt}")
    return out_srt


# ---------------------------------------------------------------- 3. 本地视频：语音转写

def transcribe_audio(media: Path, model_name: str, language: str, outdir: Path,
                     vad: bool = True, initial_prompt: str = "以下是普通话内容。") -> dict:
    """faster-whisper 转写，输出 SRT / TXT / JSON 三种格式。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("[错误] 当前 Python 未安装 faster-whisper。")
        print("       必须用装有它的 Python 运行本脚本，例如：")
        print('       "C:\\Users\\lk\\AppData\\Local\\hermes\\hermes-agent\\venv\\Scripts\\python.exe" video_text_extract.py transcribe ...')
        sys.exit(1)

    model_path = resolve_model(model_name)
    print(f"\n🎙 语音转写: {media.name}")
    print(f"   模型={model_name} 语言={language} VAD={vad}（本地无缓存时会自动下载，请耐心等待）")
    t0 = time.time()
    # 自动检测 GPU（2026-08-07 整合：RTX 2070 SUPER 实测比 CPU 快 5.3 倍）
    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
    except ImportError:
        device, compute_type = "cpu", "int8"
    print(f"   转写设备: {device} ({compute_type})")
    model = WhisperModel(model_path, device=device, compute_type=compute_type)
    print(f"   模型加载完成 ({time.time()-t0:.1f}s)，开始转写…")

    segments_iter, info = model.transcribe(
        str(media),
        language=language,
        vad_filter=vad,
        word_timestamps=False,
        initial_prompt=initial_prompt,
    )
    segments = list(segments_iter)
    elapsed = time.time() - t0
    print(f"   ✔ 转写完成: {len(segments)} 段, 耗时 {elapsed:.1f}s")

    srt_lines, txt_lines, json_segs = [], [], []
    for i, seg in enumerate(segments, 1):
        text = seg.text.strip()
        if not text:
            continue
        srt_lines.append(f"{i}\n{fmt_ts(seg.start)} --> {fmt_ts(seg.end)}\n{text}\n")
        txt_lines.append(text)
        json_segs.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": text})

    base = f"{media.stem}_whisper_{model_name}_{language}"
    write_utf8(outdir / "srt" / f"{base}.srt", "\n".join(srt_lines))
    write_utf8(outdir / "txt" / f"{base}.txt", "\n".join(txt_lines))
    write_utf8(outdir / "json" / f"{base}.json",
               json.dumps({"video": media.name, "model": model_name, "language": language,
                           "duration": info.duration, "segments": json_segs},
                          ensure_ascii=False, indent=2))
    return {"base": base, "srt": "\n".join(srt_lines), "txt": "\n".join(txt_lines), "json": json_segs}


# ---------------------------------------------------------------- 4. 在线链接：yt-dlp 系列

def ytdlp_base_cmd() -> list:
    yt = find_tool("yt-dlp")
    return [yt]


def fetch_meta(url: str, outdir: Path) -> Path:
    """抓取链接的元数据（info.json + description），不下载媒体。"""
    print(f"\n📋 抓取元数据: {url}")
    run_cmd(ytdlp_base_cmd() + [
        "--write-info-json", "--write-description",
        "--skip-download",
        "-o", str(outdir / "%(title)s" / "%(title)s.%(ext)s"),
        url,
    ])
    infos = list(outdir.rglob("*.info.json"))
    if not infos:
        print("  ⚠ 未找到 info.json")
        sys.exit(1)
    info_path = infos[-1]
    print(f"  ✔ 元数据已保存: {info_path}")
    return info_path


def fetch_subs(url: str, outdir: Path) -> bool:
    """下载中文字幕（不下载视频）。返回是否成功拿到字幕。"""
    print(f"\n💬 检索中文字幕: {url}")
    r = run_cmd(ytdlp_base_cmd() + ["--list-subs", url], quiet=False)
    if "has no subtitles" in r.stdout.lower() or "does not have subtitles" in r.stdout.lower():
        print("  ⚠ 该视频无可用字幕轨")
        return False
    run_cmd(ytdlp_base_cmd() + [
        "--write-subs", "--sub-langs", "zh.*,zh-Hans,zh-CN",
        "--skip-download",
        "-o", str(outdir / "%(title)s" / "%(title)s.%(ext)s"),
        url,
    ])
    found = [p for p in outdir.rglob("*") if p.suffix.lower() in (".srt", ".vtt", ".ass", ".json")
             and "info.json" not in p.name]
    if found:
        print(f"  ✔ 字幕已下载: {found[-1]}")
        return True
    print("  ⚠ 未匹配到中文字幕轨")
    return False


def download_media(url: str, outdir: Path, audio_only: bool = False) -> Path:
    """下载视频或纯音频，返回媒体文件路径。"""
    print(f"\n⬇ 下载{'纯音频' if audio_only else '视频'}: {url}")
    cmd = ytdlp_base_cmd() + ["-f", "ba/b" if audio_only else "bv*+ba/b"]
    if audio_only:
        cmd += ["-x", "--audio-format", "mp3"]
    cmd += ["-o", str(outdir / "%(title)s" / "%(title)s.%(ext)s"), url]
    run_cmd(cmd)
    if audio_only:
        cands = list(outdir.rglob("*.mp3"))
    else:
        cands = [p for p in outdir.rglob("*") if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".flv", ".mov")]
    if not cands:
        print("  ⚠ 下载完成但未找到媒体文件")
        sys.exit(1)
    media = cands[-1]
    print(f"  ✔ 媒体已下载: {media} ({media.stat().st_size/1024/1024:.1f} MB)")
    return media


def link_pipeline(url: str, model_name: str, language: str, outdir: Path) -> None:
    """一键链路：元数据 → 字幕检索 → 无字幕则下音频转写 → 汇总报告。"""
    print(f"\n🚀 开始全链路处理: {url}")
    t0 = time.time()

    # Step 1: 元数据
    info_path = fetch_meta(url, outdir)

    # Step 2: 字幕检索
    has_subs = fetch_subs(url, outdir)

    if has_subs:
        # 找字幕文件，转 SRT（vtt 直接改名，json 需转）
        sub_files = [p for p in outdir.rglob("*") if p.suffix.lower() in (".srt", ".vtt")
                     and "info.json" not in p.name]
        print(f"\n✅ 链路完成（字幕路线），耗时 {time.time()-t0:.0f}s")
        print(f"   最终成稿需 WorkBuddy 结合元数据整理: {info_path.parent}")
    else:
        # Step 3: 下载纯音频 + 转写
        audio = download_media(url, outdir, audio_only=True)
        print(f"\n   ▶ 音频就绪，开始转写…")
        transcribe_audio(audio, model_name, language, audio.parent, initial_prompt="以下是普通话视频内容。")
        print(f"\n✅ 链路完成（转写路线），耗时 {time.time()-t0:.0f}s")
        print(f"   输出目录: {audio.parent}")


# ---------------------------------------------------------------- 4b. B站专用链路（绕过 yt-dlp 412 反爬）

# ---------------------------------------------------------------- B站下载（BBDown 开源下载器）
def find_bbdown() -> str:
    """定位 BBDown（开源 B站下载器，v1.6.3+，自带多线程 + WBI 签名，规避 yt-dlp 的 412 反爬）。
    hermes venv 的 PATH 不含 C:\\Users\\lk\\bin，故优先用绝对路径常量，其次回退 shutil.which。"""
    p = shutil.which("BBDown")
    if p:
        return p
    if BBDown_PATH.exists():
        return str(BBDown_PATH)
    raise FileNotFoundError(
        f"找不到 BBDown。请下载自包含版放到 {BBDown_PATH}，或加入 PATH。\n"
        f"下载: https://github.com/nilaoda/BBDown/releases"
    )


def bili_download_audio(bvid: str, out_dir: Path, ffmpeg: str) -> Path:
    """用 BBDown 下载 B站最低码率音频并转码为 mp3，返回 mp3 路径。

    为什么用 BBDown 而非自写下载：
      - 自带 WBI 签名 + 多线程 + 正确 CDN host，不会被 412 拦（yt-dlp 会）；
      - 自管临时文件，不踩 WorkBuddy 沙箱拦截 unlink/rmdir 的坑；
      - 自动选最低码率音轨（--audio-ascending），转写够用、文件最小。
    速度说明：本机到 B站 CDN 链路约 40~60KB/s，26MB 音频约 8~10 分钟，属带宽上限，
    非工具瓶颈；BBDown 已用满可用带宽。
    """
    bbdown = find_bbdown()
    # BBDown 在 work-dir 下产出 <bvid>.m4a（--audio-only 默认 m4a 容器）
    run_cmd([bbdown, bvid, "--audio-only", "--audio-ascending",
             "--work-dir", str(out_dir), "-F", bvid,
             "--ffmpeg-path", ffmpeg])
    cands = [p for p in out_dir.glob(f"{bvid}.*")
             if p.suffix.lower() in (".m4a", ".mp3", ".aac", ".flac", ".ogg") and p.is_file()]
    if not cands:  # 兜底：work-dir 下任意音频文件
        cands = [p for p in out_dir.iterdir()
                 if p.is_file() and p.suffix.lower() in (".m4a", ".mp3", ".aac")]
    if not cands:
        raise FileNotFoundError(f"BBDown 未产出音频文件，请检查 work-dir: {out_dir}")
    src = cands[0]
    mp3 = out_dir / "audio.mp3"
    if src.suffix.lower() != ".mp3":
        run_cmd([ffmpeg, "-y", "-i", str(src), "-vn", "-c:a", "libmp3lame", "-b:a", "128k", str(mp3)])
        try:
            src.unlink()
        except OSError:
            pass  # 沙箱可能拦截删除，残留 m4a 无害
    else:
        mp3 = src
    return mp3



def bili_pipeline(url: str, model_name: str, language: str, outdir: Path) -> None:
    """B站官方 API 直连全链路：元数据(view) → 音频流(playurl) → ffmpeg 下载转码 → 转写。
    规避 yt-dlp 抓 B站常见的 HTTP 412 反爬。无公开字幕时（CC 需 cookie）自动走音频转写。"""
    import re as _re, json as _json, urllib.request as _ureq
    print(f"\n🚀 [B站API] 开始全链路: {url}")
    t0 = time.time()
    # 直连优化：清空代理 env，避免 B站流量被沙箱 HTTP_PROXY 强制绕到海外代理节点。
    # 实测：走代理 ~0.2MB/s，清空后直连 ~7.7MB/s（差 ~37 倍）。B站为国内域名，直连即最快路径。
    for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(_k, None)
    m = _re.search(r"BV[0-9A-Za-z]+", url)
    if not m:
        print("  ⚠ 无法从链接解析 BV 号"); sys.exit(1)
    bvid = m.group(0)
    hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/"}
    def _get(u):
        req = _ureq.Request(u, headers=hdrs)
        with _ureq.urlopen(req, timeout=20) as r:
            return _json.load(r)

    # Step 1: 元数据
    print(f"\n📋 [B站API] 抓取元数据: {bvid}")
    d = _get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")["data"]
    cid = d["cid"]
    title = d["title"]
    meta = {"title": title, "owner": d["owner"]["name"], "pubdate": d["pubdate"],
            "duration": d["duration"], "stat": d["stat"], "desc": d.get("desc") or "",
            "tname": d.get("tname")}
    sub = outdir / title
    sub.mkdir(parents=True, exist_ok=True)
    info_path = sub / "info.json"
    write_utf8(info_path, _json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"  ✔ 元数据已保存: {info_path}")

    # Step 2: 字幕（未登录通常空，直接进入音频路线）
    subs = (_get(f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}").get("data") or {}).get("subtitle", {}).get("subtitles", [])
    if subs:
        print(f"  ℹ 发现 {len(subs)} 条字幕，但本链路优先音频转写（如需字幕请用 subs 子命令）")

    # Step 3: 用 BBDown 下载最低码率音频 → 转码 mp3（BBDown 多线程，规避 412，自管临时文件）
    print("  ℹ 无公开字幕，进入音频下载转写路线（BBDown 多线程下载）")
    ff = find_tool("ffmpeg")
    mp3 = bili_download_audio(bvid, sub, ff)
    print(f"  ✔ 音频就绪 ({mp3.stat().st_size/1024/1024:.1f} MB)，开始转写…")

    # Step 4: 转写
    transcribe_audio(mp3, model_name, language, mp3.parent, initial_prompt="以下是普通话视频内容。")
    print(f"\n✅ [B站API] 链路完成，耗时 {time.time()-t0:.0f}s")
    print(f"   输出目录: {mp3.parent}")


# ---------------------------------------------------------------- 弹幕下载与解析（内嵌标注环节的数据准备）

def danmaku_download(url: str, outdir: Path) -> Path:
    """下载 B站弹幕并解析为带时间戳的文本（[mm:ss] 内容，按时间排序）。

    产物：<标题>.xml（B站标准弹幕 XML）+ <标题>.ass + 弹幕时间线.txt
    用途：供 WorkBuddy 做「弹幕评论拆解点评」——把弹幕按时间点对齐文案段落。
    依赖 BBDown（自带 WBI 签名，老 API x/v1/dm/list.so 已失效）。
    """
    import re as _re
    bbdown = find_bbdown()
    if not bbdown:
        raise FileNotFoundError(
            f"找不到 BBDown。请下载自包含版放到 {BBDOWN_PATH}，或加入 PATH。\n"
            f"下载: https://github.com/nilaoda/BBDown/releases"
        )
    m = _re.search(r"(BV[0-9A-Za-z]{10})", url)
    bvid = m.group(1) if m else url
    run_cmd([bbdown, bvid, "--danmaku-only", "--work-dir", str(outdir)])

    xmls = list(outdir.glob("*.xml"))
    if not xmls:
        raise FileNotFoundError(f"BBDown 未产出弹幕 xml，请检查 work-dir: {outdir}")
    xml = xmls[0]
    txt = xml.read_text(encoding="utf-8", errors="ignore")
    dms = _re.findall(r'<d p="([^"]+)">([^<]+)</d>', txt)
    dms_sorted = sorted(dms, key=lambda x: float(x[0].split(",")[0]))

    out = outdir / "弹幕时间线.txt"
    lines = []
    for p, t in dms_sorted:
        sec = float(p.split(",")[0])
        mm, ss = divmod(int(sec), 60)
        lines.append(f"[{mm:02d}:{ss:02d}] {t}")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"  ✔ 弹幕 {len(dms)} 条 → {out} (+ {xml.name} / .ass)")
    return out


def _parse_ts(s: str) -> float:
    """'mm:ss' / 'hh:mm:ss' / 纯秒 → 秒。"""
    parts = s.strip().split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(s)


def _fmt_ts(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _pick_timestamps(outdir: Path, frames: int, at: str = "", llm: bool = True) -> tuple:
    """决定抽帧时间点（秒），返回 (时间点列表, 来源说明, 命中明细列表)。

    优先级：
      1. --at 显式指定（最高）
      2. LLM 语义判断（大模型读带时间戳文案，理解式判断哪些位置需要抽帧，
         不依赖关键词；失败自动降级）
      3. 关键词启发式（信号词分级 + 分段择优，兜底）
      4. 均匀采样（无时间戳信息）
    命中明细: [{"ts": float, "word": str, "level": int, "text": str}, ...]
    """
    # 1. 显式指定（最高优先级）
    if at:
        tss = [_parse_ts(x) for x in at.split(",") if x.strip()]
        return tss, f"显式指定 {len(tss)} 个时间点", []

    # 2. LLM 语义判断 + 关键词兜底（读 *_segments.json）
    segs = sorted(outdir.glob("*_segments.json"))
    if segs:
        import json as _json
        try:
            data = _json.loads(segs[0].read_text(encoding="utf-8"))
            if isinstance(data, list) and data and "start" in data[0]:
                if llm:
                    llm_tss, llm_hits, llm_desc = _llm_pick_timestamps(data, frames)
                    if llm_tss:
                        return llm_tss, llm_desc, llm_hits
                tss, hits, desc = _semantic_pick(data, frames)
                return tss, desc + "（LLM 不可用，降级关键词）", hits
        except Exception:
            pass

    # 3. 退回均匀采样（无时间戳信息）
    total = 300.0
    if frames == 1:
        tss = [total / 2]
    else:
        tss = [total * i / (frames - 1) for i in range(frames)]
    return tss, "均匀采样（无时间戳/segments.json 缺失）", []


def _llm_pick_timestamps(segments: list, frames: int,
                         model: str = "deepseek-v4-flash") -> tuple:
    """大模型语义判断抽帧点：读带时间戳的文案，理解式判断哪些位置需要抽帧。

    核心差异 vs 关键词：UP 的表达千变万化（"这地方有讲究/大家感受一下/看这个细节"），
    关键词永远覆盖不全；LLM 语义理解可识别「引导观众看画面/展示材料/讲重点」的
    任意表达。失败返回 ([], [], desc) 由调用方降级。

    返回 (tss, hits, desc)；hits 里 level=4 表示 LLM 判断，word 为理由。
    """
    import json as _json
    import urllib.request as _urlreq
    # 1. 从 models.json 取网关凭据（与 vision-analyze 同源，opencode.ai）
    models_json = os.path.expanduser("~/.workbuddy/models.json")
    if not os.path.isfile(models_json):
        return [], [], "LLM 选帧跳过（models.json 缺失）"
    try:
        with open(models_json, encoding="utf-8") as f:
            mdata = _json.load(f)
        lst = mdata["models"] if isinstance(mdata, dict) else mdata
        if not lst:
            return [], [], "LLM 选帧跳过（models.json 为空）"
        api_url, api_key = lst[0]["url"], lst[0]["apiKey"]
    except Exception as e:
        return [], [], f"LLM 选帧跳过（凭据读取失败: {e}）"

    # 2. 组装带时间戳的文案（全部段，标注 [mm:ss]）
    lines = []
    for s in segments:
        if not s.get("text", "").strip():
            continue
        mm = int(s["start"] // 60)
        ss = int(s["start"] % 60)
        lines.append(f"[{mm:02d}:{ss:02d}] {s['text'].strip()}")
    content = "\n".join(lines)
    if len(content) > 12000:  # 超长截断保护（保头尾）
        content = content[:9000] + "\n...(中段省略)...\n" + content[-2500:]

    prompt = (
        "你是视频抽帧点选择专家。下面是一段教学/课程/屏幕录制类视频的语音转写稿，"
        "每行带时间戳 [mm:ss]。请判断：**哪些时间点的画面值得抽帧分析**？\n"
        "抽帧点应满足以下任一条件：\n"
        "1. 讲者引导观众看画面的位置（如'看这里''大家看''注意''就是这个''我给大家展示'，"
        "包括各种口语化/隐性表达：'这地方有讲究''感受一下''看这个细节''你们看下这个设计'等）\n"
        "2. 正在讲解关键材料的位置（PPT/文档/网页/图表/参考书/示例文字被展示时）\n"
        "3. 讲者强调'重点''关键''核心'或说'举个例子'时的示范画面\n"
        f"请选出 {frames} 个最值得抽帧的时间点，覆盖整段视频（不要扎堆在前几分钟），"
        "返回 JSON 数组，每个元素格式：{\"ts\": \"mm:ss\", \"reason\": \"为什么抽这帧（10字内）\"}\n"
        "只返回 JSON，不要其他文字。\n\n"
        f"转写稿：\n{content}"
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2000,
        "temperature": 0.2,
    }
    # 3. 调用（curl 子进程绕过 Cloudflare，同 vision_ask 方案；截断自动重试）
    import subprocess as _sp
    import tempfile as _tmp
    curl = _sp.run(["where", "curl"], capture_output=True, text=True).stdout.strip() or "curl"
    fd, tmp_path = _tmp.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        _json.dump(payload, f, ensure_ascii=False)
    reply = ""
    try:
        for attempt in range(3):
            cmd = [curl, "-s", "--max-time", "180",
                   "-H", f"Authorization: Bearer {api_key}",
                   "-H", "Content-Type: application/json",
                   "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                   "--data-binary", f"@{tmp_path}", api_url]
            proc = _sp.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=200)
            raw = proc.stdout.strip()
            if not raw:
                continue
            try:
                resp = _json.loads(raw)
            except Exception:
                continue
            reply = (resp["choices"][0]["message"]["content"] or "").strip()
            finish = resp["choices"][0].get("finish_reason", "")
            if reply and finish == "length":
                payload["max_tokens"] = int(payload["max_tokens"] * 1.5)  # 截断则加量重试
                with os.fdopen(tmp_path, "w", encoding="utf-8") as f:
                    _json.dump(payload, f, ensure_ascii=False)
                continue
            if reply:
                break
    except Exception as e:
        return [], [], f"LLM 选帧跳过（调用失败: {str(e)[:80]}）"
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if not reply:
        return [], [], "LLM 选帧跳过（无响应）"

    # 4. 解析返回 JSON（强容错：定位首个 [ 到末个 ]，剥离代码块/前后缀/注释）
    import re as _re
    start = reply.find("[")
    end = reply.rfind("]")
    if start < 0 or end <= start:
        return [], [], "LLM 选帧跳过（返回非 JSON）"
    frag = reply[start:end + 1]
    # 去除行注释（//）与尾随逗号（JSON5 兼容）
    frag = _re.sub(r"//[^\n]*", "", frag)
    frag = _re.sub(r",\s*([}\]])", r"\1", frag)
    try:
        items = _json.loads(frag)
    except Exception:
        return [], [], "LLM 选帧跳过（JSON 解析失败）"
    tss, hits = [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            ts = _parse_ts(str(it.get("ts", "")))
        except Exception:
            continue
        if ts < 0:
            continue
        tss.append(ts)
        hits.append({"ts": ts, "word": str(it.get("reason", "LLM 判断")),
                     "level": 4, "text": ""})
    if not tss:
        return [], [], "LLM 选帧跳过（未返回有效时间点）"
    # 按时间排序 + 去重（间隔 < 5s 保留先者）
    order = sorted(range(len(tss)), key=lambda i: tss[i])
    tss = [tss[i] for i in order]
    hits = [hits[i] for i in order]
    dedup_t, dedup_h = [], []
    for t, h in zip(tss, hits):
        if all(abs(t - x) >= 5 for x in dedup_t):
            dedup_t.append(t)
            dedup_h.append(h)
    return dedup_t, dedup_h, f"LLM 语义判断（{model}）→ {len(dedup_t)} 帧"


# ── 语义导向抽帧：信号词分级 + 评分选帧 ──────────────────────────────
# A 级（3 分）：UP 明确引导观众看画面（"看这里/大家看/注意/就是这里…"）
SIGNAL_A = [
    "看这里", "看这个", "看那边", "大家看", "你们看", "你看",
    "注意看", "注意", "重点来了", "划重点", "看重点",
    "就是这里", "就在这", "这个地方", "这个位置", "这里就是",
    "可以看到", "大家可以看到", "我给大家看", "给大家看", "给大家展示",
    "展示一下", "演示一下", "你们注意", "仔细看",
]
# B 级（2 分）：内容强调/转折（接下来要讲重点，画面常对应关键材料）
SIGNAL_B = [
    "重点", "关键", "核心", "最", "特别要", "一定要", "非常重要",
    "说白了", "其实", "本质上", "真相是", "重点是",
    "举个例子", "比如说", "例如", "比如", "看这个例子",
    "接下来", "下面我们", "我们来看", "我们看看",
]
# C 级（1 分）：屏幕操作/对象指称（画面正在展示文档/网页/图）
SIGNAL_C = [
    "打开", "切到", "切回", "翻到", "拉到", "点到", "点开", "拖到",
    "这个页面", "这个界面", "这本书", "这个文档", "这个网站", "这个图",
    "这个表格", "这个界面", "上面写着", "写着", "这张图", "这个截图",
    "屏幕", "画面里", "大家看屏幕",
]
MIN_GAP = 8.0  # 选中时间点最小间隔（秒），防密集抽帧


def _seg_score(text: str) -> tuple:
    """给一段文案打分：返回 (分数, 命中的最高级信号词)。"""
    for w in SIGNAL_A:
        if w in text:
            return 3, w
    for w in SIGNAL_B:
        if w in text:
            return 2, w
    for w in SIGNAL_C:
        if w in text:
            return 1, w
    return 0, ""


def _semantic_pick(segments: list, frames: int) -> tuple:
    """语义导向选帧：时间轴分段覆盖 + 段内信号择优。

    策略：把视频时长均分为 N 个窗口，每个窗口内取信号分最高的段落（优先 A 指示
    > B 强调 > C 操作），无信号的窗口取该段中点——保证全片覆盖（不出现密集扎堆
    或大片空白），同时信号词优先。返回 (tss, hits, desc)。
    """
    if not segments:
        return [], [], "无 segments"
    total = segments[-1].get("end") or segments[-1].get("start") or 300.0
    n = min(frames, max(1, len(segments)))
    hits = []

    # 预计算每段信号分
    scored = []
    for s in segments:
        txt = s.get("text", "")
        if not txt:
            continue
        score, word = _seg_score(txt)
        scored.append({"start": s["start"], "end": s["end"], "mid": (s["start"] + s["end"]) / 2,
                       "score": score, "word": word, "text": txt.strip()[:60]})
    if not scored:
        scored = [{"start": s.get("start", 0), "end": s.get("end", 300), "mid": (s.get("start", 0) + s.get("end", 300)) / 2,
                   "score": 0, "word": "", "text": ""} for s in segments]

    for k in range(n):
        win_start = total * k / n
        win_end = total * (k + 1) / n
        in_win = [s for s in scored if s["start"] < win_end and s["end"] > win_start]
        if in_win:
            # 段内按信号分降序取最优
            best = max(in_win, key=lambda s: (s["score"], s["start"]))
        else:
            # 空窗口兜底：取窗口中点附近最近的段
            best = min(scored, key=lambda s: abs(s["mid"] - (win_start + win_end) / 2))
        hits.append({"ts": best["mid"], "word": best["word"], "level": best["score"],
                     "text": best["text"]})

    tss = [h["ts"] for h in hits]
    n_signal = sum(1 for h in hits if h["level"] > 0)
    desc = (f"语义分段 {n} 窗口 → 信号命中 {n_signal} 帧"
            f"（A指示/强调/操作） + 无信号窗口 {n - n_signal} 帧取中点")
    return tss, hits, desc


def frames_extract(url: str, outdir: Path, frames: int = 8, model: str = "minimax-m3",
                   at: str = "", llm: bool = True) -> list:
    """B站视频画面信息提取（识图）：下载低画质视频 → ffmpeg 按时间点抽帧 → 视觉模型 OCR。

    抽帧时间点 = 大模型语义判断（读 *_segments.json 文案，理解式判断哪些位置
    需要抽帧）+ 关键词启发式兜底 + --at 显式指定；不再固定间隔盲抽。
    产物 <outdir>/画面信息.md，纠错成稿时按段落引用（不改原文）。
    """
    import re as _re
    import glob as _glob
    bbdown = find_bbdown()
    if not bbdown:
        raise FileNotFoundError(f"找不到 BBDown（用于下载视频画面），请安装到 {BBDOWN_PATH}")
    m = _re.search(r"(BV[0-9A-Za-z]{10})", url)
    bvid = m.group(1) if m else url

    # 1. 下载低画质视频（只取画面，360P 足够 OCR）
    print(f"  [识图] 下载低画质视频 {bvid} …")
    run_cmd([bbdown, bvid, "--video-only", "--dfn-priority", "360P", "--work-dir", str(outdir)])
    vids = _glob.glob(str(outdir / "*.mp4")) + _glob.glob(str(outdir / "*.flv")) + _glob.glob(str(outdir / "*.m4v"))
    if not vids:
        raise FileNotFoundError(f"BBDown 未产出视频文件，请检查 work-dir: {outdir}")
    video = vids[0]

    # 2. 决定抽帧时间点（大模型语义判断，关键词兜底）
    import subprocess as _sp
    tss, src_desc, hits = _pick_timestamps(outdir, frames, at, llm=llm)
    print(f"  [识图] 抽帧时间点（{src_desc}）: {', '.join(_fmt_ts(t) for t in tss)}")
    for h in hits:
        lvl = {4: "LLM判断", 3: "A指示", 2: "B强调", 1: "C操作", 0: "补齐"}.get(h["level"], "?")
        print(f"    - [{_fmt_ts(h['ts'])}] {lvl}「{h['word']}」 {h['text']}")

    # 3. ffmpeg 按时间点逐帧抽取（-ss 精确定位）
    frame_dir = outdir / "_frames"
    frame_dir.mkdir(exist_ok=True)
    frame_paths = []
    for i, ts in enumerate(tss, 1):
        fp = frame_dir / f"frame_{i:02d}_{_fmt_ts(ts).replace(':', '-')}.jpg"
        _sp.run(["ffmpeg", "-y", "-ss", str(ts), "-i", video, "-frames:v", "1",
                 "-vf", "scale=640:-1", str(fp)],
                capture_output=True, text=True, timeout=120, check=False)
        if fp.is_file():
            frame_paths.append(fp)
        else:
            print(f"  [识图] ⚠️ 时间点 {_fmt_ts(ts)} 抽帧失败（可能超出时长）")
    if not frame_paths:
        raise FileNotFoundError("所有时间点抽帧均失败，请检查 --at 时间点是否在视频时长内")

    # 4. 视觉模型 OCR（vision-analyze 技能）
    vision_py = r"C:\Users\lk\.workbuddy\skills\vision-analyze\scripts\vision_ask.py"
    if not os.path.isfile(vision_py):
        raise FileNotFoundError(f"找不到视觉辅助脚本: {vision_py}")
    md_lines = ["# 画面信息（AI 识图，语义导向抽帧）", "",
                f"> 视频: {url} | 模型: {model} | 抽帧 {len(frame_paths)} 张 | 选帧: {src_desc}",
                "> 每帧对应一段文案（信号词/补充说明见帧标题），音频未覆盖的信息在纠错成稿时可作补充引用。", ""]
    for i, f in enumerate(frame_paths, 1):
        print(f"  [识图] 分析第 {i}/{len(frame_paths)} 帧（{f.name}）…")
        r = run_cmd([
            "C:/Users/lk/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe", vision_py,
            "--image", str(f), "--model", model,
            "--prompt", "这是教学视频的截帧。请完整提取画面中所有可见的文字信息：PPT标题、要点列表、图表、书籍封面文字、字幕等。如果画面主要是人像没有文字，请说明画面内容和是否有文字。用中文回答。",
        ], quiet=False)
        text = r.stdout.strip()
        # 帧标题带上命中信息（时间点 + 来源 + 理由），供 WorkBuddy 对齐章节
        hit = hits[i - 1] if i - 1 < len(hits) else None
        head = f"## 帧{i}（{f.name}）"
        if hit:
            lvl = {4: "LLM判断", 3: "A指示", 2: "B强调", 1: "C操作", 0: ""}.get(hit["level"], "")
            head += f"｜ 命中: {lvl}「{hit['word']}」"
            if hit.get("text"):
                head += f"｜ 对应文案: {hit['text']}"
        md_lines.append(head)
        md_lines.append("```")
        md_lines.append(text[:2000])
        md_lines.append("```")
        md_lines.append("")
    md_out = outdir / "画面信息.md"
    md_out.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"  ✔ 画面信息 {len(frame_paths)} 帧 → {md_out}")
    return [md_out]


# ---------------------------------------------------------------- 主入口

def main() -> None:
    parser = argparse.ArgumentParser(description="视频文案提取工具（本地视频 + 在线链接）")
    parser.add_argument("command",
                        choices=["probe", "subtitle", "transcribe", "auto",
                                 "fetch", "subs", "download", "pipeline", "bili",
                                 "danmaku", "frames"],
                        help="本地视频: probe/subtitle/transcribe/auto | 在线链接: fetch/subs/download/pipeline | B站弹幕: danmaku | 画面识图: frames")
    parser.add_argument("target", type=str, help="视频文件路径 或 视频链接(B站/抖音/YouTube等)")
    parser.add_argument("--track", type=int, default=0, help="字幕轨序号（默认 0，即第一条）")
    parser.add_argument("--model", default="small", help="whisper 模型（tiny/base/small/medium/large-v3，默认 small；自动使用本地缓存）")
    parser.add_argument("--language", default="zh", help="语言代码（默认 zh，中文）")
    parser.add_argument("--no-vad", action="store_true", help="关闭 VAD 静音过滤")
    parser.add_argument("--frames", type=int, default=8, help="识图抽帧数（默认 8）")
    parser.add_argument("--at", default="", help="显式指定抽帧时间点，逗号分隔如 00:30,02:15,05:00（不指定则用 LLM 语义判断）")
    parser.add_argument("--no-llm", action="store_true", help="关闭大模型语义选帧，只用关键词启发式（默认 LLM 优先）")
    parser.add_argument("--outdir", type=Path, default=Path("outputs"), help="输出目录（默认 ./outputs）")
    args = parser.parse_args()

    outdir = args.outdir

    # 在线链接命令（yt-dlp 系 + B站专用 bili）
    if args.command in ("fetch", "subs", "download", "pipeline", "bili", "danmaku", "frames"):
        if not args.target.startswith(("http://", "https://")):
            print("[错误] 在线链路命令需要视频链接（http/https 开头）")
            sys.exit(1)
        if args.command == "fetch":
            fetch_meta(args.target, outdir)
        elif args.command == "subs":
            fetch_subs(args.target, outdir)
        elif args.command == "download":
            download_media(args.target, outdir, audio_only=False)
        elif args.command == "pipeline":
            link_pipeline(args.target, args.model, args.language, outdir)
        elif args.command == "bili":
            bili_pipeline(args.target, args.model, args.language, outdir)
        elif args.command == "danmaku":
            danmaku_download(args.target, outdir)
        elif args.command == "frames":
            frames_extract(args.target, outdir, frames=args.frames, at=args.at,
                           llm=not args.no_llm)
        print("\n✅ 完成")
        return

    # 本地视频命令
    video = Path(args.target)
    if not video.exists():
        print(f"[错误] 视频文件不存在: {video}")
        sys.exit(1)
    for sub in ("srt", "txt", "json"):
        (outdir / sub).mkdir(parents=True, exist_ok=True)

    if args.command == "probe":
        probe_video(video)
    elif args.command == "subtitle":
        extract_subtitle(video, args.track, outdir)
    elif args.command == "transcribe":
        transcribe_audio(video, args.model, args.language, outdir, vad=not args.no_vad)
    elif args.command == "auto":
        info = probe_video(video)
        if info["suggest_route"] == "subtitle":
            extract_subtitle(video, args.track, outdir)
        else:
            transcribe_audio(video, args.model, args.language, outdir, vad=not args.no_vad)
    print("\n✅ 完成")


if __name__ == "__main__":
    main()
