# Onevoke 工作流规则

- 规则集入口: 本文件 `ONEVOKE-AGENTS.md`. 只放分册索引, 优先级和默认取值; 通用条款在同目录 `BASE-RULES.md`.
- 当前读取的本文件位置决定安装作用域. 不维护两套规则; 安装器把同一套分册原文覆盖到当前作用域的规则根, 不改写 Markdown 正文.
- POSIX 用 `install.sh`, 原生 Windows 用 `install.ps1`. Windows 不自动修改用户 `PATH`; Onevoke 自身的 `.cmd` 入口只供人工交互的普通参数. 含特殊字符的 Onevoke 自动化必须用进程 API 的 argv 数组直接调用显式 Python 解释器和命令根里的 Python 入口, 不得经过 PowerShell/cmd 命令字符串. 执行 Agent 与 Reviewer 在所有平台都把完整任务内容写入 UTF-8 临时文件, 启动参数只保留 CLI 必需的控制参数和一句文件路径指令; 文件内要求 Agent 完成后尝试删除, 删除失败或遗留不影响结果. 这类任务文件不做 POSIX 权限或 Windows ACL 检查与收紧; 配置、审核 runtime 和其他已有私有边界的安全要求不变. 原生 Windows 优先使用 Codex, Claude, Grok 与 Cursor 的 `.exe`; 只有 `.cmd`/`.bat` 时通过显式 `cmd.exe /d /s /v:off /c` 和四种 Agent 适配层的参数编码启动, 不作为任意批处理脚本的通用调用契约. 本机的执行 Agent, launcher, 各审核角色及各 Agent 的模型档位保存在配置文件, 用命令根下的 `onevoke config` 查看, `onevoke welcome --reset` 修改.
- 全局安装且 `~/.agents/AGENTS.md` 不存在时, POSIX 安装器将其符号链接到本文件; Windows 安装器优先创建硬链接并回落到符号链接, 无法安全创建则安装失败. 已有同名入口时保持不变. 项目安装不创建或修改该全局入口.
- 配置文件和审核运行目录必须仅允许当前用户访问: POSIX 使用 `0600`/`0700`; Windows 私有目录/文件在创建瞬间即使用关闭继承的受保护 DACL, 不得先按继承 ACL 发布再收紧. Windows 审核运行目录必须在敏感文件写入、Reviewer 运行、进程树收集和清理期间持续持有不共享 WRITE/DELETE 的根句柄, 同时阻止入口改名和原地 reparse 切换; 清理从固定句柄逐层拒绝 reparse point, 并设置有界预算, 清理失败时审核失败. Windows 配置路径必须从卷/UNC anchor 逐分量拒绝 reparse point; 内容读取、schema 校验和有效旧配置 DACL 迁移必须保持同一固定句柄, 无效配置不迁移 ACL; 保存时临时文件先私有再写入, 并只收紧本次新建的配置目录, 不得改动既有祖先 DACL. Windows 的目标记忆目录/文件也必须迁移为当前用户独占的受保护 DACL. 看板、Git exclude 及记忆合并在 Windows 拒绝符号链接、junction 等 reparse point; Git exclude 保持既有 ACL 并在同一固定句柄内去重追加, 记忆合并通过固定句柄读取/追加并使用 `LockFileEx`; 禁绕过 Onevoke 命令直接操作这些边界.

## 作用域

本文件所在目录即「规则根」. 由规则根判定作用域, 并映射命令根, 配置文件和资源目录:

| 逻辑名 | 全局安装 | 项目安装 |
|---|---|---|
| 规则根 | `~/.agents` | `<主 worktree>/.onevoke/rules` |
| 命令根 | `~/.local/bin` | `<主 worktree>/.onevoke/bin` |
| 配置文件 | `~/.config/onevoke/config.json` | `<主 worktree>/.onevoke/config.json` |
| 资源目录 | `~/.local/share/onevoke` | `<主 worktree>/.onevoke/share` |

- 全局安装: 规则根是用户 HOME 下的 `.agents`.
- 项目安装: 规则根是当前 Git 项目主 worktree 下的 `.onevoke/rules`. 项目载荷只落在该主 worktree 的 `.onevoke/`; 任务 worktree 共享这一份, 不建副本, 镜像或符号链接. 项目安装零全局写入, 不读取或写入 HOME 下的 Onevoke 路径.
- 两种安装可同时存在. 以当前读取的入口为准; 同时存在时项目入口和项目命令根下的绝对命令优先于 PATH 中的全局同名命令.
- 分册一律用「规则根」「命令根」「配置文件」「资源目录」这些逻辑名称引用路径, 不把全局路径写成唯一有效路径.
- 调用命令时使用当前作用域命令根下的入口. 全局安装可使用已加入 PATH 的命令名 (Windows 须先把命令根加入 PATH). 项目安装必须使用绝对入口, 例如 POSIX 的 `<命令根>/kanban` 与 `<命令根>/onevoke`; Windows 人工交互可用 `<命令根>\kanban.cmd` 与 `<命令根>\onevoke.cmd`. 禁止改用 PATH 中的全局同名命令.

## 分册

用到哪份读哪份. 下表文件均在规则根, 与本文件同目录:

| 分册 | 何时读 |
|---|---|
| `BASE-RULES.md` | 每个任务开始时 |
| `GIT-RULES.md` | 建分支, 提交, push, 审核, 集成前 |
| `REVIEW-RULES.md` | 触发审核前 |
| `CODE-RULES.md` | 改代码前 |
| `KANBAN-RULES.md` | 收到 Bug 或功能开发需求时, 及操作看板前; 用命令根下的 `kanban rules` 读取 |

## 优先级

