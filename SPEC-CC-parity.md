# SPEC-CC-parity：Claude Code 交互契约对齐清单

> 状态：验收标尺（不是第六份实施 SPEC）
> 日期：2026-09-03
> 作者：claude（Claude Code 本体，契约描述来自第一人称使用；zylab 现状来自当日只读实测）
> 用法：每条契约对应一个 PTY 断言；`parity = 通过数 / 总数`。改 TUI 必须重跑（同 AGENTS.md 第 6 条的逻辑）。
> 与 `PRODUCT-UX-ROADMAP.md` 的关系：路线图决定**做什么**，本文决定**做到没有**。

## 0. 先说三句话

1. **Claude Code 的 UX 不是视觉，是一小组交互契约加克制。** 复刻契约，不复刻像素。
2. **"完美复刻"不可测，"这些契约在 PTY 里全过"可测。** zylab 已有 `tests/pty_harness.py`
   与 `tests/terminal_screen.py`，成本在写断言，不在搭设施。
3. **模型是天花板。** DeepInfer/Boyue 的模型在首字延迟、思考流、工具调用可靠性上和
   Claude 不同。契约里凡是碰到模型能力的，目标是**诚实降级**，不是假装一样。

## 1. 契约清单

状态列：✅ 已有 · ◐ 部分 · ❌ 缺 · ？未查。数字是 09-03 对工作树的 grep/PTY 实测。
带提交号的 ✅ 是 09-04 在 `cc-parity` 分支落地并钉进记分板的；其余状态仍是 09-03 快照。
记分板才是现状：`python3 scripts/cc_parity.py`。
**2026-09-04：36/36 全部钉住。**横幅是一个小方框（名字/build、模型@网关、目录、一行帮助），按键仍只在 `?` / `/keys`。
此后 §J（后台工作可见性）新增 7 条、§K（离开期间的 recap）新增 6 条，清单共 **49** 条（`tests/parity_manifest.py` 的 `CONTRACTS` 才是总数的唯一出处）；
2026-09-20 实测 **parity = 49/49**。这里的数字只是快照，与记分板不一致时**以记分板为准**。

### A. 输入与按键

| # | 契约 | Claude Code 的行为 | zylab 现状 | PTY 断言 | 优先级 |
|---|---|---|---|---|---|
| A1 | Enter 提交，唯一提交键 | 是 | ✅ | 已有（test_tui_pty） | — |
| A2 | 换行不提交 | Shift+Enter；CC 会向终端请求 modifyOtherKeys/kitty 协议，所以多数终端开箱可用 | ✅ 解码三种编码（CSI-u / modifyOtherKeys / ESC+CR）并有 PTY 真序列测试；仍不向终端发协议请求，Alt+Enter 是保底（`79a2f36` 钉住） | 发 `ESC[27;2;13~` 与 `ESC\r` 各一次，草稿含 `\n` 且无 submit | P2（放能力探测后面，会动所有修饰键） |
| A3 | 多行粘贴是一条 prompt | bracketed paste；超过 N 行折叠成 `[Pasted text +N lines]` | ✅ 粘贴已修（`4243ced`）；≥4 行或 ≥600 字折叠成 `[Pasted text #n +N lines]`，提交时原样展开（`fe903d8`） | 粘 3 行→1 条 user message（已有）；粘 40 行→输入框显示折叠标记 | P3 |
| A4 | Esc 中断当前 turn | 是，一次 | ✅ | 已有 | — |
| A5 | ↑↓ 翻历史 | 是 | ✅ | 已有 | — |
| A6 | **Ctrl+R 历史搜索** | 增量搜索历史 prompt | ✅ Ctrl+R 反向增量搜索：Enter 采用 / Esc 取消 / 再按更旧（`10a1e11`） | Ctrl+R 后键入片段，回车填入匹配项 | P2 |
| A7 | `@` 文件补全 | 模糊匹配路径 | ✅ `_at_matches` | 已有 | — |
| A8 | `!` 前缀直跑 shell | `! ls` 不经模型 | ✅ controller 层分发为 RUN_SHELL，不经模型（`d0a386a` 钉住） | `!echo hi\r` 输出 hi 且无 provider 请求 | P3 |
| A9 | Ctrl+C 清空输入，连按两次退出 | 是 | ✅ 有草稿→清空；空草稿 1.5s 内连按两次→退出，第一次给一行提示；Esc 永不退出（`79a2f36`） | 单按草稿清空；双按 exit | P3 |

### B. 权限与模式

