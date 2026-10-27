# -*- coding: utf-8 -*-
"""material-parser 公共库：内容寻址缓存、断点清单、PID 锁、外部工具定位、依赖自检。

所有解析脚本共用本模块。单独运行可做依赖自检 / 入库后清缓存：

    D:\\python\\python.exe scripts/common.py --doctor
    D:\\python\\python.exe scripts/common.py --clean <缓存目录> [--force]

缓存语义（详见 解析手册.md）：
- 缓存根三级：--cache-root 参数 > MATERIAL_PARSER_CACHE 环境变量 > 向上找 大纲.md 定位库根
  （<库根>/解析缓存）；三级都落空 → 运行时报错要求显式指定，不静默落错位置；
- 缓存目录 `<文件名>-<sha1前10位>`，查找按 sha1 扫 manifest —— 改名不失效；
- manifest.json 记录关键参数，参数不匹配视为未命中，重跑流程（慢步骤按参数复用）；
- lock.pid 运行锁：活进程持锁则拒绝，陈旧锁自动清理。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# 库根探测：从脚本位置逐级向上找 大纲.md（模块可嵌在技能目录任意深度，
# 如 库根/knowledge-vault/material-parser/scripts/）。找不到且未设 MATERIAL_PARSER_CACHE
# 时，解析入口会明确报错要求指定缓存根（防静默落错位置）。
SCRIPTS_DIR = Path(__file__).resolve().parent
ENV_CACHE = "MATERIAL_PARSER_CACHE"
ROOT_MARKER = "大纲.md"


def root_by_marker():
    for d in SCRIPTS_DIR.parents:
        if (d / ROOT_MARKER).exists():
            return d
    return None


if os.environ.get(ENV_CACHE):
    CACHE_ROOT = Path(os.environ[ENV_CACHE])
    CACHE_SOURCE = f"{ENV_CACHE} 环境变量"
else:
    _r = root_by_marker()
    if _r is not None:
        CACHE_ROOT = _r / "解析缓存"
        CACHE_SOURCE = f"向上定位库根（{ROOT_MARKER}）"
    else:
        CACHE_ROOT = None
        CACHE_SOURCE = None


def ensure_cache_root(cli_value=None) -> str:
    """解析脚本入口调用：CLI 参数 > 环境变量 > 库根标记；都落空则报错退出（不静默乱建）。"""
    if cli_value:
        return cli_value
    if CACHE_ROOT is not None:
        return str(CACHE_ROOT)
    utf8_console()
    sys.exit(f"❌ 未定位到库根（从 {SCRIPTS_DIR} 向上找不到 {ROOT_MARKER}），且未设 {ENV_CACHE}。\n"
             f"   请设 {ENV_CACHE}=<库根>/解析缓存（推荐，长期有效），或本次调用加 --cache-root <目录>。")

# hf_xet 传输后端与 hf-mirror 不兼容会静默挂起，禁用走普通 HTTP（用户显式设置则不覆盖）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

TEXT_MIN_CHARS = 25      # 一页文本层少于该字符数 → 视为扫描页，走 OCR
DEFAULT_CHUNK_PAGES = 50
DEFAULT_CHUNK_SEC = 600  # 10 分钟一块

# selftest 注入中断/离线桩的钩子（环境变量）
ENV_FAIL_OCR = "MATERIAL_PARSER_FAIL_AFTER_OCR_PAGES"
ENV_FAIL_SEG = "MATERIAL_PARSER_FAIL_AFTER_SEGMENTS"
ENV_FAKE_SUBS = "MATERIAL_PARSER_FAKE_SUBS"  # 值=本地 vtt 路径，字幕链路离线自测用


def utf8_console():
    """Windows 控制台默认 GBK，统一成 UTF-8 防止中文/符号打印报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today():
    return datetime.now().strftime("%Y-%m-%d")


def file_sha1(path, chunk=4 << 20):
    """流式 sha1，几 GB 视频约 10 秒/GB，属正常成本。"""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# 缓存根级 sha1 索引：记录 源路径 → (size, mtime_ns, sha1)。重跑同一文件时
