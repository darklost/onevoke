# Repository Guidelines

本文件是 Onevoke 仓库自身的开发规则. 仓库对外发布的工作流规则在 `rules/`, 那些文件是交付物, 不是本仓库的开发指引.

## 本仓库特例

- 本仓库第二阶段安全角色 `CSA` 和 `Hacker` 一律标记 N/A, 不运行; `PM` 和 `QA` 保持适用.
- 审核 base 以来全部改动都是 Markdown 规则或文档时, 不运行审核. 只要包含任一脚本, 代码或其他非 Markdown 文件, 就按适用规则运行 `PM` 和 `QA`; `CSA` 和 `Hacker` 仍按上一条标记 N/A.
- 对外发布的分支模型固定为 `main` 稳定分支加 `develop` 集成分支, 不提供其他长期分支或集成分支选项; 缺少 `develop` 时从 `main` 自动初始化.

## Project Structure & Module Organization

- `rules/ONEVOKE-AGENTS.md` 是发布规则的入口, 只放分册索引, 优先级和默认行为. 其余分册由它的分册表按需引用: `BASE-RULES.md` 跨项目通用条款, `KANBAN-RULES.md` 看板行为契约, `GIT-RULES.md` Git 工作流, `REVIEW-RULES.md` 审核契约, `CODE-RULES.md` 架构与代码质量契约. 它们是面向用户和 Agent 的对外接口, 改动前确认与 `bin/` 下实现一致. 全部装到 `~/.agents/` 下的同名文件.
- `install.sh` 遍历 `bin/*` 和 `rules/*.md`, 把全部普通文件直接覆盖到 `~/.local/bin/` 与 `~/.agents/`, 包括 `ONEVOKE-AGENTS.md`. 若存在 `share/kanban-web/`, 同步安装到 `~/.local/share/onevoke/kanban-web/` 供 `kanban web` 使用. 升级时检测已退役的 `codex-review.sh`、`claude-review.sh` 和 `grok-review.sh`, 提示用户且仅在明确确认后删除; 拒绝或无输入时保留. `~/.agents/AGENTS.md` 不存在时创建指向 `ONEVOKE-AGENTS.md` 的相对符号链接, 已有任何同名入口时保持不变. 唯一稳定 stdout 是 `Onevoke installed`; 最后必须用绝对路径运行 `onevoke welcome`. 同名目标是目录时须在写任何文件前拒绝, 防止 `install` 把源文件塞进错误目录.
- `bin/onevoke_config.py` 是 `onevoke` 与 `kanban` 共用的配置边界, 配置默认在 `~/.config/onevoke/config.json`, 测试用 `ONEVOKE_CONFIG` 隔离. 配置写入必须校验 schema, 用同目录临时文件加 `os.replace()` 原子替换, 权限为 `0600`. `models` 段保存 kanban 与 review 的模型和推理档位, 缺失层级用默认值补齐, 未知键拒绝; `model` 允许空串表示用 CLI 默认模型. 它同时是脚本, `review-model <agent>` 子命令输出两行 (`<model>` 与 `<effort>`, model 可为空行) 供 `onevoke-review.sh` 读取.
- `bin/onevoke` 提供 `welcome`, `doctor`, `config`, `review`. welcome 只在 tty 中提问, 无 tty 时诊断后正常提示重跑; 它显示当前配置总览, 只进入用户选择的单项编辑, 总览直接回车保存, yes/no 使用文本输入. 依赖安装必须经用户明确选择; 模型菜单只列本次配置用到的执行 Agent 和 Reviewer. MemSearch Codex 插件只克隆官方仓库并运行上游安装脚本, 不检查仓库和安装状态. `review` 按角色配置选择 Codex、Claude 或 Grok, 并把 reviewer agent 参数传给唯一公开入口 `onevoke-review.sh`.
- `bin/kanban` 命令细节 (自 `rules/KANBAN-RULES.md` 迁入, 改实现时同步更新本条): `init` 幂等创建看板和 6 个状态目录, Git 项目只写本地 `.git/info/exclude`, 最后输出全局规则路径; `rules` 不要求已有看板. `list` 按状态分组、组内按显示时间倒序, 同时或缺失时按任务 ID 倒序, 默认彩色表格并标出规模, `--mobile` 输出竖屏布局; `working` 显示开始时间, `done` 显示完成时间, 旧卡缺完成时间时用文档最后修改时间. `new` 在 `backlog/` 创建小任务, `--large` 创建含 `spec.md` 的大任务目录. `pick` 执行 `backlog -> todo` 及完整性校验, 不给 ID 时只列候选; `move` 只执行状态模型允许且满足目标要求的迁移. `start` 只接受 `todo` 卡, 原子执行 `todo -> working`、写负责人和开始时间再启动 Agent; 模型和大小任务的推理档位读生效配置的 `models.kanban.<agent>`, 默认值为 Codex `gpt-5.6-sol` high/medium, Claude `opus` high/medium, Grok 不锁模型 xhigh/high, 模型为空串时不传 `--model`; 三种 launcher 的 cwd 都是项目根, `tmux` 只在当前 session 建 `kb-<任务标题>` window 不建 session, `tmux-session` 按项目根绝对路径算出 `kb-<目录名>-<sha256 前 8 位>` 的专属 session, 不存在时 `new-session -d` 新建并 best-effort 写 `@onevoke_project` 标记, 已存在且标记为空或匹配时复用并 `new-window`, 标记属于其他项目时退避到 `-2`…`-9` 候选, `foreground` 要求三个标准流都是 TTY 并等待 Agent 退出. `check` 列出全部无效入口并以非零退出, 其他命令忽略无关的无效入口只在目标任务违规时失败, 状态目录缺失或不可写时全部失败. `web` 的 `--host`/`--port` 覆盖监听地址, `--refresh` 控制服务端扫描秒数, `--assets` 覆盖资源目录, `--open` 尝试打开浏览器; `tui` 的 `--single` 强制单栏, 默认每栏最小 40 列并按宽度自适应显示部分或全部栏目, 不足最小栏宽时按实际宽度显示单栏并保证选中栏可见, `--refresh` 控制自动刷新秒数 (默认 30), `--theme` 指定 auto/light/dark 配色 (运行中用 `t` 循环切换), 且要求 stdin/stdout 都是 TTY.
- `bin/kanban` 的 `start` 未传 `--agent` 时读取生效的 `kanban_agent`; `--agent` 始终优先. `--launcher` 可覆盖本次启动且不改机器配置; launcher 为 `tmux` 时沿用独立 window 且必须已在 tmux session 内, 为 `tmux-session` 时不要求已在 tmux 内, 启动后不 attach 或 switch-client, 只打印 session 名, window id 和 attach 提示, 为 `foreground` 时必须有交互 tty 并在当前终端等待 Agent 退出. `web` 启动只读看板 UI, 默认 `127.0.0.1:8080`, 服务端默认每 60 秒扫描并仅在内容变化时通过 SSE 推送, 客户端按任务 ID 原位更新; 资源来自 `share/kanban-web/` 或已安装的 `~/.local/share/onevoke/kanban-web/`, 由 `bin/kanban_web.py` 用标准库 HTTP 服务和 `string.Template` 渲染. `tui` 复用 Web payload 的扫描、排序和搜索字段, 默认按终端宽度显示活跃栏目, 宽度不足时少显示或按实际宽度显示单栏并保持选中栏可见, `a` 切换到全部 6 栏; `bin/kanban_tui.py` 用标准库 `curses` 负责多栏/单栏导航、任务详情、终端缩放和默认每 30 秒的原位刷新, 刷新时按任务 ID 更新并尽量保留选中项和滚动位置.
- 新增分册时把它加进 `ONEVOKE-AGENTS.md` 的分册表即可; `install.sh` 和安装测试都遍历 `rules/*.md`, 不必改.
- 本仓库根目录的 `AGENTS.md` 是本仓库自己的开发规则, 与 `rules/` 下的发布物是两回事, 不要混改.
- `bin/kanban` 是 Python 3 CLI 的唯一入口, 包含看板定位、任务校验、状态迁移和命令解析; `bin/kanban_web.py` 与 `bin/kanban_tui.py` 分别封装只读 Web 和终端界面. `bin/onevoke` 负责首次引导、环境诊断、配置展示和 Reviewer 分发.
- `bin/onevoke-review.sh` 是 Codex、Claude 与 Grok 审核的唯一公开入口和单一门禁实现, 第一个参数选择 agent, 集中维护 commit 校验、evidence、prompt 骨架、超时监督和 worktree 篡改检测. 模型与推理档位按 环境变量 > Onevoke 配置 (经同目录 `onevoke_config.py review-model`) > 内置默认 解析, 配置读取失败时回落到内置默认, 不阻塞审核. Codex 在目标 worktree 内以 `--sandbox read-only --ephemeral` 运行; Claude 在外部 runtime 目录以 `--permission-mode plan --tools Read,Grep,Glob --safe-mode --no-session-persistence` 运行; Grok 在外部 runtime 目录以 `--sandbox read-only --no-memory --no-subagents` 运行且只开放 `read_file,grep,list_dir`. 新增 reviewer 只扩展该入口适配层, 不新增脚本. `bin/merge-worktree-memory.py` 在集成后合并 worktree 的 memsearch 记忆, 并清除合并结果中的非法 UTF-8 字节.
- `tests/test-onevoke.py` 用临时 HOME 和伪终端覆盖 welcome、配置和 Reviewer 分发. `tests/test-kanban.py` 覆盖看板生命周期、三种 launcher (含 `tmux-session` 的建/复用/退避/回滚)、安装及初始化, 并用伪终端覆盖 TUI 启动退出. `tests/test-merge-worktree-memory.py` 覆盖记忆合并; 三个 agent 审核测试覆盖共用入口门禁.
- 运行时创建的 `kanban/` 是本机共享数据, 不属于仓库源码, 不得提交.