| # | 契约 | Claude Code | zylab | 断言 | 优先级 |
|---|---|---|---|---|---|
| B1 | **Shift+Tab 循环权限模式，模式显示在输入区** | default → accept-edits → plan（→ bypass 若允许）；提示符前缀 `⏵⏵ accept edits on` / `⏸ plan mode on` | ✅ default → accept-edits → plan 循环，提示符前缀 `⏵⏵›`（`c4d4c4d`） | 按 Shift+Tab 三次，snapshot 的 prompt 前缀依次变化；模式影响下一次工具审批 | **P1** |
| B2 | 每次审批显示**最终**参数 | bash 显示完整命令；edit 显示 diff | ✅（hook 改写后的最终参数） | 已有 | — |
| B3 | 硬边界不在模式循环内 | — | ✅ 受保护路径守卫 | **不复制** CC 的 `--dangerously-skip-permissions` 语义到硬守卫 | 守住 |
| B4 | plan 模式下不落盘 | 只读探索 + 出计划 | ✅ 61 处 | 已有 | — |

### C. 工具活动渲染

| # | 契约 | Claude Code | zylab | 断言 | 优先级 |
|---|---|---|---|---|---|
| C1 | 每个工具调用一行，结果默认折叠 | `⏺ Read(a.py)` / `⎿ 120 lines (ctrl+o to expand)` | ✅ 折叠 + `/expand [id]`（102 处） | 已有 | — |
| C2 | **Ctrl+O 切换展开/折叠** | 全局 verbose 开关，即时重绘 | ✅ Ctrl+O 展开最近一段折叠输出（`9babf79`） | Ctrl+O 后同一 snapshot 中工具输出行数变化 | **P1**（CC 用户 Enter/Esc 之后最常按的键） |
| C3 | 运行中 spinner：动词 + 耗时 + token | `✻ Thinking… (5s · ↑1.2k)` | ✅ "思考中/输出中 0s · ctx" | 已有 | — |
| C4 | 长输出永不刷屏 | 截断 + 折叠 | ✅ `TerminalRenderer._fold_transcript_result`（多行→`N lines · /expand id`，单行 96 列截断，失败结果不折叠）+ `truncate_display`（`9610737` 钉住；09-03 写的 `_clip` 不存在） | 已有 | — |
| C5 | todo 清单原地更新 | ☐/☒ 列表随 turn 变化 | ✅ 24 处 | 已有 | — |
| C6 | 结构化前缀对齐 | 图标与文字一格间隔 | ✅ 首行由 ⏺ 托管时去掉结构化行的两格缩进：`⏺ ◆ 标题`（`2c5c249`）；后续结构化行仍缩两格对齐正文 | 那条测试 | P1（几分钟） |

### D. 思考与延迟

| # | 契约 | Claude Code | zylab | 断言 | 优先级 |
|---|---|---|---|---|---|
| D1 | **思考过程折叠显示** | 暗色 `✻ Thinking…` 块，可展开，从不混进正文 | ✅ `<think>` 正文与 `reasoning` 事件两种来源都折叠成 `✻ 思考 (N 字 · /expand think 展开)`（`09a1b82`） | 注入 `{"t":"reasoning"}` 事件→出现折叠块且正文无内容；注入含 `<think>…</think>` 的 text→同上 | **P1（本清单里对这些模型贡献最大的一条）** |
| D2 | 首字延迟被解释 | 空等极少；有阶段提示 | ✅ 首字之前按阶段显示（连接 / 等首字…），超 8s 加提示（`6d7455b`） | 首字前 snapshot 依次含 "连接" → "等待首字"（对应 SPEC-P0-latency-feedback） | P1 |
| D3 | 失败三分 | 没 key / 路由错 / 网关挂，文案不同 | ✅ 403 额度不足单列为 quota；认证/限流/瞬时各一句可行动提示（`e9c19c5`） | 三种 mock 响应→三种不同提示 | P2 |

### E. 会话与恢复

| # | 契约 | Claude Code | zylab | 断言 | 优先级 |
|---|---|---|---|---|---|
| E1 | `--resume` 选择器 / `--continue` | 是 | ✅ session picker | 已有 | — |
| E2 | 恢复后**重投影**时间线，不回放旧屏幕 | 是 | ✅ resume 走 set_transcript → hydrate，从规范消息重投影；PTY 断言每条一次、顺序不变、无模型调用（`34bffa5` 钉住） | resume 后 transcript 与退出前语义一致、无重复 | P1（随 codex 落地） |
| E3 | 自动 compact 且有提示 | 阈值触发 + "Context compacted" | ✅ 29 处 | 已有 | — |
| E4 | 状态栏：模型 / 上下文占比 / 成本 | 是 | ✅ | 已有 | — |

