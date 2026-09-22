# zylab

终端里的编码助理，接任何 OpenAI 兼容的网关。照搬 Claude Code 的 harness 形状，
用 Python 标准库实现：零第三方依赖，克隆即用，不需要 node、pip 或 venv。
借的是架构，不是代码。

**自带公开厂商的地址，不预置任何 key，也不预置任何私有端点。**
下载下来填一把自己的 key 就能用：

```bash
git clone https://github.com/CROZHuang/zylab && cd zylab
export OPENAI_API_KEY='sk-…'                 # 或别家的 key
./zylab init --gateway openai --key-env OPENAI_API_KEY --yes
./zylab                                       # 开工
```

`init` 会做四件事：写 endpoint → 收 key（`0600`，不回显）→ 拉一次模型目录并落盘 →
调一次默认模型验明正身；**默认模型这把 key 用不了就当场换一个能用的**并写进配置。

内置 profile（地址来自厂商公开文档，对不对由 `init` 那次真实探测当场验）：
`openai` `anthropic` `openrouter` `deepseek` `moonshot` `zhipu` `dashscope`
`siliconflow` `groq` `xai` `mistral` `together`，本机推理 `ollama` `lmstudio`。
自建 / 公司内部网关只给 key 变量名、地址自己填：

```bash
./zylab init --gateway mycorp --base https://<host>/v1 --key-env MYCORP_API_KEY --yes
```

唯一的硬条件是 **OpenAI 兼容**：提供 `/v1/chat/completions` 与 `/v1/models`，并支持
tool calling。

```bash
zylab                          # 交互会话
zylab -p "把 x.py 并行化"       # 单次执行后退出，可管道
zylab -p "…" --max-turns 30     # 无人值守时给工具循环兜底；默认不设上限
zylab --resume                 # 接最近一次会话
zylab --resume <id>            # 接指定会话
zylab --resume <id> --resume-cwd current  # cwd 不同时留在当前目录
zylab --resume <id> --resume-cwd saved    # cwd 不同时切回保存目录
zylab --sessions               # 列出保存的会话
zylab --models                 # 列出网关上的模型
zylab --usage                  # 历史用量汇总（按天 / 按模型）
zylab --usage 7                # 最近 7 天
zylab --usage <session前缀>     # 单个会话
zylab --yolo                   # 仅批准普通 ask；hard/deny 仍拒绝（慎用）
```

## 安装

不需要 pip / venv，纯标准库，Python 3.10+。

```bash
git clone https://github.com/CROZHuang/zylab.git && cd zylab
./zylab init        # 填 endpoint → 收 key（不回显）→ 真实探测一次
./install.sh        # 可选：在 ~/.local/bin/zylab 生成薄 stub 进 PATH（不是 symlink，不是副本）
```

`init` 的每一步失败都自带下一步动作，而不是只丢一个错误码。

### Windows

一样是克隆即用，只是入口换成 `zylab.cmd`（`./zylab` 没有扩展名，cmd 和
PowerShell 不认 shebang）：

```bat
git clone https://github.com/CROZHuang/zylab.git
cd zylab
set OPENAI_API_KEY=sk-…
zylab init --gateway openai --key-env OPENAI_API_KEY --yes
zylab
```

两个 Windows 专属的准备：

- **Python 3.10+**。`zylab.cmd` 会先试 `py -3` 再试 `python`，两个都会先做一次
  真实的版本探测 —— 因为没装 Python 时 PATH 上**仍然有** `python.exe`：那是
  微软商店的应用执行别名，运行它只会静默弹出商店页面。
