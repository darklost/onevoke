# solo-mode

单人开发者的 AI Agent 工作流: 一套全局规则, 一个本地文件看板, 一个代码审核门禁.

面向一个人带多个 Agent 干活的场景. 没有服务端, 没有数据库, 没有守护进程, 全部是本机文件和标准库.

## 三块内容

| 位置 | 作用 |
|---|---|
| `rules/SOLO-AGENTS.md` | 全局工作流规则. Git worktree 隔离、提交与 push 策略、三阶段审核门、集成与清理 |
| `rules/KANBAN-RULES.md` + `bin/kanban` | 文件看板. 任务卡片的状态流转、领取并发、文档完整性门禁 |
| `bin/codex-review.sh` | 审核 wrapper. 用 Codex CLI 只读跑 `PM` / `QA` / `CSA` / `Hacker` 四个角色 |

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

命令装到 `~/.local/bin/` (`kanban`, `codex-review.sh`, `merge-worktree-memory.py`), 规则装到 `~/.agents/` (`SOLO-AGENTS.md`, `KANBAN-RULES.md`).

安装器**不碰** `~/.agents/AGENTS.md` — 那是你自己的全局规则, 与本仓库无关. 两份规则文件都由本仓库拥有, 每次安装直接覆盖.

装完后要让 Agent 真正读到规则, 这一步安装器不做. 没有自己的全局规则时直接软链:

```sh
ln -s ~/.agents/SOLO-AGENTS.md ~/.codex/AGENTS.md   # Codex
ln -s ~/.agents/SOLO-AGENTS.md ~/.claude/CLAUDE.md  # Claude Code
```

已经有自己的全局规则时, 由你决定怎么合并 — 直接把 `SOLO-AGENTS.md` 的内容并进去, 或在自己的规则里指向它.

## 看板

在项目主 worktree 初始化, 数据在 `kanban/`, 不进 Git:

```sh
kanban init
kanban new feature login-retry 登录重试              # 在 backlog/ 生成卡片
$EDITOR kanban/backlog/20260802-login-retry-task.md  # 填掉 <填写> 占位
kanban move 20260802-login-retry-task todo
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
- 第二阶段 `QA` 核对功能正确性、回归、测试与代码质量
- 第三阶段按改动风险决定是否运行 `CSA` 和 `Hacker`, 未触发标记 N/A

任一阶段有需修复的 finding, 修复提交后从第一阶段重启. wrapper 以只读 sandbox 运行, 结束时校验 worktree 未被改动; 参数、commit 关系和工作树清洁度都在调用前校验.

可调环境变量: `CODEX_REVIEW_MODEL` (默认 `gpt-5.6-sol`), `CODEX_REVIEW_REASONING_EFFORT` (默认 `high`), `CODEX_REVIEW_MAX_RUNTIME_SECONDS` (默认 1800).

## 开发

见 `AGENTS.md`.

```sh
KANBAN_COMMAND="$PWD/bin/kanban" python3 tests/test-kanban.py
```
