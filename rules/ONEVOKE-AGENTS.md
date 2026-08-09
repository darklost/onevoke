# Onevoke 全局工作流规则

- 规则集入口, 装在 `~/.agents/ONEVOKE-AGENTS.md`. 只放分册索引, 优先级和少量默认取值; 通用条款在 `~/.agents/BASE-RULES.md`.
- 安装器只在本文件不存在时写一次, 之后升级只覆盖分册, 不动本文件. 改这里的取值不会被下次安装冲掉.
- 本文件不是用户自己的 `~/.agents/AGENTS.md`, 两者不互相覆盖.

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

- 高到低: 当前任务明确用户指令 > 离目标文件最近项目级 `AGENTS.md` 或 `CLAUDE.md` > 本文件「默认取值」 > 上表各分册.
- 分册定机制, 本文件定取值. 只有「默认取值」里写到的条目高于分册; 其余一切以分册为准, 本文件不复述分册内容.
- 项目要改这三项取值, 写进项目级 `AGENTS.md` 或 `CLAUDE.md`, 不改本文件. 本文件是本机默认值.
- 同目录 `AGENTS.md` 与 `CLAUDE.md` 冲突且用户指令未消解时, 停止受影响操作, 问用户.

## 默认取值

### 分支

- 仓库默认两条长期分支: `main` 稳定分支, `develop` 集成分支.
- 默认集成分支是 `develop`. 任务分支从最新 `origin/develop` 切, 完成后合回 `develop`.
- `main` 只从 `develop` 前进, 且必须用户明确确认. Agent 不自动推 `main`.
- 仓库实际没有 `develop` 时, 回落到 `refs/remotes/origin/HEAD` 指向的分支, 并在交付说明里写明实际用了哪条.

### Reviewer

- `PM`, `CSA`, `Hacker`, `QA` 四个角色一律用 `codex-review.sh`.
- 同一任务不混用两个 reviewer. 换 reviewer 要用户明确指定, 换了就从第一阶段重启.

### 看板任务完成

- 卡片实现, 验证和审核都过之后, 先向用户报告并等确认, 确认后才合回初始分支.
- 用户确认前不 push 集成分支, 不清理 worktree, 卡片留 `working/`.
- 合回并清理完才填 `结果: completed`, 迁 `done/`, 再发「完成报告」.
