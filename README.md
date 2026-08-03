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
| `rules/GROK-REVIEW-RULES.md` + `bin/grok-review.sh` | 明确指定 Grok 时使用的等价审核门禁 |

`bin/merge-worktree-memory.py` 在任务集成后把 worktree 的 memsearch 记忆并回主树, 顺带清掉记忆里的非法 UTF-8 字节.

## 依赖

- Python 3 (仅标准库), Git, POSIX shell
- Codex CLI (`codex`) — 默认审核门禁必需; 使用 Grok 审核时改需 Grok CLI (`grok`)
- tmux — `kanban start` 启动 Agent 需要

memsearch 是**可选**的, 需要自己安装. 装了才适用 `SOLO-AGENTS.md` 的「记忆管理」一节; 没装时该节不适用, `merge-worktree-memory.py` 报告无事可做并以 0 退出, 集成流程可以无条件调用它.

## 安装

```sh
./install.sh
```

命令装到 `~/.local/bin/` (`kanban`, `codex-review.sh`, `grok-review.sh`, `merge-worktree-memory.py`), `rules/` 下全部规则装到 `~/.agents/`.

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

## 完整工作流

Onevoke 把需求讨论和任务执行分开. 人始终保留一个需求会话, 用高推理强度模型逐项定方案; 已定案的任务交给独立 tmux window 执行, 原会话立即进入下一轮讨论.

- 需求会话是决策面: 使用 Plan mode 明确需求、方案、验收条件和范围.
- 主 worktree 是协调面: 放唯一的本机看板, 由人确认任务进入 `todo`.
- 任务 worktree 是执行面: 每张已领取的代码任务独占一个分支和 worktree.
- Codex 或 Claude 是执行 Agent: 读取规则与任务卡, 实现、验证、提交和集成.
- Codex 审核角色是独立门禁: `PM` 检查需求完整性, `QA` 检查正确性与回归, 安全角色按风险触发.

![Onevoke 工作流](docs/workflow.svg)

### 1. 新建计划会话

每轮从一个干净会话开始. 创建新会话后立即进入 Plan mode, 让 Agent 先分析和提问, 不直接改文件.

需求讨论使用高推理强度模型:

- Codex: `gpt-5.6-xhigh`.
- Claude: `opus-xhigh`.

高推理强度只用于需求澄清和方案决策. 后续 `kanban start` 会按任务规模选择执行模型和推理强度, 不必让计划会话继续承担实现.

### 2. 在 Plan mode 中确定方案

在 Plan mode 中和 Agent 反复讨论, 直到实现方向、边界和验收方式都明确. 不先写一份脱离上下文的长规格, 但讨论至少要落到四件事:

- 任务目标: 改什么, 为什么改.
- 预期成果: 完成后能观察到什么.
- 验收条件: 用什么命令、行为或人工检查判定完成.
- 不在本轮范围: 哪些相关工作明确不做, 以及原因.

方向和取舍由人拍板. Agent 可以提出方案, 但不能把自己的建议写成 "用户决策". 稳定的架构、API 或长期规则最终仍写回仓库文档或项目 `AGENTS.md`, 卡片不充当永久知识库.

方案未确定时继续留在 Plan mode, 不建任务卡. 只有方案细节已定、四项契约可直接写清时才退出 Plan mode.

### 3. 退出 Plan mode 并创建任务卡

退出 Plan mode 后, 明确让当前 Agent 使用 `kanban new` 创建任务卡. Agent 必须基于刚完成的讨论填完整卡片, 不能只生成带 `<填写>` 的空模板.

```sh
kanban new feature login-retry 登录重试
```

新卡片先进入 `backlog`; 这表示 "方案已记录", 还未进入执行队列. Agent 创建后应返回任务 ID, 并确认任务目标、用户决策、预期成果、验收条件、威胁模型和不在本轮范围均已填写.

