# Repository Guidelines

本文件是 Onevoke 仓库自身的开发规则. 仓库对外发布的工作流规则在 `rules/`, 那些文件是交付物, 不是本仓库的开发指引.

## 本仓库特例

- 本仓库第二阶段安全角色 `CSA` 和 `Hacker` 一律标记 N/A, 不运行; `PM` 和 `QA` 保持适用.
- 审核 base 以来全部改动都是 Markdown 规则或文档时, 不运行审核. 只要包含任一脚本, 代码或其他非 Markdown 文件, 就按适用规则运行 `PM` 和 `QA`; `CSA` 和 `Hacker` 仍按上一条标记 N/A.
- 对外发布的分支模型固定为 `main` 稳定分支加 `develop` 集成分支, 不提供其他长期分支或集成分支选项; 缺少 `develop` 时从 `main` 自动初始化.
- 功能或修复改完后默认: 在 `develop` 提交 → fast-forward 合入 `main` → 推送 `develop` 与 `main` → POSIX 运行 `./install.sh`, 原生 Windows 运行 `install.ps1` 更新本机安装; 用户另有指示时除外.

## Project Structure & Module Organization

- `rules/ONEVOKE-AGENTS.md` 是发布规则的入口, 只放分册索引, 优先级和默认行为. 其余分册由它的分册表按需引用: `BASE-RULES.md` 跨项目通用条款, `KANBAN-RULES.md` 看板行为契约, `GIT-RULES.md` Git 工作流, `REVIEW-RULES.md` 审核契约, `CODE-RULES.md` 架构与代码质量契约. 它们是面向用户和 Agent 的对外接口, 改动前确认与 `bin/` 下实现一致. 全部装到 `~/.agents/` 下的同名文件.
- `install.sh` 与 `install.ps1` 分别是 POSIX 和原生 Windows 安装器. 两者遍历 `bin/*` 和 `rules/*.md`, 把全部普通文件直接覆盖到 `~/.local/bin/` 与 `~/.agents/`, 包括 `ONEVOKE-AGENTS.md`; Windows 安装器不修改用户 `PATH`, 必须提示用户把 `~/.local/bin` 加入 `PATH`, 命令通过 `.cmd` 包装入口运行. Windows 安装器读取既有配置语言时必须实际执行候选 Python 3, `py -3` 失败后继续探测 `python.exe`; 不得从当前工作目录选择同名程序, 拒绝该候选后须继续探测 PATH 中后续同名程序. 若存在 `share/kanban-web/`, 同步安装到 `~/.local/share/onevoke/kanban-web/` 供 `kanban web` 使用. 升级时检测已退役的 `codex-review.sh`、`claude-review.sh` 和 `grok-review.sh`, 提示用户且仅在明确确认后删除; 拒绝或无输入时保留. `~/.agents/AGENTS.md` 不存在时, POSIX 创建指向 `ONEVOKE-AGENTS.md` 的相对符号链接, Windows 优先创建硬链接并回落到符号链接, 两者都不得用独立副本冒充入口; 已有任何同名入口时保持不变. 唯一稳定 stdout 按 locale 为 `Onevoke 已安装` 或 `Onevoke installed`; 最后必须用绝对路径运行 `onevoke welcome`. 同名目标是目录或 Windows reparse point 时须在写任何文件前拒绝, 防止安装器把源文件写入错误边界.
- `bin/onevoke_config.py` 是 `onevoke` 与 `kanban` 共用的配置边界, 配置默认在 `~/.config/onevoke/config.json`, 测试用 `ONEVOKE_CONFIG` 隔离. 配置写入必须校验 schema; POSIX 用同目录临时文件加 `os.replace()` 原子替换, 权限为 `0600`. Windows 的 `configured_language`, 读取和写入必须从卷/UNC anchor 逐分量 no-follow, 拒绝符号链接、junction 等 reparse point; load 在不共享 WRITE/DELETE 的同一固定句柄上完成读取、schema 校验和旧配置 DACL 迁移, 无效配置不迁移 ACL; save 仅收紧本次新建的配置目录, 临时文件必须先变为当前用户独占的受保护 DACL 再写入, 最后相对固定父句柄原子替换; 不得收紧既有祖先目录. 任一权限或安全后端失败必须报错. `language` 为 `cn`/`en`, 默认 `cn`, 可在 welcome 设置; 生效优先级为 `--lang` (经 `ONEVOKE_LANG_CLI` 跨进程传递) > 配置 > 环境变量. `launcher` 允许 `tmux`/`tmux-session`/`foreground`/`console`, POSIX 默认 `tmux`, Windows 默认 `console`; `console` 仅支持 Windows. `models` 段保存 kanban 与 review 的模型和推理档位, 缺失层级用默认值补齐, 未知键拒绝; `model` 允许空串表示用 CLI 默认模型. `review_stages` 为 `PM`/`CSA`/`Hacker`/`QA` 各指定 `auto`/`skip`/`required`, 缺省全为 `auto`. 它同时是脚本, `review-model <agent>` 子命令输出两行 (`<model>` 与 `<effort>`, model 可为空行) 供 `onevoke_review.py` 读取; `review-stages` 按角色顺序输出四行环节策略; `configured-language` 在配置文件存在时输出 `cn`/`en`.
- `bin/onevoke` 提供 `welcome`, `doctor`, `config`, `review`. welcome 只在 tty 中提问, 无 tty 时诊断后正常提示重跑; 它显示当前配置总览, 只进入用户选择的单项编辑, 总览直接回车保存, yes/no 使用文本输入. 依赖安装必须经用户明确选择; 模型菜单只列本次配置用到的执行 Agent 和 Reviewer; Windows 只把解析为原生 `.exe` 的 Agent 视为可用, 不为 `.cmd`/`.bat` 运行 `--version`; Windows launcher 菜单只列 `console` 与 `foreground`, POSIX 按 tmux 可用性列适用选项; 审核环节菜单为四角色配置 `auto`/`skip`/`required`. MemSearch Codex 插件只克隆官方仓库并运行上游安装脚本, 不检查仓库和安装状态; 上游安装器需要 Bash, 因此原生 Windows 当前不启用该集成. `review` 按角色配置选择 Codex、Claude 或 Grok, POSIX 分发到 `onevoke-review.sh`, Windows 直接用当前 Python 进入同目录的 `onevoke_review.py`, 两者共享唯一门禁实现.
- `bin/kanban` 命令细节 (自 `rules/KANBAN-RULES.md` 迁入, 改实现时同步更新本条): `init` 幂等创建看板和 6 个状态目录, Git 项目只写本地 `.git/info/exclude`, 最后输出全局规则路径; Windows 新看板目录必须用固定父句柄和 `CREATE_NEW` 在创建时应用私有 DACL, 创建竞态失败关闭, 既有目录只迁移叶目录 ACL; Git exclude 的父链逐分量 no-follow, 既有 ACL 保持不变, 去重读取与追加使用同一固定叶句柄及文件锁. `rules` 不要求已有看板. `list` 按状态分组、组内按显示时间倒序, 同时或缺失时按任务 ID 倒序, 默认彩色表格并标出规模, `--mobile` 输出竖屏布局; `working` 显示开始时间, `done` 显示完成时间, 旧卡缺完成时间时用文档最后修改时间. `new` 在 `backlog/` 创建小任务, `--large` 创建含 `spec.md` 的大任务目录. `pick` 执行 `backlog -> todo` 及完整性校验, 不给 ID 时只列候选; `move` 只执行状态模型允许且满足目标要求的迁移. `start` 只接受 `todo` 卡, 原子执行 `todo -> working`、写负责人和开始时间再启动 Agent; 模型和大小任务的推理档位读生效配置的 `models.kanban.<agent>`, 默认值为 Codex `gpt-5.6-sol` high/medium, Claude `opus` high/medium, Grok 不锁模型 xhigh/high, 模型为空串时不传 `--model`; 四种 launcher 的 cwd 都是项目根, `tmux` 只在当前 session 建 `kb-<任务标题>` window 不建 session, `tmux-session` 按项目根绝对路径算出 `kb-<目录名>-<sha256 前 8 位>` 的专属 session, 不存在时 `new-session -d` 新建并 best-effort 写 `@onevoke_project` 标记, 已存在且标记为空或匹配时复用并 `new-window`, 标记属于其他项目时退避到 `-2`…`-9` 候选, `foreground` 要求三个标准流都是 TTY 并等待 Agent 退出, `console` 仅支持 Windows, 用独立控制台启动后返回 PID. `console` 不创建或复用 session, 不支持 attach 或输出抓取, 不得描述为 tmux 等价实现. `check` 列出全部无效入口并以非零退出, 其他命令忽略无关的无效入口只在目标任务违规时失败, 状态目录缺失或不可写时全部失败. `web` 的 `--host`/`--port` 覆盖监听地址, `--refresh` 控制服务端扫描秒数, `--assets` 覆盖资源目录, `--open` 尝试打开浏览器; `tui` 的 `--single` 强制单栏, 默认每栏最小 40 列并按宽度自适应显示部分或全部栏目, 可用 `-`/`=` 调节栏宽并写入 `~/.config/onevoke/tui.json` (测试用 `ONEVOKE_CONFIG` 同目录), 不足最小栏宽时按实际宽度显示单栏并保证选中栏可见, `--refresh` 控制自动刷新秒数 (默认 30), `--theme` 指定 auto/light/dark 配色 (运行中用 `t` 循环切换), 且要求 stdin/stdout 都是 TTY.
- `bin/kanban` 的 `start` 未传 `--agent` 时读取生效的 `kanban_agent`; `--agent` 始终优先. `--launcher` 可覆盖本次启动且不改机器配置; launcher 为 `tmux` 时沿用独立 window 且必须已在 tmux session 内, 为 `tmux-session` 时不要求已在 tmux 内, 启动后不 attach 或 switch-client, 只打印 session 名, window id 和 attach 提示, 为 `foreground` 时必须有交互 tty 并在当前终端等待 Agent 退出, 为 `console` 时必须是 Windows, 创建独立控制台进程后打印 PID 并立即返回. `web` 启动只读看板 UI, 默认 `127.0.0.1:8080`, 服务端默认每 60 秒扫描并仅在内容变化时通过 SSE 推送, 客户端按任务 ID 原位更新; 资源来自 `share/kanban-web/` 或已安装的 `~/.local/share/onevoke/kanban-web/`, 由 `bin/kanban_web.py` 用标准库 HTTP 服务和 `string.Template` 渲染, 是原生 Windows 第一阶段保证的看板 UI. `tui` 复用 Web payload 的扫描、排序和搜索字段, 默认按终端宽度显示活跃栏目, 宽度不足时少显示或按实际宽度显示单栏并保持选中栏可见, `-`/`=` 调节并记住栏宽, `a` 切换到全部 6 栏, `y` 复制当前任务 ID, 任务卡与详情支持鼠标拖选自动复制, 详情内 `v`/`V` 切换字符/行选择并用 `y` 复制; `bin/kanban_tui.py` 依赖 `curses` 负责多栏/单栏导航、鼠标点选与滚轮、任务详情 (vim 翻页与正文搜索)、终端缩放和默认每 30 秒的原位刷新, 刷新时按任务 ID 更新并尽量保留选中项和滚动位置. Windows 上只有当前 Python 提供可用 curses 后端时才能运行 TUI, 本阶段不保证.
- 新增分册时把它加进 `ONEVOKE-AGENTS.md` 的分册表即可; `install.sh`, `install.ps1` 和安装测试都遍历 `rules/*.md`, 不必改.
- 本仓库根目录的 `AGENTS.md` 是本仓库自己的开发规则, 与 `rules/` 下的发布物是两回事, 不要混改.
- `bin/kanban` 是 Python 3 CLI 的唯一实现入口, 包含看板定位、任务校验、状态迁移和命令解析; `bin/kanban.cmd` 是 Windows 包装入口. `bin/kanban_web.py` 与 `bin/kanban_tui.py` 分别封装只读 Web 和终端界面. `bin/onevoke` 负责首次引导、环境诊断、配置展示和 Reviewer 分发, `bin/onevoke.cmd` 是 Windows 包装入口.
- `bin/onevoke_review.py` 是 Codex、Claude 与 Grok 共用的单一审核门禁实现, 集中维护 commit 校验、evidence、prompt 骨架、超时监督和 worktree 篡改检测; POSIX 的公开入口是 `bin/onevoke-review.sh`, Windows 的人工交互入口是 `bin/onevoke-review.cmd`, `onevoke review` 的 Windows 程序化分发直接进入该 Python 实现. 模型与推理档位按 环境变量 > Onevoke 配置 > 内置默认 解析, 配置读取失败时回落到内置默认, 不阻塞审核. 用户可见输出语言优先级为 `ONEVOKE_LANG_CLI` 标记的显式 `--lang` > 配置 > 环境变量. Codex 在目标 worktree 内以 `--sandbox read-only --ephemeral` 运行; Claude 在外部 runtime 目录以 `--permission-mode plan --tools Read,Grep,Glob --safe-mode --no-session-persistence` 运行; Grok 在外部 runtime 目录以 `--sandbox read-only --no-memory --no-subagents` 运行且只开放 `read_file,grep,list_dir`. Windows 通过 `GetTempPathW` 只取得临时根的词法路径, 不允许 `tempfile` 在 no-follow 校验前写探测; runtime 从固定临时根句柄以 `CREATE_NEW` 和创建时 protected DACL 生成, 随机名碰撞重试, 不得先发布继承 ACL 的目录再收紧. runtime 的根句柄必须不共享 WRITE/DELETE 并持有到 Reviewer 进程树收集、worktree 校验和敏感文件清理完成, 阻止入口改名及原地切换为 reparse point; 清理必须从该固定句柄逐层拒绝 reparse point, 并受深度、条目与轮次预算约束, 任一清理失败都使审核失败. Windows 上 Reviewer CLI 必须是原生 `.exe`, 禁执行 `.cmd`/`.bat`; Windows 适配不得改变隔离参数. 新增 reviewer 只扩展该实现的 agent 适配层, 不新增脚本.
- `bin/onevoke_fs.py` 是跨平台安全文件边界. POSIX 继续使用 no-follow/openat 语义、`0600`/`0700` 和 `flock`; Windows 必须拒绝符号链接、junction 等所有 reparse point, 用已校验的 Win32 句柄完成普通文件读写、同边界原子替换与迁移, 用受保护 DACL 限制私有对象的当前用户访问, 用 `LockFileEx` 实现阻塞式独占锁. 私有对象不得回落到继承 ACL; Git exclude 等明确保持既有权限的中性对象可继承父 ACL, 但仍必须逐分量 no-follow 并固定句柄. 不得在 Windows 回落到未经句柄校验的 `Path.rename()` 或无锁实现. `bin/merge-worktree-memory.py` 在集成后合并 worktree 的 memsearch 记忆, 清除合并结果中的非法 UTF-8 字节, 并通过该文件系统层固定来源/目标句柄、拒绝 reparse point、迁移目标记忆 DACL 及加锁; Windows 由 `bin/merge-worktree-memory.cmd` 包装启动.
- `tests/test-onevoke.py` 用临时 HOME 和伪终端覆盖 welcome、配置和 Reviewer 分发. `tests/test-kanban.py` 覆盖看板生命周期、POSIX launcher (含 `tmux-session` 的建/复用/退避/回滚)、安装及初始化, 并用伪终端覆盖 TUI 启动退出. `tests/test-merge-worktree-memory.py` 覆盖跨平台记忆合并; 三个 agent 的 POSIX 审核测试覆盖共用门禁. `tests/test-install-windows.py`, `tests/test-windows-automation.py`, `tests/test-windows-console.py`, `tests/test-windows-fs.py`, `tests/test-windows-review.py`, `tests/test-windows-web.py` 分别覆盖 PowerShell 安装、不可用 `py.exe` 到 `python.exe` 的实际回退与 `.cmd` 入口、Windows 自动化文档的包装入口和 argv 边界、Windows console 领取/启动/PID/失败回滚、Win32 reparse/ACL/LockFileEx 文件安全 (含配置及 Git exclude 的 parent/leaf junction、FSCTL 原地切换、固定叶替换、创建时私有 DACL、创建碰撞、校验期写入/替换、无效配置 ACL、新建目录与相对 override)、Windows 审核 runtime 创建时 DACL、全生命周期 WRITE/DELETE 根句柄租约、根 FSCTL 切换拒绝、安全清理、临时根 reparse、隔离/超时/篡改检测与分发、原生 Windows Web 的 UTF-8 HTTP 端到端流程.
- 运行时创建的 `kanban/` 是本机共享数据, 不属于仓库源码, 不得提交.

