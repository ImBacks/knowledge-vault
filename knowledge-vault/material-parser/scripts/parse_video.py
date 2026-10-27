# -*- coding: utf-8 -*-
"""音视频/在线视频解析：字幕优先（CC → AI → 本地转写兜底）+ 分块缓存 + 关键帧。

用法：
    python scripts/parse_video.py <视频/音频路径 或 URL>
        [--model small] [--language zh] [--chunk-sec 600] [--keyframes]
        [--no-transcribe] [--max-frames 120] [--cue-words 词1,词2] [--keep-audio]
        [--device cpu|cuda] [--cookies chrome|edge|firefox|brave] [--force] [--cache-root 目录]

在线视频三级链路（yt-dlp 支持的站点均可：B站/抖音/TikTok/YouTube 等）：
    1. CC 字幕：yt-dlp 只拉字幕不下载，秒级，免登录；
    2. AI 字幕：--cookies 走浏览器登录态拉取（需本机浏览器已登录该站）；
    3. 兜底：下载音频 → faster-whisper 本地转写（默认 CPU int8，约视频时长的 0.3~0.6 倍；
       本机有 N 卡可加 --device cuda 提速（实测参考见解析手册），环境自检 common.py --doctor，后台跑）。
    抖音类站点普遍无 CC/AI 字幕，基本确定落兜底转写；部分内容需 --cookies。

关键帧双信号（--keyframes，调用方按视频类型自行取舍，工具不替人分类）：
    - 场景检测：画面突变（翻页/切屏/镜头切换）处抽帧，阈值 0.2；
    - 讲述锚点：转写/字幕文本命中指画面的用语（"看这个界面""如图""手上"）时，
      在该时间戳 seek 抽帧——抓"话指向画面"而画面没变的瞬间（举物讲解、指屏幕角落）。
    两路去重合并、封顶 --max-frames；锚点优先保住，场景帧均匀填充。
    --no-transcribe：无口播/纯画面展示类视频跳过字幕与转写，只出关键帧（需配合 --keyframes）。

产物（解析缓存/<名>-<sha1前10位>/）：
    概览.md / chunks/chunk-NN.md（带 [HH:MM:SS] 标记）/ transcript.srt / segments.jsonl / keyframes/（可选）

断点续跑：转写段落逐条追加 segments.jsonl，中断后重跑同一命令自动续。
中间 wav / 下载的媒体在转写成功后默认删除（GB 级），中断时保留供续跑；--keep-audio 可保留。
缓存命中：status=done 且（转写路线下）model/language 一致 → 秒回（--device 不参与判定：换设备沿用
已有转写缓存，要按新设备重转用 --force）；改 --chunk-sec 只重新分块
（不重拉字幕、不重转写）；keyframes 是叠加产物，缺了单独补；--max-frames 变化会重抽帧。
此前纯画面（visual）路线的缓存，重跑去掉 --no-transcribe 会自动补转写。
URL 缓存键 = 站点+视频ID（短链/参数变体同一视频共用缓存；分 P 单独成键）。
缓存根：--cache-root > 环境变量 MATERIAL_PARSER_CACHE > 向上找 大纲.md 定位库根；三级落空则报错。
"""
from __future__ import annotations

import argparse
import inspect
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
SUB_LANGS = ["zh-CN", "zh-Hans", "zh", "ai-zh", "zh-TW", "en"]
TRACKING_PARAMS = {"spm", "vd_source", "from_spm", "buvid", "bbid", "ts",
                   "share_source", "share_medium", "share_plat", "share_tag"}


def normalize_url(url):
    """去掉常见追踪参数、统一小写 host，减少同一视频的不同 URL 形态各建缓存。"""
    try:
        u = urlparse(url.strip())
        keep = sorted((k, v[0]) for k, v in parse_qs(u.query).items()
                      if k not in TRACKING_PARAMS)
        query = "&".join(f"{k}={v}" for k, v in keep)
        return f"{u.scheme.lower()}://{u.netloc.lower()}{u.path}" + (f"?{query}" if query else "")
    except Exception:
        return url.strip()


# ---------------------------------------------------------------- 在线链路

def ydl_info(url):
    fake = os.environ.get(C.ENV_FAKE_SUBS)  # selftest 离线桩
    if fake:
        return {"title": "自测-字幕视频", "duration": 120,
                "id": "selftest01", "extractor": "SelfTest"}
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        print(f"ℹ 视频信息获取失败（{type(e).__name__}），按 URL 原文处理：{str(e)[:100]}")
        return None


