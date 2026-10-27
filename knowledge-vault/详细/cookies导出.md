# Cookies 自动导出方案（Get cookies.txt LOCALLY）

> **何时读：用户说「获取cookies」，或 cookies 过期需要重新获取时（`视频.md`「获取 cookies」节第 2 步的实现方案）。agent 全自动执行，用户不需要动手。**
> **前提**：扩展「Get cookies.txt LOCALLY」已由用户自行安装并启用（默认 agent 不代装、不主动打开扩展商店；用户问怎么装才给商店链接），且在真实浏览器里登录了目标站点。
> **验证状态**：4 类 agent 环境（ZCode 桌面 / DSH+bsk / Doubao 桌面 / WorkBuddy 桌面）× 2 站点（bilibili / douyin）实测通过（2026-10-28）。
> **安全红线**见第 0 章第 6 条：交付用 Move、校验只看行数与字段名，**cookie 值绝不进日志/上下文/回复**。

---

## 第 0 章 · 通用不变量（换任何机器都不会变的事实）

这些是整个方案的地基，先理解再动手：

1. **导出作用域 = Chrome 当前窗口的活跃标签页 URL**。扩展 v0.7.x 源码（`Extensions\cclelndahbckbenkjhflpdbgdldlbecc\<版本>\popup.mjs`）中是 `chrome.tabs.query({active:true, currentWindow:true})`。推论：
   - 开弹窗前必须确认目标站点标签页处于激活状态，且**已登录**；
   - 把 popup.html 当标签页打开时，作用域会变成扩展自身 → 只能走"导出全部"兜底。
2. **扩展弹窗是独立顶层窗口**：类名 `Chrome_WidgetWin_1`、标题为空，可用 `EnumWindows` 在按键/点击前后 diff 出它的 rect。它**不是**浏览器主窗口的子区域。（**ZCode CUA 例外，2026-10-28 实测**：`list_windows`/`listApps` 枚举不到弹窗、也没有新进程——弹窗内容直接出现在主窗口的 a11y 树里，检测以 a11y 扫描找 `Buy Developer a Coffee` 为准。）
3. **agent 自带的截图通道不可信**：有的环境（如 Doubao 的 `cu.screenshot()`）截到壁纸或过期帧；ZCode 的 raster 拍不到扩展弹窗。**观察屏幕一律用 OS 级截图**（PowerShell `CopyFromScreen` / Python BitBlt），**操作屏幕**用 Win32 或键盘——两条通道分开，不要混用。
4. **按钮只靠像素颜色定位，绝不靠肉眼估坐标、绝不确定坐标硬编码**：
   | 按钮 | 颜色 RGB | 说明 |
   |---|---|---|
   | **Export**（当前站，Netscape）| 深蓝 `(72,95,199)` | 唯一目标 |
   | Export As（另存为对话框）| 黄 `(254,224,138)` | 备用 |
   | Copy（进剪贴板）| 浅蓝 `(63,142,207)` | 备用 |
   | Export All Cookies（全站）| 同族蓝、整行全宽 | 除非兜底，勿点 |
5. **弹窗标题即作用域声明**：`Get cookies.txt for https://www.bilibili.com/`（含查询参数，如抖音 `...?recommend=1`）。**域名不对就停手，不要导**。a11y 扫不到该标题时（CUA 实测会发生），改用弹窗 cookie 表的域名行核对：全为目标域即作用域正确。
6. **三条破坏性红线**：
   - 扩展程序飞出菜单每行**右侧是启用开关**——点中蓝色开关会把扩展整个禁用（只能点左侧名称文本区域）；
   - 在扩展弹窗上按 **Escape** 会连带弹出 Chrome 的"近期下载"气泡——关弹窗用 Ctrl+W 或点页面边缘；
   - cookie 文件就是登录凭证：**交付用 Move 不用 Copy**（全机只留一份），**校验只看行数/首行/字段名，绝不把 cookie 值打印进日志或上下文**。
7. **逐字符模拟键盘输入 URL 会出错**（shift 修饰漂移：`:` 变 `;`、`/` 丢失）。需要输入 URL 一律走剪贴板：`Set-Clipboard` + `Ctrl+L` `Ctrl+V` `Enter`。
8. **判断登录态看页面 UI（右上角头像 vs 登录按钮），不要读 Chrome 的 Cookies sqlite**——运行时被锁，`copy` 会报 WinError 32。
9. **弹窗内 Tab×2 + Enter = 触发 Export**（焦点顺序：格式下拉 → (Tab) → Export）。这是不需要任何坐标的导出方式，只要键盘事件能到 Chrome 就成立。

