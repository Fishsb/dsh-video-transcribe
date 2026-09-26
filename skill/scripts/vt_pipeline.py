#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vt_pipeline.py —— video-transcribe 自有实现层·主链路（纯标准库，零硬编码本机路径）

设计约束（2026-09-25 实测基线）：
  · 只用 http.client / json / subprocess / shutil / argparse / re / pathlib 等标准库；
    不依赖 requests / bs4 / yt-dlp / faster_whisper（本机四者全无）。
  · 所有外部程序与外部服务地址一律「运行时探测 + 环境变量覆盖」，代码里不出现本机绝对路径。
  · 转写走宿主 ASR 服务（默认 http://127.0.0.1:3082/rpc，SenseVoice）；
    BBDown / yt-dlp / faster-whisper 只作为可选适配器，缺失不影响主路线。

环境变量（全部可选）：
  VT_ASR_URL     宿主 ASR RPC 地址，默认 http://127.0.0.1:3082/rpc
  VT_FFMPEG      ffmpeg 可执行文件全路径（默认探测 PATH）
  VT_FFPROBE     ffprobe 可执行文件全路径（默认探测 PATH）
  VT_OUTDIR      产物根目录，默认 ./output
  VT_PYTHON      写入元数据里的解释器路径（默认 sys.executable）
  VT_TIMEOUT_HTTP / VT_TIMEOUT_ASR  秒，默认 60 / 600
  VT_VISION_URL / VT_VISION_MODEL   本地视觉模型端点 / 模型名
  VT_VISION_THINK   `think` 开关：缺省 0（关思考，实测 3.5x）；1 开；omit = 完全不传该参数
  VT_VISION_RETRY  单帧调用次数上限（缺省 2，即失败重试一次）
  VT_VISION_MAX_SIDE  送模型的帧最长边像素（缺省 960）

子命令：
  probe       探测本机外部程序与 ASR 服务可用性（不联网、不写盘）
  meta        抓 B站元数据（view + tag + reply + 弹幕），产出 info.json
  danmaku     只抓弹幕，产出 弹幕时间线.txt / 弹幕.json
  audio       抓最低码率音轨 → ffmpeg 转 16k 单声道 wav
  transcribe  对音频调宿主 ASR 转写（长音频自动 ffmpeg 切片）
  frames      抽帧 + 本地视觉模型 OCR → 帧OCR原始结果.md（画面补充层）
  bili        一条龙：meta → audio → transcribe [→ frames]

产物默认落在 <VT_OUTDIR>/<bvid>/ 下；仅新增，绝不写 samples/。

2026-09-26 主链路改造（依据 docs/审查与优化方案-2026-09-26.md 第一/二档，逐条实测）：
  · 画面 OCR 默认传 `think:false`：同一帧实测 91.2s → 25.9s（3.5x），thinking 1629 字 → 0 字，
    识别内容无实质差异；content 为空而 thinking 非空时**显式抛错**（<<疑似思考模式吞输出>>），
    不再静默返回空串——静默失败会让空帧守卫假绿。
  · bili --frames [--interval N]：一条命令产出含画面层的全套产物；视频流与 audio.m4s 同源，
    已下过就不重下。
  · --resume：已有 info.json / audio.wav / 原始转写.txt / 已完成帧的 OCR 结果则跳过重做。
  · 每阶段打印进度：[3/6] 转写中… 预计 01:03。预计时间只是**提示**，不作任何判据。
  · 中间物（segs/ / audio.wav / video.m4s）默认清理，--keep-intermediate 保留。
    清理只针对本次运行实际产出的中间物，绝不遍历删除 output/ 下的历史产物。
"""
from __future__ import annotations

import argparse
import base64  # noqa: F401  (vision_ocr 编码图像用)
import gzip
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_ASR_URL = "http://127.0.0.1:3082/rpc"
DEFAULT_OUTDIR = "output"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BILI_REFERER = "https://www.bilibili.com/"

# ---------------------------------------------------------------- 运行时定位


def env_path(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def find_tool(name: str, env_var: str = "") -> str | None:
    """三层探测：环境变量覆盖 → PATH(shutil.which) → 相邻候选目录。

    返回 '' 之外的绝对路径；找不到返回 None（不抛错，由调用方决定降级）。
    """
    if env_var:
        p = env_path(env_var)
        if p and Path(p).exists():
            return str(Path(p))
    hit = shutil.which(name)
    if hit:
        return hit
    exe = name + ".exe" if os.name == "nt" and not name.lower().endswith(".exe") else name
    cands = []
    if env_path("VT_TOOL_DIR"):
        cands.append(Path(env_path("VT_TOOL_DIR")))
    for raw in env_path("PATH").split(os.pathsep):
        if raw:
            cands.append(Path(raw))
    for d in cands:
        c = d / exe
        try:
            if c.exists():
                return str(c)
        except OSError:
            continue
    return None


def ffmpeg_path() -> str | None:
    return find_tool("ffmpeg", "VT_FFMPEG")


def ffprobe_path() -> str | None:
    return find_tool("ffprobe", "VT_FFPROBE")


def asr_url() -> str:
    return env_path("VT_ASR_URL") or DEFAULT_ASR_URL


def out_root() -> Path:
    return Path(env_path("VT_OUTDIR") or DEFAULT_OUTDIR)


def timeout_http() -> float:
    return float(env_path("VT_TIMEOUT_HTTP") or 60)


def timeout_asr() -> float:
    return float(env_path("VT_TIMEOUT_ASR") or 600)


def write_utf8(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    print(f"  写出: {path}")


def run_cmd(cmd: list, timeout: float = 600) -> subprocess.CompletedProcess:
    """执行外部命令；失败时打印完整命令与 stderr 尾部后退出。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except FileNotFoundError:
        print(f"[错误] 命令不存在: {cmd[0]}")
        sys.exit(1)
    if r.returncode != 0:
        print(f"[错误] 命令失败(exit {r.returncode}): {' '.join(str(c) for c in cmd)}")
        if r.stderr:
            print(r.stderr[-2000:])
        sys.exit(1)
    return r


# ---------------------------------------------------------------- B站 HTTP（标准库，无第三方）

BILI_HOST = "api.bilibili.com"


def _decode_body(headers, raw: bytes) -> str:
    """B站接口的 Content-Encoding 声明与实体并不总是一致（实测 list.so 会声明 deflate 却发明文）。

    因此不按声明解码，而是按「候选顺序逐个试、取第一个能直接解码出内容的结果」：
    明文 → gzip → zlib(deflate) → raw deflate。全部失败才抛出可读错误。
    """
    import zlib
    candidates = [("plain", raw)]
    enc = (headers.get("content-encoding") or "").lower()
    decompressors = [
        ("gzip", lambda b: gzip.decompress(b)),
        ("deflate-zlib", lambda b: zlib.decompress(b)),
        ("deflate-raw", lambda b: zlib.decompress(b, -zlib.MAX_WBITS)),
    ]
    if "gzip" in enc:
        decompressors.sort(key=lambda x: 0 if x[0] == "gzip" else 1)
    candidates += decompressors
    last = None
    for name, fn in candidates:
        try:
            data = fn(raw) if name != "plain" else raw
            return data.decode("utf-8")
        except Exception as exc:  # 解码失败继续试下一种
            last = f"{name}: {exc}"
    raise RuntimeError(f"响应解码失败（content-encoding={enc or '未声明'}）: {last}")


def bili_get(path_with_query: str, timeout: float | None = None) -> str:
    """GET api.bilibili.com，返回解码后的文本。评论区/弹幕/元数据共用。"""
    conn = http.client.HTTPSConnection(BILI_HOST, timeout=timeout or timeout_http())
    try:
        conn.request("GET", path_with_query, headers={
            "User-Agent": UA,
            "Referer": BILI_REFERER,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
        })
        resp = conn.getresponse()
        raw = resp.read()
        text = _decode_body(resp.headers, raw)
        if resp.status != 200:
            raise RuntimeError(f"B站接口 HTTP {resp.status}: {path_with_query} -> {text[:200]}")
        return text
    finally:
        conn.close()


def bili_get_json(path_with_query: str) -> dict:
    text = bili_get(path_with_query)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"B站接口返回非 JSON: {text[:200]}") from exc


def parse_bvid(arg: str) -> str:
    m = re.search(r"(BV[0-9A-Za-z]{10})", arg or "")
    if not m:
        raise SystemExit(f"[错误] 无法从参数中解析 BV 号: {arg}")
    return m.group(1)


def fetch_view(bvid: str) -> dict:
    j = bili_get_json(f"/x/web-interface/view?bvid={bvid}")
    if j.get("code") != 0:
        raise SystemExit(f"[错误] view 接口 code={j.get('code')} message={j.get('message')} "
                         f"（视频不存在/被删/地区限制）")
    return j["data"]


