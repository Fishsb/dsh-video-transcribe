# -*- coding: utf-8 -*-
"""解析 画面信息.md（多帧 OCR），构建 18 本书的书名/首订/星级变体矩阵。
用法: python parse_frames.py <画面信息.md 路径>
输出: 每本书每个字段的多帧变体列表，用于三方交叉验证。"""
import io, re, sys, json

def load(path):
    with io.open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()

def main(path):
    text = load(path)
    # 按帧切块
    frame_blocks = re.split(r'^## 帧\d+（', text, flags=re.M)
    rows = []  # (frame_idx, rownum, raw_line)
    for i, block in enumerate(frame_blocks[1:], 1):
        # 帧内所有表格行/Row 行
        for line in block.split('\n'):
            s = line.strip()
            if not s:
                continue
            m = re.match(r'\|?\s*(Row\s*)?(\d{1,2})\s*\||^\|\s*(\d{1,2})\s*\|', s)
            m2 = re.match(r'\|?\s*(\d{1,2})\s*\|\s*([^|]+?)\s*\|', s)
            m3 = re.match(r'Row (\d{1,2}):\s*(.+)$', s)
            m4 = re.match(r'\| (\d{1,2}) \| ([^|]+) \|', s)
            rec = None
            if m3:
                rec = (i, int(m3.group(1)), m3.group(2))
            elif m4:
                rec = (i, int(m4.group(1)), m4.group(2))
            elif m2 and int(m2.group(1)) >= 1:
                rec = (i, int(m2.group(1)), m2.group(2))
            if rec:
                rows.append(rec)
    # 汇总每本书
    books = {}
    for frame, rownum, raw in rows:
        if rownum < 1 or rownum > 20:
            continue
        books.setdefault(rownum, []).append((frame, raw))
    for num in sorted(books):
        print(f"\n=== 书 #{num} ===")
        for frame, raw in books[num][:6]:
            print(f"  帧{frame}: {raw[:150]}")

if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '画面信息.md')