## Build, Test, and Development Commands

本项目核心仅依赖 Python 标准库, 无构建步骤. POSIX 入口依赖 POSIX shell, 原生 Windows 安装入口依赖 PowerShell; Windows TUI 另要求运行时 Python 提供可用 curses 后端, 不是第一阶段必需能力.

POSIX:

```sh
./install.sh
python3 bin/kanban --help
python3 tests/test-onevoke.py
python3 tests/test-kanban.py
python3 tests/test-merge-worktree-memory.py
python3 tests/test-codex-review.py
python3 tests/test-claude-review.py
python3 tests/test-grok-review.py
python3 -m py_compile bin/onevoke bin/onevoke_config.py bin/onevoke_fs.py bin/onevoke_review.py bin/kanban bin/kanban_web.py bin/merge-worktree-memory.py tests/*.py
sh -n install.sh && sh -n bin/onevoke-review.sh
```

原生 Windows (PowerShell):

```powershell
.\install.ps1
py -3 bin\kanban --help
py -3 tests\test-merge-worktree-memory.py
py -3 tests\test-install-windows.py
py -3 tests\test-windows-automation.py
py -3 tests\test-windows-console.py
py -3 tests\test-windows-fs.py
py -3 tests\test-windows-review.py
py -3 tests\test-windows-web.py
py -3 -m py_compile bin\onevoke bin\onevoke_config.py bin\onevoke_fs.py bin\onevoke_review.py bin\kanban bin\kanban_web.py bin\merge-worktree-memory.py tests\test-install-windows.py tests\test-windows-automation.py tests\test-windows-console.py tests\test-windows-fs.py tests\test-windows-review.py tests\test-windows-web.py
```