def fetch_tags(bvid: str) -> tuple[list, str]:
    """返回 (tag 名列表, 失败原因或 '')。tag 接口失败不致命。"""
    try:
        j = bili_get_json(f"/x/tag/archive/tags?bvid={bvid}")
    except Exception as exc:  # 单接口失败不应打断整条链路
        return [], str(exc)
    if j.get("code") != 0:
        return [], f"tag code={j.get('code')} message={j.get('message')}"
    return [t.get("tag_name", "") for t in (j.get("data") or [])], ""


def fetch_replies(aid: int, ps: int = 20) -> tuple[list, str]:
    """热评 + 一级回复。无登录也可用（实测 oid=aid, sort=2）。"""
    try:
        j = bili_get_json(f"/x/v2/reply?type=1&oid={aid}&sort=2&ps={ps}&pn=1")
    except Exception as exc:
        return [], str(exc)
    if j.get("code") != 0:
        return [], f"reply code={j.get('code')} message={j.get('message')}"
    out = []
    for r in (j.get("data") or {}).get("replies") or []:
        out.append({
            "user": (r.get("member") or {}).get("uname", ""),
            "like": r.get("like", 0),
            "msg": r.get("content", {}).get("message", ""),
            "replies": [
                {"user": (rr.get("member") or {}).get("uname", ""),
                 "like": rr.get("like", 0),
                 "msg": rr.get("content", {}).get("message", "")}
                for rr in (r.get("replies") or [])
            ],
        })
    return out, ""


def fetch_danmaku(cid: int) -> tuple[list, str]:
    """弹幕 XML 解析。格式：<d p="时间秒,模式,字号,颜色,时间戳,池,用户hash,行号">文本</d>

    返回 (弹幕列表, 失败原因或 '')；空弹幕是正常结果（本片应为 0 条）。
    """
    try:
        text = bili_get(f"/x/v1/dm/list.so?oid={cid}")
    except Exception as exc:
        return [], str(exc)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return [], f"弹幕 XML 解析失败: {exc}"
    items = []
    for d in root.findall("d"):
        p = (d.get("p") or "").split(",")
        if not p or not p[0]:
            continue
        try:
            t = float(p[0])
        except ValueError:
            continue
        items.append({"time": round(t, 1), "text": (d.text or "").strip()})
    items.sort(key=lambda x: x["time"])
    return items, ""


def fetch_playurl(bvid: str, cid: int) -> dict:
    """dash 音轨列表（fnval=16 开 dash）。返回原始 data。"""
    q = (f"/x/player/playurl?bvid={bvid}&cid={cid}&fnval=16&fnver=0&fourk=0")
    j = bili_get_json(q)
    if j.get("code") != 0:
        raise SystemExit(f"[错误] playurl code={j.get('code')} message={j.get('message')}")
    return j["data"]


def dash_of(payload: dict) -> dict:
    """兼容两种入参：playurl 的 data 整体（含 "dash" 子对象）或 dash 子对象本身。"""
    d = payload or {}
    inner = d.get("dash")
    return inner if isinstance(inner, dict) else d


def pick_lowest_audio(payload: dict) -> dict:
    """取最低码率音轨；dash 无普通音轨时退 dolby / flac。"""
    d = dash_of(payload)
    audios = list(d.get("audio") or [])
    if not audios:
        for key in ("dolby", "flac"):
            alt = d.get(key)
            if isinstance(alt, dict) and alt.get("audio"):
                audios.append(alt["audio"])
    if not audios:
        raise SystemExit(
            "[错误] playurl 未返回可下载音轨（可能需要登录 cookie 或视频无音轨）；"
            f"dash 顶层键={sorted(d.keys())}")
    return sorted(audios, key=lambda a: a.get("bandwidth", 0))[0]


# ---------------------------------------------------------------- 音频下载 / 转码


def download_url_to_file(url: str, dest: Path, referer: str = BILI_REFERER) -> int:
    """单连接顺序下载（B站音频体积小，够用；不引入第三方下载器）。"""
    u = urllib.parse.urlsplit(url)
    scheme = u.scheme or "https"
    host = u.netloc
    path = u.path + (("?" + u.query) if u.query else "")
    conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(host, timeout=timeout_http())
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        conn.request("GET", path, headers={
            "User-Agent": UA,
            "Referer": referer,
            "Accept": "*/*",
            "Connection": "close",
        })
        resp = conn.getresponse()
        if resp.status not in (200, 206):
            raise RuntimeError(f"音频下载失败 HTTP {resp.status}（{host}）")
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
    finally:
        conn.close()
    return total


def download_audio(dash: dict, dest: Path) -> Path:
    """优先 baseUrl，失败依次试 backupUrl。"""
    audio = pick_lowest_audio(dash)
    urls = [audio.get("baseUrl")] + list(audio.get("backupUrl") or [])
    last_err = None
    for i, url in enumerate([u for u in urls if u]):
        try:
            size = download_url_to_file(url, dest)
            if size <= 0:
                raise RuntimeError("下载 0 字节")
            print(f"  音频已下载: {dest} ({size / 1048576:.2f} MB, bandwidth={audio.get('bandwidth')})")
            return dest
        except Exception as exc:
            last_err = exc
            print(f"  [警告] 第 {i + 1} 个音轨地址失败: {exc}")
    raise SystemExit(f"[错误] 所有音轨地址均失败: {last_err}")


def to_wav16k(src: Path, dest: Path) -> Path:
    """必须用 ffmpeg 转码：手写 WAV 头会让宿主 sherpa-onnx 解析失败。"""
    ff = ffmpeg_path()
    if not ff:
        raise SystemExit("[错误] 找不到 ffmpeg。请安装并加入 PATH，或用 VT_FFMPEG 指定全路径。")
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_cmd([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)])
    print(f"  已转 16k 单声道 wav: {dest} ({dest.stat().st_size} 字节)")
    return dest


