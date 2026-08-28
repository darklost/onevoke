# Onevoke

一个人用看板调度多个 AI Agent.

![Onevoke 工作流](docs/workflow.svg)

## 1. 安装

需要 Python 3, Git, 以及 Codex, Claude 或 Grok 中至少一个. POSIX 系统还需要 POSIX shell; 原生 Windows 使用 PowerShell.

Onevoke 有两种安装作用域, 共用同一套规则和程序, 不维护两套模板, 安装时也不改写 Markdown 正文. 当前读取的 `ONEVOKE-AGENTS.md` 入口位置决定作用域; 两种安装同时存在时, 项目入口和项目绝对命令优先.

### 1.1 全局安装

POSIX:

```sh
./install.sh
```

原生 Windows (PowerShell):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

Windows 安装器把命令装到 `~/.local/bin`, 规则装到 `~/.agents`, 但不会修改用户 `PATH`. 请把 `~/.local/bin` (通常是 `%USERPROFILE%\.local\bin`) 加入 `PATH` 并重新打开终端; `onevoke`, `kanban`, 审核和记忆合并分别提供对应的 `.cmd` 交互入口. 安装器和这些入口都会实际验证 Python 3: `py -3` 存在但不可用时继续尝试 `python.exe`. Windows 批处理无法为任意参数提供无损 argv 边界: 自动化若要传 `&|<>^%!`, 引号或结尾反斜杠等数据, 必须用进程 API 的 argv 数组直接调用当前 Python 和安装目录里的 Python 入口, 例如 Python 调用方使用 `subprocess.run([sys.executable, str(Path.home() / ".local/bin/onevoke"), ...])`; 不得再经过 `.cmd` 或 PowerShell/cmd 命令字符串.

安装过程会显示当前配置菜单, 可按需修改默认 Agent、各角色 Reviewer、启动方式、模型与推理档位、MemSearch 或审核环节; 直接回车保存当前值, 输入 `q` 退出且不保存.
审核在 POSIX 通过 `onevoke-review.sh`, Windows 人工显式调用可通过 `onevoke-review.cmd`; `onevoke review` 在 Windows 内部直接进入同目录的 `onevoke_review.py`, 避免批处理重解析任务文本. 这些路径共享唯一门禁实现; 新增 Reviewer 时扩展该实现, 不新增按 Agent 命名的脚本. 原生 Windows 上 Codex, Claude 与 Grok CLI 必须解析为原生 `.exe`; `.cmd`/`.bat` Agent 不会被 welcome、doctor、看板启动或审核执行.

如果 `~/.agents/AGENTS.md` 不存在, 安装器会将其链接到 `ONEVOKE-AGENTS.md`; 已有文件不会修改.

如果 welcome 提示 Agent 尚未接入规则:

- Claude: 在 `~/.claude/CLAUDE.md` 加 `@~/.agents/ONEVOKE-AGENTS.md`.
- Codex: 将 `~/.codex/AGENTS.md` 软链接到该入口, 或把入口内容合入现有文件.
- Grok: 将 `~/.grok/AGENTS.md` 软链接到该入口, 或把入口内容合入现有文件.

### 1.2 项目本地安装

把载荷装到目标 Git 项目主 worktree 的 `.onevoke/`, 完全跳过 HOME 下的全局 Onevoke 路径.

POSIX:

```sh
./install.sh --project <项目目录>
```