测试默认针对当前工作树. `tests/test-kanban.py` 可用 `KANBAN_COMMAND` 指向别的入口; 三个 POSIX 审核测试和 `tests/test-windows-review.py` 都用假 Codex/Claude/Grok 二进制驱动, 不调用真的 CLI, 也不产生网络请求. Windows 专项测试只在原生 Windows 运行, 其他平台会 skip; POSIX 的 pty/tmux 专项测试不作为 Windows 第一阶段门禁.

`install.sh` 和 `install.ps1` 都复制 `bin/` 和 `rules/` 下全部普通文件, 仅在 `~/.agents/AGENTS.md` 不存在时创建受支持的链接入口, 最后运行 welcome. Windows 安装器安装到 `~/.local/bin`, 不自动修改 `PATH`, 由 `.cmd` 包装器统一启用 UTF-8 后进入 Python 实现. 手工试验时, POSIX 必须设置临时 `HOME`, Windows 必须设置临时 `USERPROFILE`; 两个平台还必须同时设置临时 `ONEVOKE_CONFIG` 和 `KANBAN_DIR`, 不得修改真实配置或看板.

## Coding Style & Naming Conventions

使用 Python 3、UTF-8、4 空格缩进及标准库优先的实现. 函数和变量采用 `snake_case`, 类采用 `PascalCase`, 常量采用 `UPPER_SNAKE_CASE`. 保持函数职责单一, 对无效输入抛出 `KanbanError`, 不静默忽略失败.

