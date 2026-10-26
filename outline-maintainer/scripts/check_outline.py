#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大纲.md 自检 + 体检工具（大纲维护员 v3 内置脚本）。

用法:
    python check_outline.py <大纲.md 路径>

只读，绝不修改大纲文件。

分两类输出：
  硬错误（❌）—— 结构不一致，必须先修正；退出码 1
  警告（⚠）  —— 内容健康度问题，不阻塞，但要报给用户；退出码仍 0

硬错误检查项（对应 §3 条目格式 / §7 写入协议 4 / §15 知识正文）:
1. 文件头版本行 `<!-- vN ｜ 条目 M ｜ 更新 YYYY-MM-DD -->` 存在且条目数与正文一致；
2. 目录逐类数字与正文实际一致、目录合计一致；
3. 正文 `###` 标题与目录条目双向互查；
4. 名字主键唯一（归一化：大小写 / 全半角 / 空格）；
5. 每条必须有 `活 YYYY-MM-DD`，格式合法、不是未来日期；
   可选的 `核 YYYY-MM-DD`（最近联网核验，§3）写了才查：格式合法、不是未来日期；
6. 知识条目 `正文：` 指向的文件必须存在，且首行是与条目名一致的一级标题；
7. `- 相关：` 指向的条目必须存在。

警告检查项（对应 §18 体检）:
A. 仍有条目使用已废弃的 `范围：` 字段（应改 `限制：` 或删掉）；
B. 单条子类（只有 1 条的分类 = 分类过碎；条目 < 30 时不报，少必然碎）；
C. `（待核验）` / `（待补充）` 条目数与占比（库的腐坏速度指标）；
D. 缺来源条目；
E. 作用字段超过 60 字（索引行退化成摘要，检索信噪比下降）；
F. 文件头日期落后于操作日志最后一条**落盘行**日期（§17 检索行不落盘、不升版本，
   不计入比对——否则每个检索日都会误报）；
G. 索引条目数逼近单文件阈值（> 700 条时提示分片）；
H. 抽查候选（§18：库 ≥20 条、距上次抽查 ≥30 天、存在超 90 天未核或无 `核` 的条目）。
   这是事实新鲜度信号，不是淘汰建议——抽查更新事实，从不删条目。
I. 无法识别的字段段（条目行里不匹配任何已知字段的段——疑似字段名手误或未登记的新字段）。

另输出「近 30 天检索 N 次、落空 M 次」（§19 度量；数据源 = §17 检索行
`- 日期 ｜ 检索 「词」 命中 N`，v27 起记录，没有就静默跳过）。

**不检查「久未使用」，也不提议淘汰**——淘汰由用户自己提（§6、§18）。