任务默认建成一个 Markdown 文件. 只有需要独立 `spec.md`、分阶段计划和最终报告时才用 `--large`; 行数多本身不是大任务. 能独立领取、验收或取消的工作应拆成多张卡, 同目标、同负责人、同生命周期的内容留在一张卡内.

### 4. 在单独的 tmux window 启动任务

切换到专门用于协调任务的 tmux window. 推荐直接告诉协调 Agent:

```text
开始任务卡 20260802-login-retry-task
```

协调 Agent 应先读 `kanban rules` 和卡片, 再依次执行 `pick` 和 `start`. 也可手动运行等价命令:

```sh
kanban pick 20260802-login-retry-task
kanban start 20260802-login-retry-task
# 或
kanban start --agent claude 20260802-login-retry-task
```

`pick` 会执行带完整性校验的 `backlog -> todo`. 卡片仍有缺失或 `<填写>` 时会拒绝, 此时回需求会话补清楚, 不绕过门禁.

不传任务 ID 时, `kanban start` 只列 `todo` 任务供人选择, 不猜优先级. 启动时会依次完成:

1. 校验 tmux、Agent 命令和任务状态.
2. 用原子重命名领取卡片, 执行 `todo -> working`.
3. 写入负责人和开始时间.
4. 在当前 tmux session 新建 `kb-<slug>` window, cwd 为项目根.
5. 要求新 Agent 先读 `kanban rules`、任务卡和项目规则, 再准备代码工作区.

小任务默认使用中等推理强度, 大任务使用高推理强度. Codex 固定用 `gpt-5.6-sol`, Claude 固定用 `opus`. `kanban start` 默认以 YOLO 模式启动, 会绕过 Agent 自身的 approval 或 permission 提示; 因此只应在可信本机和已确认范围内使用.

只有 `todo` 卡能启动. tmux window 创建前失败会回滚卡片; window 已创建后 Agent 认证失败、退出或中断, 卡片继续留在 `working`, 不自动重派.

### 5. 回到需求会话讨论下一项

任务启动成功后, 不在协调 window 等它完成. 回到刚才讨论需求的会话, 执行 `/new` 创建干净上下文, 再次进入 Plan mode, 用 `gpt-5.6-xhigh` 或 `opus-xhigh` 讨论下一个需求.

此时形成稳定流水线:

- 需求会话串行做决策, 每次只讨论一个需求.
- 每个已定案任务在独立 tmux window 和 worktree 中执行.
- 看板显示所有任务处于 `backlog`、`todo`、`working`、`done` 中的哪个阶段.
- 人只在方案确认、任务优先级、审核风险和最终验收处介入.

不要在新会话继续携带上一项实现细节. 上一项的契约已在任务卡, 执行状态已在看板, 实现 Agent 会独立完成后续链路.

### 6. 隔离实现

执行 Agent 先检查主工作树, 再按项目规则准备工作区:

- 纯 Markdown 小改, 且默认分支干净并已同步 upstream 时, 可直接修改默认分支.
- 其他改文件任务使用独立任务分支和 `<仓库根>/worktrees/<task-name>/`.
- 有 `origin` 时先 fetch, 从最新远端默认分支建任务分支; 无 `origin` 时明确走本地集成路径.
- 主 worktree 的 `kanban/` 是唯一看板. 任务 worktree 不复制、不链接、不提交它.

Agent 在任务 worktree 里先找既有实现和调用链, 再做最小正确改动. 改代码前读取 `CODE-RULES.md`; 不扩写无关重构, 不覆盖用户已有修改. 验证以能直接证明本次行为的最小命令开始, 风险或影响面较大时再扩大测试范围.

实施期只把关键决策、实际命令、结果、环境缺口和 commit 写回任务卡. 不复制整段会话流水. 大任务把计划写进 `plan.md`, 完成后把实际结果写进 `report.md`.

### 7. 提交与同步任务分支

一个独立关注点一个 commit, subject 使用简短中文动宾短语. 有可写 `origin` 时推任务分支; 无远端时保留本地提交并明确报告, 不把 "未 push" 说成 "已完成远端交付".