Shell 脚本使用 2 空格缩进, `set -eu`, 引用所有变量展开, 错误信息写 stderr 并返回非零状态.

PowerShell 脚本使用 `$ErrorActionPreference = "Stop"`, 字符串路径经 `-LiteralPath` 或 .NET 路径 API 传递, 用户可见错误写 stderr 并返回非零状态. `.cmd` 只作人工交互 shell 的 UTF-8 Python 启动包装, 不承载业务或安全门禁逻辑, 也不作为任意 argv 的程序调用边界; 含特殊字符的自动化必须用进程 API 的 argv 数组直接调用显式 Python 解释器和对应 Python 入口, 不得再经过 PowerShell/cmd 命令字符串. 包装器在首个外部命令前禁用当前目录可执行文件搜索, 只用绝对 `where.exe` 从 `PATH` 找候选, 验证 Python 3 后再运行.

任务 ID 必须匹配 `YYYYMMDD-short-slug-task`; slug 仅使用小写 ASCII 字母、数字和连字符. 用户可见错误信息及规则文档沿用中文和 ASCII 标点. CLI、TUI、Web、`install.sh` 与 `install.ps1` 的用户可见输出默认中文; 语言优先级为 `--lang` > `config.json` 的 `language` > 环境变量 (`ONEVOKE_LANG`/`LC_ALL`/`LC_MESSAGES`/`LANG` 的 `en` 前缀选英文). `language` 可在 `onevoke welcome` 配置. Windows Python 和 `.cmd` 入口必须保持 UTF-8 输入输出, 不依赖活动代码页.

