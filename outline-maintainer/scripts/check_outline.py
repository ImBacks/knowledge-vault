#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大纲.md 只读自检工具（大纲维护员技能内置脚本）。

用法:
    python check_outline.py <大纲.md 路径>

职责（对应 SKILL.md 的 §0 开局动作 2 与 §7 写入协议 4）:
1. 读文件头版本行 `<!-- vN ｜ 条目 M ｜ 更新 YYYY-MM-DD -->`，报告声明值；
2. 数正文条目：只数 `- **` 开头的行；`- 相关：` 与备注行不计；
3. 数目录：`## 目录` 下每行 `- 路径（数字）`，合计与逐类数字核对；
4. 互查：目录里的分类在正文有没有对应 `###` 标题；正文 `###` 标题是否都进了目录；
5. 退出码：0 = 全部一致；1 = 存在不一致；2 = 文件无法读取 / 用法错误。

本脚本只读，绝不修改大纲文件。
"""

import re
import sys
from pathlib import Path

HEADER_RE = re.compile(r"<!--\s*v(\d+)\s*｜\s*条目\s*(\d+)\s*｜\s*更新\s*([0-9\-]+)\s*-->")
TOC_RE = re.compile(r"^-\s*(.+?)（(\d+)）\s*$")
ENTRY_RE = re.compile(r"^-\s+\*\*")
RELATED_RE = re.compile(r"^-\s*相关\s*[:：]")

LIBRARIES = ("工具库", "知识库")


def read_text(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError(f"无法按 UTF-8 读取 {path}")


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python check_outline.py <大纲.md 路径>")
        return 2
    p = Path(sys.argv[1])
    if not p.exists():
        print(f"错误：文件不存在：{p}")
        return 2
    try:
        text = read_text(p)
    except Exception as e:
        print(f"错误：无法读取 {p}：{e}")
        return 2

    lines = text.splitlines()
    problems = []

    # 1. 文件头版本行
    header = None
    for ln in lines[:10]:
        m = HEADER_RE.search(ln)
        if m:
            header = (int(m.group(1)), int(m.group(2)), m.group(3))
            break
    if header:
        print(f"文件头：v{header[0]} ｜ 条目 {header[1]} ｜ 更新 {header[2]}")
    else:
        problems.append("未找到文件头版本行 `<!-- vN ｜ 条目 M ｜ 更新 YYYY-MM-DD -->`")

    # 2. 目录解析
    toc = []  # (path, declared_count, line_no)
    in_toc = False
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        if s.startswith("## "):
            in_toc = (s[3:].strip() == "目录")
            continue
        if in_toc:
            if s.startswith("#"):
                in_toc = False
                continue
            m = TOC_RE.match(s)
            if m:
                toc.append((m.group(1).strip(), int(m.group(2)), i))
    toc_total = sum(c for _, c, _ in toc)
    print(f"目录：{len(toc)} 行，合计声明 {toc_total} 条")

    # 3. 正文条目与分类计数
    current_lib = None
    current_cat = None
    cat_counts = {}  # (lib, path) -> count
    cat_line = {}    # (lib, path) -> heading line no
    entry_total = 0
    related_total = 0
    heading_order = []
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        if s.startswith("## "):
            name = s[3:].strip()
            current_lib = name if name in LIBRARIES else None
            current_cat = None
            continue
        if s.startswith("### "):
            current_cat = s[4:].strip()
            key = (current_lib, current_cat)
            cat_counts.setdefault(key, 0)
            cat_line[key] = i
            heading_order.append(key)
            continue
        if s.startswith("# "):
            current_lib = None
            current_cat = None
            continue
        if ENTRY_RE.match(s):
            entry_total += 1
            if current_cat and current_lib:
                cat_counts[(current_lib, current_cat)] += 1
            continue
        if RELATED_RE.match(s):
            related_total += 1

    print(f"正文条目：{entry_total}（只数 `- **` 开头行）｜ 相关行 {related_total}（不计条目）")

    # 4. 文件头声明 vs 实际
    if header and header[1] != entry_total:
        problems.append(f"文件头声明条目 {header[1]}，正文实际 {entry_total}，以正文为准")

    # 5. 目录逐类核对 + 互查
    toc_keys = set()
    for path, declared, ln in toc:
        if "›" not in path:
            problems.append(f"目录第 {ln} 行无法解析库名：{path}")
            continue
        # 只在第一个 › 处拆分：库名取前半，子类路径保留「 › 」内部空格，与正文 ### 标题逐字一致
        lib, sub = path.split("›", 1)
        lib, sub = lib.strip(), sub.strip()
        toc_keys.add((lib, sub))
        actual = cat_counts.get((lib, sub), 0)
        if declared != actual:
            problems.append(f"目录「{path}（{declared}）」与正文实际 {actual} 不符（第 {ln} 行）")
    for key in heading_order:
        if key not in toc_keys:
            problems.append(f"正文标题「{key[0]} › {key[1]}」（第 {cat_line[key]} 行）未在目录中列出")
    if toc_total != entry_total:
        problems.append(f"目录合计 {toc_total} 与正文条目 {entry_total} 不一致")

    # 6. 输出
    if problems:
        print("\n不一致：")
        for item in problems:
            print(f"- {item}")
        print("结论：未通过，先修正再报。")
        return 1
    print("结论：一致，可回报。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