def probe_duration(path: Path) -> float:
    """优先 ffprobe(JSON)，回退解析 ffmpeg -i 的 stderr；都拿不到返回 0。"""
    fp = ffprobe_path()
    if fp:
        try:
            r = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=nw=1:nk=1", str(path)],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0 and r.stdout.strip():
                return float(r.stdout.strip())
        except Exception:
            pass
    ff = ffmpeg_path()
    if not ff:
        return 0.0
    try:
        r = subprocess.run([ff, "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0


# ---------------------------------------------------------------- 宿主 ASR 转写


def asr_health(url: str | None = None) -> dict:
    """GET /health（由 rpc 地址推导）。返回 {} 表示不可达。"""
    u = urllib.parse.urlsplit(url or asr_url())
    try:
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
        conn.request("GET", "/health", headers={"User-Agent": UA, "Connection": "close"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        conn.close()
        return json.loads(body)
    except Exception:
        return {}


def asr_transcribe(wav: Path, language: str = "zh") -> str:
    """POST {VT_ASR_URL}，body 见模块 docstring。

    契约（2026-09-25 实测）：
      成功 HTTP 200 {"ok":true,"text":"…"}；
      失败 HTTP 200 {"ok":false,"code":"ASR_LOCAL_FAILED"|"BAD_AUDIO", …}；
      缺 data: 前缀 → BAD_AUDIO；非 PCM 数据 → ASR_LOCAL_FAILED。
    失败必须显式抛错，禁止把空文本当成功。
    """
    data = wav.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    body = json.dumps({
        "method": "transcribe",
        "args": {"audioBase64": "data:audio/wav;base64," + b64, "language": language},
    }).encode("utf-8")
    u = urllib.parse.urlsplit(asr_url())
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout_asr())
    t0 = time.time()
    try:
        conn.request("POST", u.path or "/rpc", body=body,
                     headers={"Content-Type": "application/json; charset=utf-8",
                              "Content-Length": str(len(body)),
                              "Connection": "close"})
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, socket.timeout) as exc:
        raise SystemExit(f"[错误] 连不上宿主 ASR {asr_url()}: {exc}\\n"
                         f"       请确认 ASR 服务在跑（GET /health），或用 VT_ASR_URL 覆盖地址。")
    finally:
        conn.close()
    elapsed = time.time() - t0
    text = raw.decode("utf-8", "replace")
    if resp.status != 200:
        raise SystemExit(f"[错误] ASR HTTP {resp.status}: {text[:300]}")
    try:
        j = json.loads(text)
    except json.JSONDecodeError:
        raise SystemExit(f"[错误] ASR 返回非 JSON: {text[:300]}")
    if not j.get("ok"):
        raise SystemExit(f"[错误] ASR 失败 code={j.get('code')} message={j.get('message')}")
    print(f"  ASR ok ({elapsed:.1f}s, {len(data) / 1048576:.2f} MB 音频)")
    return j.get("text", "")


def segment_wav(wav: Path, seg_dir: Path, seconds: int) -> list:
    """ffmpeg -f segment 切片，返回按序号排序的 wav 列表。"""
    ff = ffmpeg_path()
    seg_dir.mkdir(parents=True, exist_ok=True)
    for old in seg_dir.glob("seg_*.wav"):
        old.unlink()
    run_cmd([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav),
             "-f", "segment", "-segment_time", str(seconds), "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(seg_dir / "seg_%04d.wav")])
    segs = sorted(seg_dir.glob("seg_*.wav"))
    if not segs:
        raise SystemExit("[错误] ffmpeg 切片未产出文件")
    return segs


def transcribe_wav(wav: Path, language: str, segment_sec: int, workdir: Path) -> dict:
    """长音频自动切片逐片转写。返回 {text, segments:[{start,end,text}], mode}。"""
    dur = probe_duration(wav)
    if dur and dur > segment_sec:
        segs = segment_wav(wav, workdir / "segs", segment_sec)
        print(f"  时长 {dur:.0f}s > {segment_sec}s，切成 {len(segs)} 片逐片转写")
        collected, parts = [], []
        for i, s in enumerate(segs):
            t = asr_transcribe(s, language)
            start = i * segment_sec
            collected.append({"start": start, "end": round(start + segment_sec, 1), "text": t})
            parts.append(t)
            print(f"    片 {i + 1}/{len(segs)} ok（累计 {sum(len(p) for p in parts)} 字）")
        return {"text": "".join(parts), "segments": collected, "mode": "segmented",
                "duration_sec": round(dur, 1), "segment_sec": segment_sec}
    text = asr_transcribe(wav, language)
    return {"text": text, "segments": [], "mode": "single",
            "duration_sec": round(dur, 1) if dur else 0.0}


def seg_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------- 元数据装配 / 产物


def build_info(bvid: str) -> dict:
    """抓 B站元数据。评论/弹幕为空时显式写 0，不静默缺字段。"""
    view = fetch_view(bvid)
    aid, cid = view.get("aid", 0), view.get("cid", 0)
    tags, tag_err = fetch_tags(bvid)
    comments, reply_err = fetch_replies(aid)
    danmaku, dm_err = fetch_danmaku(cid)
    stat = view.get("stat") or {}
    notes = []
    if tag_err:
        notes.append(f"tag 接口不可用: {tag_err}")
    if reply_err:
        notes.append(f"reply 接口不可用: {reply_err}")
    if dm_err:
        notes.append(f"弹幕接口不可用: {dm_err}")
    info = {
        "source": f"https://www.bilibili.com/video/{bvid}",
        "bvid": bvid,
        "aid": aid,
        "cid": cid,
        "title": view.get("title", ""),
        "desc": view.get("desc", ""),
        "duration_sec": view.get("duration", 0),
        "pubdate": view.get("pubdate", 0),
        "tname": view.get("tname", ""),
        "tags": tags,
        "owner": {"mid": (view.get("owner") or {}).get("mid", 0),
                  "name": (view.get("owner") or {}).get("name", "")},
        "stat": stat,
        "subtitle_available": 1 if ((view.get("subtitle") or {}).get("allow_submit")) else 0,
        "comments_count": len(comments),
        "comments": comments,
        "danmaku_count": len(danmaku),
        "danmaku": danmaku,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "pipeline": {
            "engine": "host-asr:sense-voice",
            "asr_url": asr_url(),
            "python": env_path("VT_PYTHON") or sys.executable,
            "ffmpeg": ffmpeg_path() or "",
            "ffprobe": ffprobe_path() or "",
            "notes": notes,
        },
    }
    return info


def danmaku_timeline(danmaku: list) -> str:
    return "".join(f"[{seg_ts(d['time'])}] {d['text']}\n" for d in danmaku)


def write_transcript(outdir: Path, tr: dict, language: str) -> None:
    write_utf8(outdir / "原始转写.txt", tr["text"] + "\n")
    if tr.get("segments"):
        lines = [f"# 原始转写（分段，每段 {tr.get('segment_sec')} 秒，共 {len(tr['segments'])} 段）\n"]
        for s in tr["segments"]:
            lines.append(f"\n## [{seg_ts(s['start'])} - {seg_ts(s['end'])}]\n\n{s['text']}\n")
        write_utf8(outdir / "原始转写_分段.md", "".join(lines))
    write_utf8(outdir / "转写元信息.json", json.dumps({
        "language": language, "mode": tr.get("mode"), "duration_sec": tr.get("duration_sec"),
        "chars": len(tr["text"]), "asr_url": asr_url(), "segments": len(tr.get("segments") or []),
    }, ensure_ascii=False, indent=2) + "\n")


# ---------------------------------------------------------------- 子命令


def cmd_probe(args) -> int:
    """不联网、不写盘，只报告本机可用性。"""
    ff, fp = ffmpeg_path(), ffprobe_path()
    h = asr_health()
    rows = [
        ("python", sys.executable),
        ("ffmpeg", ff or "（缺失：可用 VT_FFMPEG 指定全路径）"),
        ("ffprobe", fp or "（缺失：时长回退解析 ffmpeg -i）"),
        ("BBDown（可选适配器）", find_tool("BBDown", "VT_BBDOWN") or "（未安装，主路线不需要）"),
        ("yt-dlp（可选适配器）", find_tool("yt-dlp") or "（未安装，主路线不需要）"),
        ("faster-whisper（可选适配器）", "见 transcribe 子命令内的显式探测"),
        ("宿主 ASR", asr_url()),
        ("ASR /health", json.dumps(h, ensure_ascii=False) if h else "（不可达）"),
        ("视觉模型端点", f"{vision_url()}  模型 {vision_model()}"),
        ("视觉 OCR 开关", f"think={vision_think()}（缺省 0=关思考；实测 91.2s→25.9s）"),
        ("送模型最长边", f"{vision_max_side()} px（缩图若劣化识别，用 VT_VISION_MAX_SIDE 调大）"),
        ("产物根目录", str(out_root().resolve())),
    ]
    print("== vt_pipeline 环境探测 ==")
    for k, v in rows:
        print(f"  {k:26s} : {v}")
    print("\n== 画面层（可选，与主路线分开判定）==")
    if not ff:
        print("  [警告] 缺 ffmpeg ⇒ frames / bili --frames 不可用")
    if args.vision:
        t0 = time.time()
        proved, note = vision_think_supported()
        print(f"  思考模式探测（{time.time() - t0:.1f}s）: {note}")
        print(f"  结论: {'think 开关被服务端接受' if proved else '未证实——不得据此认为 think:false 已生效'}")
    else:
        print("  思考模式探测: 未跑（加 --vision 联网探查：服务端认不认 think、该模型会不会")
        print("                产生思考输出。**未跑即未证实**，不写成「已生效」）")
    ok = bool(ff) and bool(h.get("ok"))
    print(f"\n主路线可用性: {'可用（ffmpeg + 宿主 ASR 均就绪）' if ok else '不可用（见上）'}")
    return 0 if ok else 2


def cmd_meta(args) -> int:
    bvid = parse_bvid(args.target)
    info = build_info(bvid)
    outdir = out_root() / bvid
    outdir.mkdir(parents=True, exist_ok=True)
    write_utf8(outdir / "info.json", json.dumps(info, ensure_ascii=False, indent=2) + "\n")
    write_utf8(outdir / "弹幕时间线.txt", danmaku_timeline(info["danmaku"]))
    print(f"  标题: {info['title']}")
    print(f"  弹幕: danmaku_count={info['danmaku_count']}  评论: comments_count={info['comments_count']}"
          f"  标签: {len(info['tags'])}")
    if info["pipeline"]["notes"]:
        for n in info["pipeline"]["notes"]:
            print(f"  [警告] {n}")
    return 0


def cmd_danmaku(args) -> int:
    bvid = parse_bvid(args.target)
    view = fetch_view(bvid)
    danmaku, err = fetch_danmaku(view["cid"])
    outdir = out_root() / bvid
    write_utf8(outdir / "弹幕时间线.txt", danmaku_timeline(danmaku))
    write_utf8(outdir / "弹幕.json", json.dumps({"bvid": bvid, "cid": view["cid"],
                                                 "count": len(danmaku), "danmaku": danmaku,
                                                 "error": err}, ensure_ascii=False, indent=2) + "\n")
    print(f"  danmaku_count={len(danmaku)}" + (f"（接口报错: {err}）" if err else ""))
    return 0


def cmd_audio(args) -> int:
    bvid = parse_bvid(args.target)
    view = fetch_view(bvid)
    dash = fetch_playurl(bvid, view["cid"])
    outdir = out_root() / bvid
    raw = outdir / "audio.m4s"
    download_audio(dash, raw)
    return 0 if to_wav16k(raw, outdir / "audio.wav") else 1


def cmd_transcribe(args) -> int:
    if args.target and Path(args.target).exists():
        src = Path(args.target)
        outdir = Path(args.outdir) if args.outdir else out_root() / src.stem
    else:
        bvid = parse_bvid(args.target)
        view = fetch_view(bvid)
        outdir = out_root() / bvid
        raw = outdir / "audio.m4s"
        if not raw.exists():
            download_audio(fetch_playurl(bvid, view["cid"]), raw)
        src = to_wav16k(raw, outdir / "audio.wav")
    wav = src if src.suffix.lower() == ".wav" else to_wav16k(src, outdir / "audio.wav")
    tr = transcribe_wav(wav, args.language, args.segment, outdir)
    write_transcript(outdir, tr, args.language)
    print(f"  转写完成: {len(tr['text'])} 字 → {outdir}")
    return 0


# ---------------------------------------------------------------- 画面 OCR（第三层信息：画面补充）

DEFAULT_VISION_URL = "http://127.0.0.1:11434/api/chat"
DEFAULT_VISION_MODEL = "qwen3.5:9b"
THINKING_SWALLOW_MARK = "<<疑似思考模式吞输出>>"


def vision_url() -> str:
    return env_path("VT_VISION_URL") or DEFAULT_VISION_URL


def vision_model() -> str:
    return env_path("VT_VISION_MODEL") or DEFAULT_VISION_MODEL


def vision_think() -> str:
    """think 开关取值：'0' 关思考（默认）/ '1' 开 / 'omit' 完全不传该参数。

    为什么默认「关」——实测（2026-09-26，同一帧 f_001.jpg，qwen3.5:9b，见
    docs/审查与优化方案-2026-09-26.md §二）：
      不传该参数：91.2s / thinking 1629 字 / content 曾实测为空串；
      传 false ：25.9s（3.5x）/ thinking 0 字 / 识别内容无实质差异（均 11 行）。
    'omit' 是留给反证与排障的开关：它会退回「老行为」，用来验证兜底真的会报错
    （VT_VISION_THINK=omit 跑一次，必须看到 <<疑似思考模式吞输出>> 或空输出异常）。
    """
    raw = (env_path("VT_VISION_THINK") or "0").strip().lower()
    if raw in ("omit", "none", "unset", "-"):
        return "omit"
    return "1" if raw in ("1", "true", "yes", "on") else "0"


def vision_retries() -> int:
    """单帧调用次数上限（默认 2：失败重试一次）。"""
    try:
        return max(1, int(env_path("VT_VISION_RETRY") or 2))
    except ValueError:
        return 2


def vision_max_side() -> int:
    """送模型的帧最长边上限（默认 960；见 frames_image_bytes 的说明）。"""
    try:
        return max(320, int(env_path("VT_VISION_MAX_SIDE") or 960))
    except ValueError:
        return 960


class VisionCallError(RuntimeError):
    """画面 OCR 单帧调用失败基类。**失败必须可见**，不得静默降级为空文本。"""


class VisionUnavailable(VisionCallError):
    """连不上 / HTTP 非 200 / 返回非 JSON / 返回体带 error —— 可重试。"""


class VisionThinkingOnly(VisionCallError):
    """content 为空但 thinking 非空：思考模式把文字吞进了 thinking（实测发生过的静默失效）。

    这是本轮修复的那一类：老代码 `return (content or "").strip()` 会把它变成空串，
    空帧守卫只在空帧率 > 1/3 时才告警 ⇒ 空帧率恰好在阈值内时**连告警都没有**。
    """


class VisionEmptyOutput(VisionCallError):
    """content 与 thinking 都为空：模型确实没输出（或响应形态变了）。

    ⚠ 这一类**不是**「画面无文字」。宁可让这一帧显式失败，也不许把接口异常
    写成「此处无文字」——那正是 check_empty_ratio 文档串里警告过的假绿来源。
    """


def _vision_post(body: bytes, timeout: float) -> tuple[dict, float]:
    """POST 视觉模型，返回 (响应 JSON, 耗时秒)。失败一律抛 VisionUnavailable（可重试）。"""
    u = urllib.parse.urlsplit(vision_url())
    conn_cls = http.client.HTTPSConnection if (u.scheme or "http") == "https" else http.client.HTTPConnection
    host = u.netloc or "127.0.0.1:11434"
    path = u.path or "/api/chat"
    conn = conn_cls(host, timeout=timeout)
    t0 = time.time()
    try:
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, socket.timeout) as exc:
        raise VisionUnavailable(f"连不上视觉模型 {vision_url()}: {exc}") from exc
    finally:
        conn.close()
    elapsed = time.time() - t0
    if resp.status != 200:
        raise VisionUnavailable(f"视觉模型 HTTP {resp.status}: {raw[:200]!r}")
    try:
        j = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise VisionUnavailable(f"视觉模型返回非 JSON: {raw[:200]!r}") from exc
    err = j.get("error")
    if isinstance(err, str) and err:
        raise VisionUnavailable(f"视觉模型返回 error: {err[:200]}")
    return j, elapsed


def frames_image_bytes(img: Path) -> tuple[bytes, int, int]:
    """返回送模型的 (图片字节, 原图最长边, 送模型最长边)。超过 VT_VISION_MAX_SIDE 时用 ffmpeg 缩小。

    为什么缩：帧图是 1280 宽的整屏截图，视觉模型对它的处理时间与像素数近似成正比；
    而 OCR 只需要看清字——1600x900 的信息量对 1280x720 的截图并无增益。缩图属**输入侧**改动，
    判据只能是「识别结果不劣化」：先用 --max-side 实测对比，劣化就退回。
    任何一步探测/缩放失败都**回退为原图**（宁慢不错）。
    """
    mx = vision_max_side()
    w = h = 0
    fp = ffprobe_path()
    if fp:
        try:
            r = subprocess.run([fp, "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(img)],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and "x" in (r.stdout or ""):
                w, h = (int(x) for x in r.stdout.strip().splitlines()[0].split("x")[:2])
        except Exception:
            w = h = 0
    if not w or not h or max(w, h) <= mx:
        return img.read_bytes(), max(w, h), max(w, h)
    ff = ffmpeg_path()
    if not ff:
        return img.read_bytes(), max(w, h), max(w, h)
    tmp = img.with_name(img.stem + ".scaled.jpg")
    try:
        run_cmd([ff, "-y", "-loglevel", "error", "-i", str(img),
                 "-vf", f"scale='if(gt(iw,ih),{mx},-2)':'if(gt(iw,ih),-2,{mx})'",
                 "-q:v", "3", str(tmp)])
        data = tmp.read_bytes()
    except Exception as exc:
        print(f"    [警告] 缩图失败，回退原图: {exc}")
        return img.read_bytes(), max(w, h), max(w, h)
    finally:
        tmp.unlink(missing_ok=True)
    return data, max(w, h), mx


def vision_think_supported() -> tuple[bool, str]:
    """探测「服务端认不认 think 开关、该模型会不会产生思考输出」。

    判据不是版本号，而是**实际响应**：显式传 think:true 发一条极小的文本消息，
    看响应里的 thinking 字段有没有内容。
      · thinking 非空 ⇒ 服务端认这个开关，且该模型会思考（不传 = 有被吞输出的风险）；
      · thinking 空   ⇒ 服务端不认该开关，或该模型本就不产生思考输出 —— 两种情况都
                        证不出「think:false 生效」，如实报「未证实」，不许写成「已生效」。
    探测本身失败（服务不在）也算未证实。返回 (是否证实, 说明)。
    """
    payload = {
        "model": vision_model(),
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
        "options": {"temperature": 0},
        "think": True,
    }
    try:
        j, _ = _vision_post(json.dumps(payload).encode("utf-8"), timeout=min(60.0, timeout_asr()))
    except VisionCallError as exc:
        return False, f"探测失败（{type(exc).__name__}: {exc}）"
    thinking = ((j.get("message") or {}).get("thinking") or "").strip()
    if thinking:
        return True, f"think:true 时 thinking 有 {len(thinking)} 字 ⇒ 服务端认该开关，该模型会思考"
    return False, "think:true 时 thinking 仍为空 ⇒ 该服务端不认该开关，或该模型不产生思考输出（think:false 生效与否无法证实）"


def pick_lowest_video(payload: dict) -> dict:
    """取最低码率视频流（画面 OCR 只需看得清文字，不追清晰度以省流量）。"""
    d = dash_of(payload)
    vids = list(d.get("video") or [])
    if not vids:
        raise SystemExit(
            "[错误] playurl 未返回可下载视频流（可能需要登录 cookie）；"
            f"dash 顶层键={sorted(d.keys())}")
    return sorted(vids, key=lambda v: v.get("bandwidth", 0))[0]


def download_video(dash: dict, dest: Path) -> Path:
    """下载视频流；主地址失败退备地址。"""
    v = pick_lowest_video(dash)
    urls = [v.get("baseUrl")] + list(v.get("backupUrl") or [])
    last = None
    for u in urls:
        if not u:
            continue
        try:
            n = download_url_to_file(u, dest)
            print(f"  视频已下载: {dest} ({n / 1048576:.2f} MB, {v.get('height')}P)")
            return dest
        except Exception as exc:  # 换备用地址重试
            last = exc
            print(f"  主地址失败，尝试备用: {exc}")
    raise SystemExit(f"[错误] 视频流全部地址下载失败: {last}")


def extract_frames(video: Path, frame_dir: Path, interval: int, at: str = "") -> list:
    """抽帧。默认按固定间隔；--at mm:ss,mm:ss 时按显式时间点抽（更省且可控）。

    返回 [(秒, 帧文件 Path), ...]，按时间升序。
    """
    ff = ffmpeg_path()
    if not ff:
        raise SystemExit("[错误] 找不到 ffmpeg（用 VT_FFMPEG 指定）")
    frame_dir.mkdir(parents=True, exist_ok=True)
    for old in frame_dir.glob("f_*.jpg"):
        old.unlink()

    picks: list = []
    if at:
        for i, ts in enumerate([s.strip() for s in at.split(",") if s.strip()], 1):
            secs = parse_mmss(ts)
            dest = frame_dir / f"f_{i:03d}.jpg"
            run_cmd([ff, "-y", "-loglevel", "error", "-ss", str(secs), "-i", str(video),
                     "-frames:v", "1", "-vf", "scale=1280:-1", "-q:v", "3", str(dest)])
            picks.append((secs, dest))
    else:
        run_cmd([ff, "-y", "-loglevel", "error", "-i", str(video),
                 "-vf", f"fps=1/{interval},scale=1280:-1", "-q:v", "3",
                 str(frame_dir / "f_%03d.jpg")])
        for i, p in enumerate(sorted(frame_dir.glob("f_*.jpg"))):
            picks.append((i * interval, p))
    print(f"  抽帧 {len(picks)} 张（{'显式时间点' if at else f'间隔 {interval}s'}）")
    return picks


def parse_mmss(s: str) -> float:
    parts = [x for x in str(s).strip().split(":") if x != ""]
    try:
        nums = [float(x) for x in parts]
    except ValueError:
        raise SystemExit(f"[错误] 无法解析时间点: {s}")
    secs = 0.0
    for n in nums:
        secs = secs * 60 + n
    return secs


def vision_ocr(img: Path, prompt: str, think: str | None = None) -> tuple[str, dict]:
    """调本地视觉模型做 OCR。返回 (文字, 本次调用元信息)；失败一律抛 VisionCallError。

    2026-09-26 修复的静默失效（本案的根因）：
      Ollama 对带思考能力的视觉模型默认可能走思考模式，把文字产在 message.thinking，
      message.content 于是为空**而不报错**。老实现直接取 content，于是把「模型答了但答在
      另一个字段」变成「画面无文字」——空帧守卫只在空帧率 > 1/3 时才告警，
      比例恰好在阈值内时连告警都不触发（失败不可观测）。
    现在三态分明：
      有 content            → 正常返回；
      空 content + 非空 thinking → VisionThinkingOnly（<<疑似思考模式吞输出>>）；
      两者都空               → VisionEmptyOutput。
    后两者都是**这一帧的失败**，不是「此处无文字」；重试一次仍失败就如实落到产物里。
    """
    use_think = vision_think() if think is None else think
    data, side_src, side_dst = frames_image_bytes(img)
    payload = {
        "model": vision_model(),
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(data).decode()]}],
        "stream": False,
        "options": {"temperature": 0},
    }
    if use_think != "omit":
        payload["think"] = (use_think == "1")
    body = json.dumps(payload).encode("utf-8")

    tries = vision_retries()
    last: VisionCallError | None = None
    for attempt in range(1, tries + 1):
        try:
            j, elapsed = _vision_post(body, timeout=timeout_asr())
        except VisionCallError as exc:
            last = exc
            print(f"    [重试 {attempt}/{tries}] {type(exc).__name__}: {exc}")
            if attempt < tries:
                continue
            raise
        msg = j.get("message") or {}
        content = (msg.get("content") or "").strip()
        thinking = (msg.get("thinking") or "").strip()
        meta = {
            "elapsed_sec": round(elapsed, 1),
            "content_chars": len(content),
            "thinking_chars": len(thinking),
            "eval_count": j.get("eval_count", 0),
            "think_param": use_think,
            "side_src": side_src,
            "side_sent": side_dst,
        }
        if content:
            return content, meta
        if thinking:
            raise VisionThinkingOnly(
                f"{THINKING_SWALLOW_MARK} content 为空但 thinking 有 {len(thinking)} 字 "
                f"(think={use_think}, {elapsed:.1f}s)；该帧文字被思考模式吞掉，未取得 OCR 结果")
        raise VisionEmptyOutput(
            f"模型输出为空（content 与 thinking 均空，think={use_think}, {elapsed:.1f}s）—— "
            f"这是调用异常而非「画面无文字」")
    raise last or VisionCallError("视觉模型调用失败")


OCR_PROMPT = ("只输出这张画面中可见的文字，逐行列出，不要描述、不要解释、不要补充。"
              "没有文字就只回复：无")
NO_TEXT_HINTS = ("无", "none", "None", "")
FRAMES_CACHE_NAME = "帧OCR缓存.json"


# ---------------------------------------------------------------- 进度 / 体积 / 续跑


def fmt_mmss(seconds: float) -> str:
    "秒 → mm:ss（进度显示用；预计值只是提示，不作判据）。"
    total = max(0, int(round(seconds)))
    m, s = divmod(total, 60)
    return f"{m:02d}:{s:02d}"


def human_size(n: int) -> str:
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n} B"


