# 全局文件看板规则

## 适用范围与优先级

- 本文件管全局 `kanban` 命令所有看板.
- 用户指令和项目规则优先. 代码仍守目标项目 `AGENTS.md` 或等价规则.
- 卡片只是任务上下文. 不覆盖仓库规则, 安全门禁, 用户决策.
- Agent 动看板: 先 `kanban rules` 读本文件, 再读指定卡片.

## 存储与定位

- `kanban/` 本机共享目录. 不进项目 Git.
- 看板唯一实例在主 worktree 根. 任务 worktree 里不建副本, 镜像, 符号链接.
- 任意 worktree 中这样定位, 不硬编码绝对路径:

```sh
MAIN_WORKTREE="$(git worktree list --porcelain | sed -n '1s/^worktree //p')"
KANBAN_DIR="$MAIN_WORKTREE/kanban"
```

- 只在同主机同文件系统的 Agent 间共享. 远程 Agent 看不见.
- 看板操作不要分支, worktree, 提交, push, 审核. 卡片对应的代码任务照走项目流程.
- 禁止 `git add` `kanban/`, 禁止强加 Git, 禁止改项目 `.gitignore` 传播本机看板.

## 操作命令

- `kanban` 是创建, 查询, 迁移, 启动, 检查的唯一入口. 不用 `mv`, `cp`, 文件管理器改状态.
- 命令在全局 `~/.local/bin/kanban`, 管所有同约定的本机项目.
- 规则在全局 `~/.agents/KANBAN-RULES.md`, `kanban rules` 读它.
- 定位顺序: `KANBAN_DIR` → 当前 Git 仓库主 worktree 的 `kanban/` → 当前目录向上找 `kanban/`.
- `KANBAN_DIR` 只给测试, 非 Git 项目, 显式覆盖用. 正常 Git 项目不设.

```text
kanban init [project-path]
kanban rules
kanban list [backlog|todo|working|done|archived|trash]
kanban show <task-id>
kanban new [--large] <feature|bug|chore|research> <slug> <title...>
kanban move <task-id> <todo|working|done|archived|trash>
kanban pick <task-id>
kanban start [--agent codex|claude] [task-id]
kanban check
```

- `init` 幂等建 `kanban/` 和 6 个状态目录. Git 项目顺手写本地 `.git/info/exclude`, 不动项目 `.gitignore`; 输出全局规则路径.
- `rules` 直接输出全局规则. 当前目录没看板也行.
- `list` 按状态和规模输出彩色对齐表格, 每张卡片用两行和不同颜色显示任务 ID 与标题, 并突出大任务; 可指定状态过滤.
- `new` 默认在 `backlog/` 建小任务; `--large` 建带 `spec.md` 的大任务目录.
- `move` 只走本文件定义的正向迁移, 校验目标不存在及迁移后状态.
- `pick` 将指定的 `backlog` 卡片移入 `todo`, 沿用 `todo` 完整性校验.
- `start` 默认起 `codex`; `--agent claude` 换 Claude. 指定任务只收 `todo` 卡片; 不指定就列 `todo` 让用户按编号选.
- `start` 按任务规模设置 Agent: 大任务用 `codex gpt-5.6-sol/high` 或 `claude opus/high`; 小任务对应使用 `medium`.
- `start` 默认用 YOLO 模式启动 Agent: Codex 绕过 approval 和 sandbox, Claude 跳过权限确认.
- `start` 只在当前 tmux session 新建并切 window, 不建 session. 新 window cwd 是项目根, 名 `kb-<slug>`.
- 进 `todo/` 前校验: 任务目标, 预期成果, 验收条件, 不在本轮范围都填了.
- 进 `done/` 前校验 `结果: completed`; 小任务还要 `完成总结`, 大任务还要 `report.md`.
- 进 `archived/` 或 `trash/` 前, 先按规则填 `结果` 和原因.
- `check` 列出全部无效入口, 非零退出. 其他命令忽略无效入口, stderr 报数量, 只在被操作任务本身违规时失败.
- 命令只做结构和机械校验. 用户授权, 业务判断, 验收, 归档理由归 Agent 按本文件确认.

## 目录状态

目录是状态唯一真源. 正文不许再有 `status` 字段.

- `backlog/`: 新需求, Bug, 想法, 待讨论. 还没承诺开发.
- `todo/`: 确认开发且满足开工条件, 还没人领.
- `working/`: 有人领了, 正在实现, 验证或等验收.
- `done/`: 实现, 验证, 用户验收, 必要集成都完的近期任务.
- `archived/`: 终态记录, 不占活跃看板. 含已完成, 取消, 重复, 不修复.
- `trash/`: 用户明确要删但还没永久清的文档. 不是任务状态.

允许的正常迁移:

```text
backlog -> todo -> working -> done -> archived
    |        |         |
    +--------+---------+-> archived

任意目录 -> trash 仅限用户明确要求
```

