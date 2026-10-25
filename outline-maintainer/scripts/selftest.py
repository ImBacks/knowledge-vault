#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""库级脚本自检（write_lock + merge_outline，合成数据，不碰真实库）。

用法:
    python selftest.py

覆盖：
1. write_lock 全路径：取锁成功 → 占用拒绝 → status → 强夺 → 错令牌拒释放 →
   正令牌释放 → 过期自动接管 → 锁文件损坏自动接管 → 无锁时释放。
2. merge_outline 四类分诊（合成 双库）：A 直接并入（含知识正文路径计划）、
   B 同名同物（活/核取新、别名并集、用法追加）、C 疑似同物（异名同 URL）、
   D 同名需仲裁（同名异源）、对方库内部重复跳过、`- 相关：` 悬空告警、
   文件头条数不符告警、只读性（跑完两库文件逐字节不变）。
"""

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"✓ {name}")
    else:
        FAIL += 1
        print(f"✗ {name}  {detail}")


def run(script, *args):
    return subprocess.run(
        [PY, str(HERE / script)] + [str(a) for a in args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def sha(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def write_lock_with(taken_iso: str, lease: int = 300, token: str = "0" * 16) -> str:
    return json.dumps({"taken": taken_iso, "lease": lease, "token": token}, ensure_ascii=False)


# ---------------------------------------------------------------- write_lock

def test_write_lock(root: Path):
    lib = root / "libA"
    lib.mkdir()
    md = lib / "大纲.md"
    md.write_text("<!-- v1 ｜ 条目 0 ｜ 更新 2026-10-25 -->\n# 大纲\n", encoding="utf-8")
    lock = lib / ".写入锁"

    r = run("write_lock.py", md, "acquire")
    m = re.search(r"token ([0-9a-f]{16})", r.stdout)
    check("acquire 成功", r.returncode == 0 and m, r.stdout + r.stderr)
    token1 = m.group(1) if m else ""
    check("锁文件已建", lock.exists())

    r2 = run("write_lock.py", md, "acquire")
    check("占用时 acquire 被拒（退出码 1）", r2.returncode == 1 and "写锁被占用" in r2.stdout, r2.stdout)

    r3 = run("write_lock.py", md, "status")
    check("status 显示持锁", "剩余约" in r3.stdout and token1[-4:] in r3.stdout, r3.stdout)

    r4 = run("write_lock.py", md, "acquire", "--force")
    m4 = re.search(r"token ([0-9a-f]{16})", r4.stdout)
    check("--force 强夺成功且警告", r4.returncode == 0 and "强夺" in r4.stdout and m4, r4.stdout)
    token2 = m4.group(1) if m4 else ""

    r5 = run("write_lock.py", md, "release", "--token", "f" * 16)
    check("错令牌释放被拒且锁仍在", r5.returncode == 1 and lock.exists(), r5.stdout)

    r6 = run("write_lock.py", md, "release", "--token", token2)
    check("正令牌释放成功", r6.returncode == 0 and not lock.exists(), r6.stdout)

    # 过期接管：手工放一把 600 秒前取的锁（租期 300）
    old = (datetime.now().astimezone() - timedelta(seconds=600)).isoformat(timespec="seconds")
    lock.write_text(write_lock_with(old), encoding="utf-8")
    r7 = run("write_lock.py", md, "acquire")
    check("过期锁自动接管", r7.returncode == 0 and "接管" in r7.stdout, r7.stdout)
    m7 = re.search(r"token ([0-9a-f]{16})", r7.stdout)
    run("write_lock.py", md, "release", "--token", m7.group(1) if m7 else "")

    # 损坏锁按过期处理
    lock.write_text("这不是JSON", encoding="utf-8")
    r8 = run("write_lock.py", md, "acquire")
    check("损坏锁自动接管", r8.returncode == 0 and "接管" in r8.stdout, r8.stdout)
    m8 = re.search(r"token ([0-9a-f]{16})", r8.stdout)
    run("write_lock.py", md, "release", "--token", m8.group(1) if m8 else "")

    r9 = run("write_lock.py", md, "release", "--token", "0" * 16)
    check("无锁时释放=提示非错误", r9.returncode == 0 and "不存在" in r9.stdout, r9.stdout)


# ------------------------------------------------------------- merge_outline

LIB_A = """<!-- v11 ｜ 条目 2 ｜ 更新 2026-10-25 -->
# 大纲

## 目录
- 工具库 › 逆向工程（1）
- 工具库 › PPT 生成（1）

## 工具库
### 逆向工程
- **rea** ｜ 作用：逆向分析 / 反编译 / 抓包 — 经 MCP 接给 AI Agent ｜ 形态：工具（Skill） ｜ 来源：github.com/morluto/rea ｜ 活 2026-10-24 ｜ 核 2026-10-23
  - 备注：B站@某UP「7个项目」00:09

### PPT 生成
- **ppt-master** ｜ 作用：PPT 生成 / 原生可编辑 — 真实形状图表 ｜ 形态：Skill（项目） ｜ 来源：github.com/hugohe3/ppt-master ｜ 活 2026-10-23

## 知识库
"""

LIB_B = """<!-- v3 ｜ 条目 99 ｜ 更新 2026-10-25 -->
# 大纲

## 目录
- 工具库 › 逆向工程（4）
- 工具库 › PPT 生成（1）
- 工具库 › 效率（2）
- 知识库 › 读书 › 认知（2）