原生 Windows (PowerShell):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 --project <项目目录>
```

目标从任一 worktree 指定时都归一到主 worktree. 布局:

- 规则根: `<主 worktree>/.onevoke/rules`
- 命令根: `<主 worktree>/.onevoke/bin`
- 配置文件: `<主 worktree>/.onevoke/config.json`
- 资源目录: `<主 worktree>/.onevoke/share`

限制:

- `.onevoke/` 写入该仓库本地 `.git/info/exclude`, 不进 Git, 也不改项目 `.gitignore`.
- 任务 worktree 共享主 worktree 的 `.onevoke/`, 不建副本, 镜像或符号链接.
- 零全局写入: 不创建, 修改或探测 `~/.agents`, `~/.local/bin`, `~/.config/onevoke` 等全局 Onevoke 路径, 也不迁移或卸载既有全局安装.
- 项目模式必须用命令根下的绝对入口, 例如 `<主 worktree>/.onevoke/bin/kanban` 和 `<主 worktree>/.onevoke/bin/onevoke`; Windows 人工交互可用对应 `.cmd`. 禁止改用 PATH 中的全局同名命令, 也不要把项目命令根加入 PATH 以免与全局安装混淆.
- Agent 规则接入指向项目入口 `<主 worktree>/.onevoke/rules/ONEVOKE-AGENTS.md`, 不要指向全局入口.

## 2. 使用

下文 `kanban` 与 `onevoke` 指当前作用域命令根下的入口. 全局安装可使用已加入 PATH 的命令名; 项目安装必须使用绝对入口, 例如 `<主 worktree>/.onevoke/bin/kanban`, 禁止改用 PATH 中的全局同名命令.

在项目目录首次使用时初始化看板:

```sh
kanban init
```

先在 Agent 中讨论需求, 明确目标, 验收条件和不做的范围.

讨论完成后, 让 Agent 创建并启动任务卡:

```text
需求已确认. 请用 kanban new & start 创建任务卡并启动.
```

Agent 会填完整任务卡, 再执行:

```sh
kanban new feature login-retry 登录重试
kanban pick 20260813-login-retry-task
kanban start 20260813-login-retry-task
```

`kanban start` 支持 `auto`, `tmux`, `tmux-session`, `herdr`, `foreground`, `console` 六种 launcher. POSIX 默认 `auto`, 原生 Windows 默认 `console`; `auto` 在启动当时解析: 处于 herdr 则新建 herdr tab, 否则处于 tmux 则新建 tmux window, 两者都不在则失败且不领取, 不回落到 `tmux-session` 或 `foreground`. `console` 仅支持 Windows, 会在独立控制台窗口启动 Agent 并立即返回 PID. 它不创建或复用 tmux session, 也不提供 attach 或输出抓取能力; 需要在当前终端等待 Agent 时使用 `foreground`. `herdr` 仅支持 POSIX, 要求当前处于 herdr (`HERDR_ENV=1`), 会在当前 workspace 新建 tab 并启动 Agent. Windows 拒绝 `auto` 和 `herdr`.

大型任务由 Agent 拆成多张可并行执行的任务卡, 再按依赖启动.

查看看板状态:

```sh
kanban list
kanban tui
kanban tui --single
kanban web
```

`kanban tui` 在当前终端启动全功能只读看板, 支持多栏浏览、搜索、任务详情、鼠标操作与剪贴板复制; 详情内可用 vim 风格翻页和文本选择. 栏宽用 `-`/`=` 调节, `--single` 单栏显示, 默认每 30 秒自动刷新. 原生 Windows 第一阶段保证 `kanban web`; `kanban tui` 仍要求当前 Python 提供可用的 `curses` 后端, 不属于本阶段的 Windows 可用性保证, 无法加载时请使用 Web 看板.

终端看板:

![终端看板](docs/onevoke-tui-01.png)

`kanban web` 默认在 `http://127.0.0.1:8080` 启动只读看板. 服务端每 60 秒扫描任务, 仅在数据变化时通过 SSE 推送; 客户端原位更新对应卡片.

Windows 后端拒绝看板、Git exclude 及记忆合并边界中的符号链接, junction 和其他 reparse point, 并通过已校验的 Win32 句柄完成任务读写、迁移、Git exclude 去重追加、记忆读取和追加; Git exclude 保持既有 ACL. 配置路径也从卷根逐级拒绝 reparse point; 读取期间固定配置文件句柄并拒绝写入/替换, schema 通过后在同一句柄上迁移 DACL; 保存时只收紧新建配置目录, 临时文件先变为私有再写入并通过固定父句柄原子替换. 在 Windows 上, 新看板目录、配置文件、审核运行目录及目标记忆目录/文件在创建瞬间即使用只允许当前用户访问的受保护 DACL; 审核运行目录的不共享 WRITE/DELETE 句柄会一直持有到 Reviewer 进程树收集和敏感文件 no-follow 清理完成, 同时阻止改名和原地 reparse 切换, 清理失败时审核失败. 记忆合并和 Git exclude 更新使用文件锁. 这些改动不改变 POSIX 现有的 no-follow 文件操作、`0600`/`0700` 权限和 `flock` 边界.

看板总览:

![只读看板总览](docs/onevoke-web-01.png)

点击卡片可查看任务详情:

![任务详情](docs/onevoke-web-02.png)

只看某个状态:

```sh
kanban list working
kanban list done
```

完整规则:

```sh
kanban rules
```

## 3. 审核

任务命中审核白名单后, 由平台审核入口按 PM -> 安全角色 -> QA 三阶段串行审核: POSIX 使用 `onevoke-review.sh`; Windows 的人工包装入口是 `onevoke-review.cmd`, `onevoke review` 的程序化分发则直接进入 `onevoke_review.py`. 两者使用同一门禁实现. Codex, Claude 与 Grok 的只读 sandbox, 权限和工具隔离参数在两个平台保持不变. 各角色是否运行由 `review_stages` 与项目规则决定, 实际运行的 QA 固定在最后. 每次修复只重跑当前阶段. 只有经主代理核实的 `blocking`, `high`, `medium` 必须修复, 其余档位不阻塞集成, 但要在闭环结束时逐项展示.

默认哪些环节运行可在配置文件的 `review_stages` 配置 (`auto` / `skip` / `required`), 项目规则或当前任务指令可覆盖. 全局安装的配置文件是 `~/.config/onevoke/config.json`, 项目安装是 `<主 worktree>/.onevoke/config.json`. 用命令根下的 `onevoke config` 查看当前值.

![Onevoke 审核流程](docs/review.svg)

## 4. 许可

本项目使用 MIT License, 见 [LICENSE](LICENSE).