# size+mtime 未变即跳过整文件哈希（缓存命中的"秒回"不再被 10 秒/GB 的哈希拖住）。
# 索引只是提示：不匹配就重算并回写；并发写以最后落盘者为准（丢一条无碍，下次重算）。
SHA1_INDEX = "sha1-index.json"


def file_sha1_cached(path, cache_root) -> str:
    path = Path(path)
    st = path.stat()
    key = str(path.resolve())
    idx_path = Path(cache_root) / SHA1_INDEX
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        idx = {}
    ent = idx.get(key)
    if ent and ent.get("size") == st.st_size and ent.get("mtime") == st.st_mtime_ns:
        return ent["sha1"]
    h = file_sha1(path)
    idx[key] = {"size": st.st_size, "mtime": st.st_mtime_ns, "sha1": h}
    try:
        tmp = Path(cache_root) / f"{SHA1_INDEX}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        tmp.replace(idx_path)
    except OSError:
        pass  # 索引写失败不影响正确性，只是下次重哈希
    return h


def text_sha1(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def safe_name(name, limit=60):
    name = re.sub(r'[\\/:*?"<>|\r\n\t ]+', "-", name).strip("-.")
    return name[:limit] or "file"


# ---------------------------------------------------------------- manifest

def load_manifest(cache_dir: Path):
    p = Path(cache_dir) / "manifest.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_manifest(cache_dir: Path, manifest: dict):
    # tmp 名带 pid：命中路径的 alias 记录是无锁写，固定名并发时可能撞车
    manifest["updated"] = now_iso()
    tmp = Path(cache_dir) / f"manifest.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(Path(cache_dir) / "manifest.json")


def find_cache_by_sha1(cache_root: Path, sha1: str):
    """按内容哈希扫全部 manifest 查找缓存（个人库量级毫秒级）。改名不失效。"""
    cache_root = Path(cache_root)
    if not cache_root.exists():
        return None
    for d in sorted(cache_root.iterdir()):
        if not d.is_dir():
            continue
        m = load_manifest(d)
        if m and m.get("sha1") == sha1:
            return d, m
    return None


def new_cache_dir(cache_root: Path, name: str, sha1: str) -> Path:
    d = Path(cache_root) / f"{safe_name(name)}-{sha1[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def chunks_ok(cache_dir: Path, manifest: dict) -> bool:
    """status=done 之后，chunk 文件与概览是否都还在（缓存目录可被人删）。"""
    cache_dir = Path(cache_dir)
    if not (cache_dir / "概览.md").exists():
        return False
    for c in manifest.get("chunks") or []:
        f = cache_dir / c.get("file", "")
        if not f.exists() or f.stat().st_size == 0:
            return False
    return True


# ---------------------------------------------------------------- PID 锁

def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Lock:
    """目录级运行锁：lock.pid 内容为「PID\\t命令」。

    活进程持锁 → 拒绝运行（避免两个进程同时写断点文件）；
    进程已死 → 视为陈旧锁，清理后续跑；正常结束自动删除。
    """

    def __init__(self, cache_dir: Path, note: str = ""):
        self.path = Path(cache_dir) / "lock.pid"
        self.note = note

    def acquire(self):
        for _ in range(2):
            try:
                with open(self.path, "x", encoding="utf-8") as f:
                    f.write(f"{os.getpid()}\t{self.note}")
                return
            except FileExistsError:
                raw = ""
                try:
                    raw = self.path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
                pid_str = raw.split("\t")[0].strip()
                try:
                    pid = int(pid_str)
                except ValueError:
                    pid = 0
                if pid_alive(pid):
                    utf8_console()
                    sys.exit(
                        f"❌ 同一材料已有解析任务在运行（PID {pid}，锁：{self.path}）。\n"
                        f"   多半是另一个会话在解析同一材料：不要换参数或 --force 强跑，等它结束后\n"
                        f"   重跑同一条命令即可命中缓存（status=done 秒回）。确认没有任何进程在跑时，\n"
                        f"   才手动删除锁文件续跑。"
                    )
                try:  # 陈旧锁
                    self.path.unlink()
                except OSError:
                    pass
        raise RuntimeError("无法获取运行锁（lock.pid）")

    def release(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


# ---------------------------------------------------------------- 小工具

def test_limit(env_key):
    """读取 selftest 注入的中断阈值；未设置返回 None。"""
    v = os.environ.get(env_key)
    try:
        return int(v) if v else None
    except ValueError:
        return None


def fmt_ts(sec):
    sec = max(0, int(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def srt_ts(sec):
    ms = int(round((sec - int(sec)) * 1000))
    if ms == 1000:
        ms = 999
    h, m, s = int(sec) // 3600, int(sec) % 3600 // 60, int(sec) % 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def find_tool(name):
    """定位 ffmpeg/ffprobe：PATH → winget Links 目录 → winget Packages 深搜。"""
    p = shutil.which(name)
    if p:
        return p
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        cand = local / "Microsoft" / "WinGet" / "Links" / f"{name}.exe"
        if cand.exists():
            return str(cand)
        pkgs = local / "Microsoft" / "WinGet" / "Packages"
        if pkgs.exists():
            for hit in pkgs.glob(f"Gyan.FFmpeg*/**/{name}.exe"):
                return str(hit)
    return None


def run_ffmpeg(args, check=True, capture=False):
    ff = find_tool("ffmpeg")
    if not ff:
        utf8_console()
        sys.exit("❌ 未找到 ffmpeg。请先安装：winget install Gyan.FFmpeg（装完重开终端）")
    cmd = [ff, "-hide_banner", "-loglevel", "error"] + args
    return subprocess.run(cmd, check=check, capture_output=capture, text=capture,
                          encoding="utf-8" if capture else None,
                          errors="replace" if capture else None)


# ---------------------------------------------------------------- --clean

def clean_cache(cache_dir: Path, force: bool = False) -> int:
    """入库落盘成功后清理该材料的缓存目录。

    缓存是收录期间的工作台，不是档案（knowledge-vault §7）。只删 status=done 的；
    未完成任务的缓存是断点，拒绝删除（--force 才删）；有任务在跑（lock.pid）一律拒绝。
    """
    utf8_console()
    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir():
        print(f"❌ 目录不存在：{cache_dir}")
        return 1
    lock = cache_dir / "lock.pid"
    if lock.exists():
        try:
            pid = int(lock.read_text(encoding="utf-8", errors="replace").split("\t")[0])
        except (OSError, ValueError):
            pid = 0
        if pid_alive(pid):
            print(f"❌ 有任务正在该目录运行（PID {pid}），先等它结束")
            return 1
        try:  # 陈旧锁：进程已死，与解析脚本的取锁行为一致（判活后清理），不该永远挡住清理
            lock.unlink()
        except OSError:
            pass
        print("ℹ 运行锁为陈旧残留（进程已退出），已清理")
    m = load_manifest(cache_dir)
    if not m:
        print(f"❌ 不是解析缓存目录（缺 manifest.json）：{cache_dir}")
        return 1
    if m.get("status") != "done" and not force:
        print(f"❌ 任务未完成（status={m.get('status')}），缓存是断点，删除将丢失进度；确认要删加 --force")
        return 1
    shutil.rmtree(cache_dir)
    print(f"✅ 已删除 {m.get('name', cache_dir.name)} 的解析缓存（{cache_dir}）")
    return 0


# ---------------------------------------------------------------- --doctor

def nvidia_dll_dirs():
    """pip 装的 nvidia-cublas-cu12 / nvidia-cudnn-cu12 里的 DLL 目录；未装或非 Windows 返回 []。"""
    try:
        import importlib.util
        spec = importlib.util.find_spec("nvidia")
        if not spec or not spec.submodule_search_locations:
            return []
        root = Path(list(spec.submodule_search_locations)[0])
        dirs = []
        for sub in ("cudnn", "cublas"):
            for cand in (root / sub / "bin", root / sub / "lib", root / sub):
                if cand.is_dir() and any(cand.glob("*.dll")):
                    dirs.append(cand)
                    break
        return dirs
    except Exception:
        return []


def enable_cuda_dlls():
    """让进程能找到 pip 装的 cuBLAS/cuDNN DLL（ctranslate2 加载用）。双保险：
    ① 前插 PATH——ctranslate2 走标准搜索顺序（LoadLibrary 不带标志）时生效；
    ② os.add_dll_directory——带 USER_DIRS 标志的加载时生效。
    手动配过 PATH/PATH 里已有 CUDA 的装法不受影响（前插的目录优先但内容一致）。
    返回 add_dll_directory 成功注册的目录数。"""
    dirs = nvidia_dll_dirs()
    if dirs:
        extra = os.pathsep.join(str(d) for d in dirs)
        os.environ["PATH"] = extra + os.pathsep + os.environ.get("PATH", "")
    n = 0
    if hasattr(os, "add_dll_directory"):
        for d in dirs:
            try:
                os.add_dll_directory(str(d))
                n += 1
            except Exception:
                pass
    return n


DEPS = [
    # (显示名, import 名, 用途, 安装命令, [兼容旧包 import 名…])
    # rapidocr 双包兼容与 parse_pdf.make_ocr 对齐：装 v1 旧包 rapidocr_onnxruntime 不算缺依赖
    ("pymupdf", "pymupdf", "PDF 文本层提取", "pip install pymupdf"),
    ("faster-whisper", "faster_whisper", "本地语音转写", "pip install faster-whisper"),
    ("rapidocr", "rapidocr", "扫描页 OCR 兜底", "pip install rapidocr", "rapidocr_onnxruntime"),
    ("yt-dlp", "yt_dlp", "在线视频字幕/音频拉取", "pip install yt-dlp"),
    ("Pillow", "PIL", "关键帧拼图 / OCR 图像", "pip install Pillow"),
    ("numpy", "numpy", "OCR 图像数组", "pip install numpy"),
    ("av", "av", "faster-whisper 音频解码", "pip install av"),
]

# 「我想干什么 → 需要哪些」：doctor 按用途报告，让用户按需安装而不是全装
INTENTS = [
    ("解析 PDF（文本层 + 扫描页 OCR）", ["pymupdf", "rapidocr", "Pillow", "numpy"], []),
    ("转写视频 / 音频", ["faster-whisper", "av"], ["ffmpeg"]),
    ("B站等在线视频（拉字幕 / 下载音频）", ["yt-dlp"], []),
    ("关键帧对照表（讲义类视频）", ["Pillow"], ["ffmpeg"]),
]


def _importable(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def doctor():
    utf8_console()
    root = root_by_marker()
    print(f"库根：{root if root else f'未定位（从脚本位置向上找不到 {ROOT_MARKER}）'}")
    if CACHE_ROOT is None:
        print(f"缓存根：❓ 未定位 —— 设 {ENV_CACHE}=<库根>/解析缓存 后才可跑解析")
    else:
        try:
            CACHE_ROOT.mkdir(parents=True, exist_ok=True)
            probe = CACHE_ROOT / ".probe"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            print(f"缓存根：{CACHE_ROOT}  ✅ 可写（来源：{CACHE_SOURCE}）")
        except Exception as e:
            print(f"缓存根：{CACHE_ROOT}  ❌ 不可写：{e}")
            return 1

    dep_ok = {}
    for dep in DEPS:
        pkg, mod, why, fix = dep[0], dep[1], dep[2], dep[3]
        fallbacks = dep[4:]
        found, note, err = None, "", None
        try:
            found = __import__(mod)
        except Exception as primary:
            err = primary
            for fb in fallbacks:   # 兼容旧包（如 rapidocr v1 的 rapidocr_onnxruntime）
                try:
                    found = __import__(fb)
                    note = f"（经旧包 {fb}）"
                    break
                except Exception:
                    continue
        if found is not None:
            dep_ok[pkg] = True
            ver = getattr(found, "__version__", "")
            print(f"✅ {pkg:<24} {why}{'  ' + ver if ver else ''}  {note}".rstrip())
        else:
            dep_ok[pkg] = False
            print(f"❌ {pkg:<24} {why} —— {fix}（{type(err).__name__}）")

    tool_ok = {}
    for tool in ("ffmpeg", "ffprobe"):
        p = find_tool(tool)
        tool_ok[tool] = bool(p)
        if p:
            print(f"✅ {tool:<24} {p}")
        else:
            print(f"❌ {tool:<24} 未找到 —— winget install Gyan.FFmpeg（装完重开终端）")

    print("\n按用途（想干什么 → 还缺什么）：")
    for intent, pkgs, tools in INTENTS:
        miss_pkgs = [p for p in pkgs if not dep_ok.get(p)]
        miss_tools = [t for t in tools if not tool_ok.get(t)]
        if not miss_pkgs and not miss_tools:
            print(f"  ✅ {intent}：依赖齐")
            continue
        parts = []
        if miss_pkgs:
            parts.append(f"{sys.executable} -m pip install {' '.join(miss_pkgs)}")
        if miss_tools:
            parts.append("winget install Gyan.FFmpeg")
        print(f"  ❌ {intent}：缺 {' '.join(miss_pkgs + miss_tools)} → {'；'.join(parts)}")

    # 蒸馏（distill/ = cangjie-skill 功能子集）：独立于上方 DEPS——缺 PyYAML 不影响解析各组的退出码，
    # 只报一行指路；蒸馏降级路线（agent 手工）见 解析手册「蒸馏」节
    distill_entry = Path(__file__).resolve().parent.parent / "distill" / "scripts" / "cangjie.py"
    if not distill_entry.exists():
        print("ℹ 蒸馏子模块（distill/）不在——蒸馏走 agent 手工路线（解析手册「蒸馏」节）")
    else:
        try:
            import yaml  # noqa: F401
            opt_miss = [m for m in ("tiktoken", "jsonschema") if not _importable(m)]
            extra = f"；可选未装：{'、'.join(opt_miss)}（token 计数 / schema 校验用，不装不影响）" if opt_miss else ""
            print(f"✅ 蒸馏（cangjie）  长内容→方法论/技能包  入口 {distill_entry}{extra}")
        except Exception:
            print(f"❌ 蒸馏（cangjie）  缺 PyYAML —— pip install pyyaml（不装则蒸馏降级为 agent 手工路线）")

    try:
        import ctranslate2
        n_gpu = ctranslate2.get_cuda_device_count()
    except Exception:
        n_gpu = None
    if n_gpu is None:
        print("ℹ ctranslate2 未装，跳过 GPU 探测")
    elif not n_gpu:
        print("ℹ 未检测到 NVIDIA GPU：转写按 CPU 运行（默认路径，功能完整，无需任何 CUDA 组件）")
    elif nvidia_dll_dirs():
        print(f"✅ GPU 转写就绪（检测到 {n_gpu} 个 CUDA 设备）：parse_video 加 --device cuda 启用"
              f"（默认仍 CPU；收益随模型/显卡而变，实测参考见 解析手册「能力总览与可选加速」）")
    else:
        print(f"⚠ 检测到 {n_gpu} 个 CUDA 设备，转写仍按 CPU 运行（默认路径，功能完整）。"
              f"长视频常客可选开启 GPU 提速：")
        print(f"    {sys.executable} -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12")
        print(f"    装完重跑 --doctor 本行变 ✅；运行时 parse_video 加 --device cuda；"
              f"不装不影响任何现有功能")

    ep = os.environ.get("HF_ENDPOINT")
    print(f"ℹ HF_ENDPOINT：{'已设置 ' + ep if ep else '未设置（脚本运行时会默认走 hf-mirror）'}")
    print("ℹ selftest 注入中断的钩子：" + f"{ENV_FAIL_OCR} / {ENV_FAIL_SEG}")
    ok = all(dep_ok.values()) and all(tool_ok.values())
    return 0 if ok else 1


if __name__ == "__main__":
    if "--doctor" in sys.argv:
        sys.exit(doctor())
    if "--clean" in sys.argv:
        i = sys.argv.index("--clean")
        try:
            d = Path(sys.argv[i + 1])
        except IndexError:
            utf8_console()
            sys.exit("用法：common.py --clean <缓存目录> [--force]")
        sys.exit(clean_cache(d, force="--force" in sys.argv))
    utf8_console()
    print(__doc__)