## 工具库
### 逆向工程
- **rea** ｜ 作用：逆向分析 / 反编译 / 固件提取 — MCP 接入，支持 WASM ｜ 形态：工具（Skill） ｜ 来源：github.com/morluto/rea ｜ 活 2026-10-25 ｜ 核 2026-10-23 ｜ 别名：REA；rea工具
  - 用法：2026-10-24 试过 APK，抓包好用
- **rea 增强版** ｜ 作用：逆向分析 / 插件加载 — rea 的社区分支 ｜ 形态：工具 ｜ 来源：github.com/morluto/rea ｜ 活 2026-10-25
- **ppt-master** ｜ 作用：PPT 生成 / 模板市场 — 同名不同物，装模板的 ｜ 形态：网站 ｜ 来源：github.com/other/ppt-master ｜ 活 2026-10-25

### 效率
- **ToolNew** ｜ 作用：剪贴板历史 / 快速粘贴 — 开源跨平台 ｜ 形态：工具 ｜ 来源：github.com/new/tools ｜ 活 2026-10-25
  - 相关：Ghost条目
- **DupEntry** ｜ 作用：重复检测 A ｜ 形态：工具 ｜ 来源：github.com/dup/one ｜ 活 2026-10-25
- **DupEntry** ｜ 作用：重复检测 B ｜ 形态：工具 ｜ 来源：github.com/dup/two ｜ 活 2026-10-25

## 知识库
### 读书 › 认知
- **《合并新知》** ｜ 作用：认知负荷 / 学习迁移 — 长文笔记 ｜ 形态：文章 ｜ 来源：本地:book.pdf ｜ 正文：知识/认知/《合并新知》.md ｜ 活 2026-10-25
- **《缺正文的书》** ｜ 作用：记忆方法 — 正文文件缺失 ｜ 形态：文章 ｜ 来源：本地:lost.pdf ｜ 正文：知识/认知/《缺正文的书》.md ｜ 活 2026-10-25
"""


def test_merge(root: Path):
    a, b = root / "libA", root / "libB2"
    b.mkdir()
    (a / "大纲.md").write_text(LIB_A, encoding="utf-8")
    (b / "大纲.md").write_text(LIB_B, encoding="utf-8")
    body = b / "知识" / "认知"
    body.mkdir(parents=True)
    (body / "《合并新知》.md").write_text(
        "# 《合并新知》\n<!-- 正文：v0 ｜ 字 100 ｜ 提取 2026-10-25 ｜ 来源 本地:book.pdf -->\n正文\n",
        encoding="utf-8",
    )  # 《缺正文的书》故意不给正文文件

    md_a, md_b = a / "大纲.md", b / "大纲.md"
    sha_a, sha_b = sha(md_a), sha(md_b)

    out_json = root / "plan.json"
    r = run("merge_outline.py", md_a, md_b, "--json", out_json)
    out = r.stdout
    check("merge 正常退出", r.returncode == 0, r.stdout + r.stderr)
    check("只读性：两库文件逐字节不变", sha(md_a) == sha_a and sha(md_b) == sha_b)

    check("计数行齐全", "A 直接并入 4" in out and "B 同名可并 1" in out
          and "C 疑似同物 1" in out and "D 同名需仲裁 1" in out, out[:400])
    check("对方库内部重复跳过", "重复跳过 1" in out, out[:200])
    check("头部条数不符告警", "对方：文件头声明 99 条" in out, out)
    check("相关悬空告警", "悬空" in out and "Ghost条目" in out, out)
    check("A 含正文计划且无冲突", "知识/认知/《合并新知》.md（无冲突）" in out, out)
    check("A 正文缺失标 ✗", "✗ 文件缺失" in out, out)
    check("B 提案活取较新", "活 → 2026-10-25" in out, out)
    check("B 用法追加", "对方 1 行 → 追加" in out, out)
    check("B 别名并入", "别名 → " in out and "REA" in out, out)
    check("B 作用差异以本库为准", "以本库为准" in out, out)
    check("C 异名同 URL", "rea 增强版" in out and "「rea」" in out, out)
    check("D 同名异源", "同名不同物" in out or ("ppt-master" in out and "互斥" in out), out)

    data = json.loads(out_json.read_text(encoding="utf-8"))
    check("JSON 计数一致", data["counts"] == {
        "A_direct": 4, "B_same": 1, "C_same_url": 1, "D_name_conflict": 1,
        "skipped_source_dups": 1}, str(data["counts"]))
    bitem = data["plan"]["B_same"][0]
    check("JSON B 提案字段", bitem["proposal"].get("活") == "2026-10-25"
          and bitem["proposal"].get("用法追加") == ["2026-10-24 试过 APK，抓包好用"], str(bitem["proposal"]))
    a_body = [i for i in data["plan"]["A_direct"] if i["name"] == "《合并新知》"][0]
    check("JSON 正文路径计划", a_body["body_src"]["exists"] and not a_body["body_dst"]["exists"]
          and a_body["body_dst"]["rel"] == "知识/认知/《合并新知》.md", str(a_body.get("body_dst")))
    check("JSON 悬空相关", data["dangling_related"] == [{"from": "ToolNew", "related": "Ghost条目"}],
          str(data["dangling_related"]))

    # 对方库根目录形式调用（传目录而非文件）
    r2 = run("merge_outline.py", md_a, b)
    check("支持传对方库根目录", r2.returncode == 0 and "A 直接并入 4" in r2.stdout, r2.stdout[:200])


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="outline_selftest_"))
    print(f"自检目录：{root}\n")
    try:
        print("── write_lock ──")
        test_write_lock(root)
        print("\n── merge_outline ──")
        test_merge(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"\n结果：{PASS} 项通过，{FAIL} 项失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
