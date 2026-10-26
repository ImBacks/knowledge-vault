#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大纲单向导入分诊器（大纲维护员 v3 内置脚本，§20）。

用法:
    python merge_outline.py <本库大纲.md> <对方大纲.md 或对方库根> [--json 输出.json] [--limit 40]

**只读。不写本库、不写对方、不复制正文文件**——落盘一律由 agent 走 §7 写入协议
（详细/写入协议.md，含第 10 条写锁）完成，本脚本只产出「分诊报告 + 导入计划」（--json 供 agent 机读）。

四类分诊（§20）：
  A 直接并入    归一化名字、来源 URL 都无冲突 → 按本库分类树归位
  B 同名同物    归一化名字相同且来源不矛盾 → 字段合并（活/核取新、别名并集、用法追加）
  C 疑似同物    名字不同、来源 URL 相同 → agent 仲裁名字与描述
  D 同名需仲裁  归一化名字相同但来源互斥 → 大概率同名不同物，用户裁决

设计边界（为什么不做「完整自动合并」）：
- 分类树与作用措辞是个人化的（A 的「视频›视频下载」= B 的「视频›下载工具」），机器对不齐；
  能机械化的只有匹配与分诊，判断归 agent，裁决归用户。
- **禁止对 大纲.md 做文本级 git merge**——两库插入位置不同，文本冲突不可收拾。
- 匹配键：归一化名字（NFKC + 去空白 + 小写，同 check_outline）为主，来源 URL 归一化
  （去 scheme/www/.git/尾斜杠/utm 类跟踪参数）为辅。只认「带路径的 URL」做同物证据，
  裸域名与 平台@作者、本地: 来源不参与同物判定（信息量不足，宁缺勿滥）。

