# Onevoke

Onevoke - One person. Many agents.

一个人调度多个 AI Agent 的本地开发工作流: 一套全局规则, 一个文件看板, 一个代码审核门禁. 没有服务端, 没有数据库, 没有守护进程, 全部基于本机文件和标准库.

## 三块内容

| 位置 | 作用 |
|---|---|
| `rules/ONEVOKE-AGENTS.md` | 全局规则入口. 分册索引、优先级和默认行为 |
| `rules/BASE-RULES.md` | 跨项目通用条款. 交流格式、工作原则、记忆管理、探索与验证、安全 |
| `rules/GIT-RULES.md` | Git 工作流. worktree 隔离、提交与 push 策略、集成与清理 |
| `rules/CODE-RULES.md` | 架构与代码质量契约. 模块边界、依赖方向、抽象门槛、错误处理与测试要求 |
| `rules/KANBAN-RULES.md` + `bin/kanban` | 文件看板. 任务卡片的状态流转、领取并发、文档完整性门禁 |
| `rules/REVIEW-RULES.md` | 审核门禁. 三阶段规则, 四个角色 `PM` / `QA` / `CSA` / `Hacker`, Codex 与 Grok 共用 |
| `bin/onevoke` | 首次引导、环境诊断、配置展示和 Reviewer 分发 |
| `bin/codex-review.sh` + `bin/grok-review.sh` | 两个 reviewer wrapper, 只读跑上述角色, 参数一致 |

`bin/merge-worktree-memory.py` 在任务集成后把 worktree 的 memsearch 记忆并回主树, 顺带清掉记忆里的非法 UTF-8 字节.

## 依赖

- Python 3 (仅标准库), Git, POSIX shell
- Codex、Claude 或 Grok 至少一个, 用于执行看板任务
- Codex CLI (`codex`) 或 Grok CLI (`grok`) 至少一个, 用于审核
- tmux 可选; 不安装时 `kanban start` 可在当前终端前台运行

memsearch 是可选依赖. welcome 检查 CLI 和可用 Agent 的插件, 缺失时询问是否安装. 没装时 `BASE-RULES.md` 的「记忆管理」不适用, 记忆合并命令仍以成功空操作退出.

## 安装

```sh
./install.sh
```

`bin/` 下全部普通文件装到 `~/.local/bin/`, `rules/` 下全部规则装到 `~/.agents/`. 所有目标每次直接覆盖, 包括 `ONEVOKE-AGENTS.md`; 本机选择保存在 `~/.config/onevoke/config.json`, 不再靠修改规则入口保存.

安装器**不碰** `~/.agents/AGENTS.md` — 那是你自己的全局规则, 与本仓库无关.

文件复制完成后, 安装器用绝对路径运行 `onevoke welcome`. welcome 会:

- 检查全部 Onevoke 命令是否能从 PATH 访问, 缺失时用醒目文字提示.
- 探测 Codex、Claude、Grok 的命令和版本, 分开展示执行、审核和 MemSearch 能力; 认证状态不冒充已验证.
- 选择 `kanban` 默认执行 Agent.
- 分别选择 `PM`、`CSA`、`Hacker`、`QA` 的 Reviewer.
- 选择 tmux window 或当前终端前台 launcher; tmux 缺失时询问安装, 拒绝后写入前台模式.
- 检查 MemSearch CLI 和 Agent 插件, 缺失时询问安装.
- 检查所选 Agent 是否已经接入 Onevoke 规则, 但不覆盖用户的 Agent 全局配置.

Codex 的 MemSearch 自动安装固定 PyPI 版本和上游 commit, 只执行 commit 匹配且工作树
clean 的插件源码; 既有缓存被修改或出现未跟踪文件时会拒绝执行.

welcome 只在 stdin 和 stderr 都是 tty 时提问. CI、管道或 hook 中仍完成文件安装, 输出诊断并提示之后在终端运行:

```sh
onevoke welcome
```

配置完成后可用 `onevoke doctor` 重新诊断, `onevoke config` 查看配置, `onevoke welcome --reset` 重新选择. welcome 遵守 `NO_COLOR`, Ctrl-D 或 Ctrl-C 时不写半套配置.

安装器不删除旧文件. 从早期版本升级时, `SOLO-AGENTS.md`、`CODEX-REVIEW-RULES.md` 和 `GROK-REVIEW-RULES.md` 仍需用户确认接入路径后手动清理.

装完规则只是躺在 `~/.agents/`, 还不生效. welcome 会检查接入状态并指向下一节, 但不会擅自改 Agent 配置.

## 接入

安装器不写任何 Agent 的配置目录. 两边机制不同, 分开说.

### Claude Code

`CLAUDE.md` 支持 `@path` 导入, 和你自己的内容共存. 在 `~/.claude/CLAUDE.md` 里加一行:

