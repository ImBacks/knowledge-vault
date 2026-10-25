#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大纲索引器：把 大纲.md 解析成 SQLite + FTS5 索引（派生层）。

用法:
    python build_index.py <大纲.md 路径>                    # 重建索引
    python build_index.py <大纲.md 路径> --search "去水印"    # 全文查候选
    python build_index.py <大纲.md 路径> --form 插件          # 按形态过滤（主/次形态都算）
    python build_index.py <大纲.md 路径> --related rea        # 查双向相关（正向+反向）
    python build_index.py <大纲.md 路径> --stats

字段 → 索引的对应关系（不是只有作用，详细/体检与规模.md §19）：
  FTS 全文（八列，任一命中即召回）：名字 name ｜ 作用短语 phrases ｜ 完整作用 purpose ｜
      分类 section ｜ 形态 form ｜ 限制 restriction ｜ 来源 source ｜ 别名 aliases
  B-tree 精确：活 active（idx_active）｜ 核 verified（idx_verified；空串 < 任何日期，
      ORDER BY verified 天然把「无核」排最前，正合抽查「无核最优先」）
  不进索引：核（可信度标记，不是检索词）｜ 备注（自由文本，检索数据一律走 `别名` 字段）｜
      正文内容（命中后照常读文件）

三条设计约束（别破坏，破坏了这套就没意义）：
1. **大纲.md 是唯一真相。** 本脚本只读它，产物是 `索引/大纲.db`，随时可删、可全量重建。
   丢了 db 不损失任何信息。
2. **不装中文分词器。** `作用` 字段按格式写成「短语 / 短语 — 说明」，脚本把 `—` 之前
   的部分按 ` / ` 切开、用空格连接后喂给 FTS5 的 unicode61。切分点是人写的短语，
   所以不需要 jieba，主索引也比 trigram 小得多。
3. **查不到就兜底。** 短语精确匹配会漏掉「用户换个说法」的情况，此时退回 LIKE 子串扫描，
   扫描列与 FTS 列保持一致（八列）。这是刻意的：召回靠别名池补，不靠分词器猜。LIKE
   全表扫在几万条内是毫秒级；再大之后换什么、何时换，定论在 详细/体检与规模.md §19「千万档」。

为什么主索引不用 Meilisearch / trigram：
- Meilisearch 要多跑一个服务进程，挂了整个库不能查；单机个人库不值得。
- trigram 对中文免分词，但索引膨胀 3–4 倍、两个字的查询（「改字幕」的「字幕」）会静默返回空，
  且 bm25 在字符窗口上算出来的排序很噪。这些缺点让它不配当**主索引**；但「免分词 + 支持子串」
  正是兜底层要的能力——百万条后 LIKE 扫不动时，trigram 只对 名字+短语+别名 三列建小表上岗（§19 千万档）。
"""

import re
import sqlite3
import sys
from pathlib import Path

HEADER_RE = re.compile(r"<!--\s*v(\d+)\s*｜\s*条目\s*(\d+)\s*｜\s*更新\s*([0-9\-]+)\s*-->")
ENTRY_RE = re.compile(r"^-\s+\*\*(.+?)\*\*(?:\s*｜\s*(.*))?$")
RELATED_RE = re.compile(r"^-\s*相关\s*[:：]\s*(.+)$")
FIELD_SEP = " ｜ "
LIBRARIES = ("工具库", "知识库")

SCHEMA = """
DROP TABLE IF EXISTS entries;
DROP TABLE IF EXISTS entries_fts;
DROP TABLE IF EXISTS related;

