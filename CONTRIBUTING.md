# 贡献指南

欢迎 issue 和 PR。这个仓库是一个 AI agent 技能（SKILL），贡献前请先读 `outline-maintainer/SKILL.md` 了解整体设计。

## 提交前自检

1. **跑自检脚本**（均不碰真实库、不联网）：

   ```bash
   python outline-maintainer/scripts/selftest.py
   python outline-maintainer/material-parser/scripts/selftest.py   # 在库根目录下运行，或设 MATERIAL_PARSER_CACHE
   ```

2. **不要提交私人数据**：`大纲.md`、`日志.md`、`知识/`、`素材/`、`索引/`、`解析缓存/` 属于使用者自建的库数据，已在 `.gitignore` 排除，请勿强行加入。

3. **不要提交个人本机配置**：脚本与文档中的路径请保持通用写法（如 `C:\Users\<用户名>\...`），不要写入真实用户名或机器专属约定。

## 约定

- 文档与注释使用中文，术语与 `SKILL.md` 及 `详细/` 各模块保持一致。
- 改动技能行为时，请同步更新 `outline-maintainer/变更记录.md`。
- 依赖保持可选：库级脚本只用 Python 标准库；新增解析能力请走 `common.py --doctor` 的依赖探测，不要引入硬性 pip 依赖。

## 提交信息

用简短中文一句话说清改动，参考现有提交风格。