```markdown
@~/.agents/ONEVOKE-AGENTS.md

## 我自己的规则

<你原有的内容照旧写在下面>
```

导入在会话启动时展开, 绝对路径和相对路径都支持, 最多递归 4 层. 升级 Onevoke 后自动生效, 不用重新接.

### Codex

Codex **没有导入指令**. 它把全局 `~/.codex/AGENTS.md`、项目根 `AGENTS.md`、子目录 `AGENTS.md` 按顺序拼接, 越靠近当前目录的优先级越高, 而全局只有一个位置. 所以分两种情况.

**没有自己的全局规则** — 软链就行, 升级自动生效:

```sh
ln -s ~/.agents/ONEVOKE-AGENTS.md ~/.codex/AGENTS.md
```

**已经有自己的全局规则** — 没有干净解, 两条路各有代价, 自己挑:

| 做法 | 得到 | 代价 |
|---|---|---|
| 软链 `ONEVOKE-AGENTS.md`, 个人规则下沉到各项目的 `AGENTS.md` | 分册升级自动传播 | 个人偏好要在每个项目重复一遍 |
| 保留自己的 `~/.codex/AGENTS.md`, 把 `ONEVOKE-AGENTS.md` 内容合并进去 | 一个文件管全部 | 升级不会传播, 每次都要重新合并 |

### Grok

Grok 的接入目标是 `~/.grok/AGENTS.md`. 可以把它软链到入口, 或把
`~/.agents/ONEVOKE-AGENTS.md` 的内容合并进已有文件; welcome 只检查接入状态, 不会修改该文件.

### 自动载入的只有入口

无论哪种接入方式, 会话启动时自动载入的都只有入口这一个文件. 分册 (含通用条款 `BASE-RULES.md`) 靠入口的分册表按需读取 — 表里写明了每份该在什么时机读, `BASE-RULES.md` 是每个任务开始时.

### 容量上限

Codex 的 `project_doc_max_bytes` 默认 32 KiB, 全局与项目的 `AGENTS.md` 合计超过就**静默截断**, 不报错. 入口 `ONEVOKE-AGENTS.md` 约 2.3 KiB, 留给项目级的还有约 29.7 KiB — 分册是按需读取的独立文件, 不占这个预算. 不够时在 `~/.codex/config.toml` 调高:

```toml
project_doc_max_bytes = 65536
```

Claude Code 的 `@` 导入不受这个限制.

## 固定工作流与本机配置

规则入口固定跨机器一致的分支模型, 并保留完成时机默认值. 本机执行和审核工具由 welcome 配置:

| 取值 | 默认 |
|---|---|
| 分支 | 固定长期分支 `main` + `develop`; `develop` 是唯一集成分支, 缺失时从 `main` 自动初始化; `main` 只在用户确认后从 `develop` 前进 |
| 执行 Agent | `onevoke welcome` 选择, 未配置时回落到 Codex |
| Reviewer | `PM` / `CSA` / `Hacker` / `QA` 分别在 welcome 中选择 Codex 或 Grok |
| launcher | 有 tmux 时默认可选独立 window; 没有时可选当前终端前台运行 |
| 看板任务完成 | 审核通过后先报告并等用户确认, 确认后才合回初始分支 |

查看和修改本机配置:

```sh
onevoke config
onevoke welcome --reset
```

Reviewer 和看板完成时机可在项目根 `AGENTS.md` 或 `CLAUDE.md` 覆盖. 分支模型不提供项目级选项: 固定使用 `main` + `develop`, 不存在 `develop` 时由 Agent 从 `main` 自动初始化, 不回落到其他分支. 当前任务的明确用户指令优先级最高. 不要直接编辑已安装的 `ONEVOKE-AGENTS.md`, 下次安装会覆盖.

## 完整工作流

Onevoke 把需求讨论和任务执行分开. 人始终保留一个需求会话, 用高推理强度模型逐项定方案; 已定案的任务交给配置的 launcher 执行.

- 需求会话是决策面: 使用 Plan mode 明确需求、方案、验收条件和范围.
- 主 worktree 是协调面: 放唯一的本机看板, 由人确认任务进入 `todo`.
- 任务 worktree 是执行面: 每张已领取的代码任务独占一个分支和 worktree.
- Codex, Claude 或 Grok 是执行 Agent: 读取规则与任务卡, 实现、验证、提交和集成.
- 审核角色是独立门禁: `PM` 检查需求完整性, `QA` 检查正确性与回归, 安全角色按风险触发.

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

计划定稿后, Agent 把 "计划是否通过" 和 "走不走看板" 合并成一组编号选项一次问完, 不分两次确认:

```text
1. 确认计划并走看板 (建卡并 kanban start)
2. 确认计划, 不走看板, 在本会话直接做
3. 调整计划
```

