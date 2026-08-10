# Repository Guidelines

本文件是 Onevoke 仓库自身的开发规则. 仓库对外发布的工作流规则在 `rules/`, 那些文件是交付物, 不是本仓库的开发指引.

## 本仓库特例

- 本仓库第三阶段安全角色 `CSA` 和 `Hacker` 一律标记 N/A, 不运行; `PM` 和 `QA` 保持适用.

## Project Structure & Module Organization

- `rules/ONEVOKE-AGENTS.md` 是发布规则的入口, 只放分册索引, 优先级和三项默认取值 (分支, Reviewer, 看板任务完成). 其余分册由它的分册表按需引用: `BASE-RULES.md` 跨项目通用条款, `KANBAN-RULES.md` 看板行为契约, `GIT-RULES.md` Git 工作流, `REVIEW-RULES.md` 审核契约 (Codex 与 Grok 共用), `CODE-RULES.md` 架构与代码质量契约. 它们是面向用户和 Agent 的对外接口, 改动前确认与 `bin/` 下实现一致. 全部装到 `~/.agents/` 下的同名文件; 用户自己的 `~/.agents/AGENTS.md` 不是安装目标, 任何脚本都不得写它.
- 入口装的是用户可改的默认取值, 分册仍每次覆盖. `install.sh` 按 `entry_action` 四态处理入口: `seed` 缺失时种一份, `same` 与模板一致时静默跳过, `replace` 用户在提示里选了覆盖, `keep` 保留. 有 tty 时先打 `diff -u` 再让用户选, 只有明确回 `1` 才是 `replace`; 无 tty 一律 `keep` 并在 stderr 说明. 交互分支只能用伪终端测, 见 `tests/test-kanban.py` 的 `run_installer_on_a_tty`.
- `install.sh` 面向用户的输出一律中文; 唯一例外是 stdout 的 `Onevoke installed`, 它是测试和用户脚本都依赖的稳定契约, 不翻译也不加行. diff 用 sed 着色并跟随 `NO_COLOR`: 文件头只按 diff 的第 1, 2 行定位 (`1s` / `2s`), 其余行再按 `@@`, `-`, `+` 的行首着色. 不许改回 `^---` / `^+++` 前缀匹配 —— 规则文件里一条 `---` 被删掉后 diff 行是 `----`, 按前缀会被误判成文件头.
- 安装测试的 `subprocess.run` 一律显式传 `stdin=subprocess.DEVNULL`, 否则从终端跑测试时会落进交互分支卡住. 入口的三项取值高于分册, 分册在对应条款里显式让位; 改取值语义时必须同步对应分册的让位措辞.
- 过期入口的判据是正文不引用 `BASE-RULES.md`. 改入口模板时保留这个引用, 否则老用户拿不到升级提示.
- 入口存在却读不到 (悬空软链, 目录, 权限不足) 是坏现场, 不是"已保留": 安装器在装任何东西之前报错退 1, 不留半套状态, 也不把它诊断成过期入口.
- 新增分册时把它加进 `ONEVOKE-AGENTS.md` 的分册表即可; `install.sh` 和安装测试都遍历 `rules/*.md`, 不必改.
- 分册改名或合并时, 把旧文件名加进 `install.sh` 的 `stale_rules`. 安装器只覆盖不删除, 旧文件会以过期内容留在用户的 `~/.agents/`, 靠这份清单点名警告; 任何情况下都不得让脚本代为删除.
- 本仓库根目录的 `AGENTS.md` 是本仓库自己的开发规则, 与 `rules/` 下的发布物是两回事, 不要混改.
- `bin/kanban` 是 Python 3 CLI 的唯一实现, 包含看板定位、任务校验、状态迁移和命令解析.
- `bin/codex-review.sh` 与 `bin/grok-review.sh` 是审核 wrapper, 分别只读运行 Codex CLI 与 Grok CLI 并输出角色报告. `bin/merge-worktree-memory.py` 在集成后合并 worktree 的 memsearch 记忆, 并清除合并结果中的非法 UTF-8 字节.
- `tests/test-kanban.py` 使用临时目录覆盖小任务、大任务、非法迁移、无效入口隔离、归档、安装及初始化流程. `tests/test-merge-worktree-memory.py` 覆盖条目切分、哈希兼容性、去重、字节清理和原子替换. `tests/test-codex-review.py` 与 `tests/test-grok-review.py` 覆盖审核门禁的全部拒绝路径、隔离参数和篡改检测.
- 运行时创建的 `kanban/` 是本机共享数据, 不属于仓库源码, 不得提交.

## Build, Test, and Development Commands

本项目仅依赖 Python 标准库和 POSIX shell, 无构建步骤或依赖安装.

```sh
./install.sh
python3 bin/kanban --help
python3 tests/test-kanban.py
python3 tests/test-merge-worktree-memory.py
python3 tests/test-codex-review.py
python3 tests/test-grok-review.py
python3 -m py_compile bin/kanban bin/merge-worktree-memory.py tests/*.py
sh -n install.sh && bash -n bin/codex-review.sh bin/grok-review.sh
```

测试默认针对当前工作树. `tests/test-kanban.py` 可用 `KANBAN_COMMAND` 指向别的入口; 两个审核测试分别用假 Codex/Grok 二进制驱动, 不调用真的 CLI, 也不产生网络请求.

安装脚本把四个命令复制到 `~/.local/bin/`, 把 `rules/` 下全部规则复制到 `~/.agents/`, 不接受参数, 不读写用户的 `~/.agents/AGENTS.md`. 后续命令依次检查入口、测试当前工作树脚本和执行快速语法检查. 手工试验应设置临时 `KANBAN_DIR`, 不要污染真实看板.

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