限制：仅支持未分片的单文件大纲。对方库已分片（目录升级为分片清单）时报错退出，
请让对方提供完整导出或逐片导入。
"""

import json
import re
import sys
import unicodedata
from pathlib import Path

HEADER_RE = re.compile(r"<!--\s*v(\d+)\s*｜\s*条目\s*(\d+)\s*｜\s*更新\s*([0-9\-]+)\s*-->")
ENTRY_RE = re.compile(r"^-\s+\*\*(.+?)\*\*(?:\s*｜\s*(.*))?$")
RELATED_RE = re.compile(r"^-\s*相关\s*[:：]\s*(.+)$")


def _unwrap_wikilink(s: str) -> str:
    # `- 相关：[[名字]]`（Obsidian 双链，v33 起推荐写法）与裸 `名字` 均接受
    s = s.strip()
    return s[2:-2].strip() if s.startswith("[[") and s.endswith("]]") else s
NOTE_RE = re.compile(r"^-\s*备注\s*[:：]\s*(.*)$")
USAGE_RE = re.compile(r"^-\s*用法\s*[:：]\s*(.*)$")
SHARD_TOC_RE = re.compile(r"^-\s*.+→.+\s*（\d+）\s*$")   # 分片清单行：- 工具库 → 工具库.md（412）
FIELD_SEP = " ｜ "
LIBRARIES = ("工具库", "知识库")
ILLEGAL_PATH_CHARS = set('\\/:*?"<>|')
# 同物判定的跟踪参数前缀（§8：去 utm 参数；顺带常见的国内统计参数）
STRIP_PARAMS = re.compile(r"^(utm_|spm|from|vd_source|share_|fr=)", re.I)
# 只认「host + 路径」形态的 URL 做同物证据；裸域名不带路径，匹配信息量不足
URL_TOKEN_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*\.[a-zA-Z]{2,}(/[^\s；;，,」]*)+")
FIELD_KEYS = ("作用", "形态", "限制", "来源", "正文", "别名", "活", "核")


def utf8_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


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


def norm_url(u: str) -> str:
    u = u.strip()
    u = re.sub(r"^https?://", "", u, flags=re.I)
    u = re.sub(r"^www\.", "", u, flags=re.I)
    if u.lower().endswith(".git"):
        u = u[:-4]
    if "?" in u:
        base, q = u.split("?", 1)
        keep = [kv for kv in q.split("&") if kv and not STRIP_PARAMS.match(kv)]
        u = base + ("?" + "&".join(keep) if keep else "")
    return u.strip("/").lower()


def url_keys(src_field: str) -> set:
    """从来源字段提取「带路径的 URL」归一化键。平台@作者、本地:、裸域名不参与。"""
    keys = set()
    for seg in re.split(r"[;；]", src_field or ""):
        seg = seg.strip()
        if not seg or seg.startswith(("本地:", "本地：")):
            continue
        for m in URL_TOKEN_RE.finditer(seg):
            keys.add(norm_url(m.group(0)))
    return keys


def parse_fields(rest: str) -> dict:
    out = {}
    for seg in (rest or "").split(FIELD_SEP):
        seg = seg.strip()
        if not seg:
            continue
        for key in FIELD_KEYS:
            if seg.startswith(key + "：") or seg.startswith(key + ":"):
                out[key] = seg[len(key) + 1:].strip()
                break
        else:
            for key in ("活", "核"):
                if seg.startswith(key + " ") or seg.startswith(key + "："):
                    out[key] = seg[len(key):].lstrip(" ：:").strip()
                    break
    return out


def parse_outline(path: Path, label: str = "对方") -> dict:
    """解析一份 大纲.md：条目（含备注/用法/相关子行）、分类段、头部、目录形态。

    label：这份文件是「本库」还是「对方」——分片检测的报错指引两边不同（本库分片
    该逐片导入，对方分片该要完整导出），不能一律说「对方库已分片」误导 agent。"""
    entries, warnings = [], []
    lib = cat = None
    cur = None
    version, header_count = None, None
    in_toc = False
    shard_toc = False
    sections = set()          # (lib, cat)，含空分类段
    for i, ln in enumerate(read_text(path).splitlines(), 1):
        s = ln.strip()
        if version is None:
            m = HEADER_RE.search(ln)
            if m:
                version, header_count = int(m.group(1)), int(m.group(2))
        if s.startswith("## "):
            name = s[3:].strip()
            lib = name if name in LIBRARIES else None
            cat = None
            in_toc = (name == "目录")
            continue
        if s.startswith("### "):
            cat = s[4:].strip()
            in_toc = False
            if lib and cat:
                sections.add((lib, cat))
            continue
        if s.startswith("#"):
            lib = cat = None
            in_toc = False
            continue
        if in_toc:
            if SHARD_TOC_RE.match(s):
                shard_toc = True
            continue
        m = ENTRY_RE.match(s)
        if m:
            cur = {
                "name": m.group(1).strip(),
                "norm": norm_name(m.group(1)),
                "fields": parse_fields(m.group(2) or ""),
                "note": "",
                "usages": [],
                "related": [],
                "lib": lib,
                "cat": cat,
                "line": i,
            }
            entries.append(cur)
            continue
        n = NOTE_RE.match(s)
        if n and cur is not None:
            cur["note"] = n.group(1).strip()
            continue
        u = USAGE_RE.match(s)
        if u and cur is not None:
            cur["usages"].append(u.group(1).strip())
            continue
        r = RELATED_RE.match(s)
        if r and cur is not None:
            cur["related"].append(_unwrap_wikilink(r.group(1)))

    if version is None:
        warnings.append("未找到文件头版本行（可能不是标准 大纲.md）")
    elif header_count != len(entries):
        warnings.append(f"文件头声明 {header_count} 条，实际扫到 {len(entries)} 条（以正文为准）")
    seen = {}
    for e in entries:
        if e["norm"] in seen:
            warnings.append(f"库内部名字重复：「{e['name']}」（第 {seen[e['norm']]} 与 {e['line']} 行），后一条按重复跳过")
        else:
            seen[e["norm"]] = e["line"]
    if shard_toc:
        if label == "本库":
            print(f"❌ 本库已分片（{path} 的目录是分片清单，条目在分片文件里），本工具暂不支持。")
            print("   请以目标分片文件（如 工具库.md）代替 大纲.md 作为第一个参数，逐片导入。")
        else:
            print(f"❌ 对方库已分片（{path}，目录是分片清单，条目在分片文件里），本工具暂不支持。")
            print("   请让对方提供未分片的完整导出，或逐片指定分片文件作为 <对方大纲.md>。")
        sys.exit(2)
    return {"version": version, "entries": entries, "sections": sections, "warnings": warnings}


def safe_filename(name: str) -> str:
    return "".join("-" if c in ILLEGAL_PATH_CHARS else c for c in name)


def sub_class(cat: str) -> str:
    """`大类 › 子类` → `子类`（无 › 则原样）。"""
    return cat.split("›")[-1].strip() if cat and "›" in cat else (cat or "")


def section_action(lib: str, cat: str, tgt_sections: set) -> str:
    if (lib, cat) in tgt_sections:
        return "归入现有子类"
    big = cat.split("›")[0].strip() if cat else ""
    if any(l == lib and c.split("›")[0].strip() == big for l, c in tgt_sections):
        return "新建子类（挂现有大类）"
    return "新建大类"


def merge_aliases(a: str, b: str) -> str:
    items, out = [], []
    for side in (a, b):
        for x in re.split(r"[;；]", side or ""):
            x = x.strip()
            if x and x.lower() not in items:
                items.append(x.lower())
                out.append(x)
    return "；".join(out)


def max_date(a: str, b: str) -> str:
    vals = [v for v in ((a or "").strip(), (b or "").strip()) if v]
    return max(vals) if vals else ""


def entry_brief(e: dict) -> dict:
    f = e["fields"]
    return {
        "name": e["name"], "line": e["line"], "lib": e["lib"], "cat": e["cat"],
        "作用": f.get("作用", ""), "形态": f.get("形态", ""), "限制": f.get("限制", ""),
        "来源": f.get("来源", ""), "正文": f.get("正文", ""), "别名": f.get("别名", ""),
        "活": f.get("活", ""), "核": f.get("核", ""),
        "备注": e["note"], "用法": e["usages"], "相关": e["related"],
    }


def main() -> int:
    utf8_console()
    args = sys.argv[1:]
    if len(args) < 2 or args[0].startswith("-") or args[1].startswith("-"):
        print(__doc__)
        return 2
    tgt_md = Path(args[0])
    src_arg = Path(args[1])
    if src_arg.is_dir():
        src_md = src_arg / "大纲.md"
    else:
        src_md = src_arg
    for p in (tgt_md, src_md):
        if not p.exists():
            print(f"错误：文件不存在：{p}")
            return 2
    if tgt_md.resolve() == src_md.resolve():
        print("错误：本库与对方是同一份文件。")
        return 2
    limit = 40
    if "--limit" in args:
        try:
            limit = int(args[args.index("--limit") + 1])
        except (IndexError, ValueError):
            pass

    tgt = parse_outline(tgt_md, "本库")
    src = parse_outline(src_md, "对方")
    warnings = list(tgt["warnings"]) + [f"对方：{w}" for w in src["warnings"]]
    tgt_base, src_base = tgt_md.parent, src_md.parent

    tgt_by_norm = {e["norm"]: e for e in tgt["entries"]}
    tgt_url_index = {}   # url key -> [target entries]
    for e in tgt["entries"]:
        for k in url_keys(e["fields"].get("来源")):
            tgt_url_index.setdefault(k, []).append(e)

    plan = {"A_direct": [], "B_same": [], "C_same_url": [], "D_name_conflict": []}
    skipped = 0
    seen_src = set()
    for e in src["entries"]:
        if e["norm"] in seen_src:
            skipped += 1
            continue
        seen_src.add(e["norm"])
        s_urls = url_keys(e["fields"].get("来源"))
        t = tgt_by_norm.get(e["norm"])
        if t is not None:
            t_urls = url_keys(t["fields"].get("来源"))
            if s_urls and t_urls and not (s_urls & t_urls):
                plan["D_name_conflict"].append({
                    "name": e["name"], "target": entry_brief(t), "source": entry_brief(e),
                })
            else:
                diffs, proposal = {}, {}
                f_t, f_s = t["fields"], e["fields"]
                for key in ("作用", "形态", "限制", "来源", "正文"):
                    if f_t.get(key, "") != f_s.get(key, ""):
                        diffs[key] = {"本库": f_t.get(key, ""), "对方": f_s.get(key, "")}
                if f_t.get("别名", "") != f_s.get("别名", ""):
                    merged = merge_aliases(f_t.get("别名", ""), f_s.get("别名", ""))
                    if merged != f_t.get("别名", ""):
                        proposal["别名"] = merged
                for key in ("活", "核"):
                    m = max_date(f_t.get(key, ""), f_s.get(key, ""))
                    if m and m != f_t.get(key, ""):
                        proposal[key] = m
                if e["usages"]:
                    proposal["用法追加"] = e["usages"]
                if e["note"] and e["note"] != t["note"]:
                    proposal["对方备注"] = e["note"]
                plan["B_same"].append({
                    "name": e["name"], "target_line": t["line"], "source_line": e["line"],
                    "diffs": diffs, "proposal": proposal,
                    "target": entry_brief(t), "source": entry_brief(e),
                })
            continue
        hits = [(k, tgt_url_index[k]) for k in s_urls if k in tgt_url_index]
        if hits:
            matched, seen_names = [], set()
            for k, tentries in hits:
                for te in tentries:
                    if te["norm"] not in seen_names:
                        seen_names.add(te["norm"])
                        matched.append({"name": te["name"], "entry": entry_brief(te)})
            plan["C_same_url"].append({
                "source": entry_brief(e),
                "matched_targets": matched,
                "urls": sorted(k for k, _ in hits),
            })
            continue
        act = section_action(e["lib"], e["cat"], tgt["sections"]) if e["lib"] else "缺库名（对方格式异常）"
        item = {"name": e["name"], "source_line": e["line"], "source": entry_brief(e),
                "section_action": act}
        if e["cat"]:
            # 新建子类/大类先沿用对方分类名，归位微调归 agent（§20）
            item["proposed_section"] = f"{e['lib']} › {e['cat']}"
        if e["fields"].get("正文"):
            rel = e["fields"]["正文"]
            item["body_src"] = {"rel": rel, "exists": (src_base / rel).exists()}
            dst_rel = f"知识/{sub_class(e['cat'])}/{safe_filename(e['name'])}.md"
            item["body_dst"] = {"rel": dst_rel, "exists": (tgt_base / dst_rel).exists()}
        plan["A_direct"].append(item)

    # 相关行悬空检查：指向的条目既不在本库、也不在本次任何导入集
    known = set(tgt_by_norm) | seen_src
    dangling = []
    for cls in ("A_direct", "B_same", "C_same_url", "D_name_conflict"):
        for item in plan[cls]:
            for r in item["source"]["相关"]:
                if norm_name(r) not in known:
                    dangling.append({"from": item["name"], "related": r})
    if dangling:
        warnings.append(f"导入条目有 {len(dangling)} 个 `- 相关：` 指向不存在的条目"
                        f"（要么连带导入，要么修剪该行，不留悬空——体检硬错误）")

    counts = {k: len(v) for k, v in plan.items()}
    print("导入分诊（单向：对方 → 本库，本库是主、对方只读）")
    print(f"本库：v{tgt['version']} ｜ {len(tgt['entries'])} 条 ｜ {tgt_md}")
    print(f"对方：v{src['version']} ｜ {len(src['entries'])} 条 ｜ {src_md}")
    print(f"A 直接并入 {counts['A_direct']} ｜ B 同名可并 {counts['B_same']} ｜ "
          f"C 疑似同物 {counts['C_same_url']} ｜ D 同名需仲裁 {counts['D_name_conflict']}"
          f" ｜ 对方库内部重复跳过 {skipped}")
    for w in warnings:
        print(f"  ⚠ {w}")

    def brief_line(b: dict) -> str:
        body = f" ｜ 正文 {b['正文']}" if b.get("正文") else ""
        return f"{b['name']}（{b['lib']} › {b['cat']}，对方 L{b['line']}）{body}"

    if counts["A_direct"]:
        print(f"\n── A 直接并入（{counts['A_direct']} 条，列前 {limit}）──")
        for i, it in enumerate(plan["A_direct"][:limit], 1):
            e = it["source"]
            print(f"{i}. {brief_line(e)}")
            print(f"   归位：{it['section_action']}"
                  + (f" → {it['proposed_section']}" if it.get("proposed_section") else ""))
            if "body_src" in it:
                ok = "✓" if it["body_src"]["exists"] else "✗ 文件缺失"
                clash = "⚠ 目标已存在" if it["body_dst"]["exists"] else "无冲突"
                print(f"   正文：对方 {it['body_src']['rel']} {ok} → 计划 {it['body_dst']['rel']}（{clash}）")
            miss = [r for r in e["相关"] if norm_name(r) not in known]
            if miss:
                print(f"   ⚠ 相关指向缺失：{'、'.join(miss)}")

    if counts["B_same"]:
        print(f"\n── B 同名可并（{counts['B_same']} 条）──")
        for i, it in enumerate(plan["B_same"][:limit], 1):
            print(f"{i}. {it['name']}（本库 L{it['target_line']} ｜ 对方 L{it['source_line']}）")
            for key, d in it["diffs"].items():
                print(f"   {key}：本库「{d['本库']}」｜ 对方「{d['对方']}」→ 以本库为准，增量由 agent 仲裁")
            for key, v in it["proposal"].items():
                if key == "用法追加":
                    print(f"   用法：对方 {len(v)} 行 → 追加")
                elif key == "对方备注":
                    print(f"   备注：对方「{v}」→ 增量并入与否由 agent 判断")
                else:
                    print(f"   {key} → {v}")
            if not it["diffs"] and not it["proposal"]:
                print("   完全一致，无动作")

    if counts["C_same_url"]:
        print(f"\n── C 疑似同物（{counts['C_same_url']} 条，名字不同、来源相同）──")
        for i, it in enumerate(plan["C_same_url"][:limit], 1):
            tnames = "、".join(m["name"] for m in it["matched_targets"])
            print(f"{i}. 对方「{it['source']['name']}」↔ 本库「{tnames}」｜ URL {'; '.join(it['urls'])}")

    if counts["D_name_conflict"]:
        print(f"\n── D 同名需仲裁（{counts['D_name_conflict']} 条，同名但来源互斥）──")
        for i, it in enumerate(plan["D_name_conflict"][:limit], 1):
            ft, fs = it["target"], it["source"]
            print(f"{i}. {it['name']}")
            print(f"   本库：{ft.get('来源') or '（缺来源）'} ｜ {ft.get('作用', '')[:40]}")
            print(f"   对方：{fs.get('来源') or '（缺来源）'} ｜ {fs.get('作用', '')[:40]}")

    print("\n下一步（§20）：agent 对 B/C/D 逐条仲裁（必要时联网核验，只核本次导入涉及的条目）→"
          " 生成落盘清单 → 用户确认 → §7 写入协议落盘（含第 10 条写锁）→ 日志记「导入」。")
    print("本脚本只读，未改动任何文件；知识正文文件的复制/并入在落盘时执行。")

    if "--json" in args:
        i = args.index("--json")
        if i + 1 >= len(args):
            print("错误：--json 需要一个输出路径（如 --json plan.json）")
            return 2
        out = Path(args[i + 1])
        payload = {
            "target": {"path": str(tgt_md), "version": tgt["version"], "entries": len(tgt["entries"])},
            "source": {"path": str(src_md), "version": src["version"], "entries": len(src["entries"])},
            "warnings": warnings,
            "counts": {**counts, "skipped_source_dups": skipped},
            "plan": plan,
            "dangling_related": dangling,
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n机读计划已写出：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
