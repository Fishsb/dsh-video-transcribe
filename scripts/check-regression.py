#!/usr/bin/env python3
"""check-regression.py —— video-transcribe 回归总检：一键跑完并给「对了/错了」结论

判据来源（项目自有验收标准，非本脚本自定）：
  · docs/方案与验收.md 板块6「样例回归」验收1/2
  · 总验收 #6「样例可回归 → 回归脚本输出 PASS/FAIL 清单」

四条检查，每条都给结论而非「跑完了」：
  R1 历史样例只增不改 —— samples/ 下已登记文件 sha256 不得变化（机检）
  R2 上游副本纯净     —— upstream/ 与原件逐件一致（未指定原件目录则显式 SKIP，不计通过）
  R3 自有实现层零硬编码 —— 调用 scripts/check-hardcode.py，透传其结论
  R4 端到端实跑（--e2e）—— 跑一次主路线，断言产物语义与样例一致
  R5 内容准入        —— 调 scripts/check-content.py，判成品文案的断言是否都有真证据
                        （总验收 #3 每个修正都有依据 / #4 无证据不编造）

三态纪律：PASS / FAIL / SKIP。SKIP **不计通过**（未验证 ≠ 通过），会以退出码 2 传出来。

用法：
  python check-regression.py                         # R1+R3（R2 需原件目录）
  python check-regression.py --upstream-origin <目录>  # 追加 R2
  python check-regression.py --e2e                   # 追加 R4（约 1 分钟）
  python check-regression.py --freeze                # 登记 samples/ 新增文件；已登记文件哈希变化则拒绝

退出码：0=全部 PASS；1=有 FAIL；2=有待办（SKIP / 未登记，不计通过）；3=用法或环境错误
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
UPSTREAM = ROOT / "upstream" / "Video-Transcribe"
HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "samples-manifest.json"
CHECK_HARDCODE = HERE / "check-hardcode.py"
CHECK_CONTENT = HERE / "check-content.py"

# 端到端断言参数（阈值先有实测值再定：本项目实测归一化 ratio=1.0，取 0.95 留出切片边界余量）
E2E_SAMPLE = "BV1JMbp6MEo5"
E2E_MIN_SIM = 0.95
E2E_MIN_KEYS = 17

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []   # (检查项, 三态, 说明)


def record(item: str, state: str, detail: str) -> None:
    results.append((item, state, detail))
    print(f"  [{state:4}] {item}：{detail}")


def child_env() -> dict:
    """子进程强制 UTF-8：Windows 中文环境下管道默认 cp936，会让中文输出解码失败。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sample_files() -> list[str]:
    if not SAMPLES.is_dir():
        return []
    out = []
    for p in sorted(SAMPLES.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            out.append(p.relative_to(ROOT).as_posix())
    return out


# ---------------------------------------------------------------- R1
def r1_samples(freeze: bool) -> None:
    print("\n【R1】历史样例只增不改（samples/ 已登记文件 sha256 不变）")
    files = sample_files()
    if not files:
        record("R1", SKIP, "samples/ 不存在或为空，无法比对")
        return

    if freeze and not MANIFEST.exists():
        manifest: dict[str, str] = {"files": {}}
    elif not MANIFEST.exists():
        record("R1", SKIP, f"清单不存在（{MANIFEST.name}）；先跑一次 --freeze 建立基线")
        return
    else:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    recorded = manifest.get("files", {})

    changed, missing, new = [], [], []
    for rel in files:
        digest = sha256_of(ROOT / rel)
        if rel not in recorded:
            new.append(rel)
        elif recorded[rel] != digest:
            changed.append(rel)
    for rel in recorded:
        if rel not in files:
            missing.append(rel)

    if freeze:
        if changed:
            record("R1", FAIL, "已登记文件哈希变化，拒绝登记：" + ", ".join(changed))
            return
        for rel in new:
            recorded[rel] = sha256_of(ROOT / rel)
        manifest["files"] = recorded
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        record("R1", PASS, f"已登记 {len(new)} 个新文件；现存 {len(recorded)} 件受保护")
        return

    if changed or missing:
        bad = []
        if changed:
            bad.append("哈希变化: " + ", ".join(changed))
        if missing:
            bad.append("已登记但消失: " + ", ".join(missing))
        record("R1", FAIL, "；".join(bad))
    elif new:
        record("R1", SKIP, f"已登记 {len(recorded)} 件不变 ✓，但发现 {len(new)} 个未登记新文件（未受保护）："
                           + ", ".join(new) + "；确认属「只增」后跑 --freeze 登记")
    else:
        record("R1", PASS, f"{len(recorded)} 件 sha256 全部与清单一致（无新增、无改动、无缺失）")


# ---------------------------------------------------------------- R2
def r2_upstream(origin: Path | None) -> None:
    print("\n【R2】上游副本纯净（upstream/ 与原件逐件一致）")
    if not UPSTREAM.is_dir():
        record("R2", SKIP, "upstream/ 不存在，无法比对")
        return
    if origin is None or not origin.is_dir():
        record("R2", SKIP, "未指定原件目录（环境变量 VT_UPSTREAM_ORIGIN 或 --upstream-origin）。"
                           "SKIP 不计通过 —— 上游纯净性本次未验证")
        return

    local = sorted(p.relative_to(UPSTREAM).as_posix() for p in UPSTREAM.rglob("*") if p.is_file())
    if not local:
        record("R2", SKIP, "upstream/ 为空，无可比对文件")
        return

    diff, absent = [], []
    for rel in local:
        src = origin / rel
        if not src.is_file():
            absent.append(rel)
            continue
        if sha256_of(src) != sha256_of(UPSTREAM / rel):
            diff.append(rel)

    if diff or absent:
        detail = []
        if diff:
            detail.append("与原件不一致: " + ", ".join(diff))
        if absent:
            detail.append("原件侧缺失: " + ", ".join(absent))
        record("R2", FAIL, "；".join(detail))
    else:
        record("R2", PASS, f"{len(local)} 件与原件 sha256 逐件一致")


# ---------------------------------------------------------------- R3
def r3_hardcode() -> None:
    print("\n【R3】自有实现层零硬编码（调用 check-hardcode.py）")
    if not CHECK_HARDCODE.is_file():
        record("R3", SKIP, "scripts/check-hardcode.py 不存在，未验证")
        return
    proc = subprocess.run([sys.executable, str(CHECK_HARDCODE)], capture_output=True, text=True,
                          encoding="utf-8", env=child_env())
    tail = [ln.strip() for ln in (proc.stdout or "").strip().splitlines() if ln.strip()][-2:]
    if proc.returncode == 0:
        record("R3", PASS, f"该工具 exit=0 且两口径 HIT=0（{' / '.join(tail)}）")
    else:
        record("R3", FAIL, f"该工具 exit={proc.returncode}：{' / '.join(tail)}")


# ---------------------------------------------------------------- R5
def r5_content() -> None:
    print("\n【R5】内容准入（总验收 #3 依据 / #4 无证据不编造）")
    if not CHECK_CONTENT.is_file():
        record("R5", SKIP, "scripts/check-content.py 不存在，未验证")
        return
    proc = subprocess.run([sys.executable, str(CHECK_CONTENT), "--all"], capture_output=True, text=True,
                          encoding="utf-8", env=child_env())
    tail = [ln.strip() for ln in (proc.stdout or "").strip().splitlines() if ln.strip()][-1:]
    if proc.returncode == 0:
        record("R5", PASS, f"成品断言均有归档证据可核（{tail[0] if tail else ''}）")
    elif proc.returncode == 2:
        record("R5", SKIP, f"有未覆盖项（{' '.join(tail)}）")
    else:
        record("R5", FAIL, f"内容准入未过：{' '.join(tail)}")


# ---------------------------------------------------------------- R4
def normalize(text: str) -> str:
    t = re.sub(r"\s+", "", text)
    return re.sub(r"[，。、；：！？（）《》“”‘’·—…,.;:!?()\[\]\"']", "", t)


def r4_e2e() -> None:
    print("\n【R4】端到端实跑（主路线 → 断言产物）")
    pipeline = ROOT / "skill" / "scripts" / "vt_pipeline.py"
    if not pipeline.is_file():
        record("R4", SKIP, "skill/scripts/vt_pipeline.py 不存在，未实跑")
        return
    # 用 tempfile.gettempdir() 而非 os.environ["TEMP"]：后者是 Windows 约定，
    # WSL/Linux 无该变量 ⇒ 取默认值 "." ⇒ 回归产物落进仓库根目录（实测踩坑）。
    # gettempdir() 按 TMPDIR/TEMP/TMP 顺序解析，全缺省时退 /tmp，跨平台安全。
    outdir = Path(tempfile.gettempdir()) / "vt-regression-out"
    env = child_env()
    env["VT_OUTDIR"] = str(outdir)
    proc = subprocess.run([sys.executable, str(pipeline), "bili", E2E_SAMPLE],
                          capture_output=True, text=True, encoding="utf-8", env=env)
    if proc.returncode != 0:
        record("R4", FAIL, f"主路线 exit={proc.returncode}；末行: {(proc.stdout or '').strip().splitlines()[-1:]}")
        return

    produced = outdir / E2E_SAMPLE
    info_p, tran_p = produced / "info.json", produced / "原始转写.txt"
    if not info_p.is_file() or not tran_p.is_file():
        record("R4", FAIL, "退出码 0 但产物缺失（info.json / 原始转写.txt）——接口成功≠达成")
        return

    info = json.loads(info_p.read_text(encoding="utf-8"))
    keys = len(info)
    dm = info.get("danmaku_count", "<缺失>")
    produced_text = normalize(tran_p.read_text(encoding="utf-8"))
    baseline_p = ROOT / "samples" / E2E_SAMPLE / "原始转写_分段.md"
    problems = []
    if keys < E2E_MIN_KEYS:
        problems.append(f"info.json 键数 {keys} < {E2E_MIN_KEYS}")
    if dm != 0:
        problems.append(f"danmaku_count={dm}（样例实测应为 0）")
    if not produced_text:
        problems.append("转写文本为空")
    if len(produced_text) < 500:
        problems.append(f"转写文本仅 {len(produced_text)} 字，疑似截断（样例基线量级 ~1600 字）")

    sim = None
    if baseline_p.is_file():
        # 基线取证：分段稿 vs 整段纯文本是两个不同对象。曾用整段稿比带 #/> 壳的分段 md，
        # 得 ratio 0.9459 被误判为回归；去掉结构行后两份逐字符相同（ratio=1.0）。
        # 因此比较前先剥掉 markdown 结构行，否则比较的是「壳」不是「内容」。
        raw = baseline_p.read_text(encoding="utf-8")
        body = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith(("#", ">")))
        baseline = normalize(body)
        sim = SequenceMatcher(None, produced_text, baseline).ratio() if baseline else 0.0
        if sim < E2E_MIN_SIM:
            problems.append(f"与样例语义相似度 {sim:.4f} < {E2E_MIN_SIM}（基线 {len(baseline)} 字 / 生成 {len(produced_text)} 字）")

    if problems:
        record("R4", FAIL, "；".join(problems))
    else:
        sim_txt = f"，语义相似度 {sim:.3f}" if sim is not None else "（无样例基线可比）"
        record("R4", PASS, f"exit=0，info.json {keys} 键，danmaku_count=0，转写 {len(produced_text)} 字{sim_txt}")


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="video-transcribe 回归总检（PASS/FAIL/SKIP 三态）")
    ap.add_argument("--upstream-origin", help="上游原件目录；也可用环境变量 VT_UPSTREAM_ORIGIN")
    ap.add_argument("--e2e", action="store_true", help="追加端到端实跑（约 1 分钟）")
    ap.add_argument("--freeze", action="store_true", help="登记 samples/ 新增文件（不改已登记项）")
    args = ap.parse_args()

    print("video-transcribe 回归总检")
    print(f"  工作区: {ROOT}")
    print(f"  解释器: {sys.executable}")

    origin_arg = args.upstream_origin or os.environ.get("VT_UPSTREAM_ORIGIN")
    origin = Path(origin_arg) if origin_arg else None

    r1_samples(args.freeze)
    if not args.freeze:
        r2_upstream(origin)
        r3_hardcode()
        r5_content()
        if args.e2e:
            r4_e2e()

    n_pass = sum(1 for _, s, _ in results if s == PASS)
    n_fail = sum(1 for _, s, _ in results if s == FAIL)
    n_skip = sum(1 for _, s, _ in results if s == SKIP)
    print(f"\n汇总：PASS={n_pass}  FAIL={n_fail}  SKIP={n_skip}")
    if n_skip:
        print("  注意：SKIP 不计通过（未验证 ≠ 通过）")
        for item, s, detail in results:
            if s == SKIP:
                print(f"    - {item}: {detail}")
    if n_fail:
        print("结论：FAIL —— 有检查未通过，见上")
        return 1
    if n_skip:
        print("结论：有待办 —— 无失败，但存在未验证项，不能判全绿")
        return 2
    print("结论：全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