选 1 才进入下面的建卡与启动流程; 选 2 在当前会话直接实施, 不建卡; 选 3 回到 Plan mode 继续调整, 更新后重新给出同一组选项. 纯问答、只读排查、纯文档或配置微调、发布部署等运维操作不触发这组选项, 已由 `kanban start` 拉起的会话也不再重复问.

退出 Plan mode 后, 明确让当前 Agent 使用 `kanban new` 创建任务卡. Agent 必须基于刚完成的讨论填完整卡片, 不能只生成带 `<填写>` 的空模板.

```sh
kanban new feature login-retry 登录重试
```

新卡片先进入 `backlog`; 这表示 "方案已记录", 还未进入执行队列. Agent 创建后应返回任务 ID, 并确认任务目标、用户决策、预期成果、验收条件、威胁模型和不在本轮范围均已填写.

任务默认建成一个 Markdown 文件. 只有需要独立 `spec.md`、分阶段计划和最终报告时才用 `--large`; 行数多本身不是大任务. 能独立领取、验收或取消的工作应拆成多张卡, 同目标、同负责人、同生命周期的内容留在一张卡内.

### 4. 启动任务

推荐直接告诉协调 Agent:

```text
开始任务卡 20260802-login-retry-task
```

协调 Agent 应先读 `kanban rules` 和卡片, 再依次执行 `pick` 和 `start`. 也可手动运行等价命令:

```sh
kanban pick 20260802-login-retry-task
kanban start 20260802-login-retry-task
# 或
kanban start --agent claude 20260802-login-retry-task
# 或
kanban start --agent grok 20260802-login-retry-task
```

`pick` 会执行带完整性校验的 `backlog -> todo`. 不传任务 ID 时, 它只列 `backlog` 任务供人选择. 卡片仍有缺失或 `<填写>` 时会拒绝, 此时回需求会话补清楚, 不绕过门禁.

不传任务 ID 时, `kanban start` 只列 `todo` 任务供人选择, 不猜优先级. 启动时会依次完成:

1. 读取 Onevoke 配置, 校验 launcher、TTY、Agent 命令和任务状态.
2. 用原子重命名领取卡片, 执行 `todo -> working`.
3. 写入负责人和开始时间.
4. tmux 模式在当前 session 新建 `kb-<任务标题>` window; foreground 模式在当前终端运行并等待退出. 两者 cwd 都是项目根.
5. 要求新 Agent 先读 `kanban rules`、任务卡和项目规则, 再准备代码工作区.

小任务默认使用中等推理强度, 大任务使用高推理强度. Codex 固定用 `gpt-5.6-sol`, Claude 固定用 `opus`; Grok 不锁模型也不接受推理强度, 一律跟随其 CLI 默认. `kanban start` 默认以 YOLO 模式启动, 会绕过 Agent 自身的 approval 或 permission 提示; 因此只应在可信本机和已确认范围内使用.

只有 `todo` 卡能启动. launcher 创建前失败会回滚卡片; tmux window 或前台 Agent 进程已经创建后, Agent 认证失败、退出或中断时卡片继续留在 `working`, 不自动重派. foreground 必须从真实交互终端运行, 在 CI、管道或无 TTY 的 Agent 工具调用中会拒绝且不领卡.

### 5. 回到需求会话讨论下一项

tmux 模式中, 协调 Agent 在 `kanban start` 成功后就结束本轮, 把执行完全交给任务 Agent: 不轮询 tmux、不抓窗口输出、不检查任务 worktree 进度、不给任务 Agent 注入指令. foreground 模式会占用当前终端直到任务 Agent 退出.

任务启动成功后, 不在协调 window 等它完成. 回到刚才讨论需求的会话, 执行 `/new` 创建干净上下文, 再次进入 Plan mode, 用 `gpt-5.6-xhigh` 或 `opus-xhigh` 讨论下一个需求.

此时形成稳定流水线:

- 需求会话串行做决策, 每次只讨论一个需求.
- tmux 模式中每个已定案任务在独立 window 和 worktree 中执行; foreground 模式一次占用一个当前终端.
- 看板显示所有任务处于 `backlog`、`todo`、`working`、`done` 中的哪个阶段.
- 人只在方案确认、任务优先级、审核风险和最终验收处介入.

不要在新会话继续携带上一项实现细节. 上一项的契约已在任务卡, 执行状态已在看板, 实现 Agent 会独立完成后续链路.

### 6. 隔离实现

执行 Agent 先检查主工作树, 再按项目规则准备工作区:

