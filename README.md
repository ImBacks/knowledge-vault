# outline-maintainer · 大纲维护员

给 AI 助手（Claude Code / ZCode 等支持 SKILL 的 agent）用的个人知识库技能：把「现在觉得有用、还不知道何时会用到」的工具和知识收进一个私人候选池，**做项目之前回来检索有没有能用上的**。

- **索引单文件**（`大纲.md`：目录 + 条目行），长正文外置 `知识/` 一条一文件，检索分级下钻不全量读——索引体积只随条数增长，可支撑百万条（SQLite + FTS5 派生索引）。
- **引用可核验**：正文引用一律指向原件 + 页码/时间戳（`【p12】`/`[00:12:34]`），OCR/转写内容标「据识别」，推断标「（推断）」。
- **内置 material-parser 子模块**加速大文件：PDF/EPUB 解析（文本层 + OCR 兜底）、视频/B站/抖音解析（CC→AI 字幕→本地转写三级链路 + 场景检测/讲述锚点双信号关键帧），内容寻址缓存 + 断点续跑。
- **skill 是死的、AI 是活的**：技能提供杠杆（任务分发表、按视频类型可选的解析开关），类型判断交给调用方 agent。

## 安装

1. 把 `outline-maintainer/` 目录整个复制进你的技能目录（或任意位置）。
2. **库根定位**：技能默认从自身位置向上找 `大纲.md` 定位库根；装在库根之外时设环境变量 `MATERIAL_PARSER_CACHE=<库根>/解析缓存`（详见 `outline-maintainer/SKILL.md` 配置节）。
3. **依赖全部可选**：用真实 Python 解释器跑 `material-parser/scripts/common.py --doctor`，按用途报缺哪些并给精确安装命令（完整清单见下方「工具清单」）。注意 Windows 商店版 `python` 是占位 stub 跑不起来——用实际安装路径，本机约定见 `outline-maintainer/SKILL.md` 配置节。不装也能用——agent 直接读原件，慢一些。
4. **自检**：`python outline-maintainer/scripts/selftest.py`（四个库级脚本全合成数据自检，不碰真实库、不联网）；`python outline-maintainer/material-parser/scripts/selftest.py` 验解析子模块——后者需先能定位缓存根（在库根目录下运行，或设 `MATERIAL_PARSER_CACHE`），否则会按设计报错退出。

## 工具清单：默认与可选

**默认（开箱即用，零 pip 依赖）**

- 技能本体 + 四个库级脚本（体检 / 派生索引 / 写锁 / 导入分诊），只用 Python 标准库；
- 大文件由 agent 直读原件——慢速但全流程可用；
- `common.py --doctor` 环境自检（只读）：报缓存根、逐项依赖、ffmpeg、GPU 三态。

**可选 · 按用途装（装哪组看你收什么，`--doctor` 给精确命令）**

| 组 | 装什么 | 解锁能力 | 代价 |
|---|---|---|---|
| A · PDF / EPUB | pymupdf、rapidocr、Pillow、numpy | 文本层秒级直提、扫描页 OCR、章节分块、断点续跑 | 纯 pip，分钟级 |
| B · 转写 | faster-whisper、av（另装 ffmpeg） | 音视频转文字（VAD 过滤、分块、断点续跑） | 模型首跑下载 ≈500MB（走 hf-mirror） |
| C · 在线视频 | yt-dlp | CC/AI 字幕秒级拉取，多数情况不用下载视频本体 | 纯 pip |
| 关键帧 | （随 A/B 组） | 双信号抽帧（场景检测 + 讲述锚点）+ 缩略图对照表 | ffmpeg 一装（`winget install Gyan.FFmpeg`） |

**可选 · GPU 转写加速（默认 CPU，功能完全一致，只影响速度）**

- **收益**（实测参考：RTX 5060 + small 模型，140 秒中文音频）：CPU 21 秒 / GPU 7 秒（约 3 倍；CUDA kernel 首跑有预热）。不同硬件与模型收益不同，不承诺固定倍数，跑一次即知；模型越大、视频越长，差距越大。
- **开启**：`python -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`（约 1.5GB，要求 N 卡驱动支持 CUDA 12、ctranslate2 ≥4.5），解析时加 `--device cuda`；`--doctor` 显示 ✅ 即就绪。
- **约束**：不装不影响任何功能；显式点名 cuda 而环境不可用 → 报错退出，不静默回落；换设备沿用已转写缓存（要重转加 `--force`）；小显存卡（≤8GB）跑大模型（medium+）先小规模验证。

## 目录

```
outline-maintainer/
├── SKILL.md        技能本体（任务分发 + 常驻核心：开局 / 模式 / 检索 / 降级安全 / 日志检索侧）
├── 详细/            流程与分型细则模块，按需读、检索会话零模块：
│                     收录主流程 · 写入协议 · 维护 · 素材处理 · 条目格式与正文 · 视频 · 图片 · 文档 · 体检与规模 · 导入合并
├── material-parser/ 大文件解析子模块（手册见其 解析手册.md）
├── scripts/         check_outline 体检 · build_index 派生索引 · write_lock 写锁 · merge_outline 导入
└── 变更记录.md       开发史
```

库的数据文件（`大纲.md`、`日志.md`、`知识/` 等）由使用者自建于自己的库根，**不在本仓库内**。

## 许可证

[MIT](LICENSE) · 欢迎通过 issue / PR 参与（见 [CONTRIBUTING.md](CONTRIBUTING.md)）。
