# Onevoke 全局工作流规则

- 规则集入口, 装在 `~/.agents/ONEVOKE-AGENTS.md`. 只放分册索引, 优先级和少量默认取值; 通用条款在 `~/.agents/BASE-RULES.md`.
- 安装器每次用当前模板覆盖本文件和全部分册. 本机的执行 Agent、launcher 和各审核角色选择保存在 `~/.config/onevoke/config.json`, 用 `onevoke config` 查看, 用 `onevoke welcome --reset` 修改.
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

- 高到低: 当前任务明确用户指令 > 离目标文件最近项目级 `AGENTS.md` 或 `CLAUDE.md` > Onevoke 本机配置与本文件「默认取值」 > 上表各分册.
- 分册定机制, 本文件定取值. 只有「默认取值」里写到的条目高于分册; 其余一切以分册为准, 本文件不复述分册内容. 分支模型是固定机制, 不属于可覆盖取值.
- 项目要覆盖 Reviewer 或看板完成时机, 写进项目级 `AGENTS.md` 或 `CLAUDE.md`, 不改本文件和本机配置. 分支模型固定为 `main` + `develop`, 不提供项目级选项.
- 同目录 `AGENTS.md` 与 `CLAUDE.md` 冲突且用户指令未消解时, 停止受影响操作, 问用户.

## 默认取值

### 分支

- 仓库固定两条长期分支: `main` 稳定分支, `develop` 集成分支. 不使用其他长期分支模型.
- 唯一集成分支是 `develop`. 任务分支从最新 `origin/develop` 切, 完成后合回 `develop`.
- `main` 只从 `develop` 前进, 且必须用户明确确认. Agent 不自动推 `main`.
- 仓库有 `main` 但没有 `develop` 时, Agent 按 `~/.agents/GIT-RULES.md` 从最新 `main` 自动初始化 `develop`, 不询问分支选择, 不回落到其他分支. 没有 `main` 时停止并报告.

### Reviewer

- `PM`, `CSA`, `Hacker`, `QA` 分别取 Onevoke 配置中的 reviewer, 未完成 welcome 时四者都回落到 Codex.
- 未被当前任务、项目规则或用户自己的全局规则覆盖时, 审核一律通过 `onevoke review` 分发. 同一角色的一轮审核中不得换 Agent; 不同角色可以按配置使用不同 Agent.

### 看板任务完成

- 卡片实现, 验证和审核都过之后, 先向用户报告并等确认, 确认后才合回 `develop`.
- 用户确认前不 push 集成分支, 不清理 worktree, 卡片留 `working/`.
- 合回并清理完才填 `结果: completed`, 迁 `done/`, 再发「完成报告」.
