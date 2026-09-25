#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SenseVoice model.int8.onnx 稳健分片下载器（Python urllib 版）。
已有完整分片跳过，缺失分片并行下载（断点续传 + 重试），最后合并。
"""
import os
import threading
import time
import urllib.request

URL = "https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/model.int8.onnx"
DIR = r"D:\lk\.cache\SenseVoiceSmall"
TOTAL = 239233841
CHUNKS = 8
SIZE = TOTAL // CHUNKS  # 29904230
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def chunk_target(i):
    start = i * SIZE
    end = (TOTAL - 1) if i == CHUNKS - 1 else (start + SIZE - 1)
    return start, end, end - start + 1

def download_chunk(i):
    path = os.path.join(DIR, f"part.{i}")
    start, end, target = chunk_target(i)
    if os.path.isfile(path) and os.path.getsize(path) == target:
        print(f"part.{i}: 已完整，跳过", flush=True)
        return True
    tries = 0
    while tries < 30:
        try:
            existing = os.path.getsize(path) if os.path.isfile(path) else 0
            if existing > target:
                with open(path, "r+b") as f:
                    f.truncate(target)
                existing = target
            if existing >= target:
                print(f"part.{i}: 完整 ({target} bytes)", flush=True)
                return True
            req = urllib.request.Request(URL)
            req.add_header("Range", f"bytes={start + existing}-{end}")
            req.add_header("User-Agent", UA)
            with urllib.request.urlopen(req, timeout=180) as resp, open(path, "ab") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
            now = os.path.getsize(path)
            if now == target:
                print(f"part.{i}: 下载完成 ({target} bytes)", flush=True)
                return True
            print(f"part.{i}: 不完整 {now}/{target}，重试 {tries+1}", flush=True)
        except Exception as e:
            print(f"part.{i}: 错误 {type(e).__name__}: {e}，重试 {tries+1}", flush=True)
        tries += 1
        time.sleep(2)
    print(f"part.{i}: 重试耗尽 FAILED", flush=True)
    return False

def main():
    results = {}
    threads = []
    for i in range(CHUNKS):
        t = threading.Thread(target=lambda i=i: results.__setitem__(i, download_chunk(i)))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    if not all(results.values()):
        print(f"FAILED: {[i for i, ok in results.items() if not ok]}", flush=True)
        return 1

    # 合并
    out_path = os.path.join(DIR, "model.int8.onnx")
    with open(out_path, "wb") as out:
        for i in range(CHUNKS):
            with open(os.path.join(DIR, f"part.{i}"), "rb") as f:
                out.write(f.read())
    for i in range(CHUNKS):
        os.remove(os.path.join(DIR, f"part.{i}"))
    print(f"合并完成: {os.path.getsize(out_path)} bytes", flush=True)

    # tokens.txt
    tok_url = URL.replace("model.int8.onnx", "tokens.txt")
    tok_path = os.path.join(DIR, "tokens.txt")
    for t in range(10):
        try:
            req = urllib.request.Request(tok_url)
            req.add_header("User-Agent", UA)
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
            with open(tok_path, "wb") as f:
                f.write(data)
            print(f"tokens.txt: {len(data)} bytes", flush=True)
            break
        except Exception as e:
            print(f"tokens.txt 错误 {type(e).__name__}: {e}，重试 {t+1}", flush=True)
            time.sleep(2)
    else:
        print("tokens.txt 下载失败", flush=True)
        return 1

    print("ALL_DONE", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