## Build, Test, and Development Commands

本项目仅依赖 Python 标准库和 POSIX shell, 无构建步骤或依赖安装.

```sh
./install.sh
python3 bin/kanban --help
python3 tests/test-onevoke.py
python3 tests/test-kanban.py
python3 tests/test-merge-worktree-memory.py
python3 tests/test-codex-review.py
python3 tests/test-claude-review.py
python3 tests/test-grok-review.py
python3 -m py_compile bin/onevoke bin/onevoke_config.py bin/kanban bin/kanban_web.py bin/merge-worktree-memory.py tests/*.py
sh -n install.sh && bash -n bin/onevoke-review.sh
```

测试默认针对当前工作树. `tests/test-kanban.py` 可用 `KANBAN_COMMAND` 指向别的入口; 三个审核测试分别用假 Codex/Claude/Grok 二进制驱动, 不调用真的 CLI, 也不产生网络请求.

安装脚本复制 `bin/` 和 `rules/` 下全部普通文件, 不接受参数, 仅在 `~/.agents/AGENTS.md` 不存在时创建入口软链接, 最后运行 welcome. 手工试验必须同时设置临时 `HOME`, `ONEVOKE_CONFIG` 和 `KANBAN_DIR`, 不得修改真实配置或看板.

## Coding Style & Naming Conventions