push 被 non-fast-forward 拒绝时先 fetch、rebase、重新验证. 只允许对任务分支使用 `--force-with-lease`; 默认集成分支永不 force-push.

### 8. 走审核闭环

代码和验证完成后, 基于同一个集成分支 commit 作为审核 base, 按顺序运行:

```text
PM -> CSA/Hacker (按风险触发) -> QA
```

- `PM` 检查任务目标、用户决策、预期成果和范围是否完整落实.
- `CSA` 只在不可信输入、认证授权、凭据、加密、网络、远程执行、文件写入或发布完整性等改动中触发.
- `Hacker` 只在新增或实质改变外部攻击面、安全专项审核或用户明确要求时触发.
- `QA` 固定最后, 检查功能、边界、回归、测试和代码质量.

reviewer 的报告不是自动真理. 主代理必须回到代码、规则和可运行证据逐项核实. 只有核实成立的 `blocking`、`high`、`medium` 必修; `low`、`推荐`、`建议` 不阻塞, 但必须在闭环结束时完整展示处理结论和来源.

修复只重跑当前阶段. `QA` 修复若实质改动安全相关代码, 先重跑已触发的安全角色, 再回 `QA`. 同一 base 下已通过的前序阶段沿用; rebase 改变 base 后从 `PM` 重启.

全是 Markdown 的改动, 或只改一个文件且增删合计不超过 10 行时, 可按规则豁免审核. 豁免必须明确告诉人 "本次未走审核闭环" 及触发条件. 人也可明确要求跳过审核, 但接受的风险要进入交付说明.

本仓库自身有特例: `CSA` 和 `Hacker` 一律 N/A, 只运行 `PM` 和 `QA`. 安装到其他项目后仍按那些项目的风险和规则判断.

### 9. 验收、集成与清理

审核通过不等于任务完成. 特别是 Bug 修复, 必须等人确认实际问题已解决; 未验收时卡片继续留在 `working`.

验收后, Agent 将任务分支 rebase 到最新集成分支并重新验证. 若无实质冲突, 沿用已完成的审核门; 有本人手工解决的代码冲突时重新审核. 集成只允许 fast-forward 或项目规定的 PR 流程, 不产生 merge commit.

直接远端集成的顺序是:

1. 非 force push 最终任务 commit 到远端默认分支.
2. fetch 远端状态.
3. 主 worktree 用 `git merge --ff-only` 同步.
4. 确认任务 commit 已进入集成分支.
5. 运行 `merge-worktree-memory.py --source <worktree-path>`.
6. 删除任务 worktree、本地任务分支和远端任务分支.

主 worktree 因用户改动、本地领先或分叉而不能 fast-forward 时, 不擅自 stash、reset 或提交这些改动. 只要已确认远端集成成功, 仍可合并任务记忆并清理任务 worktree, 同时报告主树未同步的原因和恢复办法.

未安装 memsearch 时, 记忆合并命令是成功的空操作. 安装后, 它会合并并去重任务 worktree 的会话记录, 清除非法 UTF-8 字节, 再允许清理 worktree.

### 10. 完成卡片

只有以下条件全部满足, 卡片才能从 `working` 进入 `done`:

- 实现完成.
- 验证完成, 环境缺口已记录.
- 必要审核完成或跳过风险已明确接受.
- 用户验收完成.
- 代码已按项目规则集成.
- 小任务写完完成总结; 大任务写完 `report.md`.

然后填 `结果: completed`, 再迁移:

```sh
kanban move 20260802-login-retry-task done
kanban list done
```

`done` 保留近期已验收任务. 人确认无需继续展示后再移入 `archived`. 取消、重复或明确不修的任务也可归档, 但必须记录 `cancelled`、`duplicate` 或 `wontfix` 及原因. `trash` 只用于人明确要求删除的卡片, Agent 不自动清空.

### 中断与阻塞