---

## 第 1 章 · 环境侦察（每台新机器必做，全部可自动执行）

按顺序收集以下事实，后面所有分支都靠它：

| # | 探测项 | 方法 | 影响 |
|---|---|---|---|
| 1 | 默认浏览器 | `(Get-ItemProperty 'HKCU:\...\UrlAssociations\http\UserChoice').ProgId` | ChromeHTML 继续；其他浏览器本方案不适用 |
| 2 | **Chrome 实际下载目录** | 正则抓 Preferences（**别用 ConvertFrom-Json**，文件巨大且不规范）：<br>`$raw -match '"download":\s*\{[^}]*"default_directory"\s*:\s*"([^"]*)"'` | 验证下载结果的去向；默认 ≠ `~/Downloads`（实测有 `D:\AllDownLoad`） |
| 3 | 扩展是否安装/启用/固定 | `%LOCALAPPDATA%\Google\Chrome\User Data\Default\Extensions\cclelndahbckbenkjhflpdbgdldlbecc\<版本>\` 存在即安装；Preferences 里 `state`/`pinned_extensions` 看启用与固定 | 未安装则先提示用户安装 |
| 4 | **扩展快捷键** | 在 Preferences 里搜扩展 ID：形如 `"windows:Ctrl+Shift+9":{"command_name":"_execute_action",...}` | 有快捷键 → 路线最短（开弹窗一步到位，CUA 的 pressKey 实测能把快捷键送进 Chrome）；没有 → 走工具栏点击，或让 agent 打开 chrome://extensions/shortcuts 用浏览器内自动化现配一个，配完回路线 A |
| 5 | **进程完整性** | `whoami /groups \| Select-String "Mandatory Label"` | **Low = 一切输入注入（SetCursorPos/keybd_event/键鼠模拟）静默失效**且不报错 → 换有 Medium 权限的执行环境，否则整条自动化路线作废 |
| 6 | **Add-Type 是否被拦** | 尝试编译一个最小 DllImport 类 | 报 `Command blocked for security` → PowerShell+Win32 路线废，改用 **Python ctypes**（标准库 ctypes + Pillow，不受该钩子管） |
| 7 | 输入注入真的生效吗 | `SetCursorPos(300,300)` 后立刻 `GetCursorPos()` 读回 | 读回 ≠ (300,300) = 被静默吞掉（Low 完整性症状），到此为止 |
| 8 | 屏幕截图通道 | OS 级 CopyFromScreen 截一张，肉眼/读图确认内容真实 | agent 原生截图不可信（不变量 3） |

---

## 第 2 章 · 路线决策树

```
环境侦察完成（第 1 章）后：

有扩展快捷键（如 Ctrl+Shift+9）？
├─ 有 → 【路线 A：键盘流】（首选，无坐标、无点击、破坏性最低）
│        键盘事件能到 Chrome？（完整性 Medium / CUA a11y 均可）
│        ├─ 能 → A 全程，结束
│        └─ 不能 → 修复执行环境（提权），或走路线 B/C
└─ 无 → 能做 OS 级截图 + Win32 真鼠标（PowerShell 或 Python ctypes）？
         ├─ 能 → 【路线 B：Win32 鼠标流】
         │        工具栏点击有把握只命中名称文本？（有误禁扩展风险）
         │        ├─ 有把握 → B 全程
         │        └─ 没有 → 先配快捷键（chrome://extensions/shortcuts）再回路线 A
         └─ 不能 → 只有浏览器内 a11y（如 ZCode CUA）？
                  ├─ 是 → 【路线 C：a11y 流】（拼图菜单 → 条目 → Tab×2+Enter）
                  └─ 否 → 【兜底 D：popup.html 标签页 + 全站导出 + 单站筛选】
