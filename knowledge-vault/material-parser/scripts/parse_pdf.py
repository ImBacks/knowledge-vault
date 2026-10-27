# -*- coding: utf-8 -*-
"""大 PDF / EPUB 快速解析：文本层直提 + 扫描页逐页 OCR 兜底 + 书签/页数分块。

用法：
    python scripts/parse_pdf.py <PDF/EPUB 路径> [--chunk-pages 50] [--dpi 150]
                                                   [--no-ocr] [--force] [--cache-root 目录]

产物（解析缓存/<文件名>-<sha1前10位>/）：
    概览.md            章节表 + chunk 索引
    chunks/chunk-NN.md 分块正文，每页开头带 【pN】 页码标记
    manifest.json      sha1、参数、进度、chunk 清单
    ocr_pages.jsonl    OCR 逐页结果（断点续跑依据）

断点续跑：OCR 按页落盘，中断后重跑同一命令自动续，已识别页不重跑。
缓存命中：status=done 且参数一致 → 秒回；参数变化 → 重建产物（dpi 未变时 OCR 结果按页复用，
dpi 变化或 --force 则 OCR 断点作废重识别）。
缓存根：--cache-root > 环境变量 MATERIAL_PARSER_CACHE > 向上找 大纲.md 定位库根；三级落空则报错。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C


# ---------------------------------------------------------------- 分块规划

def plan_chunks(n_pages, toc, chunk_pages):
    """返回 [(标题|None, 起始页, 结束页)]，1-based 闭区间，覆盖 1..n_pages 不留缺口。

    有书签按一级书签切章：书签前的页（封面/目录等）自成一块；同起始页的书签只留
    第一个（避免 end<start 的空章把整章页丢掉）；超过 2*chunk_pages 的章再均分。
    无书签每 chunk_pages 页一块。
    """
    chapters = []
    if toc:
        entries = [(t, p) for l, t, p in toc if l == 1]
        if not entries:
            entries = [(t, p) for _l, t, p in toc]
        kept, seen = [], set()
        for title, start in sorted(entries, key=lambda x: x[1]):
            start = max(1, min(start, n_pages))
            if start in seen:
                continue
            seen.add(start)
            kept.append((str(title).strip(), start))
        for i, (title, start) in enumerate(kept):
            end = kept[i + 1][1] - 1 if i + 1 < len(kept) else n_pages
            chapters.append((title, start, end))
        if kept and kept[0][1] > 1:
            chapters.insert(0, ("（书签前：封面/目录等）", 1, kept[0][1] - 1))
    if not chapters:
        chunks = [(None, s, min(s + chunk_pages - 1, n_pages))
                  for s in range(1, n_pages + 1, chunk_pages)]
        return chunks

    chunks = []
    for title, s, e in chapters:
        total = e - s + 1
        if total <= chunk_pages * 2:
            chunks.append((title, s, e))
            continue
        n_parts = math.ceil(total / chunk_pages)
        size = math.ceil(total / n_parts)
        for k in range(n_parts):
            ps = s + k * size
            pe = min(ps + size - 1, e)
            chunks.append((f"{title}（{k + 1}/{n_parts}）", ps, pe))
    return chunks


def write_chunks(cache_dir: Path, chunks, page_texts, src_name):
    chunks_dir = cache_dir / "chunks"
    chunks_dir.mkdir(exist_ok=True)
    for f in chunks_dir.glob("chunk-*.md"):
        f.unlink()
    out = []
    for i, (title, s, e) in enumerate(chunks):
        rel = f"chunks/chunk-{i:02d}.md"
        rng = f"p{s}–p{e}"
        head = f"# {title}（{rng}）" if title else f"# {rng}"
        lines = [f"<!-- material-parser ｜ chunk {i:02d} ｜ 第 {s}–{e} 页 ｜ 来源 {src_name} -->",
                 head, ""]
        for p in range(s, e + 1):
            txt = (page_texts.get(p) or "").strip()
            lines.append(f"【p{p}】{txt}".rstrip())
        (cache_dir / rel).write_text("\n".join(lines) + "\n", encoding="utf-8")
        out.append({"file": rel, "start": s, "end": e, "title": title or ""})
    return out


def write_overview(cache_dir: Path, src_name, sha1, n_pages, stats, chunks, toc, params):
    lines = [f"# 概览 ｜ {src_name}", "",
             f"- sha1：{sha1[:10]} ｜ 页数：{n_pages} ｜ 生成：{C.today()}",
             f"- 文本层：{stats['text_pages']} 页直提"
             f" ｜ OCR：{stats['ocr_pages']} 页{'（' + stats['ocr_brief'] + '）' if stats['ocr_brief'] else ''}",
             f"- 分块：{len(chunks)} 块（书签分章，超长章按 ≤{params['chunk_pages']} 页拆段）"
             if toc else
             f"- 分块：{len(chunks)} 块（无书签，每 {params['chunk_pages']} 页一块）",
             "",
             "> 正文引用一律指向原文件 + 页码（【pN】标记），不要指向本缓存目录。", ""]
    if toc:
        lines += ["## 章节表（书签）", "", "| 章节 | 起始页 |", "|---|---|"]
        for _l, t, p in toc:
            if _l == 1:
                lines.append(f"| {t} | {p} |")
        lines.append("")
    lines += ["## chunk 索引", "", "| chunk | 页范围 | 内容 | 文件 |", "|---|---|---|---|"]
    for i, c in enumerate(chunks):
        lines.append(f"| {i:02d} | p{c['start']}–p{c['end']} | {c['title'] or '（无书签）'} | {c['file']} |")
    (cache_dir / "概览.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- OCR

def ocr_page(page, dpi, ocr):
    import numpy as np
    pix = page.get_pixmap(dpi=dpi)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        img = img[:, :, :3]
    img = img[:, :, ::-1].copy()  # RGB → BGR
    return ocr(img)


def make_ocr():
    """兼容新旧两代 RapidOCR：rapidocr v2+ 返回 .txts；rapidocr_onnxruntime v1 返回 (list, elapse)。"""
    try:
        from rapidocr import RapidOCR  # v2/v3 统一包
        engine = RapidOCR()

        def run(img):
            out = engine(img)
            txts = getattr(out, "txts", None)
            return "\n".join(txts) if txts else ""
        return run
    except ImportError:
        from rapidocr_onnxruntime import RapidOCR  # v1 旧包（Py≤3.12）
        engine = RapidOCR()

        def run(img):
            result, _ = engine(img)
            return "\n".join(str(line[1]) for line in result) if result else ""
        return run


def run_ocr(doc, todo, cache_dir: Path, dpi):
    """逐页 OCR，结果按页追加到 ocr_pages.jsonl（断点依据）。返回 {页码: 文本}。"""
    ocr_path = cache_dir / "ocr_pages.jsonl"
    done = {}
    if ocr_path.exists():
        for line in ocr_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                done[int(rec["page"])] = rec.get("text", "")
            except Exception:
                continue
    todo = [p for p in todo if p not in done]
    if not todo:
        return done, 0

    ocr = make_ocr()
    limit = C.test_limit(C.ENV_FAIL_OCR)
    print(f"OCR：需识别 {len(todo)} 页（断点已有 {len(done)} 页），约 1~2 秒/页 …")
    n_new = 0
    with open(ocr_path, "a", encoding="utf-8") as f:
        for p in todo:
            text = ocr_page(doc[p - 1], dpi, ocr)
            f.write(json.dumps({"page": p, "text": text}, ensure_ascii=False) + "\n")
            f.flush()
            done[p] = text
            n_new += 1
            if n_new % 5 == 0:
                print(f"  OCR 进度：{n_new}/{len(todo)} 页")
            if limit and n_new >= limit:
                raise RuntimeError(f"selftest 注入中断（已完成 {n_new} 页 OCR，断点已落盘）")
    return done, n_new


# ---------------------------------------------------------------- 主流程

def main():
    C.utf8_console()
    ap = argparse.ArgumentParser(description="大 PDF / EPUB 快速解析（文本层 + OCR 兜底 + 分块缓存）")
    ap.add_argument("pdf", help="PDF / EPUB 文件路径（fitz 兼容格式 xps/mobi/cbz 等同样可试）")
    ap.add_argument("--chunk-pages", type=int, default=C.DEFAULT_CHUNK_PAGES,
                    help=f"无书签时每块页数（默认 {C.DEFAULT_CHUNK_PAGES}），书签章超长时按此拆段")
    ap.add_argument("--dpi", type=int, default=150, help="OCR 渲染分辨率（默认 150）")
    ap.add_argument("--no-ocr", action="store_true", help="跳过 OCR，扫描页留空")
    ap.add_argument("--force", action="store_true", help="忽略缓存全部重做（含 OCR）")
    ap.add_argument("--cache-root", default=None,
                    help="缓存根目录（默认：MATERIAL_PARSER_CACHE 或向上定位 <库根>/解析缓存）")
    args = ap.parse_args()

    pdf = Path(args.pdf).resolve()
    if not pdf.exists():
        C.utf8_console()
        sys.exit(f"❌ 文件不存在：{pdf}")
    cache_root = Path(C.ensure_cache_root(args.cache_root))
    cache_root.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    print(f"计算 sha1 …")
    sha1 = C.file_sha1_cached(pdf, cache_root)

    hit = C.find_cache_by_sha1(cache_root, sha1)
    if hit:
        cache_dir, manifest = hit
        if manifest.get("name") != pdf.name:
            manifest.setdefault("aliases", [])
            if pdf.name not in manifest["aliases"]:
                manifest["aliases"].append(pdf.name)
                C.save_manifest(cache_dir, manifest)
            print(f"ℹ 改名不失效：沿用缓存 {cache_dir.name}（原名 {manifest.get('name')}）")
    else:
        cache_dir = C.new_cache_dir(cache_root, pdf.name, sha1)
        manifest = None

    params = {"chunk_pages": args.chunk_pages, "dpi": args.dpi, "ocr": not args.no_ocr}

    if (not args.force and manifest and manifest.get("status") == "done"
            and manifest.get("params") == params and C.chunks_ok(cache_dir, manifest)):
        print(f"✅ 缓存命中（参数一致，{time.time() - t0:.1f} 秒）：{cache_dir / '概览.md'}")
        return

    if manifest and manifest.get("status") == "done" and not args.force:
        print("ℹ 参数变化或产物缺失：重建产物（dpi 未变时 OCR 结果按页复用，已识别页不重跑）")

    with C.Lock(cache_dir, f"parse_pdf {pdf.name}"):
        run(pdf, cache_dir, manifest, params, args, sha1, t0)


def run(pdf, cache_dir, manifest, params, args, sha1, t0):
    try:
        import pymupdf as fitz  # pymupdf ≥1.24
    except ImportError:
        import fitz  # 旧版

    manifest = manifest or {}
    old_dpi = (manifest.get("params") or {}).get("dpi")
    manifest.update({"schema": 1, "kind": "pdf", "name": pdf.name, "sha1": sha1,
                     "size": pdf.stat().st_size, "params": params, "status": "running",
                     "source_path": str(pdf)})
    C.save_manifest(cache_dir, manifest)
    # --force 或 dpi 变化：旧 OCR 断点作废（拿锁后再删，避免与运行中任务抢文件）
    if args.force or old_dpi != params["dpi"]:
        (cache_dir / "ocr_pages.jsonl").unlink(missing_ok=True)

    doc = fitz.open(pdf)
    n_pages = doc.page_count
    try:
        toc = doc.get_toc()
    except Exception:
        toc = []

    # 文本层直提（秒级），每页独立判断是否需要 OCR
    page_texts, need_ocr, text_pages = {}, [], 0
    for i in range(n_pages):
        txt = doc[i].get_text("text", sort=True).strip()
        page_texts[i + 1] = txt
        if len(txt) >= C.TEXT_MIN_CHARS:
            text_pages += 1
        elif params["ocr"]:
            need_ocr.append(i + 1)

    ocr_new = 0
    ocr_map = {}
    if need_ocr:
        ocr_map, ocr_new = run_ocr(doc, need_ocr, cache_dir, params["dpi"])
        for p in need_ocr:
            page_texts[p] = ocr_map.get(p, "")

    chunks = plan_chunks(n_pages, toc, params["chunk_pages"])
    chunk_list = write_chunks(cache_dir, chunks, page_texts, pdf.name)

    ocr_pages = sorted(ocr_map) or []
    brief = ", ".join(f"p{p}" for p in ocr_pages[:20]) + ("…" if len(ocr_pages) > 20 else "")
    stats = {"text_pages": text_pages, "ocr_pages": len(ocr_pages), "ocr_brief": brief}
    write_overview(cache_dir, pdf.name, sha1, n_pages, stats, chunk_list, toc, params)

    manifest.update({"status": "done", "chunks": chunk_list, "stats": stats,
                     "page_count": n_pages})
    C.save_manifest(cache_dir, manifest)
    doc.close()

    print(f"✅ 完成：{n_pages} 页（文本层 {text_pages} + OCR {len(ocr_pages)}）→ {len(chunk_list)} 块，"
          f"本次新 OCR {ocr_new} 页，耗时 {time.time() - t0:.1f} 秒")
    print(f"   概览：{cache_dir / '概览.md'}")
    print(f"   提示：重跑同一命令即命中缓存秒回。")


if __name__ == "__main__":
    main()
