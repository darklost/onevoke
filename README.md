# Onevoke

一个人用看板调度多个 AI Agent.

## 1. 安装

需要 Python 3, Git, POSIX shell, 以及 Codex, Claude 或 Grok 中至少一个.

```sh
./install.sh
```

安装过程会引导选择默认 Agent, Reviewer 和启动方式.

如果 `~/.agents/AGENTS.md` 不存在, 安装器会将其链接到 `ONEVOKE-AGENTS.md`; 已有文件不会修改.

如果 welcome 提示 Agent 尚未接入规则:

- Claude: 在 `~/.claude/CLAUDE.md` 加 `@~/.agents/ONEVOKE-AGENTS.md`.
- Codex: 将 `~/.codex/AGENTS.md` 软链接到该入口, 或把入口内容合入现有文件.
- Grok: 将 `~/.grok/AGENTS.md` 软链接到该入口, 或把入口内容合入现有文件.

## 2. 使用

在项目目录首次使用时初始化看板:

```sh
kanban init
```

先在 Agent 中讨论需求, 明确目标, 验收条件和不做的范围.

讨论完成后, 让 Agent 创建并启动任务卡:

```text
需求已确认. 请用 kanban new & start 创建任务卡并启动.
```

Agent 会填完整任务卡, 再执行:

```sh
kanban new feature login-retry 登录重试
kanban pick 20260813-login-retry-task
kanban start 20260813-login-retry-task
```

大型任务由 Agent 拆成多张可并行执行的任务卡, 再按依赖启动.

查看看板状态:

```sh
kanban list
```

只看某个状态:

```sh
kanban list working
kanban list done
```

完整规则:

```sh
kanban rules
```