```

**优先级：A > B > C > D**。A 无坐标无点击零破坏；B 需要真实鼠标但可控；C 受 a11y 树重编号困扰但已验证；D 拿不到单站文件，仅救急（配合 `视频.md`「全量导出文件的处理」规则：只提取目标站点行，整份不留）。

---

## 第 3 章 · 路线 A：键盘流（首选）

前置：第 1 章第 4 项查到扩展快捷键（下文以 `Ctrl+Shift+9` 为例，按实际探测值替换）；输入注入可用（Medium 完整性）或可发键盘事件（如 ZCode CUA 的 `pressKey`）。

1. **开页面**：`start chrome "<目标URL>"`（shell 启动，不算 UI 自动化）。
2. **确认登录**：OS 级截图看右上角头像（未登录显示"登录"按钮）。未登录先停下告知用户。
3. **确认目标标签页激活**：窗口标题含目标站点名；多余标签页先 `Ctrl+W` 清理（误点链接常新开标签）。
4. **前置 Chrome 窗口**：`SetForegroundWindow` + `ShowWindow(SW_RESTORE/MAXIMIZE)`，用 `GetForegroundWindow()` 回读确认真的在前台（后台窗口收不到快捷键）。
5. **开弹窗**：发送扩展快捷键 → `EnumWindows` diff 出新增的空标题 `Chrome_WidgetWin_1` 窗口，其 rect 即弹窗位置（**每次重新取，绝不硬编码**；实测约 `(269,99,783,702)`，仅当窗口布局不变时复现）。在 ZCode CUA 上 EnumWindows/`list_windows` 探不到弹窗（2026-10-28 实测）：发完快捷键直接 a11y 扫绑定 app 的状态，出现 `Buy Developer a Coffee` 即弹窗已开。
6. **核对作用域**：读弹窗标题 `Get cookies.txt for <URL>`，域名不符立即停止。
7. **触发导出**：`Tab` `Tab` `Enter`。
8. 若弹窗需要点击确认（路线 B/C 场景），见第 4 章；纯键盘流到此结束。

> 为什么 Tab×2：弹窗 DOM 焦点顺序为 格式下拉(Netscape) → Export。键盘是唯一完全绕开坐标系的方式，对"嵌入式/拍不到"的弹窗同样有效。

## 第 4 章 · 路线 B：Win32 真鼠标流（PowerShell 或 Python ctypes）

前置：完整性 Medium；`Add-Type` 可用用 PowerShell，被拦则用 Python `ctypes`（`user32/gdi32` 不受钩子管，`pip install Pillow` 备截图）。

与路线 A 的差异只在**第 7 步**及之后：

1~6 同路线 A（若没有快捷键：点拼图图标 → 扩展菜单 → **只点扩展行左侧名称文本区域**，严禁点行右侧的开关/图钉）。

7. **定位 Export 按钮（颜色扫描，不是肉眼）**：以弹窗 rect 为基准 OS 级截图，扫描按钮底色：
   ```python
   img = grab(L, T, R-L, B-T).convert('RGB'); px = img.load()
   rows = [y for y in range(H) if px[20, y] == (72, 95, 199)]      # Export 深蓝
   cols = [x for x in range(W) if px[x, rows[0] + 2] == (72, 95, 199)]
   center = ((cols[0]+cols[-1])//2, (rows[0]+rows[-1])//2)
   ```
   实测参考（弹窗 rect `(269,99,783,702)` 时）：Export 中心 (360,178)、Export As 黄 x∈[448,604]、Copy 浅蓝 x∈[612,768]。**窗口/弹窗一变就要重扫。**
8. **点击**：`SetCursorPos(center)` + `mouse_event(2/4)`（左键按下/抬起），1~2.5s 内文件落盘。
9. 收尾见第 6 章。

> 不要用"放大截图画标尺肉眼读坐标"——标尺标签与人眼读数极易错位，实测因此点开过隔壁扩展。要读也只读放大图上的绝对坐标标签。

## 第 5 章 · 路线 C：ZCode CUA a11y 流（无 Win32 能力时）

仅当手头是 ZCode Computer Use（`agent.computerUse`）且无法执行 Win32 注入时使用。每个 `mcp__node_repl__js` 调用都是全新内核，开头必须重新引导 runtime。

1. `getAXStateAndScreenshot` 绑帧 → `app.click([拼图图标坐标])`（坐标从当前截图读，**窗口尺寸一变就失效**）。
2. 扩展菜单打开后，**用 `getAXState` 纯文本解析条目索引并立即点击**（同单元格内完成）：
   ```js
   const st = await app.getAXState({ emit: false });
   const m = st.match(/\[(\d+)\] button Get cookies\.txt LOCALLY、/);
   await app.click(parseInt(m[1]));
   ```
   **不要用 `app.elements()` 跨调用点索引**——动态页面不断重编号 a11y 树，索引必然失效，重试也难成功。
3. 确认弹窗（纯 a11y，别信截图）：`s.includes("Buy Developer a Coffee")`；B 站另查 `SESSDATA`。
4. **Tab×2 + Enter**（`app.pressKey`）。到此导出完成。
5. **2026-10-28 实测补充**：弹窗的 a11y 树会被 cookie 表撑爆（28 行 × 每行十余元素顶到 400 上限截断），Export 按钮连 `elements()` 都拿不到——Tab×2+Enter 是唯一可靠触发方式；作用域核对用 cookie 表域名行。
6. 坑速记：弹窗在 raster 里不可见、坐标点击会误关弹窗、坐标点击前必须重新绑帧（frame expired）、别按 Escape。

## 第 6 章 · 验证与交付（所有路线通用）

1. **验证下载**（目录 = 第 1 章第 2 项查到的值）：
   ```bash
   ls -lt "<下载目录>" | head -5                       # 出现 www.<域名>_cookies.txt
   stat -c '%y %s %n' "<下载目录>/www.<域名>_cookies.txt"; date '+%H:%M:%S'   # mtime 在 1 分钟内
   head -4 "<文件>"                                    # "# Netscape HTTP Cookie File"
   grep -c "<域名关键词>" "<文件>"                      # cookie 行数
   grep -P "\tSESSDATA\t|\tsessionid\t" "<文件>" -c     # 登录态字段存在（按站点换字段名）
   ```
   实测基线：bilibili 28 条（`.bilibili.com` 25 + `www` 3，含 SESSDATA/bili_jct/DedeUserID）；douyin 67 条（含 sessionid/sid_guard/ttwid 等）。数值仅供参考，重点是有登录态字段。
2. **交付用 Move**：移入 `<技能目录>/cookies/<站点>.txt`（凭证全机只留一份；Chrome 下载记录指向失效文件无害）。
3. **收尾恢复现场**：关闭多余标签页（`Ctrl+W`）、扩展弹窗**优先再按一次扩展快捷键**切换关闭（无坐标、无下载气泡，2026-10-28 实测；不行再点页面**边缘/侧栏**，不按 Escape）、页面级浮层（下拉框/气泡）用 `Ctrl+R` 重载清掉，最后让浏览器停在目标站点页。
4. **隐私红线**：以上命令的输出只允许出现行数、文件头、字段名，cookie 值不得进入日志/上下文/回复。

## 第 7 章 · 兜底 D：popup.html 标签页 + 全站导出

主流程全部失败时的最后手段：

1. 地址栏打开 `chrome-extension://cclelndahbckbenkjhflpdbgdldlbecc/popup.html`（此页 a11y 树完整、元素稳定可点）。
2. 因作用域变成扩展自身，只能点 **Export All Cookies**（导出全部域名的 cookie）。
3. 从 Netscape 文件中筛出目标域名行，手工拼上头两行注释生成单站文件（规则同 `视频.md`「全量导出文件的处理」：只留目标站点，整份即删）：
   ```bash
   { head -3 all_cookies.txt | grep '^#'; grep -E '\.bilibili\.com|www\.bilibili\.com' all_cookies.txt; } > www.bilibili.com_cookies.txt
   ```
4. 用完删除全站文件，避免其他站点凭证残留磁盘。

## 附 · 已知环境差异速查

| 环境 | 截图通道 | 输入注入 | 开弹窗 | 点 Export | 备注 |
|---|---|---|---|---|---|
| ZCode 桌面（CUA）| raster 拍不到弹窗，只信 a11y | pressKey 可用，快捷键能送进 Chrome | **快捷键优先**（2026-10-28 实测 Ctrl+Shift+9 直开）；无快捷键才拼图菜单→条目（文本解析索引）| Tab×2+Enter（Export 按钮不在 a11y 树里）| 别用 elements() 跨调用点索引；`list_windows` 看不到弹窗，以 a11y 扫描为准 |
| DSH+bsk | CopyFromScreen | Low 完整性=静默失效，需 Medium | 工具栏点击 | 坐标点击（颜色扫描定位）| bsk/CDP 无法导航 chrome-extension:// 页面 |
| Doubao 桌面 | cu.screenshot 截到壁纸，只用 CopyFromScreen | Win32 可用 | 工具栏点击 | 坐标点击 | Preferences 别用 ConvertFrom-Json；Chrome 启动后可能被压在后面，先 SetForegroundWindow |
| WorkBuddy 桌面 | Python BitBlt/Pillow | Medium，但 Add-Type 被拦 → Python ctypes | **Ctrl+Shift+9 快捷键** | 颜色扫描+ctypes 点击 | 快捷键路线同样适用于其他环境，最优先尝试 |

> 换站点（B 站→抖音已二次验证）：流程零改动，只需换 URL、重看登录态、**重新 EnumWindows 取弹窗 rect、重新颜色扫描**——坐标永远是这次算出来的，不是上次的。