class Progress:
    """阶段进度：[3/6] 转写中… 01:12 / 预计 00:58。

    每条阶段开始时打印一次、结束时补一行「完成（耗时 mm:ss）」。
    预计值只是**提示**：预估不准不影响任何判定，也不写进产物——写进产物的只有真实耗时。
    """

    def __init__(self, total: int):
        self.total = max(1, total)
        self.step = 0
        self.t0 = time.time()

    def start(self, label: str, eta_sec: float | None = None) -> float:
        self.step += 1
        eta = f" / 预计 {fmt_mmss(eta_sec)}" if eta_sec else ""
        print(f"[{self.step}/{self.total}] {label}…{eta}", flush=True)
        return time.time()

    def done(self, label: str, t_start: float, note: str = "") -> None:
        tail = f"  {note}" if note else ""
        print(f"[{self.step}/{self.total}] {label} 完成（{fmt_mmss(time.time() - t_start)}）{tail}",
              flush=True)

    def skip(self, label: str, note: str = "") -> None:
        self.step += 1
        tail = f"  {note}" if note else ""
        print(f"[{self.step}/{self.total}] {label}：跳过（{tail.strip() or "-"}）", flush=True)


def image_fingerprint(img: Path) -> str:
    "帧图指纹：文件名 + 体积。同名同体积即认为同一帧（抽帧参数变了体积必然变）。"
    try:
        st = img.stat()
    except OSError:
        return "missing"
    return f"{img.name}:{st.st_size}"