- 纯 Markdown 小改, 且 `develop` 干净并已同步 upstream 时, 可直接修改 `develop`.
- 其他改文件任务使用独立任务分支和 `<仓库根>/worktrees/<task-name>/`.
- 有 `origin` 时先 fetch, 从最新 `origin/develop` 建任务分支; 无 `origin` 时从本地 `develop` 建任务分支并明确走本地集成路径.
- 主 worktree 的 `kanban/` 是唯一看板. 任务 worktree 不复制、不链接、不提交它.

Agent 在任务 worktree 里先找既有实现和调用链, 再做最小正确改动. 改代码前读取 `CODE-RULES.md`; 不扩写无关重构, 不覆盖用户已有修改. 验证以能直接证明本次行为的最小命令开始, 风险或影响面较大时再扩大测试范围.

实施期只把关键决策、实际命令、结果、环境缺口和 commit 写回任务卡. 不复制整段会话流水. 大任务把计划写进 `plan.md`, 完成后把实际结果写进 `report.md`.

### 7. 提交与同步任务分支

一个独立关注点一个 commit, subject 使用简短中文动宾短语. 有可写 `origin` 时推任务分支; 无远端时保留本地提交并明确报告, 不把 "未 push" 说成 "已完成远端交付".

push 被 non-fast-forward 拒绝时先 fetch、rebase、重新验证. 只允许对任务分支使用 `--force-with-lease`; `main` 和 `develop` 永不 force-push.

### 8. 走审核闭环

代码和验证完成后, 基于同一个 `develop` commit 作为审核 base, 按顺序运行:

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

#### 指定 reviewer

四个角色分别由 Codex 或 Grok 执行, 两者规则一样, 只是 wrapper 和 CLI 不同. 按下面四档逐角色确定, 优先级从高到低:

**只改这一个任务** — 在任务提示里说一句就行:

```text
这个任务的 QA 用 Grok, 其余角色沿用配置
```

**改整个项目** — 在项目根的 `AGENTS.md` 或 `CLAUDE.md` 里加一节, 该项目的所有任务都生效:

```markdown
## 审核

本项目的 PM 和 QA 用 Grok, 安全角色用 Codex, 按 `~/.agents/REVIEW-RULES.md` 执行.
```

**改 Agent 自己的全局规则** — 可在实际会被载入的 `~/.claude/CLAUDE.md`、`~/.codex/AGENTS.md`、`~/.grok/AGENTS.md` 或 `~/.agents/AGENTS.md` 中逐角色指定. 这档高于 Onevoke 配置, 适合已有统一规则管理的用户.

**改整台机器** — 重新运行 welcome, 它把每个角色的选择写入 Onevoke 配置:

```sh
onevoke welcome --reset
```

前三档都没指定时, 调 `onevoke review` 按角色读取本机配置. 不同角色可以使用不同 reviewer; 同一角色的一轮修复与复核不能中途换 Agent. 选定 CLI 不可用时不会自动换另一个, 而是按审核规则让用户决定.

### 9. 验收、集成与清理

审核通过不等于任务完成. 入口的默认取值要求**所有**看板任务在合回前先报告并等人确认, 不只是 Bug 修复; 未确认时不 push 集成分支、不清理 worktree, 卡片继续留在 `working`. 某个项目要审核通过就自动合回, 在该项目的 `AGENTS.md` 里改这项取值.

验收后, Agent 将任务分支 rebase 到最新 `develop` 并重新验证. 若无实质冲突, 沿用已完成的审核门; 有本人手工解决的代码冲突时重新审核. 集成只允许 fast-forward 或项目规定的 PR 流程, 不产生 merge commit.

直接远端集成的顺序是:

1. 非 force push 最终任务 commit 到 `origin/develop`.
2. fetch 远端状态.
3. 主 worktree 用 `git merge --ff-only` 同步.
4. 确认任务 commit 已进入 `develop`.
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

卡片进入 `done` 后, 执行 Agent 按 `KANBAN-RULES.md`「完成报告」的固定模板汇报一次, 覆盖任务卡与最终提交、交付结果、验收条件、验证结果、审核结果、集成与清理、偏差风险遗留和完成结论. 没内容的字段写 `无` 或 `N/A`, 失败或未执行的验证不许写成通过, 最终提交写完整 40 位 SHA. 这份报告取代平时的一句话任务总结; 代码任务未完成集成或卡片没进 `done` 时不发.

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

然后在协调终端对 Agent 说 "开始任务卡 20260802-login-retry-task". Agent 会执行:

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
onevoke review <worktree绝对路径> <base-commit-SHA> <commit-SHA> PM "<任务目标>"
onevoke review <worktree绝对路径> <base-commit-SHA> <commit-SHA> QA "<任务目标>"
```

当前任务或项目明确指定 reviewer 时, 可把命令换成 `codex-review.sh` 或 `grok-review.sh`, 参数完全一样. 其他情况由 `onevoke review` 分发.

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
python3 tests/test-onevoke.py
```
