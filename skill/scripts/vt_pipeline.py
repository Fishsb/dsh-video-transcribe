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

子命令：
  probe       探测本机外部程序与 ASR 服务可用性（不联网、不写盘）
  meta        抓 B站元数据（view + tag + reply + 弹幕），产出 info.json
  danmaku     只抓弹幕，产出 弹幕时间线.txt / 弹幕.json
  audio       抓最低码率音轨 → ffmpeg 转 16k 单声道 wav
  transcribe  对音频调宿主 ASR 转写（长音频自动 ffmpeg 切片）
  bili        一条龙：meta → audio → transcribe

产物默认落在 <VT_OUTDIR>/<bvid>/ 下；仅新增，绝不写 samples/。
"""
from __future__ import annotations

import argparse
import base64
import gzip
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
        ("ffprobe", fp or "（缺失：时长回退解析 ffmpeg -i，仍可工作）"),
        ("BBDown（可选适配器）", find_tool("BBDown", "VT_BBDOWN") or "（未安装，主路线不需要）"),
        ("yt-dlp（可选适配器）", find_tool("yt-dlp") or "（未安装，主路线不需要）"),
        ("faster-whisper（可选适配器）", "见 transcribe 子命令内的显式探测"),
        ("宿主 ASR", asr_url()),
        ("ASR /health", json.dumps(h, ensure_ascii=False) if h else "（不可达）"),
        ("产物根目录", str(out_root().resolve())),
    ]
    print("== vt_pipeline 环境探测 ==")
    for k, v in rows:
        print(f"  {k:26s} : {v}")
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


def cmd_bili(args) -> int:
    bvid = parse_bvid(args.target)
    info = build_info(bvid)
    outdir = out_root() / bvid
    outdir.mkdir(parents=True, exist_ok=True)
    write_utf8(outdir / "info.json", json.dumps(info, ensure_ascii=False, indent=2) + "\n")
    write_utf8(outdir / "弹幕时间线.txt", danmaku_timeline(info["danmaku"]))
    dash = fetch_playurl(bvid, info["cid"])
    raw = outdir / "audio.m4s"
    download_audio(dash, raw)
    wav = to_wav16k(raw, outdir / "audio.wav")
    tr = transcribe_wav(wav, args.language, args.segment, outdir)
    write_transcript(outdir, tr, args.language)
    print(f"\n完成: {outdir}")
    print(f"  info.json 键数={len(info)}  danmaku_count={info['danmaku_count']}  "
          f"comments_count={info['comments_count']}  转写 {len(tr['text'])} 字")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vt_pipeline.py",
        description="video-transcribe 主链路（纯标准库 + ffmpeg + 宿主 ASR）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("probe", help="探测本机外部程序与 ASR 可用性（不联网不写盘）")
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

    b = sub.add_parser("bili", help="一条龙：元数据 → 音频 → 转写")
    b.add_argument("target", help="B站 URL 或 BV 号")
    b.add_argument("--language", default="zh")
    b.add_argument("--segment", type=int, default=30)
    b.set_defaults(func=cmd_bili)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
