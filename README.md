# Onevoke

Onevoke - One person. Many agents.

一个人调度多个 AI Agent 的本地开发工作流: 一套全局规则, 一个文件看板, 一个代码审核门禁. 没有服务端, 没有数据库, 没有守护进程, 全部基于本机文件和标准库.

## 三块内容

| 位置 | 作用 |
|---|---|
| `rules/SOLO-AGENTS.md` | 全局规则入口. 优先级、交流格式、工作原则、记忆与验证, 其余按需引用下面几份 |
| `rules/GIT-RULES.md` | Git 工作流. worktree 隔离、提交与 push 策略、集成与清理 |
| `rules/CODE-RULES.md` | 架构与代码质量契约. 模块边界、依赖方向、抽象门槛、错误处理与测试要求 |
| `rules/KANBAN-RULES.md` + `bin/kanban` | 文件看板. 任务卡片的状态流转、领取并发、文档完整性门禁 |
| `rules/CODEX-REVIEW-RULES.md` + `bin/codex-review.sh` | 审核门禁. 三阶段规则, 与只读跑 `PM` / `QA` / `CSA` / `Hacker` 四个角色的 wrapper |

`bin/merge-worktree-memory.py` 在任务集成后把 worktree 的 memsearch 记忆并回主树, 顺带清掉记忆里的非法 UTF-8 字节.

## 依赖

- Python 3 (仅标准库), Git, POSIX shell
- Codex CLI (`codex`) — 审核门禁必需
- tmux — `kanban start` 启动 Agent 需要

memsearch 是**可选**的, 需要自己安装. 装了才适用 `SOLO-AGENTS.md` 的「记忆管理」一节; 没装时该节不适用, `merge-worktree-memory.py` 报告无事可做并以 0 退出, 集成流程可以无条件调用它.

## 安装

```sh
./install.sh
```

命令装到 `~/.local/bin/` (`kanban`, `codex-review.sh`, `merge-worktree-memory.py`), `rules/` 下全部规则装到 `~/.agents/`.

安装器**不碰** `~/.agents/AGENTS.md` — 那是你自己的全局规则, 与本仓库无关. 规则文件都由本仓库拥有, 每次安装直接覆盖.

装完规则只是躺在 `~/.agents/`, **还不生效**. 接入是单独一步, 安装器不做 — 见下一节.

## 接入

安装器不写任何 Agent 的配置目录. 两边机制不同, 分开说.

### Claude Code

`CLAUDE.md` 支持 `@path` 导入, 和你自己的内容共存. 在 `~/.claude/CLAUDE.md` 里加一行:

```markdown
@~/.agents/SOLO-AGENTS.md

## 我自己的规则

<你原有的内容照旧写在下面>
```

导入在会话启动时展开, 绝对路径和相对路径都支持, 最多递归 4 层. 升级 Onevoke 后自动生效, 不用重新接.

### Codex

Codex **没有导入指令**. 它把全局 `~/.codex/AGENTS.md`、项目根 `AGENTS.md`、子目录 `AGENTS.md` 按顺序拼接, 越靠近当前目录的优先级越高, 而全局只有一个位置. 所以分两种情况.

**没有自己的全局规则** — 软链就行, 升级自动生效:

```sh
ln -s ~/.agents/SOLO-AGENTS.md ~/.codex/AGENTS.md
```

**已经有自己的全局规则** — 没有干净解, 两条路各有代价, 自己挑:

| 做法 | 得到 | 代价 |
|---|---|---|
| 软链 `SOLO-AGENTS.md`, 个人规则下沉到各项目的 `AGENTS.md` | 升级自动传播 | 个人偏好要在每个项目重复一遍 |
| 保留自己的 `~/.codex/AGENTS.md`, 把 `SOLO-AGENTS.md` 内容合并进去 | 一个文件管全部 | 升级不会传播, 每次都要重新合并 |

### 容量上限

Codex 的 `project_doc_max_bytes` 默认 32 KiB, 全局与项目的 `AGENTS.md` 合计超过就**静默截断**, 不报错. `SOLO-AGENTS.md` 约 3.7 KiB, 留给项目级的还有约 28.3 KiB — 其余规则是按需读取的独立文件, 不占这个预算. 不够时在 `~/.codex/config.toml` 调高:

```toml
project_doc_max_bytes = 65536
```

Claude Code 的 `@` 导入不受这个限制.

## 看板

在项目主 worktree 初始化, 数据在 `kanban/`, 不进 Git:

```sh
kanban init
kanban new feature login-retry 登录重试              # 在 backlog/ 生成卡片
$EDITOR kanban/backlog/20260802-login-retry-task.md  # 填掉 <填写> 占位
kanban pick 20260802-login-retry-task
kanban start                                         # 选一张 todo 卡片启动 Agent
kanban list working
```

编辑那一步不能跳过: 新卡片的任务目标、预期成果、验收条件和不在本轮范围都是 `<填写>` 占位, `move ... todo` 会拒绝并列出缺哪几项.

状态流转单向: `backlog -> todo -> working -> done -> archived`. 每次迁移由命令校验文档完整性 — 进 `todo` 要有上述四项, 进 `done` 要有完成总结或 `report.md`.

并发领取靠同一文件系统上的原子重命名, 只有 `move` 成功的那个 Agent 拿到任务. 完整规则见 `kanban rules`.

## 审核

代码任务在独立 worktree 完成、提交、验证后, 按阶段串行审核:

```sh
codex-review.sh <worktree绝对路径> <base-commit-SHA> <commit-SHA> PM "<任务目标>"
codex-review.sh <worktree绝对路径> <base-commit-SHA> <commit-SHA> QA "<任务目标>"
```

- 第一阶段 `PM` 核对实现是否完整达到任务目标
- 第二阶段按改动风险决定是否运行 `CSA` 和 `Hacker`, 未触发标记 N/A
- 第三阶段 `QA` 核对功能正确性、回归、测试与代码质量

所有输出按 `blocking` / `high` / `medium` / `low` / `推荐` / `建议` 六档标注, 只有前三档必须修. 修复只重跑当前阶段: `PM` 的修复重跑 `PM`, 安全角色的修复重跑安全角色, `QA` 的修复只重跑 `QA` — 同一 base 下靠前阶段的结论沿用. 唯一例外是 `QA` 修复动了安全相关代码, 那要把已触发的安全角色重跑一遍. wrapper 以只读 sandbox 运行, 结束时校验 worktree 未被改动; 参数、commit 关系和工作树清洁度都在调用前校验.

可调环境变量: `CODEX_REVIEW_MODEL` (默认 `gpt-5.6-sol`), `CODEX_REVIEW_REASONING_EFFORT` (默认 `high`), `CODEX_REVIEW_MAX_RUNTIME_SECONDS` (默认 1800).

## 开发

见 `AGENTS.md`.

```sh
KANBAN_COMMAND="$PWD/bin/kanban" python3 tests/test-kanban.py
```