### F. 后台与队列

| # | 契约 | Claude Code | zylab | 断言 | 优先级 |
|---|---|---|---|---|---|
| F1 | Ctrl+B 把 bash 转后台 | 同一个键 | ✅ | 已有 | — |
| F2 | 后台任务一处可见 | `/tasks` 类视图 | ✅ `/tasks` | 已有 | — |
| F3 | 工作中可排队下一条 | 是 | ✅ + **可编辑/提前/取消（CC 没有，保留）** | 已有 | 守住 |
| F4 | 工作中的斜杠命令 | 少数可用 | ✅ 18 个 live + 忙时菜单（`c4b5a0b`） | 已有 | 守住 |

### G. 命令与发现

| # | 契约 | Claude Code | zylab | 断言 | 优先级 |
|---|---|---|---|---|---|
| G1 | `/` 面板模糊过滤 | 是 | ✅（还有二三级菜单） | 已有 | — |
| G2 | `?` 快捷键总览 | 一屏 | ✅ `?` 与 `/keys`，空闲与忙时都可查，一屏 14 行（`07f8689`） | 输入 `?` 出现总览 | P3 |
| G3 | 稳定按键词汇 ≤ 10 | Enter Esc Tab Shift+Tab Ctrl+C Ctrl+B Ctrl+O Ctrl+R + `/ @ ?` | ✅ 卡片上修饰键组合 9 个（≤10），测试守住上界（`07f8689`）；模态各自的键仍是设计纪律，见 §I | 见 §I | P1（设计纪律） |

### H. 通知与终端

| # | 契约 | Claude Code | zylab | 断言 | 优先级 |
|---|---|---|---|---|---|
| H1 | **完成响铃 / 系统通知** | 空闲时 turn 完成响铃（可关） | ✅ 空闲时 turn 完成响铃，`notify.bell` 可关（`4ba65b5`） | turn 完成后 PTY 输出含 `\x07` 或 OSC 9 | P1（极低成本，手感极高） |
| H2 | **终端标题** | `✳ Claude Code — <任务摘要>` | ✅ OSC 0 标题随状态，退出时清（`4ba65b5`） | 输出含 `ESC]0;…` | P1（同上） |
| H3 | 退出时终端干净 | 复原所有模式 | ✅ raw_mode 引用计数、`?2004l` | 已有 | — |

### I. 克制规则（不是功能，是禁令）

| # | 规则 | 为什么 |
|---|---|---|
| I1 | 所有面板共用一套导航语法：↑↓ 选、Enter 确认、Esc 退 | CC 从不给某个面板发明专属键。queue 面板的 `e/s/d` 应改为选中后 Enter 弹动作菜单 |
| I2 | 新能力先进 `/`，不进快捷键 | 词汇表只减不增 |
| I3 | 一次不在屏幕上同时出现两个模态 | decision gate 与 picker 互斥 |
| I4 | 不抓鼠标、不画伪选区 | Persistent Workspace 已做此决定；Gate 0 残留应删净 |

### J. 后台工作的可见性（PLAN-agent-visibility-20260907）

三类后台工作（subagent / workflow 节点 / Ctrl+B task）先归一成 `AgentEvent`，界面行、名册、
状态栏计数、交回模型的通知体都从它生成（`core/agent_events.py`）。符号语法：`›` 用户、`⏺`/`●`
模型发言或已完成事件、`✻` 会被替换的过渡行、`⎿` 结果或续行、`○` 子代理运行中、`✗` 失败。

| # | 契约 | Claude Code 的行为 | 状态 |
|---|---|---|---|
| J1 | 子代理完成落一条永久行，带 label、动词、耗时、token | `● Agent "…" finished · 11m 54s` | ✅ |
| J2 | 子代理正文不进父时间线，只有主模型转述 | 只有 harness 行 + 模型发言 | ✅ |
| J3 | 等待行原地替换，带剩余个数 | `✻ Waiting for N…` | ✅ |
| J4 | 名册有活代理才出现、随之消失；行含状态点、席位、label、activity、耗时、token | 常驻名册 | ✅ |
| J5 | 状态栏 `N agents` | `← 1 agent` | ✅ |
| J6 | 通知体标明非用户输入并带用量，与界面行同源 | task-notification | ✅ |
| J7 | subagent / workflow 节点 / Ctrl+B task 同一套行 | 语法一致 | ✅ |
| J8 | attach 即整屏切到子代理的记录：开头是主代理交给它的原话，之后是它每一步；打字发给它；Esc / 行首 ← 回 main，主会话原样还在、其间的输出补上 | 选中子代理后整屏换成它的 transcript（CC 2.1.270） | ✅ |
| J9 | 子代理视图的标题行变色、写明在看谁与怎么回去 | 分隔线变色 + 任务标题徽标 | ✅ |
| J10 | 状态栏在模型旁显示所选推理强度 | `Fable 5.1 · max` | ✅ |