使用 Python 3、UTF-8、4 空格缩进及标准库优先的实现. 函数和变量采用 `snake_case`, 类采用 `PascalCase`, 常量采用 `UPPER_SNAKE_CASE`. 保持函数职责单一, 对无效输入抛出 `KanbanError`, 不静默忽略失败.

Shell 脚本使用 2 空格缩进, `set -eu`, 引用所有变量展开, 错误信息写 stderr 并返回非零状态.

任务 ID 必须匹配 `YYYYMMDD-short-slug-task`; slug 仅使用小写 ASCII 字母、数字和连字符. 用户可见错误信息及规则文档沿用中文和 ASCII 标点.

## Testing Guidelines

测试框架为 `unittest`; 测试方法命名为 `test_<behavior>`. 每项行为变更至少覆盖成功路径和相关拒绝路径. 使用 `TemporaryDirectory` 隔离文件系统状态, 不依赖或改写用户真实看板, 不写入真实 `$HOME`. 提交前运行完整测试命令; 当前项目未设置覆盖率阈值.

## Commit & Pull Request Guidelines

新提交使用简短中文动宾 subject, 每个 commit 只包含一个关注点, 例如 `修复重复任务检测`. PR 应说明行为变化、原因和实际验证命令; 关联任务或 issue. CLI 输出变化附终端示例, 无界面改动时无需截图.

## Security & Configuration

`KANBAN_DIR` 仅用于测试、非 Git 项目或明确覆盖. 不提交 token、凭据、敏感服务地址、真实任务卡片或本机路径. 文件写入和状态迁移必须继续经过现有校验, 不得绕过 `scan()` 或 `validate_target()` 直接操作任务入口.

`onevoke-review.sh` 的只读隔离、commit 校验和 worktree 篡改检测是审核门禁的一部分; 不得绕过该入口直调 reviewer CLI, 也不得为方便调试而放宽 agent 隔离参数.