CREATE TABLE entries (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    kind     TEXT,              -- 工具 / 知识
    purpose  TEXT,              -- 完整作用（含说明）
    phrases  TEXT,              -- 空格分隔的功能短语（索引主体）
    form     TEXT,
    restriction TEXT,            -- 可选：硬限制
    source   TEXT,
    body     TEXT,              -- 知识条目的正文相对路径
    section  TEXT,              -- 大类 › 子类
    active   TEXT,              -- 活 YYYY-MM-DD（最近活跃）
    verified TEXT,              -- 核 YYYY-MM-DD（可选，最近联网核验；不进 FTS）
    aliases  TEXT,              -- 别名：a；b（可选，检索数据，进 FTS）
    line     INTEGER            -- 在 大纲.md 的行号，用来跳转
);

CREATE VIRTUAL TABLE entries_fts USING fts5(
    name, phrases, purpose, section, form, restriction, source, aliases,
    tokenize = 'unicode61 remove_diacritics 1'
);

-- 双向相关对表：`- 相关：B` 写在 A 名下 → (A, B)。反向查询就是 WHERE tgt = ?。
-- 主数据里每个方向只写一遍，这里两个方向都能查，代价是派生层一张小表。
-- （列名不能用 to，SQLite 保留字）
CREATE TABLE related (
    frm TEXT NOT NULL,          -- 写「相关」行的条目（提到方）
    tgt TEXT NOT NULL           -- 被指向的条目（被提到方）
);
CREATE INDEX idx_related_tgt ON related(tgt);
CREATE INDEX idx_related_frm ON related(frm);
CREATE INDEX idx_active ON entries(active);
-- 核：抽查按「核」最旧挑、无核最优先。空串 < 任何日期，ORDER BY verified 天然如此。
CREATE INDEX idx_verified ON entries(verified);
"""


def read_text(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法按 UTF-8 读取 {path}")


def split_phrases(purpose: str) -> str:
    """`A / B / C — 说明` → `A B C`。没有 `/` 就整段当短语（退化为粗索引）。"""
    head = re.split(r"\s*[—–-]{1,2}\s", purpose, maxsplit=1)[0]
    parts = [p.strip() for p in head.split("/") if p.strip()]
    return " ".join(parts) if parts else purpose.strip()


def parse_fields(rest: str) -> dict:
    out = {}
    for seg in (rest or "").split(FIELD_SEP):
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
                break
    return out


def parse(path: Path):
    entries, lib, cat, version = [], None, None, "?"
    cur = None  # 最近一条条目，「相关」行挂在它名下
    for i, ln in enumerate(read_text(path).splitlines(), 1):
        s = ln.strip()
        if not version or version == "?":
            m = HEADER_RE.search(ln)
            if m:
                version = f"v{m.group(1)}"
        if s.startswith("## "):
            name = s[3:].strip()
            lib, cat = (name if name in LIBRARIES else None), None
            continue
        if s.startswith("### "):
            cat = s[4:].strip()
            continue
        if s.startswith("#"):
            lib, cat = None, None
            continue
        m = ENTRY_RE.match(s)
        if m:
            f = parse_fields(m.group(2) or "")
            purpose = f.get("作用", "")
            cur = {
                "name": m.group(1).strip(),
                "kind": "知识" if "正文" in f else "工具",
                "purpose": purpose,
                "phrases": split_phrases(purpose),
                "form": f.get("形态", ""),
                "restriction": f.get("限制", ""),
                "source": f.get("来源", ""),
                "body": f.get("正文", ""),
                "section": f"{lib or '?'} › {cat or '?'}",
                "active": f.get("活", ""),
                "verified": f.get("核", ""),
                "aliases": f.get("别名", "").replace("；", " "),
                "related": [],
                "line": i,
            }
            entries.append(cur)
            continue
        r = RELATED_RE.match(s)
        if r and cur is not None:
            cur["related"].append(r.group(1).strip())
    return version, entries


def build(db: Path, version: str, entries) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.executemany(
        "INSERT INTO entries (name,kind,purpose,phrases,form,restriction,source,body,section,"
        "active,verified,aliases,line) VALUES (:name,:kind,:purpose,:phrases,:form,:restriction,:source,"
        ":body,:section,:active,:verified,:aliases,:line)",
        entries,
    )
    con.executemany(
        "INSERT INTO entries_fts (rowid, name, phrases, purpose, section, form, restriction, source, aliases)"
        " VALUES ((SELECT id FROM entries WHERE name = ?), ?, ?, ?, ?, ?, ?, ?, ?)",
        [(e["name"], e["name"], e["phrases"], e["purpose"], e["section"], e["form"],
          e["restriction"], e["source"], e["aliases"]) for e in entries],
    )
    con.execute("INSERT INTO entries_fts(entries_fts) VALUES('optimize')")
    pairs = [(e["name"], t) for e in entries for t in e["related"]]
    con.executemany("INSERT INTO related (frm, tgt) VALUES (?, ?)", pairs)
    con.commit()
    con.close()


def search(db: Path, query: str, limit: int = 20) -> None:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    terms = [t for t in query.replace("，", " ").replace(",", " ").split() if t]
    # FTS5 的 MATCH 不接受裸标点，逐词加引号当短语查
    match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
    hits, n_fts = [], 0
    try:
        hits = con.execute(
            "SELECT e.*, bm25(entries_fts) AS rank FROM entries_fts f"
            " JOIN entries e ON e.id = f.rowid"
            " WHERE entries_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
        n_fts = len(hits)
    except sqlite3.OperationalError:
        hits = []
    n_like = 0
    # 召回不足 → 退化子串扫，扫描列与 FTS 列一致（八列）。这是刻意的：短语索引只能
    # 命中"写过的那几个词"，用户换个说法就漏。兜底扫全表，几万条内是毫秒级；
    # 再大之后换 trigram 小表，见 详细/体检与规模.md §19「千万档」。
    if n_fts < 2:
        like = f"%{query}%"
        more = con.execute(
            "SELECT *, -1.0 AS rank FROM entries"
            " WHERE purpose LIKE ? OR name LIKE ? OR phrases LIKE ? OR section LIKE ?"
            " OR form LIKE ? OR restriction LIKE ? OR source LIKE ? OR aliases LIKE ? LIMIT ?",
            (like, like, like, like, like, like, like, like, limit),
        ).fetchall()
        seen = {h["name"] for h in hits}
        add = [m for m in more if m["name"] not in seen]
        n_like = len(add)
        hits = list(hits) + add
    con.close()

    print(f"查询：{query} → 短语索引 {n_fts} 条｜子串兜底 +{n_like} 条｜合计 {len(hits)} 条")
    if not hits:
        print("没找到。请把查询词追加到 日志.md 的「落空记录」。")
        return
    for h in hits:
        star = "★" if h["phrases"] and any(t in h["phrases"] for t in terms) else " "
        print(f"{star} {h['name']}  L{h['line']}  [{h['section']}]")
        print(f"    {h['purpose']}")
        if h["body"]:
            print(f"    →正文 {h['body']}")


def form_query(db: Path, form_value: str, limit: int = 50) -> None:
    """按形态过滤：--form 插件。主/次形态都算（`工具（插件）` 搜「插件」命中）。

    走 FTS 列过滤（form:"X"），是索引查找不是全表扫——千万条量级仍快（详细/体检与规模.md §19）。
    「dsh 插件」这类聚合查询的正式入口。
    """
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    match = 'form:"' + form_value.replace('"', '""') + '"'
    try:
        total = con.execute(
            "SELECT COUNT(*) FROM entries_fts WHERE entries_fts MATCH ?", (match,)
        ).fetchone()[0]
        hits = con.execute(
            "SELECT e.*, bm25(entries_fts) AS rank FROM entries_fts f"
            " JOIN entries e ON e.id = f.rowid"
            " WHERE entries_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        total, hits = 0, []
    if not hits:
        # 形态是封闭枚举（§3），正常都该走 FTS；LIKE 兜底防写法出入（半角括号等）
        like = f"%{form_value}%"
        hits = con.execute(
            "SELECT *, -1.0 AS rank FROM entries WHERE form LIKE ? ORDER BY name LIMIT ?",
            (like, limit),
        ).fetchall()
        total = len(hits)
    con.close()

    more = f"（列前 {limit}）" if total > len(hits) else ""
    print(f"形态过滤：{form_value} → {total} 条{more}")
    if not hits:
        print("没找到。形态是封闭枚举（§3）：工具｜网站｜插件｜Skill｜项目｜模型｜课程｜书｜文章｜概念｜硬件｜服务")
        return
    for h in hits:
        print(f"  {h['name']}  L{h['line']}  [{h['section']}]")
        print(f"    形态：{h['form']} ｜ {h['purpose']}")
        if h["body"]:
            print(f"    →正文 {h['body']}")


def related(db: Path, name: str) -> None:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    if not con.execute("SELECT 1 FROM entries WHERE name = ?", (name,)).fetchall():
        print(f"查无此条目：{name}（按条目名精确匹配，主键唯一）")
        con.close()
        return
    fwd = [r["tgt"] for r in con.execute("SELECT tgt FROM related WHERE frm = ? ORDER BY tgt", (name,))]
    rev = [r["frm"] for r in con.execute("SELECT frm FROM related WHERE tgt = ? ORDER BY frm", (name,))]
    con.close()
    print(f"「{name}」的双向相关：")
    print(f"  正向（它提到）：{'、'.join(fwd) if fwd else '无'}")
    print(f"  反向（提到它）：{'、'.join(rev) if rev else '无'}")


def stats(db: Path, src: Path, entries) -> None:
    tools = sum(1 for e in entries if e["kind"] == "工具")
    know = len(entries) - tools
    print(f"源文件：{src}（{src.stat().st_size/1024:.1f} KB）")
    print(f"条目：{len(entries)}（工具 {tools} / 知识 {know}）")
    n_rel = sum(len(e["related"]) for e in entries)
    print(f"相关：{n_rel} 对" + ("（还没有，--related 要等第一条 `- 相关：` 行" if not n_rel else ""))
    print(f"索引：{db}（{db.stat().st_size/1024:.1f} KB）" if db.exists() else "索引：未建立")
    secs = {}
    for e in entries:
        secs[e["section"]] = secs.get(e["section"], 0) + 1
    print(f"分类：{len(secs)} 个")
    for k, v in sorted(secs.items(), key=lambda x: -x[1]):
        flag = "  ⚠ 超过 300" if v > 300 else ""
        print(f"  {v:>5}  {k}{flag}")
    prim = {}
    for e in entries:
        p = e["form"].split("（")[0].strip() or "（缺形态）"
        prim[p] = prim.get(p, 0) + 1
    print("形态分布（按主形态）：")
    for k, v in sorted(prim.items(), key=lambda x: -x[1]):
        print(f"  {v:>5}  {k}")
    acts = sorted((e["active"], e["name"]) for e in entries if e["active"])
    if acts:
        print(f"最久未活跃：{acts[0][0]}  {acts[0][1]}")
        print(f"最近活跃：{acts[-1][0]}  {acts[-1][1]}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    if not src.exists():
        print(f"错误：文件不存在：{src}")
        return 2
    db = src.parent / "索引" / "大纲.db"
    version, entries = parse(src)

    if "--search" in sys.argv:
        q = sys.argv[sys.argv.index("--search") + 1]
        if not db.exists():
            build(db, version, entries)
        search(db, q)
        return 0

    if "--form" in sys.argv:
        q = sys.argv[sys.argv.index("--form") + 1]
        if not db.exists():
            build(db, version, entries)
        form_query(db, q)
        return 0

    if "--related" in sys.argv:
        q = sys.argv[sys.argv.index("--related") + 1]
        if not db.exists():
            build(db, version, entries)
        related(db, q)
        return 0

    build(db, version, entries)
    if "--stats" in sys.argv:
        stats(db, src, entries)
    else:
        print(f"已重建 {db}：{version}，{len(entries)} 条，{db.stat().st_size/1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
