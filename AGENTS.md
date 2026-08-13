# Repository Guidelines

本文件是 Onevoke 仓库自身的开发规则. 仓库对外发布的工作流规则在 `rules/`, 那些文件是交付物, 不是本仓库的开发指引.

## 本仓库特例

- 本仓库第二阶段安全角色 `CSA` 和 `Hacker` 一律标记 N/A, 不运行; `PM` 和 `QA` 保持适用.
- 审核 base 以来全部改动都是 Markdown 规则或文档时, 不运行审核. 只要包含任一脚本, 代码或其他非 Markdown 文件, 就按适用规则运行 `PM` 和 `QA`; `CSA` 和 `Hacker` 仍按上一条标记 N/A.
- 对外发布的分支模型固定为 `main` 稳定分支加 `develop` 集成分支, 不提供其他长期分支或集成分支选项; 缺少 `develop` 时从 `main` 自动初始化.

## Project Structure & Module Organization

- `rules/ONEVOKE-AGENTS.md` 是发布规则的入口, 只放分册索引, 优先级和默认行为. 其余分册由它的分册表按需引用: `BASE-RULES.md` 跨项目通用条款, `KANBAN-RULES.md` 看板行为契约, `GIT-RULES.md` Git 工作流, `REVIEW-RULES.md` 审核契约, `CODE-RULES.md` 架构与代码质量契约. 它们是面向用户和 Agent 的对外接口, 改动前确认与 `bin/` 下实现一致. 全部装到 `~/.agents/` 下的同名文件.
- `install.sh` 遍历 `bin/*` 和 `rules/*.md`, 把全部普通文件直接覆盖到 `~/.local/bin/` 与 `~/.agents/`, 包括 `ONEVOKE-AGENTS.md`. `~/.agents/AGENTS.md` 不存在时创建指向 `ONEVOKE-AGENTS.md` 的相对符号链接, 已有任何同名入口时保持不变. 唯一稳定 stdout 是 `Onevoke installed`; 最后必须用绝对路径运行 `onevoke welcome`. 同名目标是目录时须在写任何文件前拒绝, 防止 `install` 把源文件塞进错误目录.
- `bin/onevoke_config.py` 是 `onevoke` 与 `kanban` 共用的配置边界, 配置默认在 `~/.config/onevoke/config.json`, 测试用 `ONEVOKE_CONFIG` 隔离. 配置写入必须校验 schema, 用同目录临时文件加 `os.replace()` 原子替换, 权限为 `0600`.
- `bin/onevoke` 提供 `welcome`, `doctor`, `config`, `review`. welcome 只在 tty 中提问, 无 tty 时诊断后正常提示重跑; 依赖安装必须经用户明确选择. MemSearch Codex 插件只克隆官方仓库并运行上游安装脚本, 不检查仓库和安装状态. `review` 按角色配置分发到 Codex 或 Grok wrapper, 当前任务或项目明确覆盖时可直接调用对应 wrapper.
- `bin/kanban` 的 `start` 未传 `--agent` 时读取生效的 `kanban_agent`; `--agent` 始终优先. `--launcher` 可覆盖本次启动且不改机器配置; launcher 为 `tmux` 时沿用独立 window, 为 `foreground` 时必须有交互 tty 并在当前终端等待 Agent 退出.
- 新增分册时把它加进 `ONEVOKE-AGENTS.md` 的分册表即可; `install.sh` 和安装测试都遍历 `rules/*.md`, 不必改.
- 本仓库根目录的 `AGENTS.md` 是本仓库自己的开发规则, 与 `rules/` 下的发布物是两回事, 不要混改.
- `bin/kanban` 是 Python 3 CLI 的唯一实现, 包含看板定位、任务校验、状态迁移和命令解析. `bin/onevoke` 负责首次引导、环境诊断、配置展示和 Reviewer 分发.
- `bin/codex-review.sh` 与 `bin/grok-review.sh` 是审核 wrapper, 分别只读运行 Codex CLI 与 Grok CLI 并输出角色报告. `bin/merge-worktree-memory.py` 在集成后合并 worktree 的 memsearch 记忆, 并清除合并结果中的非法 UTF-8 字节.
- `tests/test-onevoke.py` 用临时 HOME 和伪终端覆盖 welcome、配置和 Reviewer 分发. `tests/test-kanban.py` 覆盖看板生命周期、两种 launcher、安装及初始化. `tests/test-merge-worktree-memory.py` 覆盖记忆合并; 两个审核测试覆盖 wrapper 门禁.
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
python3 tests/test-grok-review.py
python3 -m py_compile bin/onevoke bin/onevoke_config.py bin/kanban bin/merge-worktree-memory.py tests/*.py
sh -n install.sh && bash -n bin/codex-review.sh bin/grok-review.sh
```

测试默认针对当前工作树. `tests/test-kanban.py` 可用 `KANBAN_COMMAND` 指向别的入口; 两个审核测试分别用假 Codex/Grok 二进制驱动, 不调用真的 CLI, 也不产生网络请求.

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

两个审核 wrapper 的只读 sandbox、commit 校验和 worktree 篡改检测是审核门禁的一部分, 不得为方便调试而放宽.