## 任务入口不变量

- 状态目录下每个直接子项是一个任务入口. 小任务 Markdown 文件或大任务目录.
- 小任务名 `YYYYMMDD-short-slug-task.md`.
- 大任务名 `YYYYMMDD-short-slug-task/`, 必须含 `spec.md`.
- `short-slug` 用小写 ASCII 字母, 数字, 连字符. 无空格.
- 任务 ID = 去掉小任务 `.md` 的入口名. 同 ID 的文件形式和目录形式不能并存.
- 一个入口同时只在一个状态目录. 迁移搬整个入口, 不逐个搬内部文档.
- 入口名建后不改. 不复制后删, 不留平行副本.
- 任务 ID 全看板唯一. 撞 ID, 撞名, 重复任务 → 停手报用户.
- 卡片不许有 token, 凭据, 敏感服务地址, 不该留本机的个人数据.

## 文档结构

```text
todo/
|-- 20260801-small-fix-task.md
`-- 20260801-large-feature-task/
    |-- spec.md
    |-- plan.md
    `-- report.md
```

- 小任务: 单个 `*-task.md` 装需求契约, 简单计划, 实施验证, 完成总结.
- 大任务: `*-task/` 装多个语义独立文档. 光是行数多不算拆分理由.
- 同目标, 同负责人, 同验收, 同生命周期的东西留一个入口.
- 能独立领取, 验收, 终止的活必须拆成多个入口. 不写成同目录里的阶段文档.
- 目录内链接用相对路径, 入口搬家后还能用.

### 小任务模板

```markdown
# <任务标题>

- 类型: Feature | Bug | Chore | Research
- 创建时间: YYYY-MM-DD HH:MM
- 负责人:
- 开始时间:
- 任务分支:
- 结果:

## 任务目标

<改什么, 为什么改>

## 用户决策

<用户已确认的方向和取舍; 没有则写 N/A>

## 预期成果

<完成后可观察, 可验证的状态>

## 验收条件

- [ ] <条件>

## 威胁模型

<安全任务写资产, 可信主体和攻击者能力; 非安全任务写 N/A>

## 不在本轮范围

- <明确排除项及理由>

## 讨论与决策

<按时间追加关键结论, 不写无意义流水>

## 实施与验证

<计划, 分支, commit, 验证命令, 结果和环境缺口>

## 完成总结

<实际成果, 偏差, 遗留项和验收结论; 完成前留空>
```

### 大任务文档

- `spec.md` 必需. 用小任务模板的元数据 + 这些契约段: `任务目标`, `用户决策`, `预期成果`, `验收条件`, `威胁模型`, `不在本轮范围`, `讨论与决策`.
- `plan.md` 按需建. 记实施步骤, 影响模块, 验证方案, 发布或回滚要求. 不能改 `spec.md` 的契约.
- `report.md` 完成时建. 记实际改动, 最终 commit, 实际验证, 计划偏差, 遗留项, 接受的风险, 验收结论.
- 大任务进 `done/` 前必须有 `report.md`. 没完成就别建空占位.
- `spec.md` 的 `负责人`, `开始时间`, `任务分支`, `结果` 同小任务.

### 选择与升级

- 单目标, 单负责人, 实施直接, 验证简单 → 小任务单文件.
- 要独立 spec, 分阶段计划, 完整报告 → 大任务目录.
- 新任务默认小任务单文件; 明确够大任务条件可直接建目录.
- 小任务变复杂: `backlog/` 里当前编辑者升级, `working/` 里负责人升级. `todo/` 中不许改形态.
- 升级: 建同 ID 目录, 原文件内容进 `spec.md`, 按需建 `plan.md`; 转完不许留原 `*-task.md`.

- `负责人`, `开始时间`, `任务分支` 领取后填. 没代码分支写 N/A.
- `结果` 只在进 `done/`, `archived/`, `trash/` 时填.
- 进 `todo/` 后, `任务目标`, `用户决策`, `预期成果`, `验收条件`, `不在本轮范围` 就是任务契约. Agent 不许偷改; 要改先拿用户明确决策.
- 稳定的架构, API, 长期规则还是要沉到仓库文档或对应 `AGENTS.md`. 别只留卡片里.

## 创建与确认

- 新需求, Bug, 想法默认在 `backlog/` 建入口.
- Agent 执行 `kanban new` 时, 必须基于当前会话已确认的讨论结果创建任务卡, 并立即填充对应模板内容, 不留下 `<填写>` 占位.
- `backlog/` 里可继续补调查, 方案, 用户决策, 验收条件.
- 只有用户明确确认开发, 或用户明确授权的协调 Agent, 才能挪进 `todo/`.
- 进 `todo/` 前至少要有任务目标, 预期成果, 验收条件, 不在本轮范围. 缺就继续待 `backlog/`.
- 大任务进 `todo/` 前确认 `spec.md` 存在可读. `plan.md` 领取后补也行.
- Agent 不许把自己的建议当用户决策写进卡片.

