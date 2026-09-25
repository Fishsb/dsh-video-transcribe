#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check-hardcode.py —— 本项目「自有实现层不得写死本机绝对路径」的可复跑验收工具

纯标准库；不装依赖；不改动任何被扫描文件（只读）。

扫描面（可 --root 覆盖仓库根）：
  skill/**  与  scripts/**  下的 .md / .py
排除：
  __pycache__ 目录、*.pyc，以及其它扩展名
绝不扫描：
  upstream/（纯上游副本，扫它反而会诱导去改上游）与 docs/ 历史档

判据口径（两个都报，不引入第三个数字）：
  strict : 斜杠无关的本机用户目录写法  →  [Cc]:[\\/]+Users[\\/]+lk
  union  : strict 再并入旧盘符根写法    →  加  D:[\\/]+lk

输出三个分母，缺一不可：
  SCANNED + SKIPPED + DECODE_ERR = 递归遇到的普通文件总数

自证不自匹配：
  本脚本源码里含有上述正则以供执行，字形上不构成自匹配
  （形如 C:<sep>Users 的写法中间隔着方括号，不满足「字母 C 紧跟冒号」）。
  脚本对被扫描文件一视同仁——没有任何按路径/名字的豁免分支。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOTS = ("skill", "scripts")
SUFFIXES = (".md", ".py")

PATTERNS = [
    ("strict", r"[Cc]:[\\/]+Users[\\/]+lk"),
    ("union", r"[Cc]:[\\/]+Users[\\/]+lk|D:[\\/]+lk"),
]

# 登记文件：这些文件一旦缺席，说明扫描面残缺，「0 命中」不成立
REQUIRED = (
    "skill/SKILL.md",
    "skill/scripts/vt_pipeline.py",
    "skill/scripts/video_text_extract.py",
    "scripts/check-hardcode.py",
)


def excluded(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if "__pycache__" in rel.parts:
        return True
    if path.suffix.lower() == ".pyc":
        return True
    return path.suffix.lower() not in SUFFIXES


def collect(root: Path):
    """返回 (扫描清单, 跳过清单, 解码失败清单)。"""
    scanned, skipped, decode_err = [], [], []
    for name in ROOTS:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            rel = path.relative_to(root).as_posix()
            if excluded(path, root):
                skipped.append(rel)
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as exc:
                decode_err.append(f"{rel}  ({type(exc).__name__})")
                continue
            scanned.append((rel, text))
    return scanned, skipped, decode_err


def scan(scanned, pattern: str):
    rx = re.compile(pattern)
    hits = []
    for rel, text in scanned:
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append((rel, i, line.strip()))
    return hits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="检查自有实现层是否写死本机绝对路径（只读扫描）")
    ap.add_argument("--root", default=".", help="仓库根目录，默认当前目录")
    ap.add_argument("--list", action="store_true", help="额外打印被跳过的文件清单")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if not (root / "skill").is_dir():
        print(f"[错误] 在 {root} 下找不到 skill/ 目录，--root 指错了？", file=sys.stderr)
        return 2

    scanned, skipped, decode_err = collect(root)
    names = {rel for rel, _ in scanned}

    missing = [r for r in REQUIRED if r not in names]
    if missing:
        print("[错误] 登记文件缺席，扫描面不完整，「0 命中」不成立：", file=sys.stderr)
        for m in missing:
            print(f"        缺失: {m}", file=sys.stderr)
        return 3

    print("== 扫描面 ==")
    for rel, _ in scanned:
        print(f"  SCAN  {rel}")
    if args.list:
        for rel in skipped:
            print(f"  SKIP  {rel}")

    total_seen = len(scanned) + len(skipped) + len(decode_err)
    print(f"\nSCANNED={len(scanned)} / SKIPPED={len(skipped)} / DECODE_ERR={len(decode_err)}"
          f"   (合计 {total_seen})")
    if decode_err:
        for d in decode_err:
            print(f"  DECODE_ERR {d}")

    failed = False
    for label, pattern in PATTERNS:
        hits = scan(scanned, pattern)
        print(f"\n== 判据 {label}: {pattern} ==")
        if hits:
            failed = True
            print(f"  HIT={len(hits)}")
            for rel, ln, text in hits:
                print(f"  {rel}:{ln}: {text}")
        else:
            print("  HIT=0")

    print("\n结果:", "存在硬编码本机路径" if failed else "0 命中（自有实现层无本机绝对路径）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
