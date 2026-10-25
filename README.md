# outline-maintainer · 大纲维护员

给 AI 助手（Claude Code / ZCode 等支持 SKILL 的 agent）用的个人知识库技能：把「现在觉得有用、还不知道何时会用到」的工具和知识收进一个私人候选池，**做项目之前回来检索有没有能用上的**。

- **索引单文件**（`大纲.md`：目录 + 条目行），长正文外置 `知识/` 一条一文件，检索分级下钻不全量读——索引体积只随条数增长，可支撑百万条（SQLite + FTS5 派生索引）。
- **引用可核验**：正文引用一律指向原件 + 页码/时间戳（`【p12】`/`[00:12:34]`），OCR/转写内容标「据识别」，推断标「（推断）」。
- **内置 material-parser 子模块**加速大文件：PDF/EPUB 解析（文本层 + OCR 兜底）、视频/B站/抖音解析（CC→AI 字幕→本地转写三级链路 + 场景检测/讲述锚点双信号关键帧），内容寻址缓存 + 断点续跑。
- **skill 是死的、AI 是活的**：技能提供杠杆（任务分发表、按视频类型可选的解析开关），类型判断交给调用方 agent。

## 安装

1. 把 `outline-maintainer/` 目录整个复制进你的技能目录（或任意位置）。
2. **库根定位**：技能默认从自身位置向上找 `大纲.md` 定位库根；装在库根之外时设环境变量 `MATERIAL_PARSER_CACHE=<库根>/解析缓存`（详见 `outline-maintainer/SKILL.md` 配置节）。
3. **依赖全部可选**：`python material-parser/scripts/common.py --doctor` 按用途报缺哪些并给精确安装命令（只想解析 PDF 就只装 PDF 那组）。不装也能用——agent 直接读原件，慢一些。
4. **自检**：`python outline-maintainer/scripts/selftest.py`（合成数据全链路，不碰真实库、不联网）；`python outline-maintainer/material-parser/scripts/selftest.py` 验解析子模块。

## 目录

```
outline-maintainer/
├── SKILL.md        技能本体（任务分发 + 主流程 + 写入协议）
├── 详细/            分型细则模块（条目格式/视频/图片/文档/体检规模/导入合并），按需读
├── material-parser/ 大文件解析子模块（手册见其 解析手册.md）
├── scripts/         check_outline 体检 · build_index 派生索引 · write_lock 写锁 · merge_outline 导入
└── 变更记录.md       开发史
```

库的数据文件（`大纲.md`、`日志.md`、`知识/` 等）由使用者自建于自己的库根，**不在本仓库内**。