def frames_cache_key(img: Path, model: str, prompt: str) -> str:
    """帧 OCR 结果的确定性键：帧图指纹 + 模型 + 提示词 + 送模型的最长边。

    任何一项变了都必须重跑（换了模型却复用旧结果 = 拿旧结论冒充新证据）。
    **刻意不含 think 开关**：think 只影响速度与输出落点，不该改变识别内容；
    把它放进来会掩盖「同一帧两种 think 取值结果不一致」这种真问题。
    """
    raw = "".join((image_fingerprint(img), model, prompt, str(vision_max_side())))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_frames_cache(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        j = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"  [警告] 帧 OCR 缓存不可读，忽略并重建：{path}")
        return {}
    return j if isinstance(j, dict) else {}


def save_frames_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")


def _is_within(path: Path, parent: Path) -> bool:
    "path 是否在 parent 之内（resolve 后比较，防 ../ 穿越）。"
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def clean_intermediate(outdir: Path, targets: list, keep: bool) -> int:
    """清理本次运行的中间物，返回释放的字节数。

    ⚠ 纪律（用户边界）：**绝不删除 output/ 下已有产物**。
    因此只删「本次运行确实产生出来的那一批」，逐条满足才删：
      ① 在 targets 名单里（调用方显式列出，不做目录遍历）；
      ② 路径 resolve 后必须落在本次运行的产物目录 outdir 之内（防穿越误删）。
    --keep-intermediate 时一条都不删。
    """
    if keep:
        print(f"  保留中间物（--keep-intermediate）：{[str(Path(t)) for t in targets]}")
        return 0
    freed = 0
    for t in targets:
        p = Path(t)
        if not _is_within(p, outdir):
            print(f"  [警告] 跳过清理（不在本次产物目录内）：{p}")
            continue
        try:
            if p.is_dir():
                n = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                shutil.rmtree(p)
                freed += n
                print(f"  清理中间物: {p}/（{human_size(n)}）")
            elif p.is_file():
                n = p.stat().st_size
                p.unlink()
                freed += n
                print(f"  清理中间物: {p}（{human_size(n)}）")
        except OSError as exc:
            print(f"  [警告] 清理失败（不影响产物）：{p} —— {exc}")
    return freed