### K. 离开期间的 recap（2026-09-20，依据 CC 2.1.272 的 awaySummary 实现）

模型现写的**一行**「总目标 + 当前任务 + 下一步」。何时写、何时不打扰在 `core/away_recap.py`
（常量逐个对着 CC 的：延迟下限 30s、失焦防抖 2s、真实提问 ≥3、距上次 recap 新增 ≥2、本轮失败
≤3、前 3 次附关闭提示、截 400 字符；提示词原文照搬）。

| # | 契约 | Claude Code 的行为 | 状态 |
|---|---|---|---|
| K1 | 终端失焦够久 → 后台写好一行等人回来；存进会话记录但**不进 messages** | blur 定时器 → 生成 → `system/away_summary` | ✅ |
| K2 | 定时器到点前回来 → 不发任何请求 | refocus 取消定时器 | ✅ |
| K3 | 人回来时在途请求立刻放手，不等网关回话 | refocus abort 在途生成 | ✅ |
| K4 | 输入框有草稿 → 不打扰（另有：后台工作在跑、已在生成、对话不够新） | `skipped: draft input present` 等 | ✅ |
| K5 | `/recap` 立刻现写一行，可取消 | "Generate a one-line session recap now" | ✅ |
| K6 | 焦点上报（DEC 1004）随输入泵开关，退出不弄脏终端；焦点事件不被当成输入 | 订阅终端 focus | ✅ |

两处刻意偏离：CC 只在 prompt cache 还热时生成（缓存年龄未知即跳过）——这里的网关不报告缓存
TTL，照搬等于永不触发，故去掉这道闸；resume 之后也给一条（上次之后没有新提问则直接用落盘的、
零调用）。写 recap 的就是这个窗口此刻在用的模型（与 CC 一致，用户 2026-09-20 定）：没有单独的
模型开关，失败时也不换别家来写。

## 2. 明确不复制

- `--dangerously-skip-permissions` 的语义**不得**触及受保护路径硬守卫（B3）。
- CC 的单网关简单性 —— 多网关 + 网关 key 状态（现在在 `/model` 里，`/gateway` 已并入）是 zylab 的资产。
- CC 没有 queue 编辑、没有带后果与代价的 decision gate —— 这两样比 CC 强，保留。

## 3. 模型天花板与诚实降级

| 能力 | Claude | DeepInfer/Boyue 实测 | 契约的处理 |
|---|---|---|---|
| 首字延迟 | ~1s | glm-5.3 4.6–5.9s；kimi-k3-256k 09-03 为 503 | D2：分阶段显示，不空等 |
| 思考流 | 结构化、可折叠 | k3 走 `reasoning_content`；minimax/glm 把 `<think>` 塞进正文 | D1：两种来源都折叠 |
| 工具调用 | 稳定 | 偶发 403 `model_not_available`、空回复 | 已有：`d6803dd` 静默终止反馈；D3 三分 |
| 额度 | — | Boyue 09-03 余额 −$0.06 全 403 | D3：额度 403 ≠ 路由 403，文案要分 |

## 4. 怎么验收

1. 每条契约一个 `tests/test_cc_parity_*.py`，用现有 `run_pty_child` + `terminal_screen`。
2. `scripts/cc_parity.py` 跑全部并打印 `parity = n/N`，按 §1 分组给出未过项。
3. 进 AGENTS.md：**改 `core/tui.py` 或 `zylab.py` 的输入/渲染路径后必须重跑 parity**。
4. 第一批只做 P1（B1、C2、C6、D1、D2、E2、H1、H2、G3/I1）—— 其中 C6、H1、H2、D1
   四条合计不到一天。

## 5. 与当前在飞工作的关系

先落地再对齐：codex 的 36 项未提交要先拆分提交、树绿；Gate 0 残留（`app_owned`、
`HistoryAnchor`、`_mouse_enabled` 残余）连旧测试一起删——I4 的前提。D1/C2/H1/H2 不碰
它在飞的区域，可以并行。