- 高到低: 当前任务明确用户指令 > 离目标文件最近的项目级 `AGENTS.md` 或 `CLAUDE.md` > 本文件「默认取值」与当前作用域 Onevoke 配置 > 上表各分册.
- 分册定机制, 本文件定取值: 只有「默认取值」列出的条目高于分册, 其余一律以分册为准, 本文件不复述分册内容.
- 项目要覆盖 Reviewer 或看板完成时机, 写进项目级 `AGENTS.md` 或 `CLAUDE.md`, 不改本文件和当前作用域配置. 分支模型是固定机制, 不提供项目级选项.
- 同目录 `AGENTS.md` 与 `CLAUDE.md` 冲突且用户指令未消解时, 停止受影响操作, 问用户.

## 默认取值

### 分支

- 固定 `main` + `develop`, 不使用其他长期分支模型; 机制与初始化见 `GIT-RULES.md`「分支与 worktree」.
- `main` 只从 `develop` 前进, 且必须用户明确确认; Agent 不自动推 `main`.

### 执行 Agent

- `kanban start` 按任务卡规模选执行 Agent: 大任务 (含 `spec.md` 的目录卡) 取配置 `kanban_agents.large`, 小任务 (单文件卡) 取 `kanban_agents.small`; 两者缺省都等于 `kanban_agent`, 用 `onevoke welcome` 菜单「执行 Agent」分别设置. `--agent` 只覆盖本次. 模型与推理档位按 `models.kanban.<agent>` 的大, 小任务档位取值.
- `kanban resume` 用卡片记录的原会话唤醒同一执行 Agent, 不换 Agent; 机制见 `KANBAN-RULES.md`「命令契约」.

### Launcher

- launcher 有 `auto`, `tmux`, `tmux-session`, `herdr`, `foreground`, `console` 六种. POSIX 默认 `auto`; 原生 Windows 默认 `console`, 且 Windows 不使用 `auto`/`tmux`/`tmux-session`/`herdr`.
- `auto` 在启动当时解析, 不把结果写回配置: 处于 herdr (`HERDR_ENV=1`) 时按 `herdr` 启动, 否则处于 tmux 时按 `tmux` 启动; 同时处于两者时 herdr 优先. 两者都不在则失败且不领取, 不回落到 `tmux-session`, `foreground` 或 `console`. 解析后的协调按实际启动方式的单卡规则. 完整契约见 `KANBAN-RULES.md`.
- `console` 仅支持 Windows: 它在独立控制台窗口启动 Agent 并返回 PID, 不创建或复用 tmux session, 不提供 attach 或输出抓取能力. 需要在当前终端等待执行结果时改用 `foreground`; 完整启动与协调契约见 `KANBAN-RULES.md`.
- `herdr` 仅支持 POSIX, 且要求当前处于 herdr (`HERDR_ENV=1`): 在当前 workspace 新建 tab, 并在根 pane 启动与 tmux 路径相同的 Agent 命令. 完整启动与协调契约见 `KANBAN-RULES.md`.

### Reviewer

- `PM`, `CSA`, `Hacker`, `QA` 各取 Onevoke 配置中的 reviewer, 未完成 welcome 时四者都回落到 Codex.
- 未被用户指令, 项目规则或用户自己的全局规则覆盖时, 审核一律通过命令根下的 `onevoke review` 分发. 同一角色一轮审核内不换 Agent; 不同角色可用不同 Agent.

### 审核环节

- 默认环节策略保存在配置文件的 `review_stages`, 用命令根下的 `onevoke config` 查看. 每个角色取 `auto`, `skip` 或 `required` 之一, 缺省为 `auto`.
- 环节是否实际运行, 按 `REVIEW-RULES.md`「审核环节」的优先级链解析; 项目级 `AGENTS.md` 或 `CLAUDE.md`, 以及当前任务的用户指令可覆盖当前作用域配置.

### 看板任务完成

- 单卡: 实现, 验证和必要审核通过后, 直接按 `GIT-RULES.md`「集成与清理」fast-forward 合回 `develop`, 不请求验收也不等确认; 合回并清理完才填 `结果: completed`, 迁 `done/`, 再发「完成报告」; 任务卡以任何方式结束都按该模板汇报一次.
- 任务组成员卡: 执行 Agent 只做实现, 验证, 提交 push 并按 `GIT-RULES.md`「组集成分支」ff 进组分支, 然后迁 `review/` 退出, 不自行审核和合回. 审核批次和组分支集成由主控按 `KANBAN-RULES.md`「任务组编排」完成; finding 派回与集成成功后的逐卡收尾通知, 主控都只调用一次 `kanban notify <task-id> --message-file <file>` 并检查退出码, 由命令选择直投或恢复通道; 非零退出即停止并报告用户, 不另行调用 `resume` 或绕过命令投递. 收尾通知成功后由原执行 Agent 做记忆合并, 删本卡 worktree 与任务分支, 补写完成总结, 迁 `done/` 并发完成报告. 无法派回或收尾未闭环时主控代做该卡收尾并汇报, 在报告中写明代做原因.
- 用户要求暂停或不合回, 必要审核未通过, 或集成, 清理失败时: 单卡留 `working/` 并保留分支与 worktree; 任务组成员卡在集成失败时全部留 `review/` 并保留资源, 集成成功后收尾中途失败时已进入 `done/` 的卡不回退, 其余停在实际状态 (`working/` 或 `review/`), 按 `KANBAN-RULES.md`「任务组编排」处理. 均按 `KANBAN-RULES.md`「完成报告」模板汇报, 并在其中写明阻塞和解除条件.
- 用户事后测试发现的问题另建新卡, 不退回也不复用已进 `done/` 的卡.