def frames_markdown(picks: list, entries: list, interval: int, model: str,
                    reused: int = 0, guard_note: str = "") -> str:
    """渲染帧 OCR 结果。entries 每项形如 {text, ok, error, meta}。

    帧头保留 ### [mm:ss] 前缀——scripts/check-content.py 的 C3/C4 靠它定位时间点；
    后面追加的耗时/行数只是给人看的注释，不参与机检解析。
    """
    ok_n = sum(1 for e in entries if e.get("ok"))
    fail_n = len(entries) - ok_n
    times = [e["meta"]["elapsed_sec"] for e in entries
             if e.get("meta") and e["meta"].get("elapsed_sec")
             and not e["meta"].get("reused")]
    med = f"{sorted(times)[len(times) // 2]:.1f}s" if times else "（无新调用）"
    mx = f"{max(times):.1f}s" if times else "-"
    total = f"{sum(times):.1f}s" if times else "0s"
    span = "间隔 " + str(interval) + "s" if interval else "（显式时间点）"
    lines = ["# 画面 OCR 原始结果", "",
             f"> 抽帧{span}，共 {len(picks)} 帧；识别模型 {model}（本地 Ollama）。",
             f"> OCR 调用：成功 {ok_n} / 失败 {fail_n}；本次复用缓存 {reused} 帧。",
             f"> think={vision_think()}（实测 3.5x：单帧 91.2s → 25.9s，依据 "
             "docs/审查与优化方案-2026-09-26.md 第二节）。",
             f"> 单帧耗时：中位 {med}，最长 {mx}，本次合计 {total}。",
             "> 空白帧表示该时刻画面无可读文字；带「调用失败」标记的帧是**接口失败**，",
             "> 不等于「此处无文字」（空帧守卫不把失败帧算作空帧）。",
             f"> 空帧守卫：{guard_note}", ""]
    for (secs, img), e in zip(picks, entries):
        meta = e.get("meta") or {}
        tag = ""
        if not e.get("ok"):
            tag = "  ← 调用失败"
        elif meta.get("reused"):
            tag = "  ← 复用缓存"
        elif meta.get("elapsed_sec"):
            tag = f"  ← {meta["elapsed_sec"]}s"
        lines.append(f"### [{seg_ts(secs)}] {img.name}{tag}")
        text = (e.get("text") or "").strip()
        if e.get("ok"):
            lines.append(text or "（无文字）")
        else:
            lines.append(text or f"（OCR 调用失败：{e.get("error") or "未记录原因"}）")
        lines.append("")
    return "\n".join(lines)


def check_empty_ratio(picks: list, ocr_results: list, ocr_entries: list | None = None) -> str:
    """空帧率守卫：空帧 ≠ 画面无文字。

    实测教训（2026-09-25）：旧流程把有内容的帧记成空（视觉模型静默失败），
    导致成品里的画面断言在归档证据中"查无实据"，险些被误判为编造。
    故空帧率 > 1/3 时必须显式告警，提示先排查 OCR 是否失败。
    """
    """空帧率守卫（2026-09-26 加严）。

    两个口径必须分开，否则会互相掩盖：
      · **失败帧**（视觉模型调用异常）—— 任何数量都要报，它是**已知的失败**，
        最容易被当成「画面无文字」蒙混过去；
      · **空帧**（调用成功但确无文字）—— 只在比例 > 1/3 时才提示复核。
    老实现只有一个「空帧」口径且不动失败帧的区分，一旦静默失败恰好把空帧率压在 1/3 以内，
    就既无告警也无痕迹。
    """
    entries = list(ocr_entries or [])
    if entries:
        empty = sum(1 for e in entries if e.get("ok") and (e.get("text") or "").strip() in NO_TEXT_HINTS)
        failed = sum(1 for e in entries if not e.get("ok"))
    else:
        empty = sum(1 for t in ocr_results if t.strip() in NO_TEXT_HINTS)
        failed = 0
    n = len(picks) or 1
    ratio = empty / n
    head = ""
    if failed:
        head = (f"⚠ OCR 调用失败 {failed}/{n} 帧（不等于「画面无文字」，已如实记入产物）；")
    if ratio > 1 / 3:
        return (head + f"⚠ 空帧 {empty}/{n}（{ratio:.0%}）超过 1/3 —— 空帧不等于画面无文字。"
                f"请先确认是画面真无字，还是视觉模型静默失败（对比帧图体积：空屏通常 <30KB；"
                f"必要时提高分辨率/换模型重跑）。")
    return head + f"空帧 {empty}/{n}（{ratio:.0%}），在正常范围。"


def eta_frames(n: int) -> float:
    """帧 OCR 的粗略预计（秒）。

    基准单帧 26s = 2026-09-26 实测 25.9s（think:false，qwen3.5:9b，1280 宽帧）。
    只是给用户看的 ETA，**不参与任何判定**，估错也不影响结果；可用 VT_ETA_PER_FRAME 覆盖。
    """
    try:
        per = float(env_path("VT_ETA_PER_FRAME") or 26.0)
    except ValueError:
        per = 26.0
    return per * max(0, n)