def fetch_subs(url, cache_dir: Path, cookies=None, auto=False):
    """拉字幕转 segments；失败返回 None（不抛出，交由调用方走下一级链路）。"""
    import yt_dlp
    tag = "AI 字幕" if auto else "CC 字幕"
    fake = os.environ.get(C.ENV_FAKE_SUBS)  # selftest 离线桩：不走网络
    if fake:
        print(f"  {tag}：使用离线桩 {fake}")
        return parse_vtt(Path(fake)) or None
    # 上一级链路的残留 vtt（可能为空文件）不参与本次挑选，先清掉
    for stale in cache_dir.glob("sub.*.vtt"):
        stale.unlink()
    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticssub": bool(auto),
        "subtitleslangs": SUB_LANGS,
        "convert_subtitles": "vtt",
        "outtmpl": str(cache_dir / "sub.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if cookies:
        opts["cookiesfrombrowser"] = (cookies,)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        print(f"  {tag}不可用：{type(e).__name__}: {str(e)[:150]}")
        return None
    vtts = sorted(cache_dir.glob("sub.*.vtt"),
                  key=lambda p: (0 if "zh" in p.name.lower() else 1,
                                 1 if "ai" in p.name.lower() else 0, p.name))
    if not vtts:
        print(f"  {tag}不可用：站点未提供该语言字幕")
        return None
    segs = parse_vtt(vtts[0])
    if not segs:
        print(f"  {tag}不可用：字幕文件为空（{vtts[0].name}）")
        return None
    return segs


def parse_vtt(path: Path):
    """VTT → segments；合并滚动字幕的重复行。"""
    ts_re = re.compile(r"(\d+):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[.,](\d{3})")
    tag_re = re.compile(r"<[^>]+>")
    segs, cur = [], None

    def push():
        nonlocal cur
        if cur and cur["text"]:
            if segs and segs[-1]["text"] == cur["text"]:
                segs[-1]["end"] = cur["end"]  # 滚动字幕重复段 → 合并
            else:
                segs.append(cur)
        cur = None

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = ts_re.search(line)
        if m:
            push()
            g = [int(x) for x in m.groups()]
            cur = {"start": g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000,
                   "end": g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000, "text": ""}
            continue
        text = tag_re.sub("", line).strip()
        if not text or text == "WEBVTT" or text.startswith(("NOTE", "Kind:", "Language:")):
            continue
        if cur is not None:
            cur["text"] = (cur["text"] + " " + text).strip()
    push()
    return segs


def download_media(url, cache_dir: Path, want_video=False) -> Path:
    import yt_dlp
    opts = {
        # 要视频只为了关键帧（缩略图 240px 宽），封顶 1080p，不拉 4K 浪费流量和磁盘
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b" if want_video else "bestaudio/best",
        "outtmpl": str(cache_dir / "media.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True, "no_warnings": True, "noplaylist": True,
    }
    print(f"  下载{'视频（含画面，供关键帧）' if want_video else '音频'} …")
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = [p for p in cache_dir.glob("media.*")
             if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".m4a", ".mp3",
                                     ".aac", ".opus", ".weba")]
    if not files:
        raise RuntimeError("下载完成但找不到媒体文件")
    return files[0]


# ---------------------------------------------------------------- 转写

def has_audio(path: Path) -> bool:
    """ffprobe 查有无音轨；探测不了时保守返回 True（维持让 ffmpeg 报错的旧行为）。"""
    fp = C.find_tool("ffprobe")
    if not fp:
        return True
    try:
        out = subprocess.run([fp, "-v", "error", "-select_streams", "a",
                              "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=60)
        return bool(out.stdout.strip())
    except Exception:
        return True


def to_wav(media_path: Path, cache_dir: Path):
    """ffmpeg 抽 16k 单声道 wav；无音轨返回 None（0 段容错，不崩）。
    wav 只由输入决定，参数变化时可复用。"""
    out = cache_dir / "audio.wav"
    if out.exists() and out.stat().st_size > 44:
        return out
    if not has_audio(media_path):
        print("  源文件无音轨 → 跳过转写（按 0 段出产物）")
        return None
    print(f"  ffmpeg 抽取 16k 单声道 wav …")
    C.run_ffmpeg(["-y", "-i", str(media_path), "-vn", "-ac", "1", "-ar", "16000", str(out)],
                 capture=True)
    return out


def read_segments(seg_path: Path):
    segs = []
    if not seg_path.exists():
        return segs
    for line in seg_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            if r.get("text"):
                segs.append({"start": float(r["start"]), "end": float(r["end"]),
                             "text": str(r["text"])})
        except Exception:
            continue
    return segs


def write_segments_jsonl(segments, seg_path: Path):
    """segments.jsonl 是产物阶段的唯一数据源：字幕路线拿到 segs 后必须先落盘。"""
    seg_path.write_text("".join(
        json.dumps({"start": round(s["start"], 3), "end": round(s["end"], 3),
                    "text": s["text"]}, ensure_ascii=False) + "\n" for s in segments),
        encoding="utf-8")


def resolve_device(requested, cuda_count):
    """--device → (device, compute_type)。cuda 点名而本机没有 N 卡 → 报错不静默回落；
    --device 不进缓存键（transcribe_params 只记 model/language），换设备沿用已转写缓存。"""
    if requested == "cuda":
        if not cuda_count:
            raise RuntimeError("本机未检测到 NVIDIA GPU，无法 --device cuda："
                               "去掉该参数按 CPU 跑（默认路径，功能完整），"
                               "或跑 common.py --doctor 查看环境")
        return "cuda", "int8_float16"
    return "cpu", "int8"


def transcribe(wav: Path, cache_dir: Path, args) -> int:
    """faster-whisper 转写，段落流式追加 segments.jsonl（断点依据）。返回总段数。"""
    import ctranslate2
    from faster_whisper import WhisperModel
    seg_path = cache_dir / "segments.jsonl"
    resume_from = 0.0
    existing = read_segments(seg_path)
    if existing:
        resume_from = existing[-1]["end"]
        print(f"  断点续跑：已有 {len(existing)} 段，从 {C.fmt_ts(resume_from)} 继续")

    cuda_count = ctranslate2.get_cuda_device_count()
    device, compute_type = resolve_device(getattr(args, "device", "cpu"), cuda_count)
    if device == "cuda":
        C.enable_cuda_dlls()  # 注册 pip 装的 cuBLAS/cuDNN DLL 目录（手动配 PATH 的装法不受影响）
    print(f"  加载 whisper {args.model}（{compute_type} / {device.upper()}，"
          f"首次运行会先下载模型走 hf-mirror）…")
    try:
        model = WhisperModel(args.model, device=device, compute_type=compute_type)
    except Exception as e:
        if device == "cuda":
            raise RuntimeError(
                f"CUDA 初始化失败（{type(e).__name__}）：{str(e)[:200]}。"
                f"跑 common.py --doctor 查缺什么（通常缺 nvidia-cublas-cu12 / nvidia-cudnn-cu12"
                f" 或 NVIDIA 驱动过旧）；去掉 --device 按 CPU 跑。") from e
        raise
    kw = {"vad_filter": True, "vad_parameters": {"min_silence_duration_ms": 500}}
    if args.language:
        kw["language"] = args.language
    if resume_from > 0:
        if "clip_timestamps" in inspect.signature(model.transcribe).parameters:
            kw["clip_timestamps"] = str(resume_from)
        else:
            print("  ⚠ 当前 faster-whisper 版本不支持断点参数，重头转写")
            seg_path.unlink(missing_ok=True)
            resume_from = 0.0

    limit = C.test_limit(C.ENV_FAIL_SEG)
    print("  转写中（VAD 过滤静音，段落实时落盘）…")
    t0, n_new = time.time(), 0
    with open(seg_path, "a", encoding="utf-8") as f:
        iter_segments, info = model.transcribe(str(wav), **kw)
        for seg in iter_segments:
            if seg.start < resume_from - 0.5:
                continue  # 续跑边界重叠防护
            text = seg.text.strip()
            if not text:
                continue
            f.write(json.dumps({"start": round(seg.start, 3), "end": round(seg.end, 3),
                                "text": text}, ensure_ascii=False) + "\n")
            f.flush()
            n_new += 1
            if n_new % 20 == 0:
                print(f"    已写 {n_new} 段 ｜ {C.fmt_ts(seg.end)}")
            if limit and n_new >= limit:
                raise RuntimeError(f"selftest 注入中断（已写 {n_new} 段，断点已落盘）")
    total = len(read_segments(seg_path))
    dur = getattr(info, "duration", None)
    proc = (dur - resume_from) if dur else None  # 续跑时只算本次实际处理的时长
    elapsed = time.time() - t0
    speed = f"，{elapsed / proc:.2f}x 实时" if proc and proc > 0 else ""
    print(f"  转写完成：共 {total} 段（本次新增 {n_new}），耗时 {elapsed:.0f} 秒{speed}")
    if elapsed >= 600 and device == "cpu" and cuda_count:
        print("  ℹ 本次 CPU 转写用时较长；本机有 NVIDIA GPU，装好 CUDA 运行库后加 --device cuda "
              "可提速（收益随模型/显卡而变；环境自检 common.py --doctor，"
              "菜单与实测数字见项目 README「工具清单：默认与可选」）")
    return total


def ensure_wav(cache_dir: Path, manifest, args, is_url, url) -> Path:
    """续跑时确保中间 wav 存在：优先用上次保留的，没有就从源重建/重下。"""
    wav = cache_dir / (manifest.get("wav_file") or "audio.wav")
    if wav.exists() and wav.stat().st_size > 44:
        return wav
    if is_url:
        media = cache_dir / manifest["media_file"] if manifest.get("media_file") else None
        if not media or not media.exists():
            media = download_media(url, cache_dir, want_video=False)
            manifest["media_file"] = media.name
            C.save_manifest(cache_dir, manifest)
        return to_wav(media, cache_dir)
    return to_wav(Path(manifest["source_path"]), cache_dir)


# ---------------------------------------------------------------- 关键帧

def _ffmpeg_frames(video_path: Path, kf_dir: Path, vf: str):
    ff = C.find_tool("ffmpeg")
    if not ff:
        C.utf8_console()
        sys.exit("❌ 未找到 ffmpeg。请先安装：winget install Gyan.FFmpeg")
    cmd = [ff, "-hide_banner", "-i", str(video_path),
           "-vf", vf + ",scale=480:-2,showinfo", "-fps_mode", "vfr", "-y",
           str(kf_dir / "kf_%04d.jpg")]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    return [float(x) for x in re.findall(r"pts_time:([\d.]+)", proc.stderr or "")]


def pick_even(n, max_frames):
    """从 n 项里均匀取 ≤max_frames 个下标（覆盖首尾），代替截前段——
    长视频的关键帧表要覆盖全程，而不是只有前一小时。"""
    if max_frames <= 0:
        return []
    if n <= max_frames:
        return list(range(n))
    if max_frames == 1:
        return [n - 1]
    step = (n - 1) / (max_frames - 1)
    return sorted({min(round(i * step), n - 1) for i in range(max_frames)})


# 讲述锚点词表：转写/字幕文本命中即在该时间戳抽帧。抓的是「话指向画面」的瞬间——
# 举物讲解、指屏幕角落这类画面几乎不变、场景检测抓不到的时刻。误报（"这个想法"没举东西）
# 代价只是一张多余缩略图，宁可多召回。调用方可用 --cue-words 按视频内容换词或禁用。
DEFAULT_CUE_WORDS = [
    "看这", "注意看", "看一下", "大家看", "你看", "如图", "看图", "图中",
    "这张", "这幅", "手上", "拿着", "举着",
    "屏幕", "界面", "页面", "菜单", "按钮", "代码", "表格", "图表", "幻灯片", "PPT",
    "右边", "左边", "右上", "左下", "上方", "下方",
]


def parse_cue_words(s):
    """--cue-words 参数 → 词表。None=内置默认；空串=禁用锚定；自定义=逗号分隔。"""
    if s is None:
        return DEFAULT_CUE_WORDS
    return [w.strip() for w in s.split(",") if w.strip()]


def cue_timestamps(segments, words):
    """从转写/字幕段落里找指画面的时刻。返回 [(段中点时间, 命中词)]，相邻 3 秒内去重。"""
    hits = []
    for s in segments or []:
        text = str(s.get("text", "")).lower()
        for w in words:
            if w.lower() in text:
                t = (s["start"] + s["end"]) / 2
                if not hits or t - hits[-1][0] >= 3:
                    hits.append((t, w))
                break
    return hits


def _seek_frames(video_path: Path, kf_dir: Path, times):
    """按时间点 seek 抽帧（讲述锚点用）。-ss 前置输入级 seek，每点只解码附近几帧，
    长视频抽几十个锚点也是秒级。返回成功落盘的文件列表（与 times 等长对齐，失败处为 None）。"""
    ff = C.find_tool("ffmpeg")
    if not ff:
        C.utf8_console()
        sys.exit("❌ 未找到 ffmpeg。请先安装：winget install Gyan.FFmpeg")
    out = []
    for i, t in enumerate(times, 1):
        dst = kf_dir / f"cue_{i:04d}.jpg"
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{max(0.0, t):.2f}", "-i", str(video_path),
               "-frames:v", "1", "-q:v", "3", str(dst)]
        ok = subprocess.run(cmd, capture_output=True).returncode == 0 and dst.exists()
        out.append(dst if ok else None)
    return out


def extract_keyframes(video_path: Path, cache_dir: Path, manifest, segments=None,
                      max_frames=120, cue_words=None):
    """关键帧双信号：场景检测（画面变了）+ 讲述锚点（话指向画面），去重合并封顶。
    锚点优先保住预算，场景帧均匀填充剩余名额；未选中的帧文件删掉不残留。"""
    kf_dir = cache_dir / "keyframes"
    kf_dir.mkdir(exist_ok=True)
    for f in list(kf_dir.glob("kf_*.jpg")) + list(kf_dir.glob("cue_*.jpg")):
        f.unlink()
    (kf_dir / "contact.jpg").unlink(missing_ok=True)

    print("  抽取关键帧（场景检测，阈值 0.2）…")
    times = _ffmpeg_frames(video_path, kf_dir, "select='gt(scene,0.2)'")
    if not times:
        dur = probe_duration(video_path)
        # 采样间隔按时长自适应：2 小时视频若仍每 30 秒采会超 120 帧，只覆盖前段
        interval = max(30, math.ceil(dur / max_frames)) if dur else 30
        print(f"  场景检测无命中 → 每 {interval} 秒采样兜底")
        times = _ffmpeg_frames(video_path, kf_dir, f"fps=1/{interval}")
    frames = sorted(kf_dir.glob("kf_*.jpg"))
    n = min(len(frames), len(times))
    scene_entries = [(times[i], frames[i], "场景") for i in range(n)]

    # 讲述锚点：转写/字幕文本指向画面的时刻，seek 抽帧（画面没变也能抓到）
    words = DEFAULT_CUE_WORDS if cue_words is None else cue_words
    cue_hits = cue_timestamps(segments, words)
    if len(cue_hits) > max_frames:
        cue_hits = [cue_hits[i] for i in pick_even(len(cue_hits), max_frames)]
    if cue_hits:
        print(f"  讲述锚点：文本命中 {len(cue_hits)} 处 → 按时间戳 seek 抽帧")
    cue_files = _seek_frames(video_path, kf_dir, [t for t, _ in cue_hits]) if cue_hits else []
    cue_entries = [(t, f, "锚点") for (t, _w), f in zip(cue_hits, cue_files) if f]

    # 合并选帧：锚点全保（已 ≤max_frames），场景帧填剩余预算，锚点 ±3 秒内不重复
    budget = max_frames - len(cue_entries)
    rest = [e for e in scene_entries if all(abs(e[0] - k[0]) > 3 for k in cue_entries)]
    keep_scene = [rest[i] for i in pick_even(len(rest), budget)] if budget > 0 else []
    kept = sorted(cue_entries + keep_scene, key=lambda e: e[0])
    keep_names = {f.name for _t, f, _k in kept}
    for f in list(kf_dir.glob("kf_*.jpg")) + list(kf_dir.glob("cue_*.jpg")):
        if f.name not in keep_names:
            f.unlink(missing_ok=True)

    frames, times, kinds = [e[1] for e in kept], [e[0] for e in kept], [e[2] for e in kept]
    if frames:
        _contact_sheet(frames, times, kf_dir, kinds)
    manifest["keyframes"] = {"done": bool(frames), "count": len(frames), "cues": len(cue_entries),
                             "max_frames": max_frames,
                             "times": [round(t, 1) for t in times], "kinds": kinds,
                             "files": [f.name for f in frames]}
    print(f"  关键帧：{len(frames)} 张（讲述锚点 {len(cue_entries)}）→ {kf_dir / 'contact.jpg'}")


def _contact_sheet(frames, times, kf_dir: Path, kinds=None):
    """5 列缩略图拼图，每帧下标注时间戳；锚点帧加 * 标记（默认字体不支持中文，用 ASCII）。"""
    from PIL import Image, ImageDraw
    cols, tw, th, label_h = 5, 240, 135, 20
    rows = max(1, math.ceil(len(frames) / cols))
    sheet = Image.new("RGB", (cols * tw, rows * (th + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    for i, (fp, t) in enumerate(zip(frames, times)):
        img = Image.open(fp).convert("RGB")
        img.thumbnail((tw - 4, th - 4))
        x, y = (i % cols) * tw, (i // cols) * (th + label_h)
        sheet.paste(img, (x + 2, y + 2))
        mark = "*" if kinds and i < len(kinds) and kinds[i] == "锚点" else ""
        draw.text((x + 6, y + th), C.fmt_ts(t) + mark, fill="black")
    sheet.save(kf_dir / "contact.jpg", quality=85)


def ensure_keyframes(cache_dir: Path, manifest, args, is_url, url, src_path):
    """keyframes 是叠加产物：只在请求且未生成时补，不影响缓存命中判定。
    --max-frames 变化会重抽（要的还是同一批画面，只是封顶不同）；--cue-words 换词需 --force。"""
    kf_info = manifest.get("keyframes") or {}
    redo = args.force or (kf_info.get("done") and args.max_frames != kf_info.get("max_frames"))
    if not args.keyframes or (kf_info.get("done") and not redo):
        return
    segments = read_segments(cache_dir / "segments.jsonl") or None  # 锚点用：字幕/转写均可
    if is_url:
        media = cache_dir / manifest["media_file"] if manifest.get("media_file") else None
        if not media or not media.exists():
            media = download_media(url, cache_dir, want_video=True)
            manifest["media_file"] = media.name
            C.save_manifest(cache_dir, manifest)
        extract_keyframes(media, cache_dir, manifest, segments=segments,
                          max_frames=args.max_frames, cue_words=parse_cue_words(args.cue_words))
        if manifest.get("status") == "done" and not args.keep_audio:
            media.unlink(missing_ok=True)  # 已完成态，不再需要续跑
    else:
        extract_keyframes(src_path, cache_dir, manifest, segments=segments,
                          max_frames=args.max_frames, cue_words=parse_cue_words(args.cue_words))
    C.save_manifest(cache_dir, manifest)


# ---------------------------------------------------------------- 产物

def write_srt(segments, path: Path):
    lines = []
    for i, s in enumerate(segments, 1):
        lines += [str(i), f"{C.srt_ts(s['start'])} --> {C.srt_ts(s['end'])}", s["text"], ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_chunks_video(segments, chunk_sec, cache_dir: Path, src_label, route):
    chunks_dir = cache_dir / "chunks"
    chunks_dir.mkdir(exist_ok=True)
    for f in chunks_dir.glob("chunk-*.md"):
        f.unlink()
    groups = {}
    for s in segments:
        groups.setdefault(int(s["start"] // chunk_sec), []).append(s)
    kind_note = "字幕" if route in ("cc", "ai") else "语音转写，可能有误"
    out = []
    for i, b in enumerate(sorted(groups)):
        segs = groups[b]
        t0, t1 = segs[0]["start"], segs[-1]["end"]
        rel = f"chunks/chunk-{i:02d}.md"
        lines = [f"<!-- material-parser ｜ chunk {i:02d} ｜ {C.fmt_ts(t0)}–{C.fmt_ts(t1)} ｜ "
                 f"来源 {src_label} ｜ {kind_note} -->",
                 f"# {C.fmt_ts(t0)} – {C.fmt_ts(t1)}", ""]
        for s in segs:
            lines.append(f"[{C.fmt_ts(s['start'])}] {s['text']}")
        (cache_dir / rel).write_text("\n".join(lines) + "\n", encoding="utf-8")
        out.append({"file": rel, "start": round(t0, 1), "end": round(t1, 1)})
    return out


def write_overview_video(cache_dir: Path, manifest, display, args, route, chunks, segments):
    dur = manifest.get("duration")
    route_desc = {"cc": "CC 字幕（平台人工字幕）",
                  "ai": "AI 字幕（平台自动字幕）",
                  "transcribe": f"本地转写（whisper {args.model}）",
                  "visual": "纯画面（未转写）"}.get(route, route)
    per = f"~{args.chunk_sec // 60} 分钟" if args.chunk_sec >= 120 else f"~{args.chunk_sec} 秒"
    lines = [f"# 概览 ｜ {display}", "",
             f"- 来源：{manifest.get('source')}",
             f"- 时长：{C.fmt_ts(dur) if dur else '?'} ｜ 路线：{route_desc}",
             f"- 段落：{len(segments)} ｜ 分块：{len(chunks)}（每块 {per}）"
             f" ｜ 生成：{C.today()}",
             ""]
    if route == "transcribe":
        lines += ["> ⚠ 转写文本属「据识别」：专有名词/术语可能有误。引用前按时间戳回查，"
                  "入库时标「（据识别，可能有误）」。", ""]
    lines += ["## chunk 索引", "", "| chunk | 时间范围 | 文件 |", "|---|---|---|"]
    for i, c in enumerate(chunks):
        lines.append(f"| {i:02d} | {C.fmt_ts(c['start'])}–{C.fmt_ts(c['end'])} | {c['file']} |")
    if not chunks:
        lines.append("| （无语音段/字幕为空） | - | - |")
    lines.append("")
    kf = manifest.get("keyframes") or {}
    if kf.get("done"):
        n = kf.get("count", 0)
        files = kf.get("files") or [f"kf_{i:04d}.jpg" for i in range(1, n + 1)]
        kinds = kf.get("kinds") or ["场景"] * len(kf.get("times") or [])
        lines += [f"## 关键帧（{n} 张，其中讲述锚点 {kf.get('cues', 0)} 张）", "",
                  "拼图见 keyframes/contact.jpg（缩略图下标注时间点，锚点带 * 号；"
                  "锚点=讲述指向画面的时刻，场景=画面突变的时刻，按时间挑帧看原图）：", "",
                  "| 帧 | 时间 | 类型 | 文件 |", "|---|---|---|---|"]
        for i, t in enumerate(kf.get("times", []), 1):
            kind = kinds[i - 1] if i <= len(kinds) else "场景"
            fname = files[i - 1] if i <= len(files) else "-"
            lines.append(f"| {i} | {C.fmt_ts(t)} | {kind} | keyframes/{fname} |")
        lines.append("")
    lines += [f"- SRT：transcript.srt ｜ 原始段落：segments.jsonl",
              "- 正文引用一律指向原视频 + 时间戳（[HH:MM:SS] 标记），不要指向本缓存目录。"]
    (cache_dir / "概览.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 主流程

def probe_duration(path: Path):
    fp = C.find_tool("ffprobe")
    if not fp:
        return None
    try:
        out = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=60)
        return float(out.stdout.strip())
    except Exception:
        return None


def main():
    C.utf8_console()
    ap = argparse.ArgumentParser(description="音视频/在线视频解析（字幕优先 → 转写兜底 → 分块缓存）")
    ap.add_argument("input", help="本地视频/音频路径，或在线视频 URL（B站等）")
    ap.add_argument("--model", default="small", help="whisper 模型：tiny/base/small/medium/large-v3（默认 small）")
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda"],
                    help="转写设备（默认 cpu，全功能；cuda 需 N 卡 + CUDA 运行库，"
                         "环境自检 common.py --doctor，菜单见 README「工具清单：默认与可选」）")
    ap.add_argument("--language", default=None, help="语言代码（如 zh），默认自动检测")
    ap.add_argument("--chunk-sec", type=int, default=C.DEFAULT_CHUNK_SEC, help="每块秒数（默认 600）")
    ap.add_argument("--keyframes", action="store_true", help="抽取关键帧（场景检测 + 讲述锚点）并生成缩略图对照表")
    ap.add_argument("--no-transcribe", action="store_true",
                    help="跳过字幕/转写，只抽关键帧（无口播、纯画面展示类；需配合 --keyframes）")
    ap.add_argument("--max-frames", type=int, default=120,
                    help=f"关键帧封顶数（默认 120；数百页幻灯片录像可放宽，如 --max-frames 300）")
    ap.add_argument("--cue-words", default=None,
                    help="讲述锚点词表（逗号分隔），按视频内容自定；传空串禁用锚定，默认用内置表")
    ap.add_argument("--keep-audio", action="store_true", help="保留中间 wav / 下载的媒体文件")
    ap.add_argument("--cookies", choices=["chrome", "edge", "firefox", "brave"],
                    help="从指定浏览器取登录态拉 AI 字幕（浏览器需已登录）")
    ap.add_argument("--force", action="store_true", help="忽略缓存全部重做")
    ap.add_argument("--cache-root", default=None,
                    help="缓存根目录（默认：MATERIAL_PARSER_CACHE 或向上定位 <库根>/解析缓存）")
    args = ap.parse_args()
    if args.no_transcribe and not args.keyframes:
        C.utf8_console()
        sys.exit("❌ --no-transcribe 只有关键帧产物，需配合 --keyframes（要文字内容就去掉它）")

    t0 = time.time()
    cache_root = Path(C.ensure_cache_root(args.cache_root))
    cache_root.mkdir(parents=True, exist_ok=True)

    is_url = bool(re.match(r"^https?://", args.input.strip()))
    url, src_path = None, None
    if is_url:
        url = args.input.strip()
        info = ydl_info(url)
        display = (info or {}).get("title") or url
        duration = (info or {}).get("duration")
        # 缓存键优先「站点+视频ID」：短链/去参链接/完整链接同一视频共用一份缓存；
        # B站分 P 的 p= 参数单独拼进键，避免分 P 之间串缓存
        if info and info.get("id") and info.get("extractor"):
            part = ""
            pq = parse_qs(urlparse(url).query).get("p")
            if pq:
                part = ":p=" + str(pq[0])
            sha1 = C.text_sha1(f"site:{info['extractor']}:{info['id']}{part}")
        else:
            sha1 = C.text_sha1("url:" + normalize_url(url))
        kind = "url"
    else:
        p = Path(args.input).resolve()
        if not p.exists():
            sys.exit(f"❌ 文件不存在：{p}")
        src_path = p
        display = p.name
        kind = "audio" if p.suffix.lower() in AUDIO_EXTS else "video"
        print("计算 sha1（首次约 10 秒/GB，之后走索引秒回）…")
        sha1 = C.file_sha1_cached(p, cache_root)
        duration = probe_duration(p)

    hit = C.find_cache_by_sha1(cache_root, sha1)
    if hit:
        cache_dir, manifest = hit
        if manifest.get("name") != display:
            manifest.setdefault("aliases", [])
            if display not in manifest["aliases"]:
                manifest["aliases"].append(display)
                C.save_manifest(cache_dir, manifest)
            print(f"ℹ 改名不失效：沿用缓存 {cache_dir.name}")
    else:
        cache_dir = C.new_cache_dir(cache_root, display, sha1)
        manifest = None

    done = bool(manifest and manifest.get("status") == "done" and C.chunks_ok(cache_dir, manifest))
    if done and manifest.get("route") in ("cc", "ai") and not manifest.get("segments"):
        print("ℹ 检测到历史空字幕产物（v1.0 缺陷）→ 自动重走链路重建")
        done = False
    if done and not args.force and manifest.get("route") == "visual" and not args.no_transcribe:
        print("ℹ 此前按纯画面解析（--no-transcribe），本次补转写/字幕")
        done = False
    redo_transcribe = False
    if done and not args.force:
        route = manifest.get("route")
        tp = manifest.get("transcribe_params") or {}
        if route == "transcribe" and (tp.get("model") != args.model
                                      or tp.get("language") != args.language):
            redo_transcribe = True
            print("ℹ 转写参数变化（model/language），按新参数重新转写")
        elif is_url and args.cookies and route == "transcribe":
            # 升级路径：试着拉 AI 字幕，拉到就重建产物（比转写准）；拉不到沿用转写缓存
            print("ℹ 指定 --cookies：尝试升级为 AI 字幕 …")
            segs = fetch_subs(url, cache_dir, cookies=args.cookies, auto=True)
            if segs:
                seg_path = cache_dir / "segments.jsonl"
                with C.Lock(cache_dir, "parse_video 升级字幕"):
                    write_segments_jsonl(segs, seg_path)
                    write_srt(segs, cache_dir / "transcript.srt")
                    cl = write_chunks_video(segs, args.chunk_sec, cache_dir, display, "ai")
                    write_overview_video(cache_dir, manifest, display, args, "ai", cl, segs)
                    manifest.update({"status": "done", "route": "ai",
                                     "segments": len(segs), "chunks": cl,
                                     "chunk_sec": args.chunk_sec})
                    C.save_manifest(cache_dir, manifest)
                print(f"✅ 已升级为 AI 字幕：{cache_dir / '概览.md'}")
                return
            print("ℹ AI 字幕仍不可用，沿用现有转写缓存")
            with C.Lock(cache_dir, "parse_video 补关键帧"):
                ensure_keyframes(cache_dir, manifest, args, is_url, url, src_path)
            print(f"✅ 缓存命中：{cache_dir / '概览.md'}")
            return
        elif manifest.get("chunk_sec") != args.chunk_sec and (cache_dir / "segments.jsonl").exists():
            # 只改分块粒度：不重拉字幕、不重转写，读现有段落重新分块
            with C.Lock(cache_dir, "parse_video 重新分块"):
                segments = read_segments(cache_dir / "segments.jsonl")
                cl = write_chunks_video(segments, args.chunk_sec, cache_dir, display,
                                        manifest.get("route") or "?")
                write_overview_video(cache_dir, manifest, display, args,
                                     manifest.get("route") or "?", cl, segments)
                manifest.update({"chunk_sec": args.chunk_sec, "chunks": cl,
                                 "segments": len(segments)})
                C.save_manifest(cache_dir, manifest)
            print(f"✅ 已按新 chunk-sec={args.chunk_sec} 重新分块：{cache_dir / '概览.md'}")
            return
        else:
            with C.Lock(cache_dir, "parse_video 补关键帧"):
                ensure_keyframes(cache_dir, manifest, args, is_url, url, src_path)
            print(f"✅ 缓存命中（{time.time() - t0:.1f} 秒）：{cache_dir / '概览.md'}")
            return

    with C.Lock(cache_dir, f"parse_video {display[:40]}"):
        manifest = manifest or {"schema": 1, "kind": kind, "sha1": sha1,
                                "source": args.input}
        manifest.update({"name": display, "kind": kind, "status": "running"})
        if duration:
            manifest["duration"] = duration
        if src_path:
            manifest["source_path"] = str(src_path)
        C.save_manifest(cache_dir, manifest)

        seg_path = cache_dir / "segments.jsonl"
        tp = {"model": args.model, "language": args.language}
        route = None

        can_resume = (manifest.get("route") == "transcribe" and seg_path.exists()
                      and (manifest.get("transcribe_params") or {}).get("model") == args.model
                      and (manifest.get("transcribe_params") or {}).get("language") == args.language)
        if args.no_transcribe:
            # 纯画面路线：不拉字幕不转写（无口播/纯音乐展示类），关键帧在 ensure_keyframes 里出
            seg_path.unlink(missing_ok=True)
            route = "visual"
        elif can_resume and manifest.get("transcribe_done"):
            # 上次转写已完成（如 chunks 被手动删导致产物重建）：直接用现有段落重建，
            # 不重下音频、不重载模型去"续跑"一个已经完成的转写
            print("ℹ 转写此前已完成：直接按现有段落重建产物")
            route = "transcribe"
        elif can_resume:
            wav = ensure_wav(cache_dir, manifest, args, is_url, url)
            if wav:
                transcribe(wav, cache_dir, args)
            route = "transcribe"
        elif redo_transcribe:
            # 显式换模型/语言 → 跳过字幕链路，按新参数重新转写
            seg_path.unlink(missing_ok=True)
            if is_url:
                media = download_media(url, cache_dir, want_video=args.keyframes)
                manifest["media_file"] = media.name
                C.save_manifest(cache_dir, manifest)
                wav = to_wav(media, cache_dir)
            else:
                wav = to_wav(src_path, cache_dir)
            upd = {"route": "transcribe", "transcribe_params": tp, "transcribe_done": False}
            if wav:
                upd["wav_file"] = wav.name
            manifest.update(upd)
            C.save_manifest(cache_dir, manifest)
            if wav:
                transcribe(wav, cache_dir, args)
            route = "transcribe"
        else:
            seg_path.unlink(missing_ok=True)
            if is_url:
                print("[1/3] 尝试 CC 字幕（免登录，秒级）…")
                segs = fetch_subs(url, cache_dir, cookies=None, auto=False)
                if segs:
                    route = "cc"
                    print(f"  ✅ CC 字幕：{len(segs)} 段")
                if segs is None and args.cookies:
                    print(f"[2/3] 尝试 AI 字幕（--cookies {args.cookies}）…")
                    segs = fetch_subs(url, cache_dir, cookies=args.cookies, auto=True)
                    if segs:
                        route = "ai"
                        print(f"  ✅ AI 字幕：{len(segs)} 段")
                if segs is None:
                    print(f"[3/3] 字幕不可用 → 下载音频本地转写（{args.device.upper()}，"
                          f"{'慢，' if args.device == 'cpu' else ''}建议后台跑）…")
                    media = download_media(url, cache_dir, want_video=False)
                    manifest["media_file"] = media.name
                    C.save_manifest(cache_dir, manifest)
                    wav = to_wav(media, cache_dir)
                    upd = {"route": "transcribe", "transcribe_params": tp, "transcribe_done": False}
                    if wav:
                        upd["wav_file"] = wav.name
                    manifest.update(upd)
                    C.save_manifest(cache_dir, manifest)
                    if wav:
                        transcribe(wav, cache_dir, args)
                    route = "transcribe"
                else:
                    write_segments_jsonl(segs, seg_path)
                    manifest["route"] = route
            else:
                wav = to_wav(src_path, cache_dir)
                upd = {"route": "transcribe", "transcribe_params": tp, "transcribe_done": False}
                if wav:
                    upd["wav_file"] = wav.name
                manifest.update(upd)
                C.save_manifest(cache_dir, manifest)
                if wav:
                    transcribe(wav, cache_dir, args)
                route = "transcribe"

        ensure_keyframes(cache_dir, manifest, args, is_url, url, src_path)

        segments = read_segments(seg_path)
        write_srt(segments, cache_dir / "transcript.srt")
        chunk_list = write_chunks_video(segments, args.chunk_sec, cache_dir, display, route)
        write_overview_video(cache_dir, manifest, display, args, route, chunk_list, segments)

        manifest.update({"status": "done", "route": route, "segments": len(segments),
                         "chunks": chunk_list, "chunk_sec": args.chunk_sec,
                         "transcribe_done": route == "transcribe"})
        C.save_manifest(cache_dir, manifest)

        # 中间文件清理：成功即删（GB 级），失败时保留供续跑；--keep-audio 保留
        if not args.keep_audio:
            if manifest.get("wav_file"):
                (cache_dir / manifest["wav_file"]).unlink(missing_ok=True)
            if is_url and manifest.get("media_file"):
                (cache_dir / manifest["media_file"]).unlink(missing_ok=True)

        print(f"✅ 完成：{route_desc(route)} ｜ {len(segments)} 段 → {len(chunk_list)} 块，"
              f"总耗时 {time.time() - t0:.0f} 秒")
        print(f"   概览：{cache_dir / '概览.md'}")
        if route == "transcribe":
            print("   提示：转写文本属「据识别」，引用前按时间戳回查。")
        if route in ("transcribe",) and not segments:
            print("   提示：转写 0 段——像无口播视频（纯画面/背景音乐），"
                  "重跑可用 --no-transcribe --keyframes 走纯画面路线")


def route_desc(route):
    return {"cc": "CC 字幕", "ai": "AI 字幕", "transcribe": "本地转写",
            "visual": "纯画面"}.get(route, route)


if __name__ == "__main__":
    main()
