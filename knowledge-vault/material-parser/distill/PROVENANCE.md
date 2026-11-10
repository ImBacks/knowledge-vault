# PROVENANCE

- 上游：https://github.com/kangarooking/cangjie-skill
- 版本：v2.5.0（tag，commit 4469212），**补包版 2026-09-13**（含「工具书友好的三重验证」等一批修订，版本号未变）；vendor 升级日期 2026-11-10（上游 HEAD `a28de55`，仅 README 宣传视频变更，功能文件与补包一致）
- 许可：MIT（见同目录 LICENSE）
- 取舍：只保留功能件 scripts / extractors / templates / methodology / schemas / registry 与 LICENSE、README.md；剔除 website / assets / benchmarks / books（成品示例）/ dist / docs / tests / .github。功能件**整目录替换、逐字节一致**（2026-11-10 用 FileHash 全量校验通过），不含任何本地裁剪。
- 改动：仅两处——① 上游 SKILL.md 去 frontmatter 改名 `蒸馏规范.md`（防被平台识别为第二个技能）；② 该文件里「视频/播客建议先用 video-downloader 拿转写文本」一处改为指向本机 `material-parser` 的 parse_video 三级链路，并补一段「视频/课程材料的额外输入」（可用 `ocr/ocr.txt` 画面文字与 keyframes 对照图，OCR 与转写冲突时两者都留并标置信度）。其余文件未动。
- 运行入口 `scripts/cangjie.py`（doctor / compile single|pack / validate / eval），需 PyYAML（requirements「D·蒸馏」组），tiktoken / jsonschema 可选。
- 未随包：上游 `tests/`（3 个 pytest 文件：test_compile_resources / test_output_evals / test_quality_contracts）被剔除；替代验收是 `scripts/cangjie.py doctor` 的 schema 校验 + `run_output_evals.py`。如需本地回归，可从上游单独取 tests/ 放临时目录跑（需 pytest）。
- 生命周期：蒸馏中间产物（.cangjie/）放解析缓存，入库落盘后即删（§7 第 9 条）；蒸馏产物本身是交付物——方法论拷进知识正文、skill 包拷进技能目录，拷出后再清缓存。