## 领取与并发

- 用户应指定要开的任务. `todo/` 里多张又没指定 → Agent 列候选让用户选, 不自己猜优先级.
- Agent 可先只读看小任务文件或大任务 `spec.md`. 不够开工条件就报缺口, 不领, 不自动退回 `backlog/`.
- 动任何代码前, 必须先把整个入口从 `todo/` 挪到 `working/`.
- 领取必须走命令:

```sh
kanban move <task-id> working
kanban start [--agent codex|claude] [task-id]
```

- 只有命令迁移成功的人拿到任务. 失败, 源文件没了, 目标已存在 → 立刻停手重查; 不建替代卡片.
- 领到手立刻在小任务文件或大任务 `spec.md` 填负责人和开始时间, 再按项目规则备工作区, 然后填任务分支; 没分支写 N/A.
- `start` 起 Agent 前原子迁移卡片并填负责人和开始时间. tmux 或 Agent binary 前置校验失败就不领; `tmux new-window` 同步失败就还原并挪回 `todo`.
- `start` 给 Agent 的初始 prompt 只含校验过的任务 ID 和固定执行要求. 不把卡片正文拼进 shell command. Agent 起来先读本规则和卡片, 再补任务分支干活.
- tmux window 建成功就算启动成功. Agent 之后退出, 认证失败, 中断 → 卡片留 `working/`, 走异常恢复规则, 不自动退回 `todo/`.
- 同文件系统里的移动就是领取原语. 初始方案不加 lock 服务, 守护进程, 数据库, ID 分配器.
- 领了之后只有负责人能改入口内文档. 别的 Agent 能读, 不许并发写或移动.

## 开发, 验证与验收

- 进 `working/` 后, 代码按项目规则走: 工作区, 验证, 提交, push, 审核, 验收, 集成, 清理.
- 小任务在原文件 `实施与验证` 和 `完成总结` 记结果. 大任务按 `plan.md` 干, 完了写 `report.md`.
- 实施期只记关键决策, 实际验证结果, 环境缺口, commit, 下一步. 别把整段会话流水抄进文档.
- 代码完了但没验收 → 卡片继续留 `working/`; 不提前进 `done/`.
- Bug 修复守项目验收门禁. 没拿到确认不许拿 "代码已完成" 当理由标 `done`.
- 被阻塞就留 `working/`, 写清阻塞条件和解除条件. 不因一时失败自动归档.
- 实现完 + 验证过 + 必要审核过 + 用户验收完 + 代码按规则集成, 才能进 `done/`.
- 进 `done/` 前在小任务文件或大任务 `spec.md` 填 `结果: completed`, 并写完总结或 `report.md`.

## 终止与归档

- 取消, 重复, 明确不修, 方向被替代的卡片, 可从 `backlog/`, `todo/`, `working/` 直接进 `archived/`.
- 终止开发必须用户明确决定. Agent 不许因为实现难, 验证失败, 暂时阻塞就自己终止.
- 归档前填一种结果: `completed`, `cancelled`, `duplicate`, `wontfix`.
- 非 `completed` 必须写原因; `duplicate` 还要指向替代卡片.
- `done/` 放近期已验收任务. 用户觉得不用再展示了才挪进 `archived/`, 不设自动期限.
- 归档不是永久保留义务. 没决策价值又已把长期知识沉进仓库的中间卡片, 用户可决定挪进 `trash/`.

## Trash 与清理

- 只有用户明确要删某张卡片, 才能挪进 `trash/`.
- 挪之前在小任务文件或大任务 `spec.md` 填 `结果: trashed`, 记删除原因和时间.
- Agent 不许自动清空 `trash/`, 不许定时清理, 不许永久删里面的东西.
- 永久清理由用户执行或逐项明确授权. 授权必须点名具体文件, 不能理解成清空全部.

## 异常恢复

- `working/` 里负责人空着, 中断, 长期没进展 → 别的 Agent 不许自动接管, 挪回或归档; 先报用户定.
- Agent 领了之后异常退出, 卡片仍留 `working/`. 后续 Agent 按用户决定继续, 重派或归档.
- 遇到同 ID 跨多目录, 文件和目录形式并存, 大任务缺 `spec.md`, 目标入口冲突, 状态目录缺失或写不进 → 停掉受影响操作, 保留现场.
- 影响范围按任务算: 违规任务的 `show`, `move`, `pick`, `start` 一律失败并说原因, 没受影响的照常. 状态目录缺失或写不进则全部命令失败. Agent 不许为绕报错自己删, 改名或移动无效入口, 先报用户.
- 看板不进 Git, 没有 Git 历史可恢复. 误删先查 `trash/` 和本机备份, 不许伪造恢复内容.