- **一个真 bash**，给 Bash 工具用：装 [Git for Windows](https://git-scm.com/download/win)，
  确保 `<Git>\usr\bin` 在 PATH 里。注意 `C:\Windows\System32\bash.exe`
  **不算** —— 那是 WSL 启动器，它会把命令送进另一个文件系统命名空间，
  zylab 的路径守卫在那边一条也对不上，所以 zylab 会拒绝用它并告诉你装什么。

终端建议用 Windows Terminal（VT 序列、真彩、鼠标上报都齐）。传统的
`conhost.exe` 也能跑，zylab 会探测能力后自己降级。

Windows 上多出来的几条本地规矩，zylab 会在系统提示里直接告诉模型，
也会在工具层拦住：`NUL`/`CON`/`AUX`/`COM1`–`COM9`/`LPT1`–`LPT9` 是**设备名
不是文件名**（`aux.txt` 也算），结尾的点或空格会被 Win32 静默剥掉。

**你要自备一个 OpenAI 兼容的 endpoint 和 key。** zylab 不预置、不代理、不转发任何
网关地址 —— 它只是客户端。任何提供 `/v1/chat/completions` 的服务都能接：厂商官方
API、公司内部网关、本地 vLLM / Ollama。唯一的硬要求是**模型支持 tool calling**，
否则工具循环无从谈起，这个 CLI 的全部价值都在那个循环上。

- 非交互初始化：
  `./zylab init --gateway myapi --base https://<host>/v1 --key-env MYAPI_API_KEY --yes`
- endpoint 存在用户级 `~/.zylab/settings.json` 的 `gateways` 段，也可以
  `export ZYLAB_BASE_MYAPI='https://<host>/v1'` 临时覆盖。
  **项目级配置改不了 endpoint** —— 项目目录常来自 clone，让它改等于让任意仓库
  把你的 key 和代码发到它选的服务器上，而界面上看不出任何异常。
- 内置两个 profile 名（`deepinfer` / `boyue`）只是名字和 key 变量名，**不含地址**；
  想要别的名字就在 `gateways` 里自己加一个。
- 先看环境再开工：`/doctor`。

**key 怎么配（每个网关一个环境变量，只配一个也能用）：**

| 网关 | 环境变量 | keys.env 里的一行 |
|---|---|---|
| deepinfer | `DEEPINFER_API_KEY` | `DEEPINFER_API_KEY=...` |
| boyue | `BOYUE_API_KEY` | `BOYUE_API_KEY=...` |
| 你自己加的 | `gateways.<name>.keys` 里声明的那个 | 同名一行 |

`keys.env` 的落点按顺序找：`ZYLAB_KEYS_FILE` 指定的文件 → `~/.zylab/keys.env` →
`~/.config/zylab/keys.env`；环境变量优先于文件。**key 不随仓库分发，也不写进任何
配置默认值。** `/model` 列表上方对每个网关标注「key 已配置 / 未配置 key」；切到没配
key 的网关会当场给出补救命令，而不是等发请求时才 401。

## 升级

```bash
cd <你的 zylab 目录> && git pull
```

就这一步。`~/.local/bin/zylab` 里的 stub 指的是目录不是版本，`git pull` 之后自动生效，
不用重跑 `install.sh`。状态（会话、记忆、key）在 `~/.zylab`，与代码目录分开，升级不会碰它。

- **别用 `--portable`，除非你确实要"一个目录装下全部"**：那模式把状态放进代码目录里的
  `.zylab-home/`，换一份代码就换一份状态。用 `git pull` 升级的人不需要它。
- 升级后想确认跑的是哪一版：`zylab --version`。
- 离线 tarball 的升级是解压到**新目录**，然后 `./install.sh` 让 stub 指过去；
  状态仍在 `~/.zylab`，不会丢。

## 出了问题先看这张表

三种失败都长得像 4xx/5xx/超时，但该做的事完全不同。`zylab init` 会替你分诊一次；
之后每次请求失败的提示也按这张表写。**请求由 zylab 直连发出，不经过 shell 里的代理变量。**

| 症状 | 真因 | 你该做的 |
|---|---|---|
| 启动就说「还没有 API key」，或 `-p` 立刻退出 1 | 没配置 key | 提示里的两条命令任选：`export DEEPINFER_API_KEY=…` 或写进它列出的 keys.env；或 `zylab init` |
| `HTTP 401`「认证失败」 | key 无效、过期或不属于这个网关 | `/doctor` 看 key 来源；换 key 或换网关 |
| `HTTP 403`「额度不足」 | 网关账户余额为负 | 充值，或 `/model gateway <另一个网关>` 切换；key 与路由都没问题 |
| `HTTP 403` 其它 | 模型权限 / 网关账号 / 服务端路由 | 先换个模型试；不要一上来换 key |
| `HTTP 429` | 限流 | 什么都不用做，会自动退避重试 |
| `HTTP 5xx`、超时、「不可达」 | 网关自己挂了或网络不通，**与 key 无关** | 稍后重试或换网关：`zylab init --gateway boyue` |
| `-p` 半天没动静 | 网关首字慢（kimi-k3-256k 实测过 100s） | stderr 每隔一阵会报「等 … 已 Ns 没有响应」；Ctrl+C 放弃或换模型 |
| 「已拒绝明文 HTTP provider 传输」 | Boyue 只有内网明文 HTTP | 按提示显式授权（交互逐 session，或 CLI 危险开关） |
| 启动就说「网关 … 还没有 endpoint 地址」 | 用的是自建网关，地址不随仓库分发 | `zylab init --gateway <名字> --base https://<host>/v1`；或直接换一个地址已内置的公开厂商 |
| `git clone` 报 `Permission denied (publickey)`，但 key 已加、指纹也对 | `ssh` 读的是 passwd 里的家目录，而 `HOME` 被重映射到了别处（容器里常见），于是钥匙生成在哪、ssh 就是找不到 | `git config --global core.sshCommand "ssh -i $HOME/.ssh/id_ed25519 -o IdentitiesOnly=yes"` 再 clone；`git` 认 `HOME`，`ssh` 不认 |
| 「需要 Python 3.10+」 | 解释器太旧 | `which -a python3` 选一个 3.10+ 的：`python3.12 zylab.py` |

## 它和「聊天窗口」的区别

不是问一句答一句。模型每轮可以发起若干工具调用，harness 执行完喂回去，
**循环直到它不再调工具**。一次需求里读文件、改代码、跑起来验证、报告结果全自动走完。

实测（2026-08-21）：`"给 fib.py 加 memoization 并验证 fib(100)"` → 它自己
read_file → edit_file → 跑 python → 拿结果和已知值对照 → 汇报，一轮全部完成。

## 工具与权限

| 工具 | 权限 |
|---|---|
| `read_file` `list_dir` `glob` `grep` `todo_write` | 自动执行 |
| `graft_find_code` `graft_file_api` `graft_trace_calls` `graft_find_all` `graft_repo_map` | 自动执行；本地 hosted 结构索引，零 provider |
| `memory_write(scope="project")` | 自动执行并在 UI 留痕 |
| `memory_write(scope="global")` `memory_forget` | **逐次 hard confirmation**（默认拒绝，只能批准本次） |
| `bash` `write_file` `edit_file` | **每次确认**（↑↓ 选择一次允许 / 本会话允许 / 拒绝） |
| `consult_session` | **每次确认**（把目标 chat 快照发送给其保存的 provider） |

`/auto` 只在当前会话自动批准 `ask`；`--yolo` 是非交互运行时的显式等价选项。
`deny` 和 hard confirmation 在两种模式下都不会被越过。非交互模式（`-p`、管道）
遇到 `ask` 默认拒绝，需要明确使用 `--yolo` 或先把对应用户规则设为 `allow`；
global memory 写入和 memory 删除即使如此也会 fail-closed。

`/permissions` 打开集中管理面板，显示每个工具的 effective 值和来源，并可连续设置
`allow` / `ask` / `deny` / `reset`；`/permission` 是同义别名。带参数时也可直接执行，
例如 `/permissions bash deny`。修改写入用户级 `settings.json`，立即刷新当前会话；
`reset` 删除用户覆盖，回到内置默认或更严格的项目规则。项目级配置只能收紧权限。
hard memory 项只能设为 `deny` 或 `reset`，不能降级为普通 `ask` 或 `allow`。
配置更新使用跨进程锁与原子替换；原文件损坏或是符号链接时拒绝覆盖。
用户直接输入 `/consult` 已构成一次明确授权，不再重复弹工具确认；若主模型自行调用
`consult_session`，默认仍为 `ask`，因为这会把另一个 chat 的保存快照发送给它记录的
provider。

破坏性 Git（如 `reset --hard`、`checkout --`、`clean -f`、`branch -D`、
`push --force`）和递归删除超过 100 个现有文件的 `rm -rf` 使用独立的高风险确认。
确认框默认选中拒绝，并展示最终 post-hook 命令、实际 `git status --porcelain` 或删除
目标计数；`/auto`、配置 `allow` 和本会话授权都不能绕过。未经这次批准的
`PreparedArguments` 在工具执行层仍会被拒绝。

global memory 写入和所有 `memory_forget` 采用同样的 defense-in-depth：确认框默认
拒绝且只有“拒绝 / 仅本次”两个选项，批准绑定到最终 post-hook `PreparedArguments`；
`/auto`、`--yolo`、配置 `allow` 和 session grant 均不能代替这次确认，非交互模式
一律拒绝。project memory 写入仍按默认 `allow` 自动执行。

斜杠命令：`/help` `/new` `/sessions` `/resume` `/rename` `/clear` `/compact`
`/context` `/memory` `/skills` `/commands` `/auto` `/permissions` `/config` `/cost` `/stats` `/model`
`/probe` `/graft` `/architecture` `/tools` `/agents` `/consult` `/workflow`
`/tasks` `/task` `/expand` `/kill` `/exit`

`/map` 是隐藏的兼容命令：它不会出现在命令面板或 `/help` 的顶层列表中，
但旧脚本和明确输入 `/map status|build|refresh|check|viz` 仍可运行。它的
确定性 Python/import 导航层保留为离线 fallback；新任务请优先使用 Graft。

## 界面：像 Claude Code 一样内联（默认）

对话直接进终端自己的 scrollback：**滚轮、拖终端右边的滚动条、鼠标拖选复制，全是终端原生的**，
zylab 不接管鼠标。输入框跟在内容下面；用户消息一行淡色 `› …`，工具调用 `⏺ Read(path)` +
`⎿  N lines · Ctrl+O 展开`，思考折叠成一行，忙碌时 `✻ 思考中 3s · esc 中断`。

- 粘贴：终端自己的快捷键（Cursor / VS Code 里 Ctrl+Shift+V，macOS Cmd+V）。
- 按键：终端支持 kitty 键盘协议时 Shift+Enter 换行、Esc 立即中断（Cursor 的终端支持）。
- `--app`：可选的全屏应用模式（备用屏）。代价是终端滚动条对它无效、滚轮由 zylab 接管——
  想像 Claude Code 那样用滚动条翻历史，就别用它。`-p` / 管道 / 非 TTY 始终是内联。
- 新终端上画边界：`python3 scripts/term_probe.py`；`zylab --terminal-caps` 重探并打印能力。

## 自包含（便携）模式：一个目录装下程序 + 配置 + 状态 + key

```
zylab/                    ← 应用根目录，整个目录可整体拷贝（tar / cp -a / 拖到别的 pod）
  zylab  zylab.py  core/  skills/  scripts/  tests/  README.md …
  .zylab-home/            ← 状态目录：sessions/ checkpoints/ memory/ settings.json keys.env ZYLAB.md …
```

- 打开：`./zylab init --portable`。家目录下已有的 `~/.zylab` 会被**整体收编**进 `.zylab-home/`
  （同文件系统 `os.rename`，目录里记的绝对路径一起改写；有活会话则拒绝，先退出旧进程）。
- 状态目录的三级优先：`ZYLAB_HOME` 显式改道 → 应用目录里的 `.zylab-home/` → `~/.zylab`。
- 拷到别处后首次启动会自动把目录里记的旧绝对路径改到新位置（`.zylab-home/.location` 记上次位置）。
- 不叫 `.zylab/`：那是**项目级**配置目录的名字，在应用目录里工作时两者会撞成同一个文件。
- `git archive` 出包天然不带它；`.gitignore` 已忽略。**别在应用目录里跑 `git clean -fdx`**——会把状态删掉。
- 退出自包含：`mv zylab/.zylab-home ~/.zylab`，下次启动自动回到默认目录。

## 家目录布局

> 状态目录默认 `~/.zylab`，可用 `ZYLAB_HOME=/some/dir` 改道——**必须在启动前 export**，
> 进程内改无效（各模块在 import 时就把路径算好了；`ZYLAB_STATE_DB` 之类是调用时求值的，行为不同）。

照 `~/.claude` 和 `~/.codex` 的形状来 —— 那两个是跑了很久的成熟产品，
分出来的每个目录都对应一类真实需求。只建有实际用途的，不摆空架子。

```
~/.zylab/
  settings.json        用户级配置（模型/网关/权限/参数）
  ZYLAB.md          跨项目的全局指令，类比 ~/.claude/CLAUDE.md
  models.json          模型能力表
  usage.jsonl          成功调用兼容流水
  stats-cache.json     兼容用量汇总（派生物，可重算）
  history.jsonl        输入历史
  installation_id      安装标识
  metrics.sqlite3      request/tool/model health 权威指标（按需创建）
  state.sqlite3        实验性事件索引（默认不创建）
  sessions/            活跃会话
  archived_sessions/   归档会话
  plans/               plan mode 批准的计划
  backups/             改文件前的原文备份
  artifacts/           managed task 的完整 stdout/stderr（0600）
  agent-runs/          child agent 与 cs-* consult 的私有 transcript/runtime（0600）
  workflows/           异构 workflow 状态、席位与 synthesis（0600）
  workflow-recipes/    用户级纯 JSON workflow recipes（0600）
  memory/              global/project 分层、带来源长期记忆（0600）
  attachments/         @file 与图片的 content-addressed 私有快照（0600）
  commands/            用户级 prompt-only Markdown 命令
  logs/                运行日志
  cache/ projects/     缓存与按项目分组的索引
```

### 实验性 SQLite state（M1）与运行指标（M5）

当前阶段 **session JSON 仍是会话启动源和权威源**。`state.sqlite3` 只做 opt-in
shadow：接收有序 event 与等价 usage 记录，用来验证下一代会话状态模型；普通启动
不会创建该数据库，也不会自动迁移、删除、移动或改写旧文件。`usage.jsonl` 是成功
调用的兼容流水，也是 `stats-cache.json` 的重算源，但不再冒充 retry、失败和无 usage
请求的完整事实源。

M5 的 `metrics.sqlite3` 与上述迁移 target 完全分离，按需保存每次 provider attempt、
tool run 和 `gateway+model` health。这个拆分避免提前创建 `state.sqlite3`，从而破坏
JSON→SQLite 迁移要求 target 尚不存在的前提。

先做只读盘点（默认行为，不创建 target/lock）：

```bash
python3 -m core.migrations
```

确认输出 `"ok": true`、counts 与 hash manifest 后，才可显式写入一个**不存在的**
target：

```bash
python3 -m core.migrations --write --target /absolute/path/state.sqlite3
```

迁移遇到坏 JSON/JSONL 会保留 `.tmp` 与 `.tmp.errors.json`，不会切换 target；
重复来源按路径与 SHA-256 幂等，内容改变则拒绝猜测合并。要在运行时验证 shadow：

```bash
ZYLAB_STATE_SHADOW=1 zylab
# 自定义位置时再加：ZYLAB_STATE_DB=/absolute/path/state.sqlite3
```

shadow 写入失败会记录 `state_shadow_error` 并在当前进程停用，JSON 主流程继续。
这不是权威存储切换；切回默认只需不设置 `ZYLAB_STATE_SHADOW`。

M5 metrics 则是 fail-closed：request intent 在打开 socket 前落盘，tool intent 在副作用
前落盘；写入失败时不启动相应操作。每行区分真实 TTFT、总延迟、retry、错误、usage/
cache measured 与 unknown。进程异常退出后，本机 PID + process-start identity 已消失的
running row 会恢复为 request `interrupted/process_crash` 或 tool `unknown`；仍存活的
owner 不会被误改。`/status`、`/trace [turn]`、`/model health` 和 `/usage` 读取这份
权威指标。

启动时状态目录会收紧为 `0700`，新写 session/usage/history/log/backup/SQLite 文件为
`0600`，避免会话里的命令参数或偶然粘贴的凭据被同机其他账号读取。

### 可审计上下文投影（M2）

从 M2 起，`sessions/<id>.json` 里的 `messages` 在正常对话中是 append-only
原始事实（显式 `/clear` 除外）。工具结果老化和 compact 不再覆盖或删除既有条目；
每次 API 调用前才生成一个独立 provider 投影：

- system / 指令前缀稳定保留；
- 只接受完整的 assistant `tool_calls` + tool result 协议单元；崩溃遗留的半对
  工具调用留在 raw 中审计，但不会发给 provider，并有明确 omission marker；
- 最近 3 个 assistant 轮次保留小工具结果全文；大结果和更早结果使用带工具名、
  参数摘要、退出状态、首尾内容及 SHA-256 artifact id 的预览；
- compact 摘要记录覆盖边界、原始前缀 SHA-256、模型、网关和生成时间。恢复时哈希
  不匹配就拒绝使用旧摘要；
- 被摘要覆盖或预算裁剪的 role=user 消息仍按原始时间顺序逐字进入 provider
  投影，不截断、不由摘要器代述；summary 与 omission 使用 assistant 控制消息，避免
  污染 user 原话序列。若这些不可裁剪原话本身超预算则 fail-closed；
- 摘要失败不会再机械删除历史：保留上一份有效摘要，必要时按完整轮次确定性省略，
  raw 始终不变；连最新受保护上下文也放不下时 fail-closed；
- `/context` 显示模型上限来源、可用预算、当前估算、上次 provider 实测、摘要覆盖、
  user verbatim、完整/预览工具结果、保留额度、被省略范围和下一步建议。

旧版本已经执行过破坏性 compact 的会话无法凭空还原当时被删除的前缀；上述不变量
只对 M2 之后仍存在于 raw `messages` / event journal 的记录生效。

### Controller、Persistent Composer 与 TaskManager（M3a–M3c-4）

M3a 的 `SessionController` 已接入交互 REPL。input、request、permission 和 tool
生命周期先进入 event journal，之后才返回可执行 action；`steer` 与 `next_turn`
保持为独立 queue item，Esc intent 也会在主动关闭 provider response 前记录。

M3b 用一个 `InputPump` 线程独占交互 REPL 的 stdin，主线程独占 Renderer、
Controller、Agent 和 SQLite 写入；交互 turn 的主回复与自动 compact HTTP 由有界
`ProviderWorker` 执行。模型工作期间可继续编辑：Shift+Enter 在 composer 内插入换行，
Enter 在下一安全边界 steer 当前任务，Tab 排下一轮，Up 取回尚未 dispatch 的输入，
Esc 中断 provider 但保留 queue 与草稿。
模型 delta 按 25 ms 或 4 KiB 批量刷新，达到 1 MiB 会强制刷新。

M3c-0 把 queue、可编辑 prompt 与 footer 合成一个常驻 composer。busy 且草稿为空时
输入框仍显示；footer 始终跟随输入框展示 chat name、`model@gateway`、context、
activity 和 cwd。`/resume` 回放后第一帧即使用恢复会话的 title/model/gateway/context，
不再等下一次模型输出后才补显示属性。

M3c-1a 已把前台 tool/subagent 搬到 `run_background` worker，主线程至多等待 25 ms
便收到 poll，因而长工具运行时仍能处理、显示并持久化 steer/next-turn。hook note 和
tool result 都先回到主线程再写 terminal/Controller；嵌套 subagent hook note 使用
thread-local channel，不直接写 stdout 或 SQLite。

M3c-1b 用 `core/tasks.py` 的 foreground TaskManager 取代交互主链里的临时 worker。
每个工具有短 task id 和 queued/running/cancelling/completed/failed/cancelled 状态；
`task_started` 先由主线程写 journal，worker 随后才启动。Bash 使用
`Popen(start_new_session=True)` 和非阻塞 stdout/stderr reader，完整输出写入私有
artifact，UI 只持有 64 KiB ring。Esc 先记录 `tool_cancel_requested`，再对进程组按
SIGINT → 750 ms → SIGTERM → 2 s → SIGKILL 升级；取消后的当前及同批剩余 tool call
都会补齐协议结果，不会留下孤儿。已知的环境 API key/token/secret 会在进入 UI、
transcript 和 artifact 前做跨 chunk 精确替换。

M3c-4 在这套 registry 上补齐 Bash 的前后台交互。foreground 工具的实时 chunk 只在
footer 显示有界进度，成功后保留「工具头 + 一行摘要」；完整脱敏输出由
`tool_call_id` / task id 索引到私有 artifact，可用 `/expand [id] [page]` 按 30 KB
分页查看。错误、拒绝和非零 Bash 仍完整显示，不会被折叠。展开只影响终端呈现，
不改 provider messages 或 context projection。功能上线前的 artifact 从
`metrics.sqlite3` 只读恢复映射；新 sidecar 与旧映射冲突时以 sidecar 为准。
Ctrl+B 只把当前 foreground Bash
转成 background，并立刻用唯一、确定性的 tool result 闭合本轮模型协议，provider
可继续工作。后台任务不会直接写终端；`/tasks` 查看
id/mode/status/elapsed/output/name/outcome（包括 exit、signal、timeout 与基础设施
error 摘要），`/task <id>` 非破坏读取 artifact tail，并可 attach 运行中的后台输出，
`/task detach` 或 Ctrl+B 退出该 live view，`/kill <id>` 显式终止。正常 CLI 退出会取消 managed background task、join
TaskManager worker，并清理 TaskManager 为 Bash 创建和管理的 process group，再由主线程
记录 terminal 结果；不支持跨 CLI 生命周期 detach。任务显式用 `setsid`、double-fork
或 daemonize 逃离该组后产生的进程不在当前保证范围内。

M3b.1 已把每个 Agent/provider/tool worker 改为持有不可变的
`gateway/model/session/hook` 快照。请求重试始终沿用原 route，不再被 UI 的网关切换
串线；provider 错误保留 HTTP status、错误 code、request id、gateway/model 和
retryable 分类。`403 model_not_available` 会在尚未输出时退避重试，普通 403 仍按权限
错误停止。`models.json`、session JSON、usage/history/log JSONL 与 `stats-cache.json` 的
并发更新均已使用文件锁及原子写；输入历史也不再由各 terminal 退出时整体覆盖。活跃
worker 不再读写兼容性全局来决定路由。

M3b.2 补齐 Claude Code 风格的权限控制面：`/permissions` 展示 effective/source，
可持久修改用户级 `allow/ask/deny` 或 reset；项目规则仍只能收紧。多个 terminal 同时
修改配置时通过 file lock + atomic replace 合并，损坏配置 fail-closed。`-p`/管道下
的普通 `ask` 也改为默认拒绝，只有显式 `--yolo`、`auto_approve` 或配置 `allow`
才放行；hard confirmation 与 `deny` 始终不受影响。

M1 的回滚合同仍有效：默认 JSON backend 下 Controller 使用进程内 journal，输入正文
仍立即进入 `history.jsonl`；设置 `ZYLAB_STATE_SHADOW=1` 后 queue/event 生命周期
才写入 SQLite 并可用于崩溃恢复验证。M3c-4 的 background task 只在当前 CLI 进程内
受管；退出时清理 TaskManager 所创建的 process group，不把它冒充正式调度任务。
可 attach 的 child-agent panel 已在 M3c-3 完成。


## 配置

三层，后面覆盖前面：内置默认 → `~/.zylab/settings.json` →
`<项目>/.zylab/settings.json`。`/config` 查看全部配置，`/permissions` 查看并修改
权限的 effective 值与来源。

**项目级只能把权限与 sandbox 收紧，不能放宽。** 理由和 Claude Code 一样：项目目录
可能是 clone 来的，让它把 bash 从「每次确认」改成「自动放行」、关闭 network
isolation 或替换 sandbox executable，等于给任意仓库在本机静默执行命令的权力。
项目级不能启用 `auto_approve`；model/gateway/参数与 model-health TTL 可覆盖。
`max_turns` 是一轮对话里工具循环的轮数上限，默认 `null` 不设上限（像 Claude Code：模型
自己停下或按 Esc 才结束；child agent 共用这个值）；无人值守想兜底就设整数，或临时用
`--max-turns N`（0 也表示不设上限）。到顶时给可见的 partial 结果，已产出的工具结果保留。

默认 sandbox 是 `ask-unsandboxed`、`network_isolation: true`，并保护
配置中的只读路径。adapter 或网络 namespace 没有通过真实 probe 时不会显示为已隔离：
`strict` 直接拒绝，`ask-unsandboxed` 需要针对该次调用明确同意，`disabled` 始终显示
`UNSANDBOXED`；项目配置不能把更严格的用户设置降级。旧的 `sandbox.network` 仍能读取，
但规范字段是 `network_isolation`。

`model_health` 默认连续 3 次 transient failure 后打开 circuit，**时长按指数退避**：
首次 30 秒，其后每多一次连续失败翻倍，封顶 10 分钟（`circuit_open_ttl` 是基数，
`circuit_open_max_ttl` 是上限）。一次成功即清零。计入 circuit 的只有可重试的瞬时
故障——认证、权限、参数、上下文超限这类不会因重试而改变的错误一律不计。
circuit 打开时，**显式指定模型/网关仍可带 warning 越过**。probe 成功缓存
24 小时，瞬时失败按 5 分钟起始、最多 1 小时退避。这些值均可配置，并在 cwd-safe
resume 时随目标项目原子切换；切换失败会恢复旧策略。

## Hook

把守卫、检查、自动化从硬编码变成可配置。两个挂载点，配置写在
`~/.zylab/settings.json`：

```json
"hooks": {
  "PreToolUse":  [{"matcher": "bash", "command": "my-check.sh"}],
  "PostToolUse": [{"matcher": "edit_file|write_file",
                   "command": "ruff check --fix \"$ZYLAB_TOOL_PATH\""}]
}
```

hook 从 stdin 收到一行 JSON（`hook` / `tool` / `args` / `cwd` / `session`，
PostToolUse 还有 `result`），常用字段也以 `ZYLAB_TOOL_COMMAND`、
`ZYLAB_TOOL_PATH` 等环境变量给出，方便写单行 shell。

退出码约定：

| 退出码 | 含义 |
|---|---|
| `0` | 放行。stdout 若是 JSON，`{"args": {...}}` 可改写参数，`{"decision":"deny"}` 也可拒绝 |
| `2` | **拦截**，stderr 作为拒绝理由回给模型 |
| 其他 / 超时 | 视为 **hook 自身故障**：告警后放行 |

最后一条是刻意的：一个写错的 lint hook 不该让整个 CLI 停摆。真要拦截请显式
`exit 2`。`matcher` 支持 `*` 和 `bash|write_file` 这样的多选。

**两条硬边界**：

1. **hook 只能在用户级配置注册。** 项目级 `<repo>/.zylab/settings.json`
   里的 `hooks` 一律丢弃并告警 —— hook 执行任意命令，而项目目录常常来自
   clone，允许它注册 hook 等于允许任意仓库在你机器上跑代码。这比「项目级
   不能放宽权限」更硬。
2. **hook 绕不过只读路径守卫。** 守卫仍硬编码在工具层，在 hook 之后独立
   执行；hook 改写过的参数照样要过守卫。hook 只能让规则更严，不能更松。

`/hooks` 查看已注册的 hook。

## 计划模式

`/plan` 进入只读调研：**写工具连 schema 都不发给模型**，它看不见就不会调；
即便凭记忆硬调也会在执行前被拦（双保险）。查清楚后给出计划，再打一次 `/plan`
批准 —— 计划存进 `~/.zylab/plans/`，并作为约束带回执行阶段。

这与普通任务里的实时计划是两件事。对于确实需要多个步骤的常规工作，主模型可用
`todo_write` 维护当前 chat 独有的 `Updated Plan`：最多一个步骤为 `in_progress`，每次
变化在 scrollback 显示一个稳定块，composer footer 始终保留 `plan 已完成/总数` 和当前
步骤。计划在正常 turn 提交时随 session 原子保存，`/resume` 后恢复；简单问答和单步
操作不应创建计划。不同 terminal/chat 不共享该状态。

## 持久目标（`/goal`）

`/goal` 是比 `todo_write` 更高一层的**完成条件**：`todo_write` 描述要做哪些步骤，
`/goal` 描述最终必须变成什么样，并允许主 agent 在当前 turn 结束后自动继续下一轮。
目标定义、进度、验证证据、模型用量和耗时会随 session 原子保存；目标本身不会把
`/resume` 变成隐式自动运行。

```text
/goal <objective>                  # 创建目标并 armed，空闲边界自动开始
/goal accept | reject              # 采纳/忽略模型用 goal_propose 起草的目标
/goal status                       # 查看 phase、round、用量与最近证据
/goal edit <objective> --rounds N  # 调整目标或上限（最多 256）
/goal pause                        # 暂停自动续轮
/goal resume                       # 明确重新 armed；会从当前状态继续
/goal complete [evidence]          # 用户手动验收并结束
/goal block <reason>               # 用户标记外部阻塞
/goal clear                        # 清除目标及尚未 dispatch 的内部续轮
```

**recap：离开期间写好的一行路标。** 行为与机制照搬 Claude Code 的 away summary——模型现写的
**一行**「总目标 + 当前任务 + 下一步」，不是从落盘数据拼出来的多行结构块（旧的零调用 recap
应付不了开放式的会话：没有 `task_plan`、没撞到压缩阈值，它就无话可说）。

- **什么时候写**：终端**失焦**（DEC 1004 焦点上报；xterm.js / VS Code、iTerm2、kitty、WezTerm
  等都支持）满 `recap_away_seconds`（默认 300s，下限 30s）且没有轮次在跑 → 后台生成，写好等你
  回来；**重新聚焦即取消定时器并中止在途请求**（shutdown socket，不等网关回话）。终端不支持
  焦点上报就永不自动触发，`/recap` 照常可用。`resume` 之后也给一条：上次那条之后没有新提问就
  直接用落盘的（零调用），否则后台现写。
- **什么时候不打扰**：输入框里有草稿 / 有子代理或后台任务在跑 / 已在生成 / 本轮连续失败 3 次 /
  真实提问不足 3 条 / 距上次 recap 新增提问不足 2 条 / 写完发现新一轮已开始（丢弃）。
  `[…]` 运行时通知与 goal 自动续轮的内部提示**不算**提问。
- **怎么写**：上下文 = 主请求的那份投影 + 末尾追加 CC 的提示词（原文照搬，补了一句中文字数
  约束和一句「别调工具」）；单轮；截到 400 字符。**请求的前缀与主循环逐字节一致**——同一份
  system、同一套工具定义（工具照发，只是不许用：回复里出现工具调用即算失败、绝不执行）——
  这样才吃得到主循环攒下的 prompt cache。结果存进会话记录的 `context.away_recap`，**绝不进
  `messages`**——模型下一轮不会看到一条自己没见过的「用户提问」。前 3 次附一句怎么关。
- **用哪个模型**：**就是这个窗口此刻在用的模型**（`/model` 切了它就跟着变），没有单独的开关，
  与 CC 一致。失败时也不换别家来写——压缩在主模型不行时会借席位池里另一家，recap 不会：那等于
  在没人要求的情况下把会话内容发给你没给这个窗口选的 provider；recap 只是个方便，写不出来就
  安静地失败（一轮最多试三次）。同一个模型最多问两次：先关思考，只有「思考吃光了额度」或
  网关拒收关思考（400）时才按模型默认方式、给足额度再问一次。
  快慢取决于缓存热不热（2026-09-20 实测，6.5 万 token 的真实会话）：与主循环同前缀时网关回报
  命中 65,280/65,356 token，首字 **0.4–0.5s**；不命中 4.6s；换了前缀的冷请求在 `glm-5.3@deepinfer`
  上测到过首字 127.9s。主循环本身 91.8%（Boyue）/ 52.9%（DeepInfer）的输入 token 来自缓存——
  所以「就用窗口当前的模型」不只是一致性，也是最快的选择。DeepInfer 是多副本，偶尔落到没缓存的
  副本上会慢一次。`/recap` 显示已等秒数，Esc 可取消。
- `/recap` 立刻现写一条（Esc / Ctrl+C 取消）。`recap_auto: false` 只关自动触发。

**压缩摘要**是另一件事：为了**塞进上下文窗口**，`模型上限 − 余量`（默认模型 238k）才触发，
会替换掉投影里的原文；recap 不替换任何东西。

**armed 到底意味着什么**：会话空闲时（200 ms 无输入）自动开始下一轮，不需要你再输入，
直到目标 complete、blocked 或用满轮数。这一点必须说清楚，因为它是这个功能的**全部价值**，
而它以前只写在这一节里 —— 模型自己都不知道它存在（2026-09-08：用户要求「持续跟踪并自动
汇报」，模型回答「必须等你 prompt」，而引擎就在旁边空转）。

**模型可以起草，但由你拍板。** 模型调 `goal_propose` 时会**弹一个确认框**（复用 `decision_gate`
那条同意通道，不新造交互类型）：`采纳并开始自动续轮` / `不采纳`，带上轮数上限与后果。点采纳
即刻 armed，不用再敲命令；点不采纳就此打住。弹不成时（非交互、child worker、用户取消）退回
留草稿，界面摆出来等 `/goal accept` —— **「没答」永远不算同意**。

armed 意味着模型在没有新 prompt 的情况下反复自己跑，那是权限升级，只能由用户拍板。工具描述
本身也是模型的发现面 —— 没有 armed goal 时 `<goal-policy>` 根本不注入，schema 里的那段话是
模型唯一知道这功能存在的地方。

**边界：自动续轮只活在这个终端进程里。** 退出 zylab、关掉终端或 pod 重启，续轮就停（目标
定义仍在，`/goal resume` 可重新 armed）。**没有 headless 续轮**，`zylab -p` 是单次模式。
需要「过夜跟踪」时目前只能让终端开着。

默认最多自动续轮 20 次；每次在 provider 请求前先预约一个 round，达到上限或本轮
失败会 fail-closed 并停在用户可见状态。目标状态会注入模型的受限 `<goal-policy>`
上下文；模型只能用 `goal_update` 以当前 `id` + `revision` 报告 `progress`、
`complete` 或具体 `blocked` 证据，不能创建目标、扩大权限或绕过用户确认。模型生成的
内部 `<goal_round>` 保留在 raw transcript 供审计，但在 resume/replay 和 composer 中
隐藏，也不会被派生 memory 或 child capsule 当成真实用户 prompt。工作流、后台 task、
plan mode 或用户 queue 正在占用安全边界时，goal 会等待而不会抢占。
`/resume` 和 `/clear` 都只恢复/保留目标定义并关闭自动续轮，必须再执行
`/goal resume`；crash 遗留的内部续轮保留在原会话，但不会阻止 `/new`、另一条
`/resume` 或 `/exit`。

## 后台任务

`bash` 带 `background: true` 时立刻返回 task id，命令继续在后台跑，模型接着做别的事；结束时
结果通过 `background_task_handoff` 自动排成一条 prompt 交回模型（`settings` 可关）。模型用
`task_status` 自己列任务、取输出尾部或终止，不必让用户去敲 `/tasks`。用户侧的 Ctrl+B、
`/tasks`、`/task`、`/kill` 不变，两条路径共用同一套 TaskManager 与审计。

**为什么补这个**：这是「引擎在、入口不在」的又一例 —— 用户按 Ctrl+B 就能把命令扔到后台，
模型却只能在前台干等一个二十分钟的命令。判据是：**只要不是逻辑上的限制（同意边界、
花钱、会话生命周期），模型自己就该够得着**。按同一条判据补齐的还有：

| 工具 | 对应的用户命令 | 说明 |
|---|---|---|
| `task_status` | `/tasks` `/task` `/kill` | 列任务、取输出尾部、终止 |
| `graft_index` | `/graft build` | 索引缺失时自己建，**不进 SAFE**（写缓存、几十秒 CPU，逐次确认） |
| `context_status` | `/context` | 自己的预算与建议；做成工具而非注入 system，避免每轮击穿 prompt cache |
| `expand_output` | `/expand` | 取回被折叠的工具输出原文，不必重跑昂贵命令 |
| `subagent_control` | `/agents list\|peek\|send` | 观察或**续跑**自己派出去的 child；别为「再查一点」重派一个 |

仍然只属于用户的：arm 目标、授予权限、切模型/网关、会话生命周期（`/clear` `/new`
`/resume` `/rewind`）。这些不是「顺手没做」，是同意边界。

2026-09-09 把 43 个命令逐条过了一遍：**11 个模型已有入口、1 个部分可达（已于 09-10 补齐）、
9 个缺口、17 个同意边界、5 个纯界面**。那 9 个缺口全是只读观测（用量、请求时间线、备份列表），
Claude Code 自己也不把它们给模型，暂不补。

## 子代理

`subagent` 工具把只读调研交给一个**全新上下文**的 child agent，只把有界报告交回来。
适合「在整个仓库里找出所有 X 并判断哪些要改」这类中间要读几十个文件、
但主线只需要结论的任务。约束编在代码里：只提供 `read_file/list_dir/glob/grep`，
**不可嵌套**、报告有长度上限，并沿用触发时冻结的模型、网关与 hook context。

普通 chat 的主模型可自行决定调用它，不需要用户先开 `/workflow` 或特殊 effort。
单个高上下文调查走 `task`；真正互相独立的调查可用 `tasks` 一次并行启动 2–3 个
同模型 child。并发硬限制为 3，每个 child 最多 5 次、整批最多 15 次 provider attempt；
简单任务和有先后依赖的步骤不得为了并发而拆分。异构模型、review 和 synthesis 仍只属于
显式且更昂贵的 `/workflow`。

每次运行现在都有稳定 `a-…` id 和独立 `c-…` transcript；raw messages、inbox、状态、
route 与有界结果原子保存在私有 `agent-runs/<id>/state.json`。运行中到达的多条 inbox
消息会在 provider/tool 安全边界作为多条独立 user event 交付；completed child 收到新
消息后可用同一 transcript 和原 route 恢复。普通 `/resume` 不展示内部 child session。
M3c-3 已提供 Claude Code 风格的 `/agents` workspace。子代理的正文**不进父时间线**：完成时
只落一条永久行 `● Agent "label" finished · 5m 5s · ↓ 89.6k tokens`（失败带原因）和一行指向
`/agents peek <id>` 的续行，报告本身作为工具结果交给主模型转述（PLAN-agent-visibility J1/J2）。
主模型等待子代理时，活动行原地显示 `✻ 等待 N 个后台代理完成`，数字随完成递减；状态栏带
`N agents`。TUI 还能 list/peek/attach child，并在主模型运行期间把 direct message 独立发进 child
inbox。当前 turn 的普通 child 会常驻在 composer 上方的名册里（`○ 席位  label  最近一步  耗时 · ↓ tokens`，
最后一个结束后随下一帧消失）；`/agents attach <id>`
查看并 attach 该线程，`/agents detach` 返回主输入框。内联模式不接管鼠标：滚轮、滚动条、
拖拽选择都是终端原生的，transcript 就在终端 scrollback 里；只有 `--app` 全屏模式由 TUI
处理滚轮与选区。

M3c-3 已实现的交互合同如下：

| 命令/按键 | 目标行为 |
|---|---|
| `/agents`、`/agents list`、Ctrl+T | 只列当前 chat 的 child；行内显示 state、short id、name、model@gateway、更新时间与 pending inbox |
| Space / `/agents peek <id>` | 有界查看最新 child transcript，不改变输入目标 |
| Enter / `/agents attach <id>` | attach 后 prompt 明示 `agent <id>›`，footer 同时保留主 chat 属性和 child 状态/route |
| Esc、列 0 Left、`/agents detach` | 回主输入框，恢复 attach 前的主草稿、历史和 queue，不取消主/child 工作 |
| `/agents send <id> <text>` | 向 child inbox 追加一条独立消息；completed/waiting child 在后台恢复同一 transcript |

attach 期间 Enter/Tab 都定向到 child inbox，主会话 steer/next-turn queue 保持原样且不与
child pending inbox 混显；slash command 仍由主 CLI 处理。panel 和后台 child 都不能直接
写 terminal，所有 lifecycle/result 先回到主线程再投影。`/new`、`/resume` 在 lease 与
controller 切换提交后才 detach 旧 runtime，防止失败时破坏旧 chat，也防止旧 target
串入新会话。M3c-4 也已完成 Bash chunk scrollback、Ctrl+B
后台化与 task 管理。M4 session picker/lease/cwd-safe resume、fork、checkpoint 与
rewind，以及 M5 metrics/router、M6 sandbox adapter 均已完成。自动跨模型 fallback
仍保持关闭：显式选择可以带警告越过 circuit，但不会在用户不知情时换模型继续。

## 跨会话只读咨询

`/consult` 让当前 Chat A 向另一个已保存 Chat B 的最后一次完整落盘快照提问，Chat A
始终留在前台。Zylab 不会 lease、resume 或写入 B；它复制 B 的 messages/context，固定
使用 B 保存的 `model@gateway`，并以**空工具集**运行，因此咨询线程不能读当前文件、联网
或修改任何项目。

每次首次咨询生成稳定的 `cs-*` id，私有副本与 provenance（来源 session、保存时间、
消息数、route、SHA-256、lease freshness）原子保存在 `agent-runs/<cs-id>/state.json`，
权限为 `0600`。follow-up 只延续这份私有副本，不重新读取、更不会把内容写回 B。若 B
捕获时仍由其他终端持有，UI 会明确标注它只包含最后已保存内容；lease 无法核验时显示
`freshness unknown`，而不是把未知误报成 inactive。

```text
/consult                               # 选择 Chat B，再输入问题
/consult <session-id> <question>       # 直接启动
/consult list                          # 当前 Chat A 的咨询线程
/consult status [cs-id]                # route、快照身份与状态
/consult open [cs-id]                  # 查看独立 transcript
/consult followup <cs-id> <question>   # 延续同一私有副本
/consult cancel [cs-id]                # 取消当前 workspace worker
```

直接 `/consult` 是用户显式操作；模型调用同等能力的 `consult_session` 工具默认需确认。
完整 B transcript 会在本机形成一份 `0600` 副本，并发送给 B 保存的 provider；这是有意的
数据流，不应把含不适合发给该 provider 的会话作为目标。CLI 退出会取消正在运行的本地
worker，但 `cs-*` 记录仍保留，之后可用 follow-up 在同一私有副本上继续。

## 异构 workflow

**编排归模型，执行归框架**（2026-09-04 起）。主模型通过 `workflow` 工具提交一个自由 DAG：
每个节点一段完整的只读任务、一个席位、`depends_on` 表达真实依赖，下游节点自动收到上游报告
（每份 ≤6000 字）作为 context。审查、汇总都是普通节点，不再有独立的 review/synthesis 阶段。
席位池只有四家旗舰：**Kimi、GLM、DeepSeek、Qwen(3.8-max)**——不必全用，同一席位可派多个
节点；未指定席位的节点轮流分到不同席位。异构是加分项而不是门槛：不同模型的交叉验证与不同
观点/侧重的碰撞是这个工具的价值，所以 ≥3 个节点的 DAG 至少要用 2 个席位，2 个节点允许同
席位（启动通知会提示"没有异构交叉验证"）。节点数按任务定，只受 `max_nodes`（默认 16）与
provider 请求预算（默认 24，模型可显式提高到 48）约束。席位来自显式白名单，同时要求
`tools ok` 与 route circuit closed；某席缺席时不会偷偷降级到 flash、mini 或较弱模型。
`/workflow models` 显示候选模型与实际网关。

**席位可配**：`/workflow seats` 查看席位、来源、实际路由与候选；`seats add` 从已探测可用（tools ok）
的模型里选一个并起名（也可 `seats add MiniMax deepinfer/minimax-m2.7`），`seats remove` 从当前席位里选
（或 `seats remove Qwen`），`seats reset` 恢复内置四席。改动写入用户级 `settings.json` 的
`workflow.seats`（只记变化：`null` 表示移除，列表表示替换/新增）并立即生效——工具 schema 的席位
枚举与配方校验都跟着生效席位表走。项目级配置只能移除席位，不能新增。

**启动前探活**（settings `workflow.preflight`，默认开）：driver 对 plan 用到的每个 model
route 各发一次真实小请求，坏的按该席位候选顺序换下一条并写回节点；某席位全坏就在花任何
预算之前把 workflow 标失败并点名，结果记在记录的 `preflight` 里。平台不稳时，这一步省掉的是
整组半途而废。预算耗尽或部分节点失败时，已产出的报告随失败一并带回主窗口，
`/workflow apply` 也能把它交给主 agent。

配方（`/workflow recipes|run|save`）是给模型看的参考编排：模型有只读工具 `workflow_recipe`，`list` 列配方、`show` 把配方展开成可直接交给 `workflow` 的 agents（配方里的 review/synthesis
也展开成普通节点）；你说"用 research-review 配方跑一次"，模型展开后再按任务增删节点。
观测与控制也并入 `/agents`：`/agents` 顶部一行 workflow 摘要，`/agents workflow
status|console|open|pause|resume|cancel [id]` 与 `/workflow` 同名子命令等价。Ctrl+T 或裸
`/agents` 打开的面板在 workflow 活动时是 **DAG 视图**：头行（节点 done/total、预算、探活结果）
+ 按依赖层级排列的节点行（角色图标、`← 依赖`）+ 未启动节点占位 + 其他 child；开着时每秒
刷新、光标按 id 跟随；头行 Enter 打开 console，`p`/`r`/`x` 暂停/恢复/取消，`/` 进入筛选。

默认全局并行不超过 3 个、同一 gateway 不超过 2 个，并错峰启动；单 child 默认最多 5 次
provider attempt，整个 workflow 默认最多 24 次，避免早启动的工具循环耗尽其他席位的预算。
所有 workflow child 在代码层只拥有 `read_file/list_dir/glob/grep`；主 chat 是唯一写入者。
完成后汇总报告作为一条正常 user prompt 排进主 chat（`auto_apply`），否则 `/workflow apply`
手动交接。

```text
# 手动模板启动（/workflow <goal>、quick|deep、review|research|debug）已退役：直接告诉模型
/workflow auto on                      # 当前 chat 允许主模型自主启动 DAG
/workflow auto status                  # 查看开关与 nodes/request 硬上限
/workflow seats [add|remove|reset]      # 查看/修改席位（用户可配，二级指令选择）
/workflow status [id]                  # 当前进度与各 child 状态
/workflow console [id]                 # DAG、attempt/token/time budget 与事件
/workflow open [id]                    # 打开 child threads，可 peek/attach
/workflow pause|resume [id]            # 安全边界暂停/恢复
/workflow restart <node> [cascade]     # 显式重跑节点；完成节点需级联确认
/workflow send <node> <message>        # 给 running node 追加线索
/workflow add <json-array>             # 运行中动态追加经校验的只读节点
/workflow recipes                      # 列出 builtin/user/project recipes
/workflow run <name> key=value...      # 参数化执行纯 JSON recipe
/workflow save [id] <name> [scope]     # 把已运行 DAG 保存成 recipe
/workflow cancel [id]                  # 取消整组
/workflow apply [id]                   # 手动把汇总报告交给主 agent
```

`auto` 默认关闭且按 chat 保存；关闭时 provider 连 `workflow` tool schema 都看不到。开启后，
主模型只应在任务能拆成真正独立的调查、且值得多次 provider 请求时提交 DAG。代码层负责
循环/悬空依赖、异构软门槛、同 chat 单 active workflow、并发配额和全生命周期 provider-attempt
budget；同一 turn 重试同一 tool call 只返回已有 workflow，不重复启动。运行中的主 agent 只能
调用 `status/add/send` 三个非破坏控制；pause/cancel/restart 始终是用户控制。默认硬上限为
16 total nodes、3 并行、同 gateway 2 并行、24 provider wire attempts、150 万 observed tokens 与
30 分钟。这里的 attempt 包括工具循环后的下一次请求、context compaction、temperature fallback
和瞬时故障 retry；每次都在 HTTP 出网前通过同一个原子计数器预约，达到上限后 fail closed。
用户配置可在代码 hard ceiling 内调整；项目配置只能收紧。

每次启动都固定一份带 SHA-256 的 Context Capsule：目标、当前计划、有效 compact 摘要、
最近 user 原话、指令摘要和 memory index 被有界交给 child；它只作为 derived clue，不能
覆盖当前仓库或工具证据。recipe 是 JSON 数据，不执行 Python/shell；同名来源不静默
shadow，参数只做字面 `${param}` 替换，保存时拒绝 symlink 越界和受保护路径。

启动时各节点依次点亮并汇聚成 `◆`；动画只占一行，结束后不污染 scrollback。
非 TTY、`TERM=dumb`、`NO_COLOR` 或 `ZYLAB_MOTION=0` 时自动使用静态反馈。
workflow 状态、DAG 节点报告和 provider-attempt 计数原子保存在
`~/.zylab/workflows/`。正常退出会暂停当前 DAG；再次 `/resume` 同一 chat 时复用已完成
节点，只重排中断节点，并继续沿用原 provider-attempt budget。旧版 active state 恢复时，
历史 child-launch 计数按保守已用额度保留，不会因此超额。进程若恰好在 provider request
中途崩溃，无法证明上游是否已经处理该请求，因此恢复属于**有界 at-least-once**：该请求
已经计入 budget，中断节点最多按 `max_node_attempts` 再试，绝不无限重放。

## 文件安全

`write_file` 和 `edit_file` 改动前自动把原文存进 `~/.zylab/backups/`
（时间戳带亚秒，同一秒连改两次不会互相覆盖）。`/backups` 查看。
原版是直接覆盖、无备份 —— 模型改错一个大文件就没了。

批准 `write_file`/`edit_file` 前会显示带行号且有界的真实 diff；同一份原文同时成为
content-addressed checkpoint before-image。执行前若文件身份或 SHA-256 已变化，写入
会拒绝而不是覆盖外部修改。`/rewind` 可恢复 conversation、code、both，或从 checkpoint
创建 branch、将前缀摘要为 projection；恢复新建文件时移入私有 trash，不直接删除。
Zylab 自身的并发 write/restore 共用 canonical-path 跨进程锁；目标写入通过验证过的
稳定 parent dirfd 完成。多文件恢复失败会补偿已恢复项；补偿失败则明确记录 partial
outcome。`both` 先创建 conversation branch，因此 branch 创建失败不会先改代码。

边界：checkpoint 只覆盖 `write_file`/`edit_file` 和 mode，不覆盖 Bash、外部进程、
owner/ACL/xattr；新文件父目录必须已存在。conversation rewind 会创建 branch，原 session
和 raw transcript 保持不变。

## 上下文管理

三级，先廉价后昂贵，而且都只改变本次 provider 投影，不改 raw：

**工具结果老化**（本地、免费）——最近几个 assistant 轮次的工具结果**原样**保留（工具在
捕获时各自截过一次，上限 30,000 字符；`read_file` 按整行分页并给出接着读的 offset，不在
文件中间挖洞）；更早的长结果投影成带工具名、参数摘要、状态、首尾 preview 和 SHA-256 的
记录，末尾写着取回原文的办法：`expand_output(target="sha256:…")`——target 照抄即可。
原始 tool message 仍逐字留在 session/event journal，完整协议对也不会被拆开。老化的窗口
与力度随模型上限放宽（256K：3 轮 / 400 字符；1M：11–12 轮 / 约 1,500 字符）。

**模型自己的工具参数从不改写。** 早先旧轮次的长参数也会被收成「头 200 字 + 占位符」。
2026-09-20 在 7 个真实会话（2,925 次请求）上重放：它只省 4.3% 的输入 token，却是 5 起
「模型模仿占位符」事故的全部来源——占位符出现在模型自己的口吻里，它就学着写（一份文档
因此丢了 2/3）；结果占位符是环境的口吻，525 处零模仿。工具层的闸仍在：有副作用的工具
（`write_file` / `edit_file` / `bash` / `memory_write` …）拒绝带投影占位符的参数，并告诉模型
「这不是内容，把完整的真实内容写出来」；只读搜索类不拦。

**为什么保留滚动老化、不改成「捕获时截一次、此后不动历史」**（Claude Code / Codex 的
做法）：同一次重放里，不老化会让输入 token 多 27.5%（1M 档）/ 14.1%（256K 档），换来的
只是每次请求少重新预填充约 2,000 token——按实测首字回归，在 Boyue 上约 0.07 s（而上下文
变大反而多 0.5 s），在 DeepInfer 上约 0.6 s；按「缓存价 = 原价 10%」折算，老化仍便宜 6–12%
（256K 档 / 1M 档）。攒到高水位再成批老化两头不讨好（token +13.6%，预填充只少 18%）。

**自动压缩**（需要一次 API 请求）——老化后仍超过可用预算才摘要较早的历史。尾部按
**token 预算**保留（阈值的 1/8，夹在 4K–40K 之间），粒度是**模型轮次**，能落在用户轮次起点
就落在那里——自治程度再高、用户轮次再少的会话也压得动；user 原话无论在不在尾部都逐字
回放。默认可用预算是 `模型上限 - min(64k, 上限×15%)`，并随模型切换，不再使用 180k 硬上限。
`/compact <要求>` 可以告诉摘要器这次要特别留住什么。

单次摘要请求的**输入**有上限（`COMPACT_SOURCE_TOKENS`，默认 60k），超出就二分切到
完整轮次边界，剩下的靠摘要链在下一段接着压（上一段摘要是下一段 source 的第一条），
一次最多连压 `COMPACT_MAX_PASSES` 段。**一旦开始压就压到阈值的一半以下**
（`COMPACT_DEEP_FRACTION`）而不是「刚装得下就停」：长会话一段压不完，停在阈值边上等于
几万 token 之后再压一次，而每次压缩都要把整个尾部重发一遍。触发点不变，仍是阈值本身
——压得更深不等于压得更早。摘要请求本身按三步降级：**关思考**（
`thinking: {"type": "disabled"}`，reasoning 模型会把思考也算进 `max_tokens`，额度小
时正文一个字都出不来）→ **大额度**（网关忽略 thinking 时）→ **换席位池里另一个模型
代写**。不许关思考的模型（如 glm-5.3@boyue）被 400 拒过一次就记住，此后直接从默认思考 +
大额度起步。压缩落地时界面直接报 `215,315 → 31,931 tokens`，代写的话注明是谁写的。

**摘要要过验收才装得上去**：被截断的、陷入重复循环的（压缩比判据）、七节结构残缺的一律
拒收并走下一级降级——以前只要非空就接受。连续失败 3 次后自动压缩歇 30 分钟（换模型或
手动 `/compact` 立即恢复；状态栏显示「自动压缩暂停」），同一份计划失败后只歇 5 分钟，
不再是「失败一次永不重试」。

摘要随身带回三样东西：`<working-set>`（被摘要那段读过 / 改过的文件清单）、
`<pinned-facts>`（阈值、硬约束、否决记录的**逐字原文**，跨代继承并做确定性保真自检），
以及压缩那一刻采下的**工作现场**——最近动过的至多 5 个文件的当前内容（改过的优先，
过与 `read_file` 相同的读闸，单文件 16K、合计 48K 字符）和未完成的任务计划。内容冻结在
摘要记录里，投影因此是确定的，不会每次请求都去读盘。

摘要失败时保留上一份有效摘要；没有有效摘要时按完整轮次生成明确的 deterministic
omission projection。raw 正文和所有 user 原话都不删除、不截断；若不可裁剪部分本身
仍超预算则 fail-closed，并让用户缩短输入、手动 compact 或换更大窗口模型。

**分层长期 memory**（本地、带来源）——canonical transcript 仍是事实权威；memory
保存用户显式 `/memory remember`、模型调用 `memory_write` 写下的 `explicit` 条目，
以及**轮末自动抽取**的 `auto_extract` 条目。条目分四类（`type`）：
`user`（用户是谁与长期偏好）、`feedback`（对工作方式的要求）、`project`（在做的事与约束）、
`reference`（外部资源指针）——与 Claude Code 同一套口径。

**索引是钩子，正文按需取。** 注入 system 的 `<memory-index>` 每条只有一行（id · 类型 ·
scope · 标题 — 一行钩子，约 68 token），模型觉得相关就用 `memory_read` 取正文（单条
4,096 字节封顶）。以前是把每条**正文**全量塞进 system 而模型没有取回的手段：实测 7 条
自动 handoff 就占掉 4,339 token。钩子里必须带上以后会用来找它的词（中文任务写中文）——
2026-09-20 在一份 59 条的真实记忆库上量过：按钩子检索 top-3 命中 16/18，按正文只有 11/18。

**轮末自动抽取**（`memory.generate`，默认开）——一轮结束、静置 60 秒且确有新内容时，
在后台发**一次与主循环共用前缀的旁路请求**（同 recap 的缓存安全形状：同 system、同工具
定义，工具照发但不许用），让模型把这次会话里值得跨会话保留的东西写成条目。压缩过一段
则必抽一次——那段细节马上就只剩摘要了。

为什么不指望主模型顺手写：`memory_write` 一直都在、系统提示也写清了该记什么，2026-09-20
统计 7 个真实会话——2,925 条助手消息里调用 **0 次**。Claude Code / Codex / ZCode 也都是
另起一个抽取通道。实测（deepseek-v4-flash@deepinfer，真实会话）3.5–24 s、约 700 输出
token，写出来的是「Triton 练习环境在哪、无 GPU 怎么验证、ptxas 不在 PATH、跨 program
归约会互相覆盖（实测 err 29.4）」这类东西——对照被它取代的确定性 handoff 写出来的
「用户目标：好的」。

抽取的输出一律当成**可能残缺**的：实测有模型在最后一个 `}` 之前停住、也有模型写成散文并
撞满额度。解析是「逐个对象抢救」，救回几条算几条；一条都救不回来就安静地什么都不写——
记错比没有更糟。落盘只进 **project 域**（global 会跨项目注入未来所有会话，必须人逐次确认），
`kind=auto_extract` 留痕，界面回执一行，`/memory forget <id>` 撤销，`/memory capture`
可以立刻抽一次。同名条目靠 `stable_key` 原地更新，不会攒出近似副本；模型点名要更新的
条目若是人写的，一律不覆盖。project 写入默认允许并在 UI 留痕；
global 写入与所有 `memory_forget` 使用逐次 hard confirmation，且 auto/yolo/allow/session
grant 均不能绕过。global 与当前 repo/cwd project 隔离，
同一 `stable_key` 原地更新，
文件锁 + atomic replace 支持多个 terminal；条目记录 source session、evidence 与更新时间，
注入前受条数/字符双预算限制。content/title/stable key 都先做本地 secret redaction；
超长、非法 scope、磁盘或 authority 错误原样返回模型。自动 capture 不调用 provider，
不额外消费 token；`/memory use|generate on|off` 可按 chat 控制。损坏 schema、project
identity、重复 id 或 symlink authority 会 fail-visible，不会静默吞掉。

**Context Capsule**（child/workflow 交接）——从当前任务计划、有效摘要、最近 user 原话、
指令和 memory index 生成确定性有界 JSON；SHA-256 在创建与再次注入时复核，已知环境
secret 与常见 token 形状先脱敏。`/context` 同时展示 raw/compact/memory/capsule 四层；
`/memory capsule` 可检查来源 hash 和实际大小。

**渐进 skills**（本地、prompt-data only）——启动或切换 cwd 时只把
`name | scope | description | SKILL.md path` 的有界索引放进 system prompt，
正文不常驻；模型仅在 description 与任务明确相关时通过现有 `read_file` 按需读取。
来源为内置 `skills/`、项目 `.zylab/skills/`、用户
`~/.zylab/skills/`，同名按 user > project > builtin 覆盖且冲突可见。
skill 不能注册工具、权限或 hook，也不能自动执行脚本；`/skills` 只读列出来源、
描述和正文字节数。索引最多 64 项，`--no-skills` 可独立关闭；
`--no-user-md` / `--no-project-md` 分别关闭两层指令，`--no-md` 仅是后二者
的兼容别名，不会顺带关闭 skills。

`@路径` 会先创建 `0600` 的 content-addressed snapshot，再在 provider 投影中展开；原始
session 只保存用户输入和 hash manifest，不塞 base64。UTF-8 文本单文件最多读取 20 MiB，
投影超过 20 万字符时保留首尾并插入显式截断标记；同一轮文本投影合计最多 40 万字符。
PNG/JPEG/GIF/WebP 最多 20 MiB，只有能力表明确 `supports_image=true` 才会发送，并用
明确的 2,048 tokens/image 保守估算显示在 `/context`。指向受保护路径的 `@` 引用、symlink 和常见
凭据路径默认拒绝；snapshot 每次发送前复核 SHA-256。

用户可把 Markdown 放进 `~/.zylab/commands/*.md`，项目可放进
`.zylab/commands/*.md`。它们只做 `$ARGUMENTS` 字面 prompt 展开，不能注册工具、执行
shell 或改变权限；内置命令永远优先，用户级同名命令优先于项目级，冲突在 `/commands`
中可见。`/commands refresh` 无需重启即可刷新命令面板。

## 仓库导航与架构 enrichment

Graft 是默认的按需结构导航层。可信 executable 存在时，模型会直接看到五个
`graft_*` 工具，无需用户先运行 `/graft open`：陌生仓库用 `find_code/file_api` 缩小读取，
穷举迁移面用 `find_all`，重构前用 `trace_calls`，只有需要全局方向感才用 `repo_map`。
首次调用懒构建，之后复用索引；`/graft status|build|rebuild|doctor [path]` 只负责人工观察、
预热和诊断。索引固定在 `~/.zylab/graft/<repo-hash>/`，不改项目文件，也不常驻注入
context（`/context` 显示为 0 injected chars）。

zylab 通过 Graft 的 `--hosted` 模式关闭 upkeep、更新检查、telemetry 和 agent-facing
提示注入；结构构建和查询不调用 provider，并强制运行在独立 Linux network namespace
中。若 `unshare --net` 能力不可用，五个工具会 fail-closed 地从模型 schema 消失，不会
退回到可联网执行。项目配置只能关闭或收紧文件/字节/结果/超时
上限，不能开启、放大、替换 executable 或重定向 cache。所有 target、file 和 scope
都经 realpath 限定在当前 workspace，受保护路径、凭据路径、隐藏状态目录和越界 symlink
会被拒绝。返回内容会去掉终端控制字符并标记为
`repository-derived untrusted data`；动态调用、反射和生成代码仍必须用源码与测试复核。

Graft 不是 vendored Python 模块，而是同一持久化根上的受管可选组件。
`graft-component.lock.json` 固定 Node 版本/SHA-256、Graft Git revision、`package-lock`
和已构建 CLI；`python3 scripts/manage_graft.py check` 做只读核验，`repair` 只会在所有
前置校验通过后原子重建 `~/.local/bin/graft` launcher。pod bootstrap 会运行这条离线
repair，但绝不 clone、下载、`npm install` 或改 Graft checkout。组件缺失/漂移时核心
zylab 继续工作，五个 `graft_*` 工具 fail-closed 隐藏；`/graft doctor` 同时展示
组件 lock 与运行时 network namespace 两层状态。维护流程见 `docs/graft-component.md`。

旧 `/map` 采用软弃用策略：实现、注册和二级/三级参数提示均保留，但顶层发现入口已隐藏。
它仍内置默认 `use=false`、`use_architecture=false`，不会自动构建、刷新或注入每轮 context。
需要兼容旧工作流时可显式输入 `/map build|refresh|check|viz`，生成原有确定性
Python/import 导航；每次显式调用都会提示迁移到 `/graft`。这样旧脚本不会失效，
同时普通新任务不会误把两个导航入口当成同一套功能。

`/architecture` 是可选的 provider-backed enrichment，不是 `/map` 的隐式第二阶段。
`/architecture build` 出网前先显示文件数、摘要 cache hit/miss、预计请求上限以及固定的
`model@gateway`；构建期间显示摘要/合成进度，Esc 可取消。逐文件摘要边挣边原子落缓存，
合成原始输出留在 `.zylab/map/.cache/last-synthesis.txt`。完整结果先写不可变
generation，再原子切换 `architecture/current.json`；同 repo 只允许一个 writer，
崩溃或取消不会切换旧 generation。节点 `Notes` 在首个请求前做有界快照并逐字迁移；
构建期间若被编辑则拒绝切换，保留用户新笔记。architecture 通过独立的
`<architecture-index authority="untrusted-derived-repo-data">` 注入；只有用户显式
`/architecture build` 才会产生 provider 请求。

## 交互（raw-mode，纯标准库）

二级命令若声明了有限参数，继续输入空格会进入三级参数菜单；每项同时显示说明，
并且只补全文本，不执行操作。例如 `/workflow auto ` 会提示 `on`、`off`、
`status`，`/memory use ` 会提示 `on`、`off`、`status`，`/goal set ` 会提示
`--rounds`。选中后仍可继续输入其余自由参数；直接输入 `/goal <objective>` 不受
菜单影响。会话运行后，`/workflow`、`/agents`、`/consult`、`/memory`、`/task`、
`/rewind` 等需要 ID 的参数会从当前本地状态中增量补全；recipe 同名时显示
`builtin:name` / `user:name` / `project:name` 以避免歧义。该发现只读、有界、节流，
不读取或显示 prompt 正文，也不替代命令处理器的权限与参数校验。

| 操作 | 效果 |
|---|---|
| 输入 `/` | 立刻弹出命令面板；精确命令 Enter 提交，前缀可 Tab/Enter 补全 |
| 输入 `/cmd ` | 若该命令有二级操作，弹出带说明的子命令列表；有枚举参数时继续输入空格显示三级参数；可用前缀筛选，或忽略列表继续输入自由参数 |
| 输入 `@路径` | 在同一菜单中有界补全；文本/图片按上述私有 snapshot 规则投影 |
| 自定义 `/name args` | 作为普通 prompt 排队；queue 与 scrollback 保留短 invocation，不展示展开全文 |
| `/model` 不带参数 | 同一 InputPump 的上下键选择器，支持增量筛选和滚动 |
| 常驻 composer | 圆角边框、内边距和焦点色；中文/emoji 按显示列换行，queue 在框上方，完整 chat 属性在 footer |
| queued prompt 到达交付点 | 从 composer 移入 scrollback，作为独立 `You` prompt card 显示且只显示一次 |
| assistant Markdown | 标题、引用、嵌套/任务列表、表格、链接和围栏代码；常用语言零依赖高亮。表格按内容定列宽、放不下时单元格内折行，不截断 |
| `/resume` | 直接用正常 prompt card/Markdown 接回最近一屏，完整会话按需上翻；不插入“历史回放结束”组件 |
| 翻阅历史（滚轮 / 滚动条 / Shift+PgUp） | 终端原生：transcript 就在终端 scrollback 里，内联模式不接管鼠标；`--resume` 走同一套实时渲染，像从没退出过 |
| 鼠标 | 内联模式（默认）不启用鼠标上报；`--app` 全屏模式才由 TUI 处理滚轮与选区 |
| 选区与复制 | 终端原生拖拽与复制（快捷键随终端，Cursor 为 Ctrl+Shift+C/V）；`--app` 模式下应用绘制选区并经 OSC 52 复制 |
| `/mouse` | 内联模式只提示"不接管鼠标"；`--app` 模式 `/mouse on\|off` 切换 |
| tool/subagent 运行中 | TaskManager 每 ≤25 ms 交还主线程；footer 显示 task id、状态和输出量 |
| 输入时按 **Shift+Enter** 或 **Alt+Enter** | 在当前光标插入逻辑换行，不提交；普通 Enter 仍是唯一提交键。**多数终端上能直接用的是 Alt+Enter** —— 见下方说明 |
| 模型工作时按 **Enter** | 保存为 steer，在纯文本结束或工具批次结束后的安全边界交付 |
| 模型工作时按 **Tab** | 保存为独立的下一轮输入 |
| 模型工作时按 **Up** | 取回最近一条尚未 dispatch 的 queue item 继续编辑 |
| 工作时 `/usage`、`/cost`、`/context`、`/model health` | 立即显示，不进入 prompt queue，也不打断当前 turn |
| 工作时 `/model`、`/model gateway` | 立即接受选择并在 footer 标记 `next`；当前 turn（含工具循环）保持原 route，结束后、下一条 queue 请求前生效 |
| 工作时 `/auto` | 立即切换，但只影响尚未开始的后续审批；不会追溯批准当前 modal 或已运行工具 |
| provider 工作时按 **Esc** | 先记录取消意图，再关闭 response；queue、草稿和会话协议保持完好 |
| foreground tool 工作时按 **Esc** | 先记录取消意图，再取消 task；Bash 信号发给整个进程组 |
| foreground Bash 工作时按 **Ctrl+B** | 转为 managed background task；立即闭合唯一 tool result，provider 继续；任务结束后结果自动排成一条 prompt 交回模型继续（settings `background_task_handoff` 可关） |
| attached task view 按 **Ctrl+B** | detach live output view；不终止后台任务 |
| `/tasks` | 显示 task 的 id、mode、status、elapsed、output size、name 与进程 outcome |
| `/task <id>`、`/task detach` | 非破坏 tail；运行中的后台任务可 attach/detach live output |
| `/expand [tool_call_id\|task_id] [page]` | 按 30 KB 页展开完整工具 artifact；缺失时明确降级到会话内有界结果 |
| `/kill <id>` | 显式终止指定 managed task |
| `/status` | 不打断当前 turn，显示 session/route/queue/task/context、Git branch/dirty、最新 request phase 与 sandbox marker |
| `/trace [turn]` | 显示 provider attempts 与 tool runs，含错误、耗时、approval、exit/signal 和 artifact path |
| `/usage`、`zylab --usage` | 分开展示兼容 JSONL 汇总与 M5 measured/unknown coverage，不重复相加 |
| `/model health` | 显示 probe freshness、成功率、TTFT/total/EWMA、circuit 与 context 证据 |
| `/doctor` | 只读检查 DB、sandbox/network capability、gateway/transport、terminal 与 session inventory |
| `/rewind` | 预览并恢复 checkpoint 的 conversation/code/both，或创建 branch/精确摘要前缀 |
| 空闲时 ↑↓；Ctrl-A/E/U/K/W | 翻历史；常规行编辑 |
| child dock 点击；Ctrl+T 或 `/agents` | dock 直接 attach 当前 turn 的 child；panel 可查看全部 child，Enter attach，Space peek |
| attached composer | Enter/Tab 发给 child；Esc 或列 0 Left detach，主/child 草稿、历史和 queue 隔离 |
| 裸 `/resume` | 打开持久 session panel；默认只看当前 repo 的 active 会话 |
| session panel 输入 `/` | 进入筛选；Esc 先退出筛选并保留 panel，再按 Esc 才关闭 |
| session panel | Enter resume；Space preview；R rename；A archive；U unarchive；Tab 切 active/archived |
| session panel Ctrl+A / Ctrl+B | 切所有项目 / 当前 branch；切换后选择按 session id 保持 |

Markdown 只在交互 TTY 中渲染；`-p` 和管道继续输出原始 Markdown，便于脚本消费。
渲染器按终端显示列处理中文、组合字符、emoji、tab 和 ANSI，模型文本里的 CSI/OSC/
DCS 控制序列会在跨 chunk 状态机中移除，不能改标题、清屏或移动光标。Markdown 颜色遵循
`NO_COLOR`；可用 `ZYLAB_MARKDOWN_THEME=dark|light` 选主题，
`ZYLAB_HYPERLINKS=0|1` 显式关闭/开启 OSC-8 链接。chat chrome 同样遵循
`NO_COLOR`。内联模式（默认）不启用鼠标上报：滚轮、滚动条、拖拽选择、复制全部由终端原生处理，
transcript 留在终端 scrollback 里。只有 `--app` 全屏模式抓鼠标：应用绘制选区，`Ctrl-C` 通过
OSC 52 请求 terminal 写入剪贴板，`Shift+拖拽` 交给终端原生选择；该模式下 `/mouse on|off`
可随时交还/收回鼠标。若本地桌面剪贴板可用，可设置 `ZYLAB_CLIPBOARD=auto`，或显式使用
`external`（依次尝试 `wl-copy`/`xclip`/`xsel`）；`osc52` 是默认后端，`off` 可禁用复制。
非 TTY 或 `TERM=dumb` 始终不启用鼠标。

**先说结论：想在输入框里换行，直接按 `Alt+Enter`。** 它在绝大多数终端上开箱可用，
不需要任何配置 —— 因为「Alt 键发送 Escape 前缀」是终端的普遍默认行为，于是
Alt+Enter 发出的是 `ESC`+`CR`，zylab 识别为换行（`core/tui.py` 的
`_modified_key_name`：codepoint 13 且带 shift 或 alt → `shift-enter`）。

`Shift+Enter` 则**不一定能用，而且这不是 zylab 能单方面决定的**。终端里
Shift+Enter 没有统一约定，多数终端把它和普通 Enter 都发成同一个 CR，应用侧
收到的字节完全相同，无从区分。zylab 能解码 Kitty/CSI-u、xterm
`modifyOtherKeys` 和 `ESC+LF/CR` 三种形式，但前提是终端愿意发其中之一。

想让 Shift+Enter 也能用，需要在终端侧配置，例如 VS Code 把 `shift+enter` 的
`workbench.action.terminal.sendSequence` 配为 `{"text":"\u001b\u000a"}`；
zylab 会把它识别为换行，且不会改变中文输入路径。

第三条路：**在别处写好多行然后粘贴**。zylab 支持 bracketed paste，
整段粘进来是一条多行 prompt，不会被拆成多条。

这三件事在 `input()`（cooked mode）下**物理上做不到**：按键停在内核行缓冲里要等
回车；方向键是转义序列被 readline 自己消费了；流式期间程序根本没在读 stdin，
所以 Esc 无人接收（Ctrl-C 能用是因为它是 tty 驱动发的**信号**，不是按键）。
`core/tui.py` 用 `termios`/`tty` 手写了 raw mode 解除这三条限制。

> 不用 prompt_toolkit：它装在 `/usr/local/lib`（临时层），pod 重建即丢。
> 非 tty（管道、`-p`、脚本）自动回退到 `input()`，行为不变。

## 会话

每个会话一个 canonical 文件 `~/.zylab/sessions/<id>.json`；旁边的
`session-index.json` 只是可重建的元数据目录。`/new` 开新会话，`/sessions` 打印列表，
`/resume <序号|id>` 直接切换，裸 `/resume` 打开选择器，`/rename` 命名。选择器的列表、
搜索和首屏渲染只读元数据；只有 Space preview 或真正 resume 才读取目标 transcript，
因此会话数量增长时不会把所有历史正文一起解析。

标题不填就取第一条用户消息。resume 会恢复 messages/context 之外的 title、model、
gateway、plan mode、累计 token、最近 context 用量与同一 session 的 grant；旧记录缺字段时
显示 unknown/usable，不伪造成 0。保存 cwd 与当前 cwd 不同时，TTY 会明确选择使用
current、切换到 saved、fork 到当前 cwd 或取消；非 TTY 必须给
`--resume-cwd current|saved|fork`。选择 current 会把新 cwd 持久写回；选择 saved 前如果
仍有 active task/child agent 会拒绝切换。

每个 live session 都有跨进程 lease。第二个 terminal 可以运行 Zylab，但不能同时
接管并写同一个 session；picker 会标记 live 会话，损坏 lease 按 fail-closed 处理。
resume 会先拿目标 lease，再重读 canonical transcript，避免另一个 terminal 在选择器
打开期间完成最后一次保存后，本端仍恢复旧快照。会话写入使用 per-session 锁、唯一
临时文件、原子替换和 catalog dirty marker；canonical 成功但 index 更新失败时，下次
查询会自动重建目录。archive 只改变 metadata status，不删除或改写消息；在 panel 用
Tab 查看 archived、U 恢复。当前 live session 不能直接 archive。F 会从所选 canonical
快照创建新的 active branch 并立即进入；父会话不变，branch 在 picker 中按 root 分组并
显示 `↳`。child 继承 model/gateway/plan mode 与对话，但不继承 session grant、queue、
task 或 child-agent runtime；完整 event cutoff 只有在 SQLite journal 有真实 seq 时才记录，
JSON backend 另存准确的 `fork_message_count`。

如果 lease 文件本身损坏，Zylab 不会猜测所有权。确认确实没有其他 Zylab 在用
该会话后，运行 `/sessions repair-lease <完整 id>`，再输入一遍完整 id；原 lease 会被
移入 `session-leases/quarantine/`，不会直接删除。

开发验收脚本：`scripts/bench_tui_latency.py` 测真实 PTY 下 managed tool 期间的
key-to-paint；`scripts/bench_session_picker.py` 在真实 PVC 上生成 10,000 个 metadata row，
每个 measured trial 启动 fresh subprocess/REPL，并从 PTY 输入真实 `/resume`；2 次 warmup
后复用同一份 PVC page cache。门禁同时要求行数准确、绘制字节非零、0 transcript read
和 0 network call。2026-08-25 本次实测分别为 p95 25.250 ms（n=135，门槛 50 ms）和
p95 112.823 ms（15 trials，每轮 10,000 rows，门槛 200 ms）。

> 旧版只有单个 `last.json` 槽位、每轮覆盖，等于没有多会话。首次运行会自动把它
> 迁成一个正常会话文件。

## 用量记账

`/cost` 给本次会话的明细（输入/输出/缓存读写/上下文/时长）。成功并取得 usage 的
调用还会追加一行到 **`~/.zylab/usage.jsonl`** —— append-only，字段扁平，方便
旧脚本、`grep` / `jq` / pandas 继续消费：

```json
{"ts":"2026-08-21T09:04:20+00:00","session":"d0031b724e12","model":"kimi-k3-256k",
 "turn":1,"prompt_tokens":6433,"completion_tokens":42,"total_tokens":6475,
 "cache_read":0,"cache_write":0,"cache_reported":false,"gateway":"deepinfer",
 "secs":4.69,"tools":[],"cwd":"/home/you/zylab"}
```

`metrics.sqlite3` 在打开网络前为每个 retry attempt 建 intent，因而还覆盖失败、中断、
无 usage、真实 phase timing、tool approval/result 和 unknown measurement。交互
`/usage` 与 `zylab --usage` 都先展示兼容 JSONL 表，再单列 M5 attempt 的
`measured/unknown` 与 cache coverage；两张表可能重叠，绝不相加冒充总量。
`/trace [turn]` 展示 request/tool 时间线，`/status` 可在另一个 terminal 中查看最新
phase。

`/stats` 从流水重算 **`~/.zylab/stats-cache.json`**，字段刻意对齐 Claude Code 的
`~/.claude/stats-cache.json`（`dailyActivity` / `dailyModelTokens` / `modelUsage` /
`hourCounts` 同名同形），已有的统计脚本能直接复用；额外多一个 `gatewayUsage` ——
同一个模型可能同时挂在两个网关下，成本与配额不同，这是本项目特有的维度。
`usage.jsonl` 是这份兼容 stats-cache 的重算源；request/tool/model-health 的权威源是
`metrics.sqlite3`。stats-cache 是派生物，删了能重算。

**缓存字段的真相（2026-08-21 实测）**：各家格式不同，已在 `client.normalize_cache`
里归一（OpenAI 的 `prompt_tokens_details.cached_tokens`、DeepSeek 的
`prompt_cache_hit_tokens`、DeepInfer 的 `created_cache_tokens`、Anthropic 的
`cache_read_input_tokens`）。但**能不能拿到数，取决于后端**：

| 后端 | 缓存读 | 缓存写 |
|---|---|---|
| DeepInfer `kimi-k3-256k` | 不上报（`prompt_tokens_details` 是 `null`） | 不上报 |
| DeepInfer `deepseek-v4-flash` | 恒为 0 | 上报 |
| Boyue `deepseek-chat` | **如实上报**（重复请求实测 3584/3606 命中） | 上报 |

「不上报」不等于「没缓存」：DeepInfer 上同一 prompt 第二次的延迟从 2.7s 掉到
0.6s，缓存明显在工作，只是网关不给数字。面板会把这三种状态分开显示，不拿 0 骗人。

## 两条硬规矩编进了代码，不是写在提示词里

1. **受保护路径只读。** 哪些路径受保护由你声明，**默认一条都没有** ——
   不预设任何一台机器的挂载布局。启动前 `export ZYLAB_PROTECTED_PATHS=/data/archive`
   （`:` 分隔多条），或写进 `sandbox.protected_paths`；rclone 之类的对象存储远端用
   `ZYLAB_PROTECTED_REMOTES=archive:bucket`（同一份数据常有两道门，只守一道等于没守）。
   用途是那种**没有第二份副本**的目录：归档盘、只读挂载、已下线存储的最后备份。

   声明之后是两层守卫，契约按沙箱是否可用分开写 —— 以前只写了一句「全部拒绝」，
   而 bash 守卫其实只认字面量（`cd <受保护目录> && echo > f`、`tar -C <受保护目录>`、
   `python -c "open(...)"` 都穿得过去），2026-09-02 修正：
   - **沙箱内**：受保护路径以只读挂载进沙箱，这是硬边界。工具层的词法守卫
     （`_guard` / `_guard_bash`）是第二道门：重定向目标、mutator 目标位、
     `cd`/`pushd` 进受保护目录、`tar -x … -C` / `unzip -d` 的解压目录、
     `rclone` 写向受保护远端，相对路径一律按 cwd 解析，cwd 落在受保护目录内直接
     拒绝。读和「拷出来」放行。
   - **沙箱不可用（UNSANDBOXED）时**：词法守卫解析不了任意 shell（解释器内部写入、
     变量拼接的路径都看不见），所以对受保护路径的**任何引用一律拒绝，读和拷出也
     不例外**（`_guard_bash_unsandboxed`）。读用 `read_file`，拷出先修好沙箱。
     用户的一次 UNSANDBOXED 确认授权的是「没有沙箱也跑」，从来不是「放宽只读不变量」。
   canonical path 与 symlink 会被解析，项目配置、permission 或 hook 都不能放宽它。
2. **`grep` 的 exit 127 和 exit 1 分开报。** 「命令不存在」和「没搜到」配上
   `2>/dev/null` 长得一模一样，已经造成过一次错误结论（有些环境里 `rg` 是注入的
   shell 函数，没有二进制，子进程里直接 127）。工具层不让前者伪装成后者。

M6 在硬守卫之外再加独立 OS boundary。mount namespace 与 network namespace 分开
做真实 probe；只有实测成立才显示 `SANDBOXED` / `NET ISOLATED`。请求隔离但无法证明
时 fail-closed，绝不把“配置想隔离”渲染成“已经隔离”。unsandboxed direct runner
仍使用独立 process group、超时后清理 descendants、有界 head/tail capture 和跨 chunk
known-secret redaction；这些是进程管理与降泄露措施，不等价于 filesystem sandbox。

## 结构

```
zylab.py     REPL、渲染、权限提示、斜杠命令
core/client.py    多网关流式客户端、阶段时间与 cancellation handle
core/controller.py 串行状态机、输入队列与副作用前事件门控（M3a–M3c-1b 已接 REPL）
core/tasks.py     managed task registry、前后台切换、artifact spool、进程组取消与有界 UI ring
core/tools.py     工具 schema、硬守卫、sandbox、workflow control 与 TaskHandle 适配
core/graft.py       hosted Graft 信任边界、外部 cache、输入上限与 worker 适配
core/graft_worker.py 最小环境中的 build/query JSON bridge 与结果消毒
core/graft_component.py 可选 Graft sidecar 的 revision/hash 核验与 launcher 原子修复
core/agent.py       agentic loop、system prompt、不可变 raw 与 runtime events
core/context.py     请求前投影、工具协议单元、老化、摘要与预算解释
core/memory.py      global/project memory、确定性 handoff 与 Context Capsule
core/repomap.py     零-provider repo map、分代 architecture enrichment 与漂移检测
core/attachments.py @file snapshot、图片能力门控与 provider 临时投影
core/commands.py    prompt-only Markdown 自定义命令发现与字面展开
core/skills.py      builtin/project/user SKILL.md 发现、覆盖与有界索引
core/workflows.py   可恢复动态异构 DAG、预算、控制事件与 synthesis
core/recipes.py     builtin/user/project 纯 JSON workflow recipes
core/store.py       JSON 主存储 + session shadow + always-on metrics facade
core/state.py       SQLite schema、event journal、request/tool/health projection
core/migrations.py  v1 JSON/JSONL dry-run 与无覆盖迁移
core/models.py      模型能力表、probe freshness 与上下文实测
core/model_health.py TTL、circuit 与显式 route 决策
core/checkpoints.py diff/before-image/CAS/rewind 与 crash recovery
core/sandbox.py     mount/network namespace capability 与启动计划
core/tui.py         单 stdin InputPump、纯状态编辑器、modal 与主线程 Renderer
tests/            守卫与状态/迁移回归测试
```

## 模型能力表

`~/.zylab/models.json` 记录每个模型的：上下文、**是否支持 tool calling**、
**参数兼容性**、真实首响应/完整响应延迟、家族、所在网关。裸 `/model` 是唯一模型列表，
`/model refresh` 刷新当前 gateway 的目录，`/model health` 查看持久健康度；
交互式裸 `/probe` 会先打开成本明确的操作菜单，`zylab --probe-all` 才做批量探测。

```text
/probe                              弹出操作菜单；Esc 取消为 0 请求
/probe <model>@<gateway>            指定目标，但不切换当前会话
/probe status [target]              只读缓存，不发送请求
/probe context [target]             逐档探顶（多次大请求，有进度条）
/probe help                         完整用法
```

能力探测把首 delta、完整响应、整次 probe 和 attempt 数分开记录；失败保留 HTTP status、
provider code 与 request id。交互 `/probe all` 会拒绝执行并指向显式的
`zylab --probe-all`，避免误触发大量请求。旧缓存没有 timing version，`status` 会把
其中原先误记的 `first_token_s` 标成“旧版整段耗时”；重新 `/probe` 后才显示“首响应”。

裸 `/probe` 菜单提供：探测当前模型、从本地能力表选择其他模型、零请求查看缓存、
高成本上下文探测和帮助。显式 `/probe status` 或 `/probe model@gateway` 直接执行，
不会重复弹窗。

Context probe 使用独立的低输出请求：user content 由短 ASCII 单元 `" x"` 重复构成，
末尾只要求回复 `OK`，`max_tokens=4` 且不发送 `temperature`。默认按
`32k → 64k → 128k → 200k → 256k → 512k → 1M → 2M → 3M` 逐档扩张，
不再像旧实现那样在 `1M` 成功后直接写“未触顶”。进度条显示当前档位、已完成档位、
逻辑请求数和 provider 返回的实际 `usage.prompt_tokens`；没有 usage 时会明确标成估算。

只有 provider 明确返回 context-too-long 才记为“已找到拒绝边界”。若 `3M` 档仍成功，
由于两个网关约 6 MB 的 request-body 约束，只记录 `probed-floor` 和“模型顶未知”，
不会把客户端可测上限冒充模型上限。最坏路径为 9 档、约 7.2M 目标输入 tokens，
网络重试另计；瞬时网络/5xx 和 request-body 拒绝均记为受阻，不会写成模型 context。

能力 probe 与 context probe 使用独立 freshness：做过高成本 context 探测不会抑制
tool/temperature 能力复测。capability fingerprint 包含 gateway、协议版本和显式 catalog
revision；瞬时网络失败保留旧能力，只有 provider 明确拒绝 tool calling 才形成稳定
不支持结论。`/model health` 展示 freshness、成功率、true TTFT、总延迟、EWMA、
circuit 和 context 证据。

chat 请求和 context-compaction 的 transient tail 会驱动持久 circuit；只有 chat 请求
进入用户可见成功率/延迟计数。用户显式 `/model`、`/model gateway` 或启动参数可以带警告
越过一次 open circuit；自动跨 gateway/model fallback 仍关闭，避免副作用后静默换脑。

模型目录识别 **OpenAI / Anthropic / 智谱 / moonshot / deepseek / 阿里 /
intern / MiniMax**；OpenAI 与 Anthropic 在这里仍经 Boyue 转发，不是官方直连。
`/model` 对 Boyue 再做旗舰精选，并过滤 embedding / reranker / voice / image
等非聊天模型。

同名模型两边都有时**优先 DeepInfer**（免费、直连）；只有一边有就自动切网关。

### 实测结论（2026-08-21，32 个模型）

28 个可当编码后端。最快一档：

| 家族 | 模型 | 网关 | 上下文 | 首 token |
|---|---|---|---|---|
| 阿里 | `Qwen3.6-35B-A3B-FP8` | deepinfer | 262k | 0.3s |
| 阿里 | `qwen3.8-27b-fp8` | deepinfer | 262k | 0.4s |
| deepseek | `deepseek-v4-flash` | deepinfer | 1049k | 1.1s |
| moonshot | `kimi-k2-thinking` | boyue | 未知 | 1.7s |

排除项：`intern-s1` 能聊天但**不支持工具**；`glm-5.2-1m` 超时；
`qwen3.6-plus`、`opengvlab/internvl3-14b` 在 Boyue 返回 500。

**三个必须知道的坑**：

1. **列得出 ≠ 调得通。** `kimi-k3` 一度 404 却仍留在能力缓存里（后来又恢复）。
   能力表因此必须可刷新，不能写死。
2. **参数兼容性因模型而异，且会静默 400。** 当前世代 Claude 拒收 `temperature`。
   客户端撞到就自动去掉重发，并把这个事实记进能力表。
3. **`reasoning_effort` 在这两个网关上不可用。** 实测它被接受但有害：
   deepseek-v4-pro 加上后**产不出内容还烧光 token**；且没有任何模型返回
   `reasoning_content`。表达推理强度的正确方式是选 `-thinking` 变体模型。

## 旧版实测数字（2026-08-21）

编码 CLI 的前提是 tool calling。实测两个网关的候选模型：

| 网关 | 模型数 | 元数据 | tool calling |
|---|---|---|---|
| DeepInfer | 12 | 全（上下文/图/推理标志） | 实测 6/6 全支持 |
| Boyue | 405 | 只有 `supported_endpoint_types` | 抽测支持（含 claude-opus-4-8） |

Boyue 上有 `claude-opus-5` / `claude-opus-4-8` / `gpt-5.6-*` / `gpt-5.x-codex` /
`kimi-k2.7-code` / `qwen3-coder-plus` 等当前世代模型，但**不给上下文长度和能力标志**，
只能实测。DeepInfer 元数据全但只有 12 个模型。

## 网关与路由

一个网关 = 一个 OpenAI 兼容 endpoint + 它的 key 环境变量名。**地址不在代码里**，
按这个顺序解析（前面覆盖后面）：

| 来源 | 形式 | 用途 |
|---|---|---|
| `ZYLAB_BASE` | `https://<host>/v1` | 覆盖当前网关，对付临时端点 |
| `ZYLAB_BASE_<网关名大写>` | `ZYLAB_BASE_BOYUE=…` | 按网关各自指定 |
| `settings.json` 的 `gateways.<name>.base` | 用户级 | `zylab init` 写的就是这里 |

key 按 `ZYLAB_KEYS_FILE` → `~/.zylab/keys.env` → `~/.config/zylab/keys.env`
的顺序找，环境变量优先于文件。多配几个就能随时切：

```bash
ZYLAB_GATEWAY=boyue zylab
# 或会话内：/model gateway boyue
```

**请求由 zylab 自己发出，不继承 shell 里的代理变量。** 这是刻意的：企业网络里
默认导出的代理常常只放行一部分域名，打到自家网关上返回 403 —— 看起来像 key 失效，
实际是路由错了。这个坑排查起来极贵，所以客户端不碰环境里的代理设置。

**如果你的 endpoint 必须经代理才够得着**，显式声明一条：

```bash
export ZYLAB_API_PROXY='http://<user>:<pass>@<host>:<port>/'
```

只认这一个变量，**不读** `http_proxy` / `https_proxy`（理由同上），也不复用
网页抓取那条 `web.proxy` —— 这条路会把 key、prompt、代码和工具结果都送过去，
所以要你为它单独表态。值的形态不对（比如误填了一条 shell 命令）就当没声明、
照常直连，不会把一个非 URL 交给 urllib。凭据在任何输出里都会被脱敏成 `…@host`。

**明文 HTTP 的 endpoint 默认被拒绝**，因为 key、prompt 和代码都会明文过链路。
确实需要时用 `transport.allowed_insecure_endpoints` 精确授权那**一个** endpoint，
而不是 `--allow-insecure-http` 那个进程级大开关。

## 实测数字（2026-08-21 复测）

- **目录成员会来回抖动，方向是双向的。** `kimi-k3` 曾 404 下线，2026-09-08 实测
  又能调通；`kimi-k3-256k` 09-07 没出现在 `/v1/models` 里，09-08 又在，而且全程可调用。
  反过来 `deepseek-v4-pro` / `-pro-0813` 已从 DeepInfer 消失（403 model_not_available），
  DeepSeek 席位因此落到 Boyue。**列得出不等于调得通，列不出也不等于调不通。**
  `fetch_catalog` 会把目录里消失的记录标成 `listed:false` + `status:delisted`（不删除，
  回来时自动复位成 `listed`），席位解析只认 `status=="ok"`，所以不会再选中已下架的 id。
  默认模型 `kimi-k3-256k`，2026-09-08 目录标称 **280k**（早先记的 1,049k 与 262k 都已过期）。

**目录自适应**（三条证据链，互相独立）：

- **目录**（一次 HTTP，便宜）——若当前网关的目录超过 `catalog_ttl_hours`（默认 24）没抓过，
  就在后台抓一次，**只在有变化时才吭声**（`[模型目录 deepinfer 已刷新：新增 …]`）。
  判断发生在**启动时和每个 turn 边界**：会话可能开好几天（那个压缩坏掉的会话开了 12 天），
  只在启动时刷一次等于没刷；用 turn 边界而不是定时器，因为那既是「马上要用模型」的时刻，
  也天然低频，且没人用时不空转。turn 边界另有 5 分钟的内存节流，免得每轮都去读
  `models.json`。切网关会清掉节流，让下一轮立刻重判。网关不通就静默放弃，绝不影响会话。
  `catalog_refresh: false` 可关掉，回到纯手动。
- **真实请求的结果**（免费）——一次成功就是「现在可用」的硬证据，清掉不可用标记；
  失败只认 `model_unavailable` 这一类，而且要**连着两次**（client 已内部重试过）才把
  `status` 降到 `unavailable`，因为 DeepInfer 的单次 403/404 可能只是后端实例没挂载。
- **`/model check`**（贵，每个模型一次真实请求）——目录 + 实调两条证据都走一遍，只测
  「真的会被用到」的 route：当前会话模型加四个席位模型，然后报告与缓存的差异。

**本地传输策略拒发不是能力证据。** 明文 HTTP 未授权时 probe 会保留原状态并标记
`probe_blocked`，不会把健康模型打成 `error`——请求根本没出这台机器。
- **延迟：首 token 0.4s**（k3-256k）。早先记录的 k3「~20-24 s」已不适用；
  同期实测 `deepseek-v4-flash` 0.1–3.1s、`minimax-m2.7` 20.9s —— 慢是模型属性，
  不是网关属性，换模型即可。
- **上下文上限按模型走，不再写死。** 启动和 `/model` 切换时从网关的
  `max_model_len` 查，压缩阈值取 `上限 − min(64k, 上限×15%)`（旧写法是
  `min(180k, 上限×0.7)`，见「上下文管理」）。
  这修掉一个真 bug：`minimax-m2.7` 只有 166k，而旧的固定阈值 180k **高于上限**，
  压缩永远不触发，直接撑爆。
- **并行工具调用可用**：一轮里能同时发起多个调用。
- 免费，且 DeepInfer 平台有 per-model 监控。

## 已知限制

- `kimi-k2` 在这个 key 下 403，只开了 k3。
- child agent 已有独立可恢复 transcript、`/agents` workspace 和只读异构
  `/workflow`；主 chat 已支持能力门控的图片输入，但 child 尚无直接图片 composer，
  也还没有 MCP、split pane 或并发可写 multi-agent。workflow
  可以恢复 durable DAG，但进程在 provider request 中途崩溃时只能提供有界
  at-least-once，无法对外部 API 提供 exactly-once。
- managed background task 不跨 CLI 生命周期；正常退出会取消 task、join worker，并
  清理 TaskManager 创建的 process group。显式 `setsid`、double-fork 或 daemonize
  逃离该组属于当前进程隔离边界之外；需要持续运行的计算应交给正式调度/作业工具。
- known-secret redaction 只降低意外泄露；未知、编码或运行时生成的 secret 仍可能进入
  transcript/artifact；显式 `@file` 也会把内容发送给当前 provider，因此 `allow bash`、
  memory redaction 和敏感文件名拒绝都不是通用 DLP。
- content-addressed attachment snapshot 当前不做自动 GC；它们保持 `0600` 且可复用，
  但长期大量引用图片/文档会增长 `~/.zylab/attachments/`。
- tool trace 已持久化并显示 stdout/stderr path、exit code 与 signal，但尚未把这些 path
  规范化成独立 artifact 外键；文件仍由 TaskManager 放在私有持久目录。
- capability fingerprint 的 gateway catalog revision 是代码中的显式版本；若上游能力
  改变但目录字段完全不变，需要强制 `/probe` 或更新 revision 才会推翻旧的稳定拒绝。
- 本机异常 owner 会立即对账；来自另一 hostname 的 running metrics row 为避免误杀，
  只在 heartbeat 超过 7 天后恢复，因此跨主机硬断电后的短期 `running` 可能是陈旧显示。