退出码：0 = 结构一致（可能有警告）；1 = 存在硬错误；2 = 文件不存在 / 用法错误。
"""

import re
import sys
import unicodedata
from datetime import date, timedelta
from pathlib import Path

HEADER_RE = re.compile(r"<!--\s*v(\d+)\s*｜\s*条目\s*(\d+)\s*｜\s*更新\s*([0-9\-]+)\s*-->")
TOC_RE = re.compile(r"^-\s*(.+?)（(\d+)）\s*$")
ENTRY_RE = re.compile(r"^-\s+\*\*(.+?)\*\*(?:\s*｜\s*(.*))?$")
RELATED_RE = re.compile(r"^-\s*相关\s*[:：]\s*(.+)$")
NOTE_RE = re.compile(r"^-\s*备注\s*[:：]\s*(.*)$")
LOG_RE = re.compile(r"^-\s*(\d{4}-\d{2}-\d{2})\b")
# §17 检索行：`- 2026-10-25 ｜ 检索 「查询词」 命中 3`（唯一不落盘也记的日志行）
SEARCH_LOG_RE = re.compile(r"^-\s*(\d{4}-\d{2}-\d{2})\s*｜\s*检索\s*「(.*?)」\s*命中\s*(\d+)")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FIELD_SEP = " ｜ "
BODY_HEAD_RE = re.compile(r"^<!--\s*正文：")

LIBRARIES = ("工具库", "知识库")
ILLEGAL_PATH_CHARS = set('\\/:*?"<>|')
PENDING_MARKS = ("（待核验）", "（待补充）")
INDEX_SOFT_LIMIT = 700
FRAGMENT_MIN_ENTRIES = 30
KNOWLEDGE_ACT_LIMIT = 60
TOOL_ACT_LIMIT = 60
SPOT_CHECK_MIN_ENTRIES = 20   # 库小于此数不提示抽查（新建的库全新鲜）
SPOT_CHECK_STALE_DAYS = 90    # `核` 超过这个天数算待抽查
SPOT_CHECK_INTERVAL = 30      # 两次抽查的最小间隔（天）


def read_text(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法按 UTF-8 读取 {path}")


def norm_name(name: str) -> str:
    s = unicodedata.normalize("NFKC", name)
    s = re.sub(r"\s+", "", s)
    return s.lower()


def parse_fields(rest: str):
    """把 `作用：… ｜ 形态：… ｜ 活 2026-10-23 ｜ …` 拆成 dict。

    `活` 不带冒号（格式是「活 2026-10-23」），单独认。
    """
    out = {}
    if not rest:
        return out
    for seg in rest.split(FIELD_SEP):
        seg = seg.strip()
        if not seg:
            continue
        matched = False
        for key in ("作用", "形态", "限制", "范围", "来源", "正文", "别名"):
            if seg.startswith(key + "：") or seg.startswith(key + ":"):
                out[key] = seg[len(key) + 1:].strip()
                matched = True
                break
        if matched:
            continue
        for key in ("活", "核"):
            if seg.startswith(key + " ") or seg.startswith(key + "：") or seg.startswith(key + ":"):
                out[key] = seg[len(key):].lstrip(" ：:").strip()
                matched = True
                break
        if not matched:
            out.setdefault("其它", []).append(seg)
    return out


def utf8_console():
    """Windows 控制台默认 GBK，统一成 UTF-8 防止中文/符号打印报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> int:
    utf8_console()
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
    base = p.parent
    hard = []
    warn = []

    # ---------- 1. 文件头 ----------
    header = None
    for ln in lines[:10]:
        m = HEADER_RE.search(ln)
        if m:
            header = (int(m.group(1)), int(m.group(2)), m.group(3))
            break
    if header:
        print(f"文件头：v{header[0]} ｜ 条目 {header[1]} ｜ 更新 {header[2]}")
    else:
        hard.append("未找到文件头版本行 `<!-- vN ｜ 条目 M ｜ 更新 YYYY-MM-DD -->`")

    # ---------- 2. 目录 ----------
    toc = []
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

    # ---------- 3. 正文扫描 ----------
    current_lib = None
    current_cat = None
    cat_counts = {}
    cat_line = {}
    heading_order = []
    entries = []  # dict(name, fields, line, lib, cat)
    related = []  # (target, line)
    log_dates = []        # 落盘行日期（警告 F 只比这些——检索行不落盘，不计入）
    spotcheck_dates = []
    search_log = []       # 检索行 (日期, 命中数)
    in_log = False

    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        if s.startswith("## "):
            name = s[3:].strip()
            current_lib = name if name in LIBRARIES else None
            current_cat = None
            in_log = (name == "操作日志")
            continue
        if s.startswith("### "):
            current_cat = s[4:].strip()
            key = (current_lib, current_cat)
            cat_counts.setdefault(key, 0)
            cat_line[key] = i
            heading_order.append(key)
            in_log = False
            continue
        if s.startswith("# "):
            current_lib = None
            current_cat = None
            in_log = False
            continue
        if in_log:
            sm = SEARCH_LOG_RE.match(s)
            if sm:
                search_log.append((sm.group(1), int(sm.group(3))))
                continue
            m = LOG_RE.match(s)
            if m:
                log_dates.append(m.group(1))
                if "抽查" in s:
                    spotcheck_dates.append(m.group(1))
            continue
        m = ENTRY_RE.match(s)
        if m:
            fields = parse_fields(m.group(2) or "")
            entries.append({
                "name": m.group(1).strip(),
                "fields": fields,
                "line": i,
                "lib": current_lib,
                "cat": current_cat,
            })
            if current_cat and current_lib:
                cat_counts[(current_lib, current_cat)] += 1
            continue
        r = RELATED_RE.match(s)
        if r:
            related.append((r.group(1).strip(), i))
            continue
        # 备注写在条目行的子行（`  - 备注：…`），挂到最近一条条目上，
        # 否则待核验检查扫不到备注里的标记
        n = NOTE_RE.match(s)
        if n and entries:
            entries[-1]["fields"]["备注"] = n.group(1).strip()

    entry_total = len(entries)
    know = [e for e in entries if "正文" in e["fields"]]
    print(f"正文条目：{entry_total} ｜ 相关行 {len(related)}（不计条目）｜ 知识条目（带正文）{len(know)}")

    if log_dates:
        print(f"操作日志：{len(log_dates)} 行，最后 {log_dates[-1]}（大纲.md 内联，旧格式）")
    else:
        log_path = base / "日志.md"
        if log_path.exists():
            try:
                # 只认 `## 操作日志` 段：落空记录区的行同样是「- 日期 …」格式，
                # 混进来会污染"日志最后日期"的判断
                in_file_log = False
                for ln in read_text(log_path).splitlines():
                    s = ln.strip()
                    if s.startswith("## "):
                        in_file_log = (s[3:].strip() == "操作日志")
                        continue
                    if not in_file_log:
                        continue
                    sm = SEARCH_LOG_RE.match(s)
                    if sm:
                        search_log.append((sm.group(1), int(sm.group(3))))
                        continue
                    m = LOG_RE.match(s)
                    if m:
                        log_dates.append(m.group(1))
                        if "抽查" in s:
                            spotcheck_dates.append(m.group(1))
            except Exception as ex:
                warn.append(f"日志.md 无法读取：{ex}")
        if log_dates:
            print(f"操作日志：{len(log_dates)} 行，最后 {log_dates[-1]}（日志.md）")
        else:
            warn.append("找不到操作日志（日志.md 的 `## 操作日志`，或 大纲.md 内联）→ 清理排序与落空率统计缺少依据（§17）")

    # 近 30 天检索/落空率（§19 度量）：数据源是检索行，有才报
    if search_log:
        cut = (date.today() - timedelta(days=30)).isoformat()
        recent = [(d, n) for d, n in search_log if d >= cut]
        if recent:
            miss = sum(1 for _, n in recent if n == 0)
            print(f"检索（近 30 天）：{len(recent)} 次 ｜ 落空 {miss} 次（{miss / len(recent):.0%}）"
                  f"→ 高 = 覆盖不够或命名不对（详细/体检与规模.md 度量指标）")

    # ---------- 4. 硬检查 ----------
    if header and header[1] != entry_total:
        hard.append(f"文件头声明条目 {header[1]}，正文实际 {entry_total}，以正文为准")

    toc_keys = set()
    for path, declared, ln in toc:
        if "›" not in path:
            hard.append(f"目录第 {ln} 行无法解析库名：{path}")
            continue
        lib, sub = path.split("›", 1)
        lib, sub = lib.strip(), sub.strip()
        toc_keys.add((lib, sub))
        actual = cat_counts.get((lib, sub), 0)
        if declared != actual:
            hard.append(f"目录「{path}（{declared}）」与正文实际 {actual} 不符（第 {ln} 行）")
    for key in heading_order:
        if key not in toc_keys:
            hard.append(f"正文标题「{key[0]} › {key[1]}」（第 {cat_line[key]} 行）未在目录中列出")
    if toc_total != entry_total:
        hard.append(f"目录合计 {toc_total} 与正文条目 {entry_total} 不一致")

    seen = {}
    for e in entries:
        k = norm_name(e["name"])
        if k in seen:
            hard.append(f"名字重复：「{e['name']}」（第 {seen[k]} 行与第 {e['line']} 行）")
        else:
            seen[k] = e["line"]
    known_names = set(seen)

    for e in know:
        rel = e["fields"]["正文"].strip()
        if not rel:
            hard.append(f"「{e['name']}」的 `正文：` 字段为空（第 {e['line']} 行）")
            continue
        target = (base / rel).resolve()
        if not target.exists():
            hard.append(f"「{e['name']}」的正文文件不存在：{rel}（第 {e['line']} 行）")
            continue
        try:
            blines = read_text(target).splitlines()
        except Exception as ex:
            hard.append(f"「{e['name']}」正文无法读取：{rel}（{ex}）")
            continue
        first = next((x.strip() for x in blines if x.strip()), "")
        if not first.startswith("# "):
            hard.append(f"「{e['name']}」正文首行不是一级标题：{rel}")
        elif norm_name(first[2:]) != norm_name(e["name"]):
            warn.append(f"「{e['name']}」正文一级标题不一致（{first[2:].strip()}）：{rel}")
        if not any(BODY_HEAD_RE.match(x.strip()) for x in blines[:5]):
            warn.append(f"「{e['name']}」正文头部缺 `<!-- 正文：… -->`（提取日期 / 原文路径）：{rel}")
        if any(c in ILLEGAL_PATH_CHARS for c in e["name"]):
            warn.append(f"「{e['name']}」名字含路径非法字符，正文文件名需替换：{rel}")

    for target, ln in related:
        t = norm_name(target)
        if t not in known_names:
            hard.append(f"`- 相关：{target}`（第 {ln} 行）指向的条目不存在")

    # 活 日期：必填、格式合法、不能是未来
    today = date.today()
    for e in entries:
        v = e["fields"].get("活")
        if not v:
            hard.append(f"「{e['name']}」缺 `活 YYYY-MM-DD` 字段（第 {e['line']} 行）")
        elif not DATE_RE.match(v):
            hard.append(f"「{e['name']}」的 `活` 日期格式非法：{v}（应为 YYYY-MM-DD，第 {e['line']} 行）")
        else:
            try:
                if date.fromisoformat(v) > today:
                    warn.append(f"「{e['name']}」的 `活` 是未来日期：{v}")
            except ValueError:
                hard.append(f"「{e['name']}」的 `活` 日期不合法：{v}")

    # 核 日期（可选，§3：最近联网核验通过的日期）：写了才查
    for e in entries:
        v = e["fields"].get("核")
        if not v:
            continue
        if not DATE_RE.match(v):
            hard.append(f"「{e['name']}」的 `核` 日期格式非法：{v}（应为 YYYY-MM-DD，第 {e['line']} 行）")
            continue
        try:
            if date.fromisoformat(v) > today:
                warn.append(f"「{e['name']}」的 `核` 是未来日期：{v}")
        except ValueError:
            hard.append(f"「{e['name']}」的 `核` 日期不合法：{v}")

    # ---------- 5. 警告体检 ----------
    legacy = [e for e in entries if "范围" in e["fields"]]
    if legacy:
        warn.append(f"仍在使用已废弃的 `范围：` 字段 {len(legacy)} 条（{len(legacy)/max(entry_total,1):.0%}）"
                    f"→ 改成 `限制：` 一句话或直接删掉（§3）")

    # 无法识别的字段段：parse_fields 归入「其它」的段此前被静默吞掉，字段名手误会无声通过全部检查
    stray = [e for e in entries if e["fields"].get("其它")]
    if stray:
        sample = "；".join(
            f"「{e['name']}」L{e['line']}：" + "、".join(str(x) for x in e["fields"]["其它"][:2])
            for e in stray[:3])
        warn.append(f"无法识别的字段段 {len(stray)} 处 → 疑似字段名手误或未登记的新字段：{sample}"
                    f" → 按 §3 字段名订正；确属新字段先改 §3 再用")

    # 分类过碎：条目少时必然碎（7 条 6 类属正常），只在规模足够时才是信号
    singles = [(k, v) for k, v in cat_counts.items() if k[0] and v == 1]
    if entry_total >= FRAGMENT_MIN_ENTRIES and len(singles) >= 3:
        names = "、".join(f"{k[1]}" for k, _ in singles)
        warn.append(f"单条子类 {len(singles)} 个：{names} → 分类过碎，考虑合并")

    pending = [e for e in entries
               if any(mk in (e["fields"].get("作用", "") + e["fields"].get("备注", "")) for mk in PENDING_MARKS)]
    if pending:
        warn.append(f"待核验 / 待补充：{len(pending)} 条（{len(pending)/max(entry_total,1):.0%}）→ 检查是否有超期未处理的")

    no_src = [e for e in entries if not e["fields"].get("来源")]
    if no_src:
        warn.append(f"缺来源：{len(no_src)} 条 → " + "、".join(e["name"] for e in no_src[:5]))

    for e in entries:
        act = e["fields"].get("作用", "")
        limit = KNOWLEDGE_ACT_LIMIT if "正文" in e["fields"] else TOOL_ACT_LIMIT
        if len(act) > limit:
            warn.append(f"「{e['name']}」作用 {len(act)} 字，超过 {limit} 字上限（第 {e['line']} 行）→ 展开内容应进正文或备注")

    if header and log_dates and header[2] < log_dates[-1]:
        warn.append(f"文件头更新日期 {header[2]} 落后于操作日志最后一条 {log_dates[-1]}")

    if entry_total > INDEX_SOFT_LIMIT:
        warn.append(f"索引 {entry_total} 条，超过单文件阈值 {INDEX_SOFT_LIMIT} → 按 §19 拆分片")

    # 抽查提示（§18）：事实新鲜度信号，不是淘汰建议
    if entry_total >= SPOT_CHECK_MIN_ENTRIES:
        stale_cut = (today - timedelta(days=SPOT_CHECK_STALE_DAYS)).isoformat()

        def _spot_candidate(e):
            """抽查候选 = 无「核」或「核」超期；已失效条目不算（已知死，不用再核）。"""
            if "（已失效" in e["fields"].get("作用", ""):
                return False
            v = e["fields"].get("核")
            return not v or v < stale_cut

        cands = [e for e in entries if _spot_candidate(e)]
        no_ver = sum(1 for e in cands if not e["fields"].get("核"))
        last_sc = spotcheck_dates[-1] if spotcheck_dates else None
        sc_due = last_sc is None or (today - date.fromisoformat(last_sc)).days >= SPOT_CHECK_INTERVAL
        if cands and sc_due:
            vers = sorted(e["fields"]["核"] for e in cands if e["fields"].get("核"))
            oldest = vers[0] if vers else "无（全部未核验）"
            sc_note = f"上次抽查 {last_sc}" if last_sc else "从未抽查"
            warn.append(
                f"抽查候选：{len(cands)} 条（含无「核」{no_ver} 条）超 {SPOT_CHECK_STALE_DAYS} 天未核或从未核验，"
                f"最老「核」{oldest}，{sc_note} → 按 §18 半自动抽查（≤5 条，过程自动，只报结果）"
            )

    # ---------- 6. 输出 ----------
    if warn:
        print(f"\n警告 {len(warn)} 条：")
        for w in warn:
            print(f"  ⚠ {w}")
    if hard:
        print(f"\n硬错误 {len(hard)} 条：")
        for h in hard:
            print(f"  ❌ {h}")
        print("\n结论：结构不一致，先修正再报。")
        return 1
    if warn:
        print("\n结论：结构一致，有上述警告，照实回报；不要静默忽略。")
    else:
        print("\n结论：结构一致，无警告。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
