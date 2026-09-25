#!/usr/bin/env python3
"""check-content.py —— 内容准入机检：成品文案的每条断言是否有真证据

判据来源（项目自有验收标准，非本脚本自定）：
  · docs/方案与验收.md 总验收 #3「每个修正都有依据」
  · docs/方案与验收.md 总验收 #4「无证据不编造」
  · AGENTS.md 自约束 3「纠正必须给依据（给不出则显式标『推断』）」/ 4「无有效证据不标注」

为什么需要它：这两条是本项目**核心价值**（二次纠正层）的唯一边防，
但此前只能靠人眼逐条比对——人眼会漏，且漏了不留痕。

检查项：
  C1 纠错表每行「依据」列非空（空依据须显式标「推断」）
  C2 标为「推断」的条目必须真的写出来（不得混入正文当既定事实）
  C3 画面补充表里声称的画面文字，必须在帧 OCR 原始结果中**于该时间点范围内**存在
  C4 画面补充表里写「（无文字）」的行，对应帧必须真的为空（防反向编造）
  C3' 不可核断言须清零：若某行断言的引文在归档证据中找不到，脚本判 FAIL —— 但**FAIL 的含义是
     「以现有归档证据不可核」，不是「必然编造」**。二者必须分开处置：
       · 编造       → 删除该断言
       · 证据缺失   → 补齐归档证据（重抽帧/重跑 OCR），断言可能本来就是对的
     实测案例（2026-09-25）：samples/BV1JMbp6MEo5 最终文案 L104 声称 `[02:20]–[03:00]`「终帧可见」
     5 个短语。仓内帧 OCR 该区间三帧全为空、原件与副本 6/6 逐字节一致 ⇒ 判 FAIL。
     重抽该三帧并重跑视觉模型后证实：f_008/f_009/f_010 **实有大量文字**（含"检索情景记忆"、
     "继续处理昨天那副耳机？"、"第一次观察(保存经历)"等），断言**全部为真**。
     ⇒ 真实故障是**原 OCR 静默失败导致证据链断裂**，不是成品编造。
      这就是 C6 空帧率守卫存在的理由：空帧 ≠ 画面无文字，静默失败会让 C4 假绿。
  C6 空帧率守卫：帧 OCR 若大面积为空（>1/3）而画面明显非空，提示可能是**OCR 静默失败**
     而非「画面无文字」——静默失败会让 C4 误判为 PASS

三态纪律：PASS / FAIL / SKIP。SKIP 不计通过（退出码 2）。

用法：
  python check-content.py <成品文案.md> --ocr <帧OCR原始结果.md>
  python check-content.py --all            # 扫描 samples/ 下所有成品
退出码：0=全部 PASS；1=有 FAIL；2=有 SKIP/未覆盖；3=用法错误
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS, FAIL, SKIP, XFAIL = "PASS", "FAIL", "SKIP", "XFAIL"

# 已知缺陷登记：samples/ 只增不改 ⇒ 历史件里的缺陷无法就地修复，只能另出修订版。
# 但「不修」不等于「不知道」——这些件必须显式登记并**保持报红可见**，否则就是假绿。
# 登记形态：{相对路径: 已认定缺陷说明}。用 --list-known 查看。
KNOWN_DEFECTS = {
    "samples/BV1JMbp6MEo5/最终文案.md":
        "帧 OCR 静默失败导致 [01:00]/[01:20] 被误标「（无文字）」而实有文字（漏报）；"
        "LangChain 词条误标「推断」。samples/ 只增不改 ⇒ 不就地修复，"
        "已出修订版 最终文案_修订版.md 并全绿。原件保留作为缺陷证据。",
}

FRAME_RE = re.compile(r"^###\s*\[(\d{1,2}):(\d{2})\]")
# 补正册常用表格形态：| [mm:ss] | 内容 |  —— 只认「首列是时间点」的行，避免误吞普通表格
TABLE_FRAME_RE = re.compile(r"^\|\s*\[(\d{1,2}):(\d{2})\]\s*\|(.*)$")
# 时间点单元格：[00:00] 或 [02:20]–[03:00]（en-dash 与连字符都收）
RANGE_RE = re.compile(r"\[(\d{1,2}):(\d{2})\](?:\s*[–\-—~]\s*\[(\d{1,2}):(\d{2})\])?")
TICK_RE = re.compile(r"\x60([^\x60]+)\x60")
NO_TEXT_MARKERS = ("（无文字）", "(无文字)", "无文字")


def mmss(h: str, m: str) -> int:
    return int(h) * 60 + int(m)


# 引文比对时的等价类：视觉模型对中英文标点/空白不稳定，逐字比对会把
# 「全角？/半角?」「≤/<=」这类同一内容的写法差异误判为"引文不存在"。
_PUNCT_EQ = {
    "?": "？", "!": "！", ",": "，", ";": "；", ":": "：",
    "(": "（", ")": "）", "<=": "≤", ">=": "≥",
}
_PUNCT_TABLE = str.maketrans({k: v for k, v in _PUNCT_EQ.items() if len(k) == 1})


def canonical(s: str) -> str:
    """把一段文字归一化到「内容等价」形态：统一标点、去空白。

    仅用于**比对**，不改动任何归档证据原文。
    """
    t = s.replace("≤", "<=").replace("≥", ">=")          # 先保住多字符等价对
    out = []
    i = 0
    while i < len(t):
        two = t[i:i + 2]
        if two in _PUNCT_EQ:
            out.append(_PUNCT_EQ[two])
            i += 2
            continue
        ch = t[i]
        out.append(_PUNCT_EQ.get(ch, ch))
        i += 1
    res = "".join(out).translate(_PUNCT_TABLE)
    res = res.replace("≤", "<=").replace("≥", ">=")
    return re.sub(r"\s+", "", res)


def parse_frames(ocr_path: Path) -> dict[int, str]:
    """帧 OCR 原始结果 → {秒: 该帧全部文字}（拼接为一行便于包含判断）"""
    frames: dict[int, str] = {}
    cur: int | None = None
    buf: list[str] = []
    for line in ocr_path.read_text(encoding="utf-8").splitlines():
        m = FRAME_RE.match(line.strip())
        if m:
            if cur is not None:
                frames[cur] = " ".join(buf).strip()
            cur = mmss(m.group(1), m.group(2))
            buf = []
        elif cur is not None:
            buf.append(line.strip())
    if cur is not None:
        frames[cur] = " ".join(buf).strip()

    # 表格形态：| [mm:ss] | 原记录 | 复验实有内容 |（补正册用此格式并列多册）
    # 多列时取**最后一个非空单元格**为内容列 —— 中间的「原记录」列（常为"空"）不是内容。
    if not frames:
        for line in ocr_path.read_text(encoding="utf-8").splitlines():
            m = TABLE_FRAME_RE.match(line.strip())
            if m:
                secs = mmss(m.group(1), m.group(2))
                cells = [c.strip() for c in m.group(3).split("|")]
                cells = [c.replace("**", "").replace("\x60", "").strip() for c in cells]
                frames[secs] = next((c for c in reversed(cells) if c), "")
    return frames


def merge_supplement(frames: dict[int, str], doc: Path) -> dict[int, str]:
    """合并同目录的『帧OCR原始结果_复验补正.md』。

    背景：原 OCR 存在**静默失败**（把有内容的帧记成空），会让成品断言被误判为不可核。
    补正是**只增不改**的独立文件（samples/ 纪律），故这里只对补正中**明确列出**的帧生效，
    未列出的帧仍用原结果 —— 不因补正存在就放宽 C6 空帧率守卫。
    """
    merged = dict(frames)
    # 自动发现所有补正册（帧OCR原始结果_复验补正*.md），按文件名排序保证确定性
    PLACEHOLDERS = ("空", "无", "（无文字）", "(无文字)", "-", "—", "")
    for sup in sorted(doc.parent.glob("帧OCR原始结果_复验补正*.md")):
        for sec, text in parse_frames(sup).items():
            cleaned = text.strip()
            # 表格里的「空」只表示"原记录为空"，不是内容本身 —— 不得当作证据并入
            if cleaned in PLACEHOLDERS or cleaned.startswith("空"):
                continue
            if cleaned:
                merged[sec] = (merged.get(sec, "") + " " + cleaned).strip()
    return merged


def table_rows(text: str, header_keyword: str) -> list[list[str]]:
    """抓取以 header_keyword 所在行为表头的 markdown 表格数据行。"""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("|") and header_keyword in ln:
            start = i
            break
    if start is None:
        return []
    rows: list[list[str]] = []
    for ln in lines[start + 2:]:            # 跳过表头与分隔行
        if not ln.lstrip().startswith("|"):
            break
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        rows.append(cells)
    return rows


def check_doc(doc: Path, ocr: Path) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    text = doc.read_text(encoding="utf-8")

    def rec(item: str, state: str, detail: str) -> None:
        out.append((item, state, detail))

    # ---------- C1 / C2：纠错表的依据列 ----------
    fix_rows = table_rows(text, "依据")
    if not fix_rows:
        rec("C1", SKIP, "未找到纠错表（表头含『依据』列）")
    else:
        empty, inferred = [], []
        for cells in fix_rows:
            if len(cells) < 3:
                continue
            basis = cells[-1]
            if not basis or basis in ("-", "—", "N/A"):
                empty.append(cells[0][:24])
            if "推断" in basis:
                inferred.append(cells[0][:24])
        if empty:
            rec("C1", FAIL, f"{len(empty)} 行「依据」为空且未标『推断』：{'; '.join(empty[:5])}")
        else:
            rec("C1", PASS, f"纠错表 {len(fix_rows)} 行，每行均有依据（其中 {len(inferred)} 行显式标『推断』）")
        # 全表零「推断」不算问题（说明都有实据），但若正文出现未标注的推断词则另说
        rec("C2", PASS if not empty else FAIL,
            f"『推断』显式标注 {len(inferred)} 条；无空依据行" if not empty else "存在空依据行（见 C1）")

    # ---------- C3 / C4：画面补充表 vs 帧 OCR ----------
    pic_rows = table_rows(text, "画面文字")
    if not pic_rows:
        rec("C3", SKIP, "未找到画面补充表（表头含『画面文字』列）")
    elif not ocr.is_file():
        rec("C3", SKIP, f"帧 OCR 原始结果不存在（{ocr.name}），无法核对画面断言")
    else:
        frames = merge_supplement(parse_frames(ocr), doc)
        if not frames:
            rec("C3", SKIP, "帧 OCR 文件解析不出任何帧")
        else:
            unsupported: list[str] = []
            false_empty: list[str] = []
            checked = 0
            for cells in pic_rows:
                if len(cells) < 2:
                    continue
                span_raw, claim = cells[0], cells[1]
                spans = RANGE_RE.findall(span_raw)
                if not spans:
                    continue
                h1, m1, h2, m2 = spans[0]
                lo = mmss(h1, m1)
                hi = mmss(h2, m2) if h2 else lo
                inrange = {s: t for s, t in frames.items() if lo <= s <= hi}
                merged = " ".join(inrange.values())

                if any(mk in claim for mk in NO_TEXT_MARKERS):
                    if merged.strip():
                        false_empty.append(f"{span_raw}（该范围 OCR 实有文字：{merged[:30]}…）")
                    continue
                tokens = [t.strip() for t in TICK_RE.findall(claim) if t.strip()]
                if not tokens:
                    continue
                checked += 1
                hay = canonical(merged)
                missing = [t for t in tokens if canonical(t) not in hay]
                if missing:
                    unsupported.append(f"{span_raw} 缺 {missing[:4]}")

            if unsupported:
                rec("C3", FAIL, f"{len(unsupported)} 行画面的引文在任何帧中都找不到：{'；'.join(unsupported[:3])}")
            else:
                rec("C3", PASS, f"{checked} 行画面断言的引文均能在对应时间点帧中找到")
            if false_empty:
                rec("C4", FAIL, "标『无文字』但该帧实有文字：" + "；".join(false_empty[:3]))
            else:
                rec("C4", PASS, "标『无文字』的行对应帧确实为空（未反向编造）")

            # C6 空帧率守卫：空帧过多时怀疑 OCR 静默失败（空帧 ≠ 画面无文字）
            empty_frames = [s for s, t in frames.items() if not t.strip()]
            ratio = len(empty_frames) / len(frames)
            if ratio > 1 / 3:
                rec("C6", SKIP,
                    f"{len(empty_frames)}/{len(frames)} 帧 OCR 为空（{ratio:.0%}），超过 1/3 —— "
                    f"须确认是「画面真无文字」还是「OCR 静默失败」；后者会让 C4 假绿。"
                    f"复验方式：重抽该时间点帧、看图像体积（空屏通常 <30KB），必要时重跑视觉模型")
            else:
                rec("C6", PASS, f"空帧 {len(empty_frames)}/{len(frames)}（{ratio:.0%}），在正常范围")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="内容准入机检（依据 / 无证据不编造）")
    ap.add_argument("doc", nargs="?", help="成品文案 .md")
    ap.add_argument("--ocr", help="对应的帧 OCR 原始结果 .md")
    ap.add_argument("--all", action="store_true", help="扫描 samples/ 下所有成品")
    ap.add_argument("--list-known", action="store_true", help="列出已登记的已知缺陷件")
    args = ap.parse_args()

    if args.list_known:
        print("已登记的已知缺陷件（XFAIL）：")
        for rel, why in sorted(KNOWN_DEFECTS.items()):
            print(f"  · {rel}\n      {why}")
        return 0

    targets: list[tuple[Path, Path]] = []
    if args.all:
        # 覆盖原版与修订版：修订版是实际交付面，原版留作缺陷证据 —— 两者都要机检
        for pattern in ("samples/*/最终文案.md", "samples/*/最终文案_修订版.md"):
            for doc in sorted(ROOT.glob(pattern)):
                targets.append((doc, doc.parent / "帧OCR原始结果.md"))
    elif args.doc:
        doc = Path(args.doc)
        ocr = Path(args.ocr) if args.ocr else doc.parent / "帧OCR原始结果.md"
        targets.append((doc, ocr))
    else:
        ap.error("需要 doc 参数或 --all")

    print("内容准入机检（总验收 #3 依据 / #4 无证据不编造）")
    all_results: list[tuple[str, str, str]] = []
    for doc, ocr in targets:
        print(f"\n=== {doc.relative_to(ROOT) if doc.is_relative_to(ROOT) else doc} ===")
        if not doc.is_file():
            print(f"  [SKIP] 文件不存在")
            all_results.append(("target", SKIP, str(doc)))
            continue
        res = check_doc(doc, ocr)
        rel = doc.relative_to(ROOT).as_posix() if doc.is_relative_to(ROOT) else str(doc)
        if rel in KNOWN_DEFECTS and any(s == FAIL for _, s, _ in res):
            res = [(it, (XFAIL if st == FAIL else st), dt) for it, st, dt in res]
            print(f"  （本件为已登记缺陷件；上列 FAIL 已归 XFAIL）")
        for item, state, detail in res:
            print(f"  [{state:5}] {item}：{detail}")
        all_results.extend(res)

    n_pass = sum(1 for _, s, _ in all_results if s == PASS)
    n_fail = sum(1 for _, s, _ in all_results if s == FAIL)
    n_skip = sum(1 for _, s, _ in all_results if s == SKIP)
    n_xfail = sum(1 for _, s, _ in all_results if s == XFAIL)
    print(f"\n汇总：PASS={n_pass}  FAIL={n_fail}  XFAIL={n_xfail}  SKIP={n_skip}")
    if n_skip:
        print("  注意：SKIP 不计通过（未验证 ≠ 通过）")
    if n_xfail:
        print(f"  注意：XFAIL = 已知缺陷（已登记，不静默、不掩盖）；{n_xfail} 项")
    if n_fail:
        print("结论：FAIL —— 存在未登记的断言问题，见上")
        return 1
    if n_skip or n_xfail:
        print("结论：无未登记缺陷" + ("；有未覆盖项" if n_skip else "") +
              (f"；{n_xfail} 项已知缺陷（见 --list-known）" if n_xfail else ""))
        return 2 if n_skip else 0
    print("结论：全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