- 实现难、测试失败或暂时缺环境: 留在 `working`, 写清阻塞与解除条件, 不自动归档.
- Agent 启动后退出: 留在 `working`, 由人决定继续、重派或归档.
- 看板发现重复 ID、跨状态副本、损坏入口: 停止受影响任务, 保留现场, 不自行删改绕过校验.
- 审核的 `PM` 或 `QA` 持续后端故障: 保留分支和 worktree, 停止集成, 由人决定重试、改期或承担风险跳过.
- 工作树含用户修改: 保留修改. 若无法隔离, 报告冲突点, 不替用户整理现场.

## 看板命令参考

在项目主 worktree 初始化, 数据在 `kanban/`, 不进 Git:

```sh
kanban init
```

推荐由计划会话中的 Agent 建卡. 对 Agent 说 "用 kanban new 创建任务卡", Agent 会运行 `new` 并基于当前讨论填完整内容:

```sh
kanban new feature login-retry 登录重试
```

然后在 tmux 协调 window 对 Agent 说 "开始任务卡 20260802-login-retry-task". Agent 会执行:

```sh
kanban pick 20260802-login-retry-task
kanban start 20260802-login-retry-task
kanban list working
```

如果人手动运行 `kanban new`, 则还要用编辑器填掉模板内全部 `<填写>` 占位. Agent 驱动的推荐流程会在建卡时完成这一步. 无论哪种方式, `pick` 都会拒绝契约不完整的卡片并列出缺失项.

状态流转单向: `backlog -> todo -> working -> done -> archived`. 每次迁移由命令校验文档完整性 — 进 `todo` 要有上述四项, 进 `done` 要有完成总结或 `report.md`.

并发领取靠同一文件系统上的原子重命名, 只有 `move` 成功的那个 Agent 拿到任务. 完整规则见 `kanban rules`.

## 审核命令参考

代码任务在独立 worktree 完成、提交、验证后, 按阶段串行审核:

```sh
codex-review.sh <worktree绝对路径> <base-commit-SHA> <commit-SHA> PM "<任务目标>"
codex-review.sh <worktree绝对路径> <base-commit-SHA> <commit-SHA> QA "<任务目标>"
```

明确指定 Grok 时, 把上述命令名换成 `grok-review.sh`.

- 第一阶段 `PM` 核对实现是否完整达到任务目标
- 第二阶段按改动风险决定是否运行 `CSA` 和 `Hacker`, 未触发标记 N/A
- 第三阶段 `QA` 核对功能正确性、回归、测试与代码质量

所有输出按 `blocking` / `high` / `medium` / `low` / `推荐` / `建议` 六档标注, 只有前三档必须修. 修复只重跑当前阶段: `PM` 的修复重跑 `PM`, 安全角色的修复重跑安全角色, `QA` 的修复只重跑 `QA` — 同一 base 下靠前阶段的结论沿用. 唯一例外是 `QA` 修复动了安全相关代码, 那要把已触发的安全角色重跑一遍. wrapper 以只读 sandbox 运行, 结束时校验 worktree 未被改动; 参数、commit 关系和工作树清洁度都在调用前校验.

可调环境变量: `CODEX_REVIEW_MODEL` (默认 `gpt-5.6-sol`), `CODEX_REVIEW_REASONING_EFFORT` (默认 `high`), `CODEX_REVIEW_MAX_RUNTIME_SECONDS` (默认 1800).

Grok 默认使用 Grok CLI profile 配置的模型; 可用 `GROK_REVIEW_MODEL` 覆盖. 其余变量为 `GROK_REVIEW_REASONING_EFFORT` (默认 `high`), `GROK_REVIEW_MAX_RUNTIME_SECONDS` (默认 1800), profile 目录跟随 `GROK_HOME` (默认 `~/.grok`).

## 开发

见 `AGENTS.md`.

```sh
KANBAN_COMMAND="$PWD/bin/kanban" python3 tests/test-kanban.py
```