def frames_stage(outdir: Path, picks: list, model: str, resume: bool, progress: Progress,
                 total_eta: float | None = None, out: Path | None = None,
                 guard_cb=None, verbose: bool = True) -> tuple[Path, list, str, str]:
    """逐帧 OCR 阶段（含缓存/续跑/进度）。返回 (产物路径, entries, 守卫文本)。

    --resume 的判据不是「有没有产物文件」，而是**逐帧的缓存键**：
    只有「同一张帧图 + 同一模型 + 同一提示词 + 同一送模型分辨率」的结果才算已完成。
    缓存键不含 think —— 它只影响速度与输出去向，不该改变识别内容；
    含进去反而会掩盖「两种 think 取值结果不一致」这种真问题。

    失败帧**不进缓存**：接口失败不是这一帧的结论，续跑时必须重试。
    失败台账落在 <帧目录>/_failures.json 里（与图像同一目录，随帧同生同灭）。
    """
    frame_dir = picks[0][1].parent if picks else outdir / "frames"
    cache_p = outdir / FRAMES_CACHE_NAME
    fail_p = frame_dir / "_failures.json"
    cache = load_frames_cache(cache_p) if resume else {}
    prev_fail: dict = {}
    if resume and fail_p.is_file():
        try:
            prev_fail = json.loads(fail_p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prev_fail = {}

    entries: list = []
    reused = 0
    failures: dict = {}
    times: list = []
    n = len(picks)
    if not verbose:
        progress.start(f"画面 OCR（{n} 帧，模型 {model}）", total_eta or eta_frames(n))
    for i, (secs, img) in enumerate(picks, 1):
        key = frames_cache_key(img, model, OCR_PROMPT)
        if resume and key in cache:
            rec = cache[key]
            entries.append({"text": rec.get("text", ""), "ok": True, "error": "",
                            "meta": {**{k: v for k, v in rec.items() if k != "text"},
                                     "reused": True, "cached_at": rec.get("cached_at", "")}})
            reused += 1
            print(f"    [{i}/{n}] [{seg_ts(secs)}] 复用缓存（{rec.get("cached_at", '?')}，"
                  f"{rec.get("content_chars", '?')} 字）")
            continue
        t0 = time.time()
        if resume and key in prev_fail:
            print(f"    [{i}/{n}] [{seg_ts(secs)}] 上次失败，重试（{prev_fail[key]['error'][:60]}）")
        try:
            text, meta = vision_ocr(img, OCR_PROMPT)
            entries.append({"text": text, "ok": True, "error": "", "meta": meta})
            times.append(meta["elapsed_sec"])
            cache[key] = {"text": text, "cached_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                          **{k: v for k, v in meta.items()}}
            failures.pop(key, None)
            preview = text.replace("\n", " / ")[:60]
            print(f"    [{i}/{n}] [{seg_ts(secs)}] {meta["elapsed_sec"]}s · "
                  f"{len(text)} 字 · thinking {meta["thinking_chars"]} 字｜{preview}")
        except VisionCallError as exc:  # 单帧失败不吞：显式记入产物与失败台账
            err = f"{type(exc).__name__}: {exc}"
            entries.append({"text": f"<<OCR 失败: {err}>>", "ok": False, "error": err,
                            "meta": {"elapsed_sec": round(time.time() - t0, 1)}})
            failures[key] = {"file": img.name, "at": seg_ts(secs), "error": err,
                             "when": time.strftime("%Y-%m-%dT%H:%M:%S")}
            print(f"    [{i}/{n}] [{seg_ts(secs)}] [失败] {err}")
        if i % 3 == 0 or i == n:
            save_frames_cache(cache_p, cache)

    save_frames_cache(cache_p, cache)
    if failures:
        fail_p.write_text(json.dumps(failures, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8", newline="\n")
    elif fail_p.is_file():
        fail_p.unlink(missing_ok=True)

    guard = guard_cb(picks, [e["text"] for e in entries], entries) if guard_cb else check_empty_ratio(
        picks, [e["text"] for e in entries], entries)
    ok_n = sum(1 for e in entries if e["ok"])
    note = f"成功 {ok_n}/{n}" + (f"，失败 {n - ok_n}" if ok_n != n else "") + (
        f"，复用 {reused}" if reused else "")
    if times:
        note += f"，中位 {sorted(times)[len(times) // 2]:.1f}s"
    out_p = out or (outdir / "帧OCR原始结果.md")
    md = frames_markdown(picks, entries, 0, model, reused=reused, guard_note=guard)
    write_utf8(out_p, md)
    return out_p, entries, guard, note


def _have(path: Path) -> bool:
    "文件存在且非空（续跑判据的最小条件）。"
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def cmd_frames(args) -> int:
    bvid = parse_bvid(args.target)
    resume = bool(args.resume)
    local_frames = Path(args.frames_dir) if args.frames_dir else None
    local_video = Path(args.video) if args.video else None
    outdir = out_root() / bvid
    outdir.mkdir(parents=True, exist_ok=True)
    frame_dir = local_frames or (outdir / "frames")

    if local_frames:
        jpgs = sorted(local_frames.glob("f_*.jpg"))
        if not jpgs:
            raise SystemExit(f"[错误] {local_frames} 下没有 f_*.jpg")
        picks = [(i * args.interval, p) for i, p in enumerate(jpgs)]
        prog = Progress(2)   # 跳过抽帧 + 画面 OCR，两步
        prog.skip("抽帧", f"直接使用本地帧目录 {local_frames}（{len(picks)} 张）")
        out_p, entries, guard, note = frames_stage(outdir, picks, vision_model(), resume, prog,
                                                  verbose=False)
        fails = sum(1 for e in entries if not e["ok"])
        print(f"\n{guard}")
        print(f"完成: {out_p}  （{note}）")
        return 1 if fails else 0

    if not ffmpeg_path():
        raise SystemExit("[错误] 找不到 ffmpeg（画面层需要抽帧/取流；已有帧可用 --frames-dir 跳过）")
    if local_video:
        if not local_video.is_file():
            raise SystemExit(f"[错误] --video 指定的文件不存在: {local_video}")
        cid = 0
        prog = Progress(3)   # 取流(本地) + 抽帧 + 画面 OCR，三步
        fresh: list = []
        vfile = local_video
        prog.skip("取视频流", f"使用本地视频 {local_video}（跳过下载）")
    else:
        offline = bool(args.offline or args.cid)
        info = {"cid": args.cid} if (offline and args.cid) else build_info(bvid)
        cid = info.get("cid")
        if not cid:
            raise SystemExit("[错误] 需要 cid（联网时自动取；离线用 --cid 指定）")
        prog = Progress(3)
        fresh = []
        vfile = outdir / "video.m4s"
        if resume and _have(vfile):
            prog.skip("取视频流", f"复用已下载 {vfile}")
        else:
            t0 = prog.start("取视频流（最低码率）", eta_sec=6)
            download_video(fetch_playurl(bvid, cid), vfile)
            fresh.append(vfile)
            prog.done("取视频流", t0)
    have_frames = bool(list(frame_dir.glob("f_*.jpg"))) if frame_dir.is_dir() else False
    if resume and have_frames:
        picks = [(i * args.interval if not args.at else 0, p)
                 for i, p in enumerate(sorted(frame_dir.glob("f_*.jpg")))]
        if args.at:
            jpgs = sorted(frame_dir.glob("f_*.jpg"))
            ats = [s.strip() for s in args.at.split(",") if s.strip()]
            if len(ats) != len(jpgs):
                raise SystemExit(
                    f"[错误] --at 给了 {len(ats)} 个时间点，但已抽帧 {len(jpgs)} 张对不上；"
                    f"       复用旧帧必须参数一致，请去掉 --resume 重抽，或清空 {frame_dir}")
            picks = [(parse_mmss(x), p) for x, p in zip(ats, jpgs)]
        prog.skip("抽帧", f"复用已有 {len(picks)} 帧")
    else:
        t0 = prog.start("抽帧", eta_sec=5)
        picks = extract_frames(vfile, frame_dir, args.interval, args.at)
        prog.done("抽帧", t0, f"{len(picks)} 张")

    out_p, entries, guard, note = frames_stage(outdir, picks, vision_model(), resume, prog,
                                              verbose=False)
    fails = sum(1 for e in entries if not e["ok"])
    print(f"\n{guard}")
    print(f"完成: {out_p}  （{note}）")
    clean_intermediate(outdir, fresh, args.keep_intermediate)
    return 1 if fails else 0


def cmd_bili(args) -> int:
    """一条龙：元数据 → 音轨 → 转写 [→ 画面层]。

    三层产物一次给全：--frames 打开画面层，视频流与音轨同源、已下过就复用。
    --resume 按**每一段产物**判断该跳过什么：info.json / audio.wav / 原始转写.txt / 已完成帧，
    各自独立跳过，不做「一刀切重跑」。中间物默认清理（只清本次新产生的）。
    """
    bvid = parse_bvid(args.target)
    outdir = out_root() / bvid
    outdir.mkdir(parents=True, exist_ok=True)
    resume = bool(args.resume)
    want_frames = bool(args.frames)
    # 步数必须与实际打印次数一致，否则进度会显示成 [8/6] 这种自相矛盾的数
    prog = Progress(8 if want_frames else 5)
    fresh: list = []

    # ---- [1] 元数据 ----
    info_p = outdir / "info.json"
    if resume and _have(info_p) and _have(outdir / "弹幕时间线.txt"):
        info = json.loads(info_p.read_text(encoding="utf-8"))
        prog.skip("元数据 + 弹幕", f"复用已有 {info_p}（{len(info)} 键）")
    else:
        t0 = prog.start("元数据 + 弹幕（view/tag/reply/dm）", eta_sec=1)
        info = build_info(bvid)
        write_utf8(info_p, json.dumps(info, ensure_ascii=False, indent=2) + "\n")
        write_utf8(outdir / "弹幕时间线.txt", danmaku_timeline(info["danmaku"]))
        prog.done("元数据 + 弹幕", t0, f"{len(info)} 键 / 弹幕 {info['danmaku_count']} 条")
    cid = info.get("cid")
    if not cid:
        raise SystemExit("[错误] info.json 里没有 cid，无法取流")

    dash_cache: dict = {}

    def get_dash() -> dict:
        if not dash_cache:
            dash_cache.update(fetch_playurl(bvid, cid))
        return dash_cache

    # ---- [2] 音轨下载 ----
    raw = outdir / "audio.m4s"
    if resume and _have(raw):
        prog.skip("下载音轨", f"复用已下载 {raw}（{human_size(raw.stat().st_size)}）")
    else:
        t0 = prog.start("下载最低码率音轨", eta_sec=3)
        download_audio(get_dash(), raw)
        fresh.append(raw)
        prog.done("下载音轨", t0)

    # ---- [3] 转 16k 单声道 wav ----
    wav = outdir / "audio.wav"
    if resume and _have(wav):
        prog.skip("转 16k 单声道 wav", f"复用已有 {wav}（{human_size(wav.stat().st_size)}）")
    else:
        t0 = prog.start("转 16k 单声道 wav（ffmpeg）", eta_sec=3)
        to_wav16k(raw, wav)
        fresh.append(wav)
        prog.done("转 wav", t0)

    # ---- [4] 转写 ----
    tr_p, seg_p = outdir / "原始转写.txt", outdir / "原始转写_分段.md"
    tr: dict = {}
    if resume and _have(tr_p):
        txt = tr_p.read_text(encoding="utf-8").strip()
        tr = {"text": txt, "segments": [], "mode": "resumed",
              "duration_sec": round(probe_duration(wav), 1)}
        prog.skip("宿主 ASR 转写", f"复用已有 {tr_p}（{len(txt)} 字）")
    else:
        seg_n = max(1, int(-(-(probe_duration(wav) or 1) // max(1, args.segment))))
        t0 = prog.start(f"宿主 ASR 转写（长音频自动切片）", eta_sec=5.3 * seg_n)
        tr = transcribe_wav(wav, args.language, args.segment, outdir)
        write_transcript(outdir, tr, args.language)
        seg_dir = outdir / "segs"
        if seg_dir.is_dir():   # 切片目录由 transcribe_wav 内部产生，这里登记进本次中间物
            fresh.append(seg_dir)
        prog.done("转写", t0, f"{len(tr['text'])} 字 / {len(tr.get('segments') or [])} 段")

    # ---- [5] 画面层（可选） ----
    frame_note = "未启用（需要画面层请加 --frames）"
    ocr_p: Path | None = None
    frame_fails = 0
    if want_frames:
        if not ffmpeg_path():
            raise SystemExit("[错误] --frames 需要 ffmpeg（用 VT_FFMPEG 指定）")
        vfile = outdir / "video.m4s"
        if resume and _have(vfile):
            prog.skip("取视频流", f"复用已下载 {vfile}（{human_size(vfile.stat().st_size)}）")
        else:
            t0 = prog.start("取视频流（最低码率，与音轨同源）", eta_sec=6)
            download_video(get_dash(), vfile)
            fresh.append(vfile)
            prog.done("取视频流", t0)
        frame_dir = outdir / "frames"
        have_frames = bool(list(frame_dir.glob("f_*.jpg"))) if frame_dir.is_dir() else False
        if resume and have_frames:
            jpgs = sorted(frame_dir.glob("f_*.jpg"))
            picks = [(parse_mmss(x), p) for x, p in
                     zip([s.strip() for s in args.at.split(",") if s.strip()], jpgs)] if args.at \
                else [(i * args.interval, p) for i, p in enumerate(jpgs)]
            prog.skip("抽帧", f"复用已有 {len(picks)} 帧")
        else:
            t0 = prog.start("抽帧", eta_sec=5)
            picks = extract_frames(vfile, frame_dir, args.interval, args.at)
            prog.done("抽帧", t0, f"{len(picks)} 张")
        ocr_p, entries, guard, note = frames_stage(outdir, picks, vision_model(), resume, prog,
                                                  verbose=False)
        frame_fails = sum(1 for e in entries if not e["ok"])
        frame_note = note + f"｜{guard}"
        print(f"  {guard}")

    # ---- [6] 收尾：清理中间物 + 汇总 ----
    t0 = prog.start("收尾")
    freed = clean_intermediate(outdir, fresh, args.keep_intermediate)
    left = [p for p in (outdir / "audio.m4s", outdir / "audio.wav", outdir / "video.m4s",
                       outdir / "segs") if p.exists() and p not in fresh]
    prog.done("收尾", t0, f"释放 {human_size(freed)}" if freed else "")
    if left and not args.keep_intermediate:
        print(f"  [提示] 有 {len(left)} 项旧中间物未清（不是本次产生的，按纪律不动它们）："
              f"{', '.join(p.name for p in left)}；需要清可自行删除")

    produced = sorted(p.name for p in outdir.iterdir() if p.is_file())
    print(f"\n完成: {outdir}")
    print(f"  info.json 键数={len(info)}  danmaku_count={info.get('danmaku_count', '-')}  "
          f"comments_count={info.get('comments_count', '-')}  转写 {len(tr.get('text', ''))} 字")
    if want_frames:
        print(f"  画面层: {ocr_p}（{frame_note}）")
    print(f"  本次产物清单({len(produced)} 件): {', '.join(produced)}")
    return 1 if (frame_fails or not tr.get("text")) else 0


def add_frames_switches(p) -> None:
    "画面层参数（frames 与 bili --frames 共用同一套语义）。"
    p.add_argument("--interval", type=int, default=20, help="抽帧间隔秒数，默认 20")
    p.add_argument("--at", default="",
                   help="显式时间点，如 00:30,02:20,03:00（优先于 --interval）")


def add_local_source(p) -> None:
    """本地素材入口：给一个已有的视频/帧目录，跳过一切下载与 ffmpeg 取流。

    用途：① 离线复跑（机器上没有 ffmpeg 也能验证 OCR 链路本身）；
         ② 回归脚本用已有素材直接验 OCR，不必依赖下载。
    """
    p.add_argument("--video", default="", help="已有视频文件的本地路径（跳过下载）")
    p.add_argument("--frames-dir", default="",
                   help="已有帧目录（直接对里面的 f_*.jpg 跑 OCR，跳过抽帧）")


def add_common_switches(p, resume_help: str) -> None:
    "--resume / --keep-intermediate：所有会产生中间物的子命令共用。"
    p.add_argument("--resume", action="store_true", help=resume_help)
    p.add_argument("--keep-intermediate", action="store_true",
                   help="保留 segs/ / audio.wav / video.m4s 等中间物（默认本次跑完即清，"
                        "只清本次新产生的，绝不动 output/ 下的历史产物）")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vt_pipeline.py",
        description="video-transcribe 主链路（纯标准库 + ffmpeg + 宿主 ASR）")
    sub = p.add_subparsers(dest="cmd", required=True)



    pr = sub.add_parser("probe", help="探测本机外部程序与 ASR / 视觉模型可用性")
    pr.add_argument("--vision", action="store_true",
                    help="额外联网探查视觉模型的思考模式（会真的发一条极小的请求）")
    pr.set_defaults(func=cmd_probe)

    for name, fn, helptext in (("meta", cmd_meta, "抓 B站元数据 → info.json + 弹幕时间线.txt"),
                               ("danmaku", cmd_danmaku, "只抓弹幕"),
                               ("audio", cmd_audio, "抓最低码率音轨 → 16k 单声道 wav")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("target", help="B站 URL 或 BV 号")
        s.set_defaults(func=fn)

    t = sub.add_parser("transcribe", help="音频文件或 B站链接 → 宿主 ASR 转写")
    t.add_argument("target", help="本地音频/视频文件，或 B站 URL / BV 号")
    t.add_argument("--language", default="zh")
    t.add_argument("--segment", type=int, default=30, help="超过该秒数则切片转写，默认 30")
    t.add_argument("--outdir", default="", help="本地文件时的产物目录")
    t.set_defaults(func=cmd_transcribe)

    f = sub.add_parser("frames", help="抽帧 + 视觉模型 OCR → 帧OCR原始结果.md（画面补充层）")
    f.add_argument("target", help="B站 URL 或 BV 号")
    f.add_argument("--interval", type=int, default=20, help="抽帧间隔秒数，默认 20")
    f.add_argument("--at", default="", help="显式时间点，如 00:30,02:20,03:00（优先于 --interval）")
    f.add_argument("--cid", type=int, default=0, help="离线时显式指定 cid")
    f.add_argument("--offline", action="store_true", help="不抓元数据（需同时给 --cid）")
    add_local_source(f)
    add_common_switches(f, resume_help="已有 video.m4s / 抽好的帧 / 已完成帧的 OCR 结果则跳过重做")
    f.set_defaults(func=cmd_frames)

    b = sub.add_parser("bili", help="一条龙：元数据 → 音频 → 转写 [→ 画面层]")
    b.add_argument("target", help="B站 URL 或 BV 号")
    b.add_argument("--language", default="zh")
    b.add_argument("--segment", type=int, default=30)
    b.add_argument("--frames", action="store_true",
                   help="一条命令连带画面层（抽帧 + 视觉模型 OCR），默认关")
    add_frames_switches(b)
    add_common_switches(b, resume_help="已有 info.json / audio.wav / 原始转写.txt / 已完成帧的 OCR 结果则跳过重做")
    b.set_defaults(func=cmd_bili)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
