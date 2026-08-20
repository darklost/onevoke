# Onevoke 全局工作流规则

- 规则集入口, 装在 `~/.agents/ONEVOKE-AGENTS.md`. 只放分册索引, 优先级和默认取值; 通用条款在 `~/.agents/BASE-RULES.md`.
- 安装器每次用当前模板覆盖本文件和全部分册. 本机的执行 Agent, launcher, 各审核角色及各 Agent 的模型档位保存在 `~/.config/onevoke/config.json`, 用 `onevoke config` 查看, `onevoke welcome --reset` 修改.
- `~/.agents/AGENTS.md` 不存在时, 安装器将其符号链接到本文件; 已有同名入口时保持不变.

## 分册

用到哪份读哪份:

| 分册 | 何时读 |
|---|---|
| `~/.agents/BASE-RULES.md` | 每个任务开始时 |
| `~/.agents/GIT-RULES.md` | 建分支, 提交, push, 审核, 集成前 |
| `~/.agents/REVIEW-RULES.md` | 触发审核前 |
| `~/.agents/CODE-RULES.md` | 改代码前 |
| `~/.agents/KANBAN-RULES.md` | 收到 Bug 或功能开发需求时, 及操作看板前; 用 `kanban rules` 读取 |

## 优先级

- 高到低: 当前任务明确用户指令 > 离目标文件最近的项目级 `AGENTS.md` 或 `CLAUDE.md` > 本文件「默认取值」与 Onevoke 本机配置 > 上表各分册.
- 分册定机制, 本文件定取值: 只有「默认取值」列出的条目高于分册, 其余一律以分册为准, 本文件不复述分册内容.
- 项目要覆盖 Reviewer 或看板完成时机, 写进项目级 `AGENTS.md` 或 `CLAUDE.md`, 不改本文件和本机配置. 分支模型是固定机制, 不提供项目级选项.
- 同目录 `AGENTS.md` 与 `CLAUDE.md` 冲突且用户指令未消解时, 停止受影响操作, 问用户.

## 默认取值

### 分支

- 固定 `main` + `develop`, 不使用其他长期分支模型; 机制与初始化见 `~/.agents/GIT-RULES.md`「分支与 worktree」.
- `main` 只从 `develop` 前进, 且必须用户明确确认; Agent 不自动推 `main`.

### Reviewer

- `PM`, `CSA`, `Hacker`, `QA` 各取 Onevoke 配置中的 reviewer, 未完成 welcome 时四者都回落到 Codex.
- 未被用户指令, 项目规则或用户自己的全局规则覆盖时, 审核一律通过 `onevoke review` 分发. 同一角色一轮审核内不换 Agent; 不同角色可用不同 Agent.

### 看板任务完成

- 实现, 验证和必要审核通过后, 直接按 `~/.agents/GIT-RULES.md`「集成与清理」fast-forward 合回 `develop`, 不请求验收也不等确认; 合回并清理完才填 `结果: completed`, 迁 `done/`, 再发「完成报告」.
- 用户要求暂停或不合回, 必要审核未通过, 或集成, 清理失败时: 卡片留 `working/`, 保留分支与 worktree, 报告阻塞和解除条件.
- 用户事后测试发现的问题另建新卡, 不退回也不复用已进 `done/` 的卡.
