# -*- coding: utf-8 -*-
"""material-parser 合成数据自测：不依赖真实素材验证全链路。

用法：
    D:\\python\\python.exe scripts/selftest.py [--keep]

覆盖（对应方案验收 1–4；B站在线链路待真实 URL 实测，CC/AI 路径用离线桩守住）：
    1. fitz 合成 ~1000 页文本 PDF → 解析/书签分块/缓存秒回/参数变化重建/改名不失效
       + 书签覆盖完整性（书签前页/同页书签不留缺口）+ EPUB 解析与文本提取
    2. 合成纯图片页 → OCR 路径 + 注入中断（环境变量）→ 续跑逐页断点不重复 + dpi 变化重识别
    3. PowerShell System.Speech（显式选中文 voice）合成语音 → 转写 + 注入中断 → 续跑续接
    4. ffmpeg testsrc 合成视频 → 抽音频 + 关键帧管线（无语音时 0 段容错）
       + 150 段硬切变长视频 → 关键帧超 120 时全程均匀采样、无多余帧文件残留
    5. 离线字幕桩（MATERIAL_PARSER_FAKE_SUBS）→ CC 路线产物非空 + chunk-sec 重新分块 + 空产物自愈
    6. 库根外部署（技能装在库根之外）：MATERIAL_PARSER_CACHE 环境变量指定缓存根，产物与默认根隔离
    7. --clean：done 缓存可删；未完成缓存拒删（保断点）；活进程持锁拒删、进程退出后陈旧锁判活清理
    8. sha1 索引：源文件未变跳过重哈希（篡改索引值验证确实读索引）；转写完成后删 chunks 重跑
       → transcribe_done 跳过重转写直接重建产物
    9. 纯画面路线 + 讲述锚点：--no-transcribe 未配 --keyframes 拒绝；无音轨视频纯画面解析
       （route=visual、0 段、--max-frames 封顶）；TTS 指画面语音 + 画面合成视频 → 转写命中
       锚点词即在该时间戳抽帧（kinds 含锚点）；转写 0 段时提示纯画面路线

中断注入钩子（正常使用不需要）：
    MATERIAL_PARSER_FAIL_AFTER_OCR_PAGES=N   OCR 完成 N 页后抛异常
    MATERIAL_PARSER_FAIL_AFTER_SEGMENTS=N    转写完成 N 段后抛异常
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C
import parse_pdf as PP

PY = sys.executable
WORK = None   # main() 里初始化：缓存根未定位时（独立装在技能目录且未设环境变量）自测直接退出并给指引
CACHE = None


def init_paths():
    global WORK, CACHE
    C.ensure_cache_root()
    WORK = C.CACHE_ROOT / "_selftest"
    CACHE = WORK / "cache"

RESULTS = []


def report(test, ok, msg=""):
    mark = "✅" if ok else "❌"
    print(f"  {mark} {test}" + (f" ｜ {msg}" if msg else ""))
    RESULTS.append((test, ok, msg))


def run_script(script, *args, env_extra=None, timeout=1800):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    return subprocess.run([PY, str(Path(__file__).parent / script), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=str(Path(__file__).parent),
                          timeout=timeout)


def read_manifest(cache_dir: Path):
    return json.loads((cache_dir / "manifest.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 1. 千页 PDF

def fitz_mod():
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    return fitz


def test_pdf_text():
    print("\n[1] 千页文本 PDF：解析 / 书签分块 / 缓存 / 参数变化 / 改名不失效")
    fitz = fitz_mod()

    pdf_path = WORK / "自测-千页.pdf"
    if not pdf_path.exists():
        t0 = time.time()
        doc = fitz.open()
        for i in range(1000):
            page = doc.new_page()
            page.insert_text((72, 100), f"第 {i + 1} 页", fontname="china-s", fontsize=18)
            page.insert_text((72, 140),
                             f"这是自测用合成文本页，验证文本层提取与分块。段落编号 {i + 1}，"
                             f"关键词：知识管理、检索优化。",
                             fontname="china-s", fontsize=11)
        doc.set_toc([[1, "第一章 总论", 1], [1, "第二章 方法", 401], [1, "第三章 结论", 801]])
        doc.save(pdf_path)
        print(f"  （合成 1000 页 PDF 用时 {time.time() - t0:.0f} 秒）")

    r = run_script("parse_pdf.py", str(pdf_path), "--cache-root", str(CACHE),
                   "--chunk-pages", "50", timeout=900)
    report("解析退出码 0", r.returncode == 0, (r.stderr or "")[-300:] if r.returncode else "")
    if r.returncode != 0:
        return
    cache_dir = C.find_cache_by_sha1(CACHE, C.file_sha1(pdf_path))[0]
    m = read_manifest(cache_dir)
    report("书签分块 20 块（3 章 400/400/200 页，各按 50 页拆）",
           len(m.get("chunks", [])) == 20, f"实际 {len(m.get('chunks', []))}")
    c0 = (cache_dir / "chunks" / "chunk-00.md").read_text(encoding="utf-8")
    report("chunk 带【pN】页码标记", "【p1】" in c0)
    report("概览.md 存在且含章节表", (cache_dir / "概览.md").exists()
           and "章节表" in (cache_dir / "概览.md").read_text(encoding="utf-8"))

    t0 = time.time()
    r = run_script("parse_pdf.py", str(pdf_path), "--cache-root", str(CACHE),
                   "--chunk-pages", "50")
    dt = time.time() - t0
    report("同参数二次运行秒回（缓存命中）",
           r.returncode == 0 and "缓存命中" in r.stdout and dt < 15, f"{dt:.1f} 秒")

    r = run_script("parse_pdf.py", str(pdf_path), "--cache-root", str(CACHE),
                   "--chunk-pages", "25")
    m2 = read_manifest(cache_dir)
    report("参数变化（chunk-pages 25）→ 重建为 40 块",
           r.returncode == 0 and len(m2.get("chunks", [])) == 40,
           f"实际 {len(m2.get('chunks', []))}")

    renamed = WORK / "自测-千页-改名.pdf"
    shutil.copyfile(pdf_path, renamed)
    r = run_script("parse_pdf.py", str(renamed), "--cache-root", str(CACHE),
                   "--chunk-pages", "25")
    m3 = read_manifest(cache_dir)
    report("改名不失效（按 sha1 沿用缓存）",
           r.returncode == 0 and "缓存命中" in r.stdout
           and "自测-千页-改名.pdf" in (m3.get("aliases") or []))

    print("  书签覆盖完整性（plan_chunks 直测）")
    for name, toc10 in [("首个书签不在第 1 页", [[1, "章A", 3], [1, "章B", 6]]),
                        ("两个一级书签同页", [[1, "章A", 3], [1, "章B", 3], [1, "章C", 5]])]:
        ch = PP.plan_chunks(10, toc10, 50)
        covered = set()
        for _t, s, e in ch:
            covered.update(range(s, e + 1))
        missing = sorted(set(range(1, 11)) - covered)
        report(f"plan_chunks 覆盖完整（{name}）", not missing,
               f"chunks={[(s, e) for _t, s, e in ch]}，缺失 {missing}")

    print("  EPUB 解析")
    epub_path = WORK / "自测-电子书.epub"
    if not epub_path.exists():
        import zipfile
        page_t = ("<?xml version='1.0' encoding='utf-8'?><html xmlns='http://www.w3.org/1999/xhtml'>"
                  "<head><title>ch</title></head><body><p>{}</p></body></html>")
        body = lambda i: f"第{i}章正文。" + "电子书解析自测内容，验证文本层提取。" * 8
        with zipfile.ZipFile(epub_path, "w") as z:
            z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml",
                       "<?xml version='1.0'?><container version='1.0' "
                       "xmlns='urn:oasis:names:tc:opendocument:xmlns:container'><rootfiles>"
                       "<rootfile full-path='OEBPS/content.opf' media-type='application/oebps-package+xml'/>"
                       "</rootfiles></container>")
            z.writestr("OEBPS/content.opf",
                       "<?xml version='1.0' encoding='utf-8'?><package version='2.0' "
                       "xmlns='http://www.idpf.org/2007/opf' unique-identifier='id'><metadata "
                       "xmlns:dc='http://purl.org/dc/elements/1.1/'><dc:title>自测书</dc:title>"
                       "<dc:identifier id='id'>selftest</dc:identifier><dc:language>zh</dc:language>"
                       "</metadata><manifest>"
                       "<item id='c1' href='ch1.xhtml' media-type='application/xhtml+xml'/>"
                       "<item id='c2' href='ch2.xhtml' media-type='application/xhtml+xml'/>"
                       "</manifest><spine><itemref idref='c1'/><itemref idref='c2'/></spine></package>")
            z.writestr("OEBPS/ch1.xhtml", page_t.format(body(1)))
            z.writestr("OEBPS/ch2.xhtml", page_t.format(body(2)))
    r = run_script("parse_pdf.py", str(epub_path), "--cache-root", str(CACHE))
    found_epub = C.find_cache_by_sha1(CACHE, C.file_sha1(epub_path))
    ok_epub = r.returncode == 0 and bool(found_epub)
    report("EPUB 解析成功", ok_epub, ((r.stderr or "")[-200:]) if not ok_epub else "")
    if found_epub:
        m_e = read_manifest(found_epub[0])
        all_text = "".join((found_epub[0] / c["file"]).read_text(encoding="utf-8")
                           for c in m_e.get("chunks", []))
        report("EPUB 文本提取非空（含正文关键词）",
               "电子书" in all_text or "正文" in all_text,
               f"{len(m_e.get('chunks', []))} 块，{len(all_text)} 字")


# ---------------------------------------------------------------- 2. OCR

def make_image_page(page, n):
    from PIL import Image, ImageDraw, ImageFont
    import io
    fitz = fitz_mod()
    font = None
    for cand in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
                 "C:/Windows/Fonts/simsun.ttc"):
        if Path(cand).exists():
            font = ImageFont.truetype(cand, 64)
            break
    img = Image.new("RGB", (1240, 1754), "white")
    d = ImageDraw.Draw(img)
    text = f"扫描测试第 {n} 页：知识管理与检索优化"
    d.text((80, 200), text, fill="black", font=font)
    d.text((80, 340), f"OCR-SELFTEST-{n:03d}", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    page.insert_image(fitz.Rect(0, 0, 595, 842), stream=buf.getvalue())


def test_pdf_ocr():
    print("\n[2] 扫描页 OCR：逐页断点 + 注入中断后续跑")
    fitz = fitz_mod()

    pdf_path = WORK / "自测-混合.pdf"
    doc = fitz.open()
    for i in range(3):  # 文本层页
        page = doc.new_page()
        page.insert_text((72, 100), f"第 {i + 1} 页（文本层）", fontname="china-s", fontsize=14)
        page.insert_text((72, 140), "这一页有文本层，不需要 OCR。" * 3,
                         fontname="china-s", fontsize=11)
    for i in range(3):  # 纯图片页（扫描模拟）
        page = doc.new_page()
        make_image_page(page, i + 4)
    doc.save(pdf_path)

    stale = C.find_cache_by_sha1(CACHE, C.file_sha1(pdf_path))
    if stale:
        shutil.rmtree(stale[0], ignore_errors=True)  # 上轮自测残留会让注入中断直接命中缓存

    r = run_script("parse_pdf.py", str(pdf_path), "--cache-root", str(CACHE),
                   env_extra={C.ENV_FAIL_OCR: "2"})
    ocr_path = C.find_cache_by_sha1(CACHE, C.file_sha1(pdf_path))[0] / "ocr_pages.jsonl"
    lines = ocr_path.read_text(encoding="utf-8").strip().splitlines() if ocr_path.exists() else []
    report("注入中断：2 页后失败且断点已落盘",
           r.returncode != 0 and len(lines) == 2, f"退出码 {r.returncode}，断点 {len(lines)} 行")
    if r.returncode == 0:
        return
    snapshot = lines

    r = run_script("parse_pdf.py", str(pdf_path), "--cache-root", str(CACHE))
    report("重跑自动续跑至完成", r.returncode == 0, (r.stderr or "")[-300:] if r.returncode else "")
    if r.returncode != 0:
        return
    lines = ocr_path.read_text(encoding="utf-8").strip().splitlines()
    report("断点不重复（前 2 行原样保留，共 3 页）",
           len(lines) == 3 and lines[:2] == snapshot)
    ocr_text = "".join(json.loads(l)["text"] for l in lines)
    report("OCR 识别出正文关键词", "扫描" in ocr_text and "页" in ocr_text,
           ocr_text[:60].replace("\n", " "))
    cache_dir = ocr_path.parent
    m = read_manifest(cache_dir)
    st = m.get("stats", {})
    report("混合页各走各路（文本层 3 + OCR 3）",
           st.get("text_pages") == 3 and st.get("ocr_pages") == 3, str(st))

    r = run_script("parse_pdf.py", str(pdf_path), "--cache-root", str(CACHE), "--dpi", "200")
    m4 = read_manifest(cache_dir)
    lines200 = ocr_path.read_text(encoding="utf-8").strip().splitlines()
    report("换 dpi=200 → OCR 断点作废、按新 dpi 重识别",
           r.returncode == 0 and m4.get("params", {}).get("dpi") == 200
           and len(lines200) == 3 and m4.get("status") == "done",
           f"dpi={m4.get('params', {}).get('dpi')}，OCR {len(lines200)} 页")


# ---------------------------------------------------------------- 3. TTS 转写

TTS_TEXT = ("知识管理需要定期整理。今天天气很好，阳光明媚。材料解析工具可以节省大量时间。"
            "第十二章讨论检索优化的方法。我们把这些内容放进知识库。工具解析完成后生成概览。"
            "断点续跑依赖逐段落盘。最后一句话，测试到此结束。")


def make_tts_wav(wav_path: Path, text=None) -> bool:
    ps = """
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$vs = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'zh*' }
if (-not $vs) { Write-Output 'NO_ZH_VOICE'; exit 2 }
$s.SelectVoice($vs[0].VoiceInfo.Name)
$s.Rate = 0
$s.SetOutputToWaveFile('WAVPATH')
$s.Speak('TTSTEXT')
$s.Dispose()
""".replace("WAVPATH", str(wav_path)).replace("TTSTEXT", text or TTS_TEXT)
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-Command", ps], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    if r.returncode == 2 or "NO_ZH_VOICE" in (r.stdout or ""):
        return False
    if r.returncode != 0 or not wav_path.exists():
        raise RuntimeError(f"TTS 失败：{r.stderr[-300:]}")
    return True


def test_tts_transcribe():
    print("\n[3] 中文语音转写（System.Speech 合成）：转写 + 注入中断后续跑续接")
    wav_raw = WORK / "tts_raw.wav"
    have_zh = make_tts_wav(wav_raw)
    if not have_zh:
        report("TTS 中文 voice", False, "系统未装中文语音（Huihui 等），跳过本项 —— 请在设置添加中文语音后重跑")
        return

    sha = C.file_sha1(wav_raw)
    stale = C.find_cache_by_sha1(CACHE, sha)
    if stale:
        shutil.rmtree(stale[0], ignore_errors=True)  # TTS 输出字节级确定，残留缓存会吞掉注入中断
    r = run_script("parse_video.py", str(wav_raw), "--cache-root", str(CACHE),
                   "--language", "zh", env_extra={C.ENV_FAIL_SEG: "3"})
    found = C.find_cache_by_sha1(CACHE, sha)
    seg_path = found[0] / "segments.jsonl" if found else None
    lines = seg_path.read_text(encoding="utf-8").strip().splitlines() if seg_path and seg_path.exists() else []
    report("注入中断：3 段后失败且断点已落盘",
           r.returncode != 0 and len(lines) == 3,
           f"退出码 {r.returncode}，断点 {len(lines)} 行")
    if r.returncode == 0 or not lines:
        return
    snapshot = lines

    r = run_script("parse_video.py", str(wav_raw), "--cache-root", str(CACHE),
                   "--language", "zh")
    report("重跑自动续跑至完成", r.returncode == 0, (r.stderr or "")[-300:] if r.returncode else "")
    if r.returncode != 0:
        return
    cache_dir = found[0]
    segs = [json.loads(l) for l in seg_path.read_text(encoding="utf-8").strip().splitlines()]
    report("断点续接不重复（前 3 行原样保留）",
           seg_path.read_text(encoding="utf-8").strip().splitlines()[:3] == snapshot)
    starts = [s["start"] for s in segs]
    report("段落时序单调不回退", all(b >= a - 0.5 for a, b in zip(starts, starts[1:])))
    report("转写到接近音频末尾", segs[-1]["end"] > 10, f"末段结束 {segs[-1]['end']:.1f}s")
    text = "".join(s["text"] for s in segs)
    kw = [w for w in ("知识", "解析", "检索", "测试") if w in text]
    report("转写文本抽查命中关键词（≥1）", len(kw) >= 1, f"命中 {kw}；全文 {len(text)} 字")
    report("产物齐全（SRT/概览/chunks/中间wav已删）",
           (cache_dir / "transcript.srt").exists()
           and "转写" in (cache_dir / "概览.md").read_text(encoding="utf-8")
           and (cache_dir / "chunks").exists()
           and not (cache_dir / "audio.wav").exists())

    print("  转写完成后删 chunks 重跑 → 不重转写，直接按现有段落重建")
    shutil.rmtree(cache_dir / "chunks", ignore_errors=True)
    r = run_script("parse_video.py", str(wav_raw), "--cache-root", str(CACHE),
                   "--language", "zh")
    report("transcribe_done 跳过重转写（重建产物且不加载模型）",
           r.returncode == 0 and "直接按现有段落重建" in r.stdout
           and (cache_dir / "chunks").exists() and (cache_dir / "概览.md").exists(),
           (r.stdout or "")[-200:].replace("\n", " ") if r.returncode else "")


# ---------------------------------------------------------------- 4. 视频管线

def test_video_pipeline():
    print("\n[4] ffmpeg testsrc 视频：抽音频 + 关键帧 + 无语音容错")
    video = WORK / "自测-视频.mp4"
    if not video.exists():
        C.run_ffmpeg(["-y", "-f", "lavfi", "-i", "testsrc=duration=30:size=640x480:rate=25",
                      "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
                      "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset",
                      "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                      str(video)], capture=True)

    r = run_script("parse_video.py", str(video), "--cache-root", str(CACHE),
                   "--keyframes", "--language", "zh")
    report("解析退出码 0", r.returncode == 0, (r.stderr or "")[-300:] if r.returncode else "")
    if r.returncode != 0:
        return
    cache_dir = C.find_cache_by_sha1(CACHE, C.file_sha1(video))[0]
    m = read_manifest(cache_dir)
    kf = m.get("keyframes") or {}
    report("关键帧已生成（场景检测或 30 秒采样兜底）",
           kf.get("done") and kf.get("count", 0) >= 1, f"{kf.get('count', 0)} 张")
    report("缩略图对照表落盘", (cache_dir / "keyframes" / "contact.jpg").exists())
    report("无语音段容错（0 段也出产物）", m.get("status") == "done"
           and (cache_dir / "概览.md").exists())
    report("0 段时提示纯画面路线（--no-transcribe）", "无口播" in (r.stdout or ""))
    report("中间 wav 默认删除", not (cache_dir / "audio.wav").exists())


def test_keyframes_even():
    """150 段红蓝硬切变（300 秒 149 次切变）→ 场景检测远超 120 帧，验证全程均匀采样。"""
    print("\n[4b] 长视频关键帧：超 120 帧时全程均匀采样（不截前段）")
    video = WORK / "自测-切变.mp4"
    if not video.exists():
        red, blue = WORK / "自测-红.mp4", WORK / "自测-蓝.mp4"
        for p, color in ((red, "red"), (blue, "blue")):
            C.run_ffmpeg(["-y", "-f", "lavfi",
                          "-i", f"color={color}:duration=2:size=320x240:rate=10",
                          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                          str(p)], capture=True)
        lst = WORK / "concat.txt"
        lst.write_text("".join(f"file '{(red if i % 2 == 0 else blue).as_posix()}'\n"
                               for i in range(150)), encoding="utf-8")
        C.run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                      "-c", "copy", str(video)], capture=True)

    r = run_script("parse_video.py", str(video), "--cache-root", str(CACHE), "--keyframes")
    report("解析退出码 0", r.returncode == 0, (r.stderr or "")[-300:] if r.returncode else "")
    if r.returncode != 0:
        return
    cache_dir = C.find_cache_by_sha1(CACHE, C.file_sha1(video))[0]
    m = read_manifest(cache_dir)
    kf = m.get("keyframes") or {}
    times = kf.get("times") or []
    n_files = len(list((cache_dir / "keyframes").glob("kf_*.jpg")))
    report("帧数封顶 120 且无多余帧文件残留",
           kf.get("count") == 120 and n_files == 120,
           f"count={kf.get('count')}，帧文件 {n_files}")
    report("采样覆盖全程（最后一帧接近 300 秒片尾）",
           bool(times) and times[-1] >= 290,
           f"首帧 {times[0] if times else '-'}s，尾帧 {times[-1] if times else '-'}s")


# ------------------------------------------------- 9. 纯画面路线 + 讲述锚点

def test_visual_and_cues():
    print("\n[9] 纯画面路线（--no-transcribe）+ 讲述锚点关键帧（--cue-words 内置表）")
    video = WORK / "自测-视频.mp4"

    r = run_script("parse_video.py", str(video), "--cache-root", str(CACHE), "--no-transcribe")
    report("--no-transcribe 未配 --keyframes → 拒绝", r.returncode != 0)

    print("  纯画面：无音轨切变视频 → 不转写直接抽帧（独立缓存根，避开转写缓存）")
    root2 = CACHE / "visualroot"
    cut = WORK / "自测-切变.mp4"
    r = run_script("parse_video.py", str(cut), "--cache-root", str(root2),
                   "--no-transcribe", "--keyframes", "--max-frames", "60")
    ok = r.returncode == 0 and "纯画面" in (r.stdout or "")
    report("纯画面路线解析成功（不转写、不抽音频）", ok,
           (r.stderr or "")[-300:] if not ok else "")
    if ok:
        found = C.find_cache_by_sha1(root2, C.file_sha1(cut))
        m = read_manifest(found[0])
        kf = m.get("keyframes") or {}
        report("route=visual、0 段、关键帧按 --max-frames=60 封顶",
               m.get("route") == "visual" and m.get("segments") == 0
               and kf.get("count") == 60,
               f"route={m.get('route')}，segments={m.get('segments')}，count={kf.get('count')}")

    print("  讲述锚点：TTS 合成指画面语音 + testsrc 画面 → 转写命中词即抽锚点帧")
    wav_cue = WORK / "自测-锚点语音.wav"
    cue_text = ("请注意看这个界面，这里就是入口。这张图就是最终的结果。"
                "大家看右边的数字，都在增长。知识管理需要定期整理。")
    if not make_tts_wav(wav_cue, cue_text):
        report("TTS 中文 voice（锚点子项）", False,
               "系统未装中文语音，跳过锚点子项——同第 3 组")
        return
    v2 = WORK / "自测-锚点.mp4"
    C.run_ffmpeg(["-y", "-f", "lavfi", "-i", "testsrc=duration=20:size=640x480:rate=25",
                  "-i", str(wav_cue), "-map", "0:v", "-map", "1:a", "-c:v", "libx264",
                  "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                  "-shortest", str(v2)], capture=True)
    r = run_script("parse_video.py", str(v2), "--cache-root", str(CACHE),
                   "--keyframes", "--language", "zh")
    ok = r.returncode == 0
    report("锚点视频解析成功（转写 + 双信号关键帧）", ok,
           (r.stderr or "")[-300:] if not ok else "")
    if ok:
        found = C.find_cache_by_sha1(CACHE, C.file_sha1(v2))
        m = read_manifest(found[0])
        kf = m.get("keyframes") or {}
        report("讲述锚点命中（kinds 含锚点帧）",
               kf.get("cues", 0) >= 1 and "锚点" in (kf.get("kinds") or []),
               f"cues={kf.get('cues')}，kinds={sorted(set(kf.get('kinds') or []))}，"
               f"段落数={m.get('segments')}")


# ---------------------------------------------------------------- 5. 离线字幕桩

def test_subtitle_offline():
    """用 MATERIAL_PARSER_FAKE_SUBS 桩守住 CC/AI 路径（v1.0 曾在此产出空产物）。"""
    print("\n[5] 在线字幕路线（离线桩）：CC 产物非空 / chunk-sec 重新分块 / 空产物自愈")
    vtt = WORK / "自测-字幕.vtt"
    cues = [f"{C.fmt_ts(i * 15)}.000 --> {C.fmt_ts(i * 15 + 5)}.000\n第{i + 1}句离线字幕自测"
            for i in range(8)]
    vtt.write_text("WEBVTT\n\n" + "\n\n".join(cues) + "\n", encoding="utf-8")

    url = "https://selftest.invalid/video"
    env = {C.ENV_FAKE_SUBS: str(vtt)}
    r = run_script("parse_video.py", url, "--cache-root", str(CACHE), env_extra=env)
    found = C.find_cache_by_sha1(CACHE, C.text_sha1("site:SelfTest:selftest01"))
    report("离线桩 CC 路线解析成功（站点+ID 成键）", r.returncode == 0 and bool(found),
           (r.stderr or "")[-300:] if r.returncode or not found else "")
    if not found:
        return
    cd = found[0]
    m = read_manifest(cd)
    srt = (cd / "transcript.srt").read_text(encoding="utf-8")
    report("字幕段落落盘且产物非空（SRT 非空 + chunks ≥1）",
           m.get("route") == "cc" and m.get("segments") == 8
           and len(srt.splitlines()) >= 4 and len(m.get("chunks") or []) >= 1,
           f"route={m.get('route')}，segments={m.get('segments')}，"
           f"chunks={len(m.get('chunks') or [])}")

    r = run_script("parse_video.py", url, "--cache-root", str(CACHE),
                   "--chunk-sec", "30", env_extra=env)
    m2 = read_manifest(cd)
    report("改 chunk-sec=30 → 只重新分块为 4 块（不重拉字幕）",
           r.returncode == 0 and "重新分块" in r.stdout
           and m2.get("chunk_sec") == 30 and len(m2.get("chunks") or []) == 4,
           f"chunk_sec={m2.get('chunk_sec')}，chunks={len(m2.get('chunks') or [])}")

    m2["segments"] = 0  # 模拟 v1.0 空产物污染的缓存
    (cd / "manifest.json").write_text(json.dumps(m2, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    r = run_script("parse_video.py", url, "--cache-root", str(CACHE), env_extra=env)
    m3 = read_manifest(cd)
    report("空产物自愈（自动重走字幕链路重建）",
           r.returncode == 0 and "缓存命中" not in r.stdout
           and m3.get("segments") == 8 and m3.get("status") == "done",
           f"segments={m3.get('segments')}，status={m3.get('status')}")


# ---------------------------------------------------------------- 6. 库根外部署

def test_cache_env():
    print("\n[6] 库根外部署：MATERIAL_PARSER_CACHE 环境变量指定缓存根")
    pdf = WORK / "自测-千页.pdf"
    env_root = CACHE / "envroot"
    r = run_script("parse_pdf.py", str(pdf), env_extra={C.ENV_CACHE: str(env_root)})
    found = C.find_cache_by_sha1(env_root, C.file_sha1(pdf)) if pdf.exists() else None
    report("产物落在环境变量指定的缓存根（与默认根隔离）",
           r.returncode == 0 and bool(found),
           (r.stderr or "")[-200:] if r.returncode or not found else "")


# ---------------------------------------------------------------- 7. 缓存清理

def test_clean():
    print("\n[7] 入库后清理 --clean：done 可删 / 未完成拒删保断点")
    d1 = CACHE / "clean-done-test"
    d1.mkdir(parents=True, exist_ok=True)
    (d1 / "manifest.json").write_text(
        json.dumps({"name": "自测A", "status": "done", "sha1": "0" * 40}), encoding="utf-8")
    r1 = run_script("common.py", "--clean", str(d1))
    report("done 缓存删除", r1.returncode == 0 and not d1.exists(),
           ((r1.stderr or "") + (r1.stdout or ""))[-150:].strip())
    d2 = CACHE / "clean-running-test"
    d2.mkdir(parents=True, exist_ok=True)
    (d2 / "manifest.json").write_text(
        json.dumps({"name": "自测B", "status": "running", "sha1": "1" * 40}), encoding="utf-8")
    r2 = run_script("common.py", "--clean", str(d2))
    report("未完成缓存拒删（保断点）", r2.returncode != 0 and d2.exists(),
           ((r2.stderr or "") + (r2.stdout or ""))[-150:].strip())
    shutil.rmtree(d2, ignore_errors=True)

    print("  运行锁：活进程持锁拒删，进程退出后陈旧锁判活清理")
    d3 = CACHE / "clean-lock-test"
    d3.mkdir(parents=True, exist_ok=True)
    (d3 / "manifest.json").write_text(
        json.dumps({"name": "自测C", "status": "done", "sha1": "2" * 40}), encoding="utf-8")
    sleeper = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])
    (d3 / "lock.pid").write_text(f"{sleeper.pid}\tparse_video 自测", encoding="utf-8")
    r3 = run_script("common.py", "--clean", str(d3))
    report("活进程持锁 → 拒删", r3.returncode != 0 and d3.exists())
    sleeper.terminate()
    sleeper.wait()
    r4 = run_script("common.py", "--clean", str(d3))
    report("进程退出后 → 陈旧锁判活清理并删除", r4.returncode == 0 and not d3.exists(),
           ((r4.stderr or "") + (r4.stdout or ""))[-150:].strip())


# ---------------------------------------------------------------- 8. sha1 索引

def test_sha1_index():
    print("\n[8] sha1 索引：源文件未变跳过重哈希（缓存秒回不再被 10 秒/GB 拖住）")
    f = WORK / "自测-索引.bin"
    f.write_bytes(b"sha1-index-selftest")
    root = CACHE / "idxroot"
    root.mkdir(parents=True, exist_ok=True)
    report("首算写入索引且值正确",
           C.file_sha1_cached(f, root) == C.file_sha1(f))
    idx_path = root / C.SHA1_INDEX
    report("索引文件已落盘", idx_path.exists())
    # 篡改索引里的 sha1（size/mtime 不动）：第二次调用返回篡改值，证明确实读索引没重哈希
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    idx[str(f.resolve())]["sha1"] = "0" * 40
    idx_path.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    report("未变文件直接用索引（不再哈希）", C.file_sha1_cached(f, root) == "0" * 40)
    f.write_bytes(b"changed")  # size 变 → 作废重算
    report("文件变化后重算并回写", C.file_sha1_cached(f, root) == C.file_sha1(f))


# ----------------------------------------------------------------

def main():
    C.utf8_console()
    ap = argparse.ArgumentParser(description="material-parser 合成数据自测")
    ap.add_argument("--keep", action="store_true", help="保留自测产物便于排查")
    args = ap.parse_args()

    init_paths()
    WORK.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    print(f"自测缓存：{CACHE}")

    test_pdf_text()
    test_pdf_ocr()
    test_tts_transcribe()
    test_video_pipeline()
    test_keyframes_even()
    test_visual_and_cues()
    test_subtitle_offline()
    test_cache_env()
    test_clean()
    test_sha1_index()

    ok = [x for x in RESULTS if x[1]]
    bad = [x for x in RESULTS if not x[1]]
    print(f"\n{'=' * 60}\n自测结果：{len(ok)} 通过 / {len(bad)} 失败 ｜ 总耗时 {time.time() - t0:.0f} 秒")
    for name, _, msg in bad:
        print(f"  ❌ {name} ｜ {msg}")
    if not bad and not args.keep:
        shutil.rmtree(WORK, ignore_errors=True)
        print("（自测产物已清理；--keep 可保留）")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