## Testing Guidelines

测试框架为 `unittest`; 测试方法命名为 `test_<behavior>`. 每项行为变更至少覆盖成功路径和相关拒绝路径. 使用 `TemporaryDirectory` 隔离文件系统状态, 不依赖或改写用户真实看板, 不写入真实 `$HOME`. 提交前运行完整测试命令; 当前项目未设置覆盖率阈值.

## Commit & Pull Request Guidelines

新提交使用简短中文动宾 subject, 每个 commit 只包含一个关注点, 例如 `修复重复任务检测`. 改完后按本仓库特例完成合入 `main`、推送与本机平台安装器更新, 无需再等用户催促. PR 应说明行为变化、原因和实际验证命令; 关联任务或 issue. CLI 输出变化附终端示例, 无界面改动时无需截图.

## Security & Configuration

`KANBAN_DIR` 仅用于测试、非 Git 项目或明确覆盖. 不提交 token、凭据、敏感服务地址、真实任务卡片或本机路径. 文件写入和状态迁移必须继续经过现有校验, 不得绕过 `scan()` 或 `validate_target()` 直接操作任务入口.

`onevoke_review.py` 的只读隔离、commit 校验和 worktree 篡改检测是审核门禁的一部分; POSIX 必须经 `onevoke-review.sh`; Windows 人工显式调用经 `onevoke-review.cmd`, Onevoke 内部程序化分发由 `onevoke review` 直接进入同目录 Python 门禁以保全 argv. 其他调用不得绕过 Onevoke 入口直调 reviewer CLI, 也不得为方便调试而放宽 agent 隔离参数.

Windows 看板文件操作必须继续拒绝符号链接、junction 和其他 reparse point, 并使用已校验 Win32 句柄维持 no-follow 与原子迁移语义; 私密文件和目录必须使用当前用户独占的受保护 DACL; 记忆合并锁必须使用 `LockFileEx`. 任一安全后端失败都显式中止, 不静默回落到普通路径 API、继承 ACL 或无锁执行.
