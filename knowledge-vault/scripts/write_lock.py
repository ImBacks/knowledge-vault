#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""库级落盘互斥锁（knowledge-vault v3 内置脚本，§7 第 10 条的机械执行者）。

多会话并发（同时开几个会话都调用技能）时，对 大纲.md 的写入是裸的
read-modify-write：后写覆盖先写、版本号算重、锚点漂移。本脚本用一把租期制锁
把「实际落盘」的几秒钟变成临界区：

    python write_lock.py <大纲.md 路径> acquire [--lease 300] [--force]
    python write_lock.py <大纲.md 路径> release --token <acquire 输出的令牌> [--force]
    python write_lock.py <大纲.md 路径> status

锁文件：`<大纲.md 同目录>/.写入锁`（JSON：taken / lease / token，不进 git）。

设计取舍：
- **租期制，不做 PID 判活**。material-parser 的 lock.pid 持锁者是单一活进程，PID 判活
  精确；这里的持锁者是 agent 会话（跨多次工具调用，没有单一进程可查），所以用租期：
  忘了 release 时租期到自动过期，不会死锁。默认 300 秒——正常落盘只要几秒，大导入
  （§20）可加 `--lease 1800`。
- **acquire 原子创建**（open "x"）：锁已存在且未过期 → 拒绝并报剩余时间；已过期或
  内容损坏 → 原子接管（tmp + os.replace）；`--force` 无视租期强夺，仅在确认没有
  其它会话在写时用（报告用户后由用户拍板）。
- **release 凭令牌**：acquire 成功时输出令牌，release 令牌不匹配 → 锁已被其它会话
  接管/过期重取，拒绝删除，防止误删别人的锁。

退出码：0 成功 ｜ 1 锁被占 / release 被拒 ｜ 2 用法错误 / 文件不存在。
"""

import json
import os
import secrets
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

DEFAULT_LEASE = 300  # 秒。正常落盘（编辑+自检+日志）只要几秒，300 秒是宽裕的余量


def utf8_console():
    """Windows 控制台默认 GBK，统一成 UTF-8 防止中文/符号打印报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def lock_path(md_path: Path) -> Path:
    return md_path.parent / ".写入锁"


def read_lock(p: Path):
    """读锁内容；损坏/缺失返回 None（调用方按过期处理）。"""
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def remaining_sec(data: dict) -> float:
    taken = data.get("taken", "")
    try:
        age = time.time() - datetime.fromisoformat(taken).timestamp()
    except (ValueError, TypeError, OSError):
        return -1.0  # 时间损坏 → 按过期处理
    try:
        lease = float(data.get("lease", DEFAULT_LEASE))
    except (TypeError, ValueError):
        lease = DEFAULT_LEASE
    return lease - age


def atomic_write(p: Path, payload: dict):
    """接管/强夺时用原子替换，避免两个取锁者互相覆盖出中间态。"""
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".写入锁.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cmd_acquire(md: Path, lease: int, force: bool) -> int:
    p = lock_path(md)
    token = secrets.token_hex(8)
    payload = {"taken": now_iso(), "lease": lease, "token": token}
    try:
        with open(p, "x", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        takeover = False
    except FileExistsError:
        data = read_lock(p)
        left = remaining_sec(data) if data else -1.0
        if force:
            takeover = True
            old = data or {}
            print(f"⚠ 已强夺持锁（原锁 token …{(old.get('token') or '????')[-4:]}，"
                  f"仅应在确认无其它会话写入时使用）")
        elif left <= 0:
            takeover = True
            print("ℹ 原锁已过期（或内容损坏），自动接管")
        else:
            utf8_console()
            print(f"❌ 写锁被占用：另一会话可能正在写入 大纲.md（剩余约 {int(left) + 1} 秒，"
                  f"锁：{p}）。等几秒重试 acquire；不要 --force 抢锁，除非用户确认没有别的会话在写。")
            return 1
        if takeover:
            atomic_write(p, payload)

    print(f"✅ 已取写锁（token {token}，租期 {lease} 秒）")
    print(f"   锁内完成：重读大纲 → 重验清单 → 编辑 → 自检 → 日志，然后释放：")
    print(f'   write_lock.py "{md}" release --token {token}')
    return 0


def cmd_release(md: Path, token: str, force: bool) -> int:
    p = lock_path(md)
    if not p.exists():
        print("ℹ 写锁不存在（可能已过期被其它会话接管）。无需释放。")
        return 0
    data = read_lock(p)
    if force:
        p.unlink()
        print("✅ 已强制删除写锁")
        return 0
    if not data or not token or data.get("token") != token:
        utf8_console()
        print("❌ 令牌不匹配：锁已被其它会话接管或过期重取，不删除（确认后可 --force）。")
        return 1
    p.unlink()
    print("✅ 写锁已释放")
    return 0


def cmd_status(md: Path) -> int:
    p = lock_path(md)
    if not p.exists():
        print("无锁。")
        return 0
    data = read_lock(p)
    if not data:
        print(f"锁文件存在但内容损坏（{p}）→ 已按过期处理，可被接管。")
        return 0
    left = remaining_sec(data)
    state = f"剩余约 {max(0, int(left))} 秒" if left > 0 else "已过期（下次 acquire 会接管）"
    print(f"持锁：taken {data.get('taken')} ｜ {state} ｜ token …{(data.get('token') or '????')[-4:]}")
    return 0


def main() -> int:
    utf8_console()
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    md = Path(args[0])
    if not md.exists():
        print(f"错误：文件不存在：{md}")
        return 2
    cmd = args[1] if len(args) > 1 else ""

    def opt_value(flag, default=None):
        return args[args.index(flag) + 1] if flag in args else default

    if cmd == "acquire":
        lease = int(opt_value("--lease", DEFAULT_LEASE))
        return cmd_acquire(md, lease, "--force" in args)
    if cmd == "release":
        return cmd_release(md, opt_value("--token", ""), "--force" in args)
    if cmd == "status":
        return cmd_status(md)
    print(f"未知命令：{cmd!r}（可用：acquire / release / status）")
    return 2


if __name__ == "__main__":
    sys.exit(main())
