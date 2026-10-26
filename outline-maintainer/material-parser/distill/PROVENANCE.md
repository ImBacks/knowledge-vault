# PROVENANCE

- 上游：https://github.com/kangarooking/cangjie-skill
- 版本：v2.5.0（tag，commit 4469212），vendor 日期 2026-10-26
- 许可：MIT（见同目录 LICENSE）
- 取舍：只保留功能件 scripts / extractors / templates / methodology / schemas / registry 与 LICENSE、README.md；剔除 website / assets / benchmarks / books（成品示例）/ dist / docs / .github。升级时对照上游同名目录整目录替换。
- 改动：上游 SKILL.md 去 frontmatter 改名 `蒸馏规范.md`（防被平台识别为第二个技能）；其余文件未动。运行入口 `scripts/cangjie.py`（doctor / compile 等），需 PyYAML（requirements「D·蒸馏」组）。
- 生命周期：蒸馏中间产物（.cangjie/）放解析缓存，入库落盘后即删（§7 第 9 条）；蒸馏产物本身是交付物——方法论拷进知识正文、skill 包拷进技能目录，拷出后再清缓存。
