# 全局文件看板规则

## 适用范围

- 本文件约束当前作用域 `kanban` 命令管理的看板. 用户指令和目标项目规则优先; 卡片只保存任务契约和执行记录, 不覆盖用户决策, 项目规则或安全门禁.
- Agent 操作看板前先运行命令根下的 `kanban rules`, 再读目标卡片. 下文命令名 `kanban` 均指该入口: 全局安装可使用已加入 PATH 的 `kanban`; 项目安装必须使用绝对入口 `<命令根>/kanban` (Windows 人工交互可用 `<命令根>\kanban.cmd`), 禁止改用 PATH 中的全局同名命令.

## 存储与定位

- `kanban/` 是不进 Git 的本机共享数据, 唯一实例位于主 worktree 根目录, 只供同主机同文件系统的 Agent 使用. 任务 worktree 不建副本, 镜像或符号链接; 远程 Agent 不可见. Windows 上符号链接、junction 和其他 reparse point 一律视为不安全入口, `kanban` 通过已校验的 Win32 句柄读写和迁移; POSIX 继续使用 no-follow 文件操作. 任一安全校验失败都停止, 禁用文件管理器或普通路径 API 绕过.
- 定位顺序是 `KANBAN_DIR` -> 当前 Git 仓库主 worktree 的 `kanban/` -> 从当前目录向上查找 `kanban/`. `KANBAN_DIR` 仅用于测试, 非 Git 项目或明确覆盖; 正常 Git 项目从任意 worktree 这样定位:

```sh
MAIN_WORKTREE="$(git worktree list --porcelain | sed -n '1s/^worktree //p')"
KANBAN_DIR="$MAIN_WORKTREE/kanban"
```

- `kanban/` 不属于 Onevoke 安装载荷, 定位不因全局或项目安装而改变. 看板操作本身不建分支, 不提交, 不 push, 不审核; 卡片对应的代码任务仍按项目规则执行. 禁止提交 `kanban/` 或修改项目 `.gitignore` 传播它.

## 命令契约

`kanban` 是创建入口, 查询和迁移状态的唯一方式; 不用 `mv`, `cp` 或文件管理器代替.

```text
kanban init [project-path]
kanban rules
kanban list [--mobile] [backlog|todo|working|review|done|archived|trash]
kanban show <task-id>
kanban new [--large] <feature|bug|chore|research> <slug> <title...>
kanban move <task-id> <todo|working|review|done|archived|trash>
kanban pick [task-id]
kanban start [--agent codex|claude|grok|cursor] [--launcher auto|tmux|tmux-session|herdr|foreground|console] [task-id]
kanban resume [--timeout SECONDS] (--message TEXT | --message-file FILE) [--launcher ...] <task-id>
kanban notify [--pane HERDR-PANE-ID] [--timeout SECONDS] (--message TEXT | --message-file FILE) <task-id>
kanban dismiss [--timeout SECONDS] <task-id>
kanban check [--all] [task-id ...]
kanban subscribe [--refresh SECONDS] [--heartbeat SECONDS] <task-group> <task-id>... [--watch <task-id|task-group-id>...]
kanban web [--host HOST] [--port PORT] [--refresh SECONDS] [--assets DIR] [--open]
kanban tui [--single] [--refresh SECONDS] [--theme auto|light|dark]
```

- `start` 的 Agent, launcher 和模型档位默认取 Onevoke 配置, welcome 未完成时回落到默认值; `--agent` 与 `--launcher` 只覆盖本次. Agent 按卡片规模选取: 大任务 (含 `spec.md` 的目录卡) 用配置 `kanban_agents.large`, 小任务 (单文件卡) 用 `kanban_agents.small`, 两者缺省都等于 `kanban_agent`; 成功输出写明规模和实际 Agent. `start` 默认使用 Agent 的免确认模式, 把执行 Agent 的会话标识写入卡片 `会话` 字段 (Claude/Grok 为 UUID, Cursor 为 chat id, Codex 只记 Agent 名), 并在紧随其后的 `窗口` 字段记录投递地址: herdr 写 `herdr:<tab-id>:<pane-id>`, tmux/tmux-session 写 `<launcher>:<session-id>:<window-id>:<pane-id>`, foreground/console 写 launcher 名. tmux/tmux-session 先创建占位 window/pane, 持久化 `窗口` 后才用 `respawn-pane` 启动 Agent, 随后用 `tmux set-option -p -t <pane-id> @onevoke_session <会话-id>` 写入 pane 会话标记; Claude/Grok/Cursor 取卡片记录的 id, Codex 复用 `notify`/`resume` 的 rollout 解析取得 id. 地址写入、Agent 启动或 pane 会话标记写入失败都关闭本次 window 并回滚卡片. 旧卡缺这两个字段时按顺序插在 `负责人` 后, 不批量改写未启动的旧卡.
- `resume` 用卡片 `会话` 记录的原 Agent 和会话唤醒执行 Agent, 保留其上下文: Claude/Grok 用 `--resume <uuid>`, Cursor 用 `--resume <chat-id>`, Codex 用 `codex resume <session-id>`, 其 session id 在 `CODEX_HOME` (默认 `~/.codex`) 的 rollout 记录中按"以该任务的 start/resume prompt 开头的用户消息"检索 (只提到任务 ID 的主控会话不算), 找不到则失败. 只接受 `review/` 或 `working/` 中的卡, 必须且只能给非空的 `--message` 或 `--message-file`; `--timeout` 必须是大于 60 的有限秒数且默认 120 秒. `review/` 卡先原子迁回 `working/`, 启动或存活校验失败时恢复原文档再迁回 `review/`; `working/` 卡不迁移, 但失败时同样恢复原文档. 文档恢复失败时不迁回, 报错写明卡片实际所在目录. launcher 与 `start` 相同; 拉起后复用 `notify` 恢复分支的同一存活判据, herdr 与 tmux/tmux-session 校验可寻址终端, foreground 与 console 在完整 timeout 观察期内要求进程不退出. herdr 校验本次 `tab create` 直接返回的 pane: `agent` 必须匹配且状态只接受 `idle`, `working`, `blocked`; pane 上报非空 `agent_session.value` 时还必须与卡片会话精确匹配, 没有上报有效身份时不以缺失本身判失败. 这里判断的是已知新 pane 是否存活, 与直投和反查必须靠会话身份确定目标的职责不同; 该降级不是同用户安全边界. 秒退时清理新实例, 非零退出并附可取得的 Agent 原始输出, 只有校验通过才按 `start` 的输出格式报告 `已唤醒`. 没有 `会话` 记录的卡 (未经 `start` 启动) 不能 `resume`. `start`, `resume` 和需要恢复进程的 `notify` 在所有平台都把完整 prompt 写入 UTF-8 临时任务文件, 内含任务 ID、固定要求和消息正文; Agent 命令行只接收一句包含该绝对路径的指令. 任务文件内要求 Agent 完成后尝试删除, 删除失败或遗留不影响结果; 这类文件不做 POSIX 权限或 Windows ACL 检查与收紧. 原生 Windows 优先使用 Agent `.exe`; Codex, Claude, Grok 或 Cursor 只有 `.cmd`/`.bat` 时, 通过显式 `cmd.exe /d /s /v:off /c` 和 Agent 适配层的参数编码启动.
- `notify` 是主控向原执行 Agent 派回事项的单一接口. 它与 `resume` 一样只接受 `review/` 或 `working/` 卡, 必须且只能给非空的 `--message` 或 `--message-file`, `--timeout` 必须是大于 60 的有限秒数且默认 120 秒. 地址优先级为显式 `--pane` 覆盖、卡片 `窗口` 快路径、缺窗口时按 Agent 与会话 id 扫描 `herdr pane list`; 覆盖与反查均继续用 `pane get` 验证 pane 存在、Agent 和 `agent_session.value` 完全匹配且既有 pane 状态为 `idle` 或 `done`. 卡片已记录 id 的 Claude/Grok/Cursor 直接比对, 只有缺 id 的旧 Codex 卡复用 `resume` 的 rollout 检索; Onevoke 不设 Agent 白名单, herdr 反查覆盖范围取决于当前版本及各 `source: herdr:<agent>` 集成是否实际报告会话身份. 唯一命中后把 `herdr:<tab-id>:<pane-id>` 写回 `窗口`; 0 个或多个命中不投递. 直投与反查负责确定既有目标身份, 因此 pane 未上报有效会话身份时继续拒绝并进入恢复链, 不使用恢复存活校验的降级判据. tmux/tmux-session 的旧卡缺窗口时仍不能反查; 有地址时同时验证 `pane_dead=0`, `pane_in_mode=0`, `pane_current_command` 与 Agent 可执行名一致, 并要求 pane 的 `@onevoke_session` 用户选项非空且与卡片解析出的会话 id 完全一致. 选项缺失或不一致均按无直投通道处理并回落, 不降级为进程名级放行. tmux 用户选项和 herdr `agent_session` 都处于同用户权限内, 只用于避免误投, 不构成抵御同用户恶意伪造的安全边界. 只有直投地址及探查通过后才创建正文载荷; Windows 临时根只取 GetTempPathW 返回的词法路径并由 no-follow 边界逐分量拒绝 reparse point; 正文写入仅当前用户可访问、创建时即收紧的 `0700` 临时目录及 `0600` 文件, foreground/console 回落不创建载荷. 终端只收到一行以 `# onevoke-notify:` 开头、含绝对路径和 marker 的指令; herdr 必须用 `agent prompt <pane-id> <instruction>` 投给 pane 内已在运行的 Agent TUI: 它按 pane 实际的 bracketed-paste 模式送正文, 再延时补一次编码后的 Enter, 并在 Agent 已停在审批或提问 UI 时先行拒绝而不是把正文塞进那个对话框. 不得改用面向 shell 的 `pane run` (正文与结尾 CR 同批写入, Cursor 等 TUI 不把它当提交, 正文会停在输入栏), 也不得把文本交给只接受按键名的 `pane send-keys`; tmux 继续用 `send-keys -l` 后单独发送 `Enter`. herdr 用 `pane wait-output --match <marker> --source recent` 做字面子串匹配, 允许 TUI 在 marker 前加渲染前缀; tmux 用有界 `capture-pane` 并按字面子串确认 marker, 同样允许渲染前缀. 投递动作成功即迁卡并成功返回; marker 超时只警告「已投递, 未在超时内确认」, 不恢复第二个进程. foreground/console、无直投通道、探查或投递动作失败才由命令内部恢复原会话; process 型恢复必须在完整 timeout 观察期内保持存活, foreground 验证并迁卡后继续占用当前终端直至 Agent 退出; herdr 恢复存活按 `resume` 的新 pane 判据执行, 其中状态仍只接受 `idle`, `working`, `blocked`, 拒绝 `done` 与 `unknown`. 恢复校验失败后的 tab/window/process 清理失败必须与原错误合并报告, 并提示新 Agent 可能仍存活. 主控不另行调用 `resume`. 两路都失败时非零退出并同时报告原因, 卡片正文与原状态不变.
- `dismiss` 只接受 `done/` 或 `archived/` 卡, 不改卡片正文和状态; `--timeout` 必须是大于 60 的有限秒数且默认 120 秒. 它按卡片 `窗口` 定位 herdr pane 或 tmux/tmux-session pane, 缺 `窗口` 的旧 herdr 卡复用 `notify` 的唯一会话反查; 0 个或多个命中都拒绝. 投递前复用 `notify` 的 Agent 与会话精确匹配: herdr 另要求 `agent_status` 为 `idle` 或 `done`, tmux 另要求 pane 存活、不在 copy-mode 且前台进程匹配. 被校验 pane 的当前 tab 或 session/window 必须与卡片地址精确一致, 且容器只能包含该 pane; pane 被移动或容器另有 pane 时必须在投递前拒绝, 等待退出期间再次验证归属并在关闭前复核容器拓扑. Claude/Codex 送 `/exit`, Grok/Cursor 送 `/quit`; herdr 用 `agent prompt`, tmux 用 `send-keys -l` 后单独发送 `Enter`. 只有确认 Agent 进程已退出才关 herdr tab 或 tmux window; tmux window 已随 Agent 自动消失时视为已关闭. 任何身份、状态、容器归属、投递、退出确认或关闭失败, 以及超时时都非零返回并保留当时现场, 不强杀、不降级关容器. `foreground`/`console` 没有可关的终端容器, Windows 当前无直投通道, 均在不做部分动作的前提下报错.
- `init` 幂等创建看板及 7 个状态目录 (`backlog`, `todo`, `working`, `review`, `done`, `archived`, `trash`), 既有看板重跑一次即补建缺失目录, Git 项目只更新本地 `info/exclude`. Windows 新目录必须相对固定父句柄以 `CREATE_NEW` 创建并在创建时应用当前用户独占的 protected DACL, 创建竞态失败关闭; 既有目录只迁移叶目录 ACL. Git exclude 的父链逐分量拒绝 reparse point, 既有 ACL 不变, 去重读取和追加在同一固定叶句柄及文件锁内完成.
- 六种 launcher: `auto` 在启动当时解析, 不把结果写回配置; 处于 herdr (`HERDR_ENV=1`) 时按 `herdr` 启动, 否则处于 tmux 时按 `tmux` 启动, 同时处于两者时 herdr 优先, 两者都不在则失败且不领取, 不回落到 `tmux-session`, `foreground` 或 `console`. `tmux` 在启动者当前 session 里后台建任务 window, 要求 `start` 本身跑在 tmux 内; `tmux-session` 按项目主树路径确定一个专属 session (`kb-<目录名>-<路径摘要>`), 不存在就新建, 已存在就复用, 同一项目的全部任务卡共用该 session, 每张卡一个后台 window, 不要求 `start` 跑在 tmux 内, 启动后不切换客户端, 只输出 session 名, window id 和 attach 提示; `herdr` 要求 `HERDR_ENV=1` 且 herdr 在 PATH, 在当前 workspace 后台新建 tab (`--no-focus`, 标签复用 `window_name()`) 后先等根 pane 就绪, 再在该 pane 执行与 tmux 相同的 Agent 命令, 不使用 `herdr agent start`; `foreground` 在当前终端前台运行并等待 Agent 退出; `console` 仅支持原生 Windows, 在独立控制台窗口启动 Agent 后立即返回 PID. `console` 没有 session/window 复用、attach 或输出抓取能力, 不是 tmux 或 `tmux-session` 的等价实现. POSIX 默认 `auto`, Windows 默认 `console`; Windows 拒绝 `auto` 和 `herdr`.
- herdr 的 `pane run` 成功且卡片会话 reference 非空时, `start` 与 `resume`/`notify` 恢复通道共用 launcher 内的上报路径: Onevoke 通过 `HERDR_SOCKET_PATH` 调用 `pane.report_agent_session`, 参数取本次 pane id、卡片 Agent、卡片 reference、`source=herdr:<agent>` 与单调递增的 `seq`, 再在有界预算内用 `pane get` 读回相同的 `agent_session.value`; 空 reference (包括新启动且尚未发现 id 的 Codex) 不上报. socket、响应或读回失败只输出一条告警, 不使启动失败, 不回滚卡片或关闭 tab. herdr 自身集成仍可上报同一身份; Onevoke 的路径用于补齐未触发集成钩子的启动方式.
- `check` 不带任务 ID 时默认列出除 `done/` `archived/` 外的无效入口并以非零退出; `--all` 才纳入这两栏. 指定一个或多个任务 ID 时只检查这些精确目标及其跨状态/入口形态冲突, 无关无效入口不影响结果, 显式指定的目标即使位于 `done/` 或 `archived/` 仍检查. 两种路径都解析适用卡片的 `前置任务`, 校验引用存在且依赖图无环; 定向检查沿指定任务可达的依赖图检查, 跨组环同样失败. 默认遇到 `done/` 或 `archived/` 中的前置卡只确认引用存在, 不检查那些卡片自身, `--all` 才沿完整可达图检查. 依赖尚未满足是正常中间态, 不使 `check` 失败. `subscribe` 要求显式任务组 ID 和非空成员任务 ID, 继续校验成员归属; `--watch` 可重复指定任意外部任务 ID 或任务组 ID, 任务组复用依赖解析的成员读取口展开为当前全部成员, 不校验外部目标的任务组归属. 外部目标不存在, 展开后为空, 与成员重复或彼此展开为重复任务时, 命令在进入订阅循环前失败. `web` 和 `tui` 启动只读看板 UI, 不提供创建, 迁移或启动 Agent.
- `subscribe` 的每行 JSON 都含 `event`, `group_id` 和按任务 ID 映射状态的 `tasks`. 初始事件的 `event` 为 `snapshot`; 状态事件为 `state-change`, 另含 `changed` 数组, 每项固定含 `task_id`, `from`, `to`; 心跳事件为 `heartbeat`. 传入 `--watch` 时, `tasks` 同时包含成员与展开后的外部任务, 每条事件另含顶层 `watched` 数组列出这些外部任务 ID; 未传时不输出 `watched`, 与既有事件兼容. 外部任务变化同样产生 `state-change` 并重置心跳计时. `--refresh` 是状态扫描秒数, 默认 1; `--heartbeat` 是无状态变化后的心跳秒数, 默认 900. 两者必须是有限且大于 0 的数值.
- `web` 是原生 Windows 第一阶段保证的看板 UI. `tui` 默认按终端宽度显示尽可能多的栏目, 每栏默认最小 40 列 (可用 `-`/`=` 调节并记住), 宽度不足时少显示, 不足一栏最小宽度时按实际宽度显示单栏, 左右切换时始终保持选中栏可见. `--single` 即使终端足够宽也只显示一栏. `--theme` 指定初始配色主题 (默认 auto 跟随终端). 方向键或 `hjkl` 切换栏目和任务, 鼠标单击栏目或任务卡聚焦/选中, 双击打开详情, 在任务卡上拖选文本自动复制到系统剪贴板, 滚轮在看板翻卡、在详情滚动正文, PgUp/PgDn 按页翻动任务列表, `/` 搜索 (也可点工具栏搜索区), `y` 复制当前任务 ID, Enter 查看任务卡, `a` 切换存档栏目, `t` 循环切换 auto/light/dark 主题, `r` 刷新, `q` 退出; 搜索覆盖标题, 任务 ID, 任务组, 类型, 负责人和状态. 任务卡详情内可用 `hjkl`/方向键移动光标, 滚轮滚动正文, Ctrl-d/u 半页, Ctrl-f/b 或 PgUp/PgDn 整页, `gg`/`G` 到顶/底, `/` 搜索正文并用 `n`/`N` 跳转匹配, `v`/`V` 进入字符/行选择模式并用 `y` 复制, 拖选正文同样自动复制. 默认每 30 秒自动刷新, 按任务 ID 原位更新并尽量保留当前栏目的选中项和滚动位置. `tui` 与 `web` 扫描看板时忽略无效入口但不向终端注入 CLI 的「运行 kanban check 查看」警告. Windows TUI 仍要求当前 Python 提供可用 curses 后端, 不属于本阶段保证; 无法加载时使用 `kanban web`.
- 命令只做结构和机械校验; 授权, 依赖和终止理由由 Agent 按本文件判断.

## 状态模型

目录是状态唯一真源; 卡片正文不设 `status` 字段.

- `backlog/`: 已记录但尚未承诺执行.
- `todo/`: 用户已确认, 契约完整, 尚未领取.
- `working/`: 已领取, 正在实现, 验证, 审核或集成; 任务组卡在修复轮次和集成后的收尾也回到这里.
- `review/`: 仅任务组卡使用. 开发, 验证已完成, 任务分支已提交 push 并 ff 进组集成分支, 等主控安排组级审核与集成; 执行 Agent 迁入后退出, 集成成功后经主控 `notify` 收尾通知回到 `working/`.
- `done/`: 已满足完成门禁的近期任务.
- `archived/`: 不占活跃看板的完成, 取消, 重复或不修复记录.
- `trash/`: 用户明确要求删除, 但尚未永久清理的入口; 不是任务状态.

```text
backlog -> todo -> working -> done -> archived        (单卡流程)
                      |  ^
                      v  |  修复轮次与收尾迁回 working
                    review -> done                     (任务组流程; 直迁仅限主控代做收尾)

backlog, todo, working, review -> archived            仅限用户授权的终止
除 trash 外任意状态 -> trash                            仅限用户明确要求
```

- 进 `todo/` 须完成任务目标, 预期成果, 验收条件和不在本轮范围; 进 `review/` 须已填写 `任务分支`; 进 `done/` 的门禁见「执行与完成」, 其余见「终止与清理」.
- 旧版看板没有 `review/`: 其余 6 个状态目录齐全时, 任一 `kanban` 命令首次定位看板即自动补建 `review/`, 不要求用户重跑 `init`; 其他状态目录缺失, 或 `review` 位置被文件/符号链接占用时所有命令失败.

## 入口与文档

### 不变量

- 状态目录的每个直接子项是一张卡: 小任务为 `YYYYMMDD-short-slug-task.md`, 大任务为同名目录且必须含普通文件 `spec.md`. `short-slug` 只含小写 ASCII 字母, 数字和连字符; 去掉扩展名的入口名即任务 ID.
- 任务 ID 全看板唯一, 不得跨状态重复或同时存在文件和目录形式. 迁移移动整个入口; 入口名创建后不改, 不复制后删, 不留副本. 大任务目录内只用相对链接, 保证迁移后有效.
- 卡片不得包含 token, 凭据, 敏感服务地址或不应留在本机的个人数据.

### 小任务模板

```markdown
# <任务标题>

- 类型: Feature | Bug | Chore | Research
- 任务组:
- 创建时间: YYYY-MM-DD HH:MM
- 负责人:
- 会话:
- 窗口:
- 开始时间:
- 完成时间:
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

- <按既有问题, 加固, 共享契约与文档, 相邻功能四类逐一写明排除或纳入, 每条附理由>

## 讨论与决策

<关键结论; 任务组卡片还要在开头记录前置任务>

## 实施与验证

<计划, 分支, commit, 验证命令, 结果, 环境缺口和阻塞>

## 完成总结

<实际成果, 偏差, 未处理问题和验收结论; 完成前留空>
```

### 大任务文档

- `spec.md` 必需, 含小任务的元数据及契约章节: 任务目标, 用户决策, 预期成果, 验收条件, 威胁模型, 不在本轮范围, 讨论与决策.
- `plan.md` 按需创建, 记录实施步骤, 影响模块, 验证, 发布和回滚计划, 不得修改 `spec.md` 契约.
- `report.md` 完成时创建, 记录实际改动, 最终 commit, 验证, 偏差, 未处理问题, 风险和验收结论; 不建空文件.

### 契约与记录

- 领取后填写负责人, 开始时间和任务分支, 无分支写 `N/A`; `start` 同时写入相邻的 `会话` 与 `窗口` 字段, 旧卡缺字段时插在负责人之后, 手工领取的卡留空. 命令迁入 `done/` 时填写完成时间. 结果只在进入 `done/`, `archived/` 或 `trash/` 前填写.
- 卡片进入 `todo/` 后, 任务目标, 用户决策, 预期成果, 验收条件, 不在本轮范围以及任务组关系冻结. 修改任何一项都要先取得用户明确决策.
- 「不在本轮范围」是审核的 risk-bounded stop 边界, 必须按四类逐一判定并写明理由: (1) 改动前已存在的问题; (2) 并发交错, 跨平台与安全加固; (3) 共享契约, 公共 API 与架构文档的同步; (4) 相邻功能与后续阶段. 验收条件未要求的类别写明排除, 由本卡承担的类别写明纳入; 只有一条泛泛的排除不算契约完整, `pick` 前由建卡 Agent 补齐. 审核时 reviewer 与主代理都以这份清单判定 finding 是否越出契约, 越出的记未处理项并建议后续卡.
- 实施期只追加关键决策, 验证, 环境缺口, commit, 阻塞和下一步, 不复制会话流水. 稳定的架构, API 和长期规则仍须写入仓库文档或项目规则.

## 任务规模与任务组

- 一张卡只承载一个任务目标. 需求含多个可分别验收的目标时必须拆成多张卡, 一卡一目标, 再按任务组组织依赖; 不得把多个目标合写进同一张卡的任务目标或验收条件.
- 默认拆小: 总体目标能拆成可独立验收的子目标就必须拆成任务组, 能否并行不是判据. 只能串行的子目标同样拆卡, 用 `前置任务` 表达顺序; 不得因为范围大, 子目标之间有依赖, 或"反正是一个人做"而保留为单张大任务卡.
- 小卡的粒度: 一个可观察, 可验证的成果; 一个执行 Agent 在一次会话内能完成; 一轮 PM/QA 审核能精读完其 diff. 出现下列任一信号即应再拆: 验收条件里有多个可分别验收的目标; 需要 `plan.md` 分阶段; 改动跨多个互不相关的模块或测试集; 完成后要分别向不同接口方交付. 行数不是判据.
- 大任务形态是例外, 只用于目标, 负责人, 验收和生命周期必须统一, 拆开后任何一张卡都无法独立验收的情形. 选 `--large` 时必须在 `讨论与决策` 写明「为何不能拆」, 没有理由的大卡在 `pick` 前退回拆分.
- 涉及共享接口, 数据格式或跨卡契约时先建一张契约卡: 它只定义接口并写进仓库文档或项目规则, 后续实现卡以它为前置任务, 避免拆小后各卡对接口理解不一致.
- 拆卡应减少依赖以便并行, 且不得职责重叠; 不能安全隔离的同资源修改须建立依赖并串行, 但不影响其他无冲突子任务并行. 组内每张子卡再按自身复杂度选小任务或大任务形态.
- 新卡默认是小任务. 小任务变复杂时, 仅 `backlog/` 的当前编辑者或 `working/` 的负责人可以升级: 建同 ID 目录, 原内容转入 `spec.md`, 按需建 `plan.md`, 不保留原文件. `todo/` 中禁止改变形态. 已由 `start` 启动的卡升级后不换 Agent, 不重新 `start`, 只在完成报告注明规模变化.

任务组只是独立卡片间的关系, 不是入口或状态. 每张卡的元数据都保留可选的 `任务组` 字段; 不属于任务组时留空, 属于任务组时必须填写组内一致的任务组 ID. 每张组内卡还在 `讨论与决策` 开头记录:

```text
前置任务: N/A
```

- 任务组 ID 格式为 `YYYYMMDD-short-slug-group`, 全看板唯一且组内一致. `前置任务` 固定写在 `讨论与决策` 开头的现有代码块中, 值为 ASCII 逗号分隔的看板任务 ID 或任务组 ID, 两类 ID 可混写; 无依赖写 `N/A`, 旧卡整行缺失也按无依赖处理. 引用任务组 ID 表示依赖该组当前全部成员卡, 解析读取口把它展开为成员任务 ID, 并将直接引用区分为组内卡、组外卡和任务组三类. 组内前置进入 `review/` 或 `done/` 即满足, 因为其改动已在本组组集成分支上; 组外任务卡以及任务组展开后的成员均须进入 `done/` 才满足, 因为只有此时改动已进入 `develop` 并可供本组使用. 不属于任务组的卡不启用依赖契约, 按无依赖处理.
- 升级前没有 `任务组` 元数据的旧卡按空值处理; 旧卡已在 `讨论与决策` 中记录 `任务组: ...` 时, 读取方继续兼容, 不要求批量改写.
- 建组时一次列全卡片和依赖图, 排除缺失引用, 环, 职责重叠及无法独立验收的卡片; 进 `todo/` 前冻结关系.

## 创建与确认

收到 Bug 或功能开发需求时, 先完成需求分析和实施计划, 再一次性让用户选择:

```text
1. 确认计划并走看板 (建卡并启动)
2. 确认计划, 不走看板, 在本会话直接做
3. 调整计划
```

- 选 1 同时确认计划, 开发和看板流程, 不再确认开工. 讨论 Agent 必须把实现委派给新执行 Agent: 单卡依次执行 `new`, 填卡, `pick`, `start`; 任务组一次创建, 填完并 `pick` 全部卡片, 再由编排 Agent 按依赖执行 `start`, 不逐卡确认. 未经用户明确覆盖时, `start` 使用 Onevoke 配置的 launcher; 配置为 `auto` 时按当前环境解析为 herdr tab 或 tmux window.
- 选 1 禁止改用 `kanban move <task-id> working` 领取, 也禁止讨论 Agent 在 `start` 启动成功后继续实现该任务; 单卡和任务组分别按「领取, 启动与协调」和「任务组编排」移交后续责任.
- 选 2 按项目规则直接实施, 不建卡; 选 3 继续调整, 不建卡或启动.
- 已由 `kanban start` 拉起, 已指定现有卡片, 纯问答, 只读排查, 纯文档或配置微调, 发布部署和合入操作, 不提供以上选项.
- `kanban new` 只在 `backlog/` 创建模板; 执行它的 Agent 须立即用已确认内容填完契约, 不留 `<填写>`. 只有用户确认开发或明确授权的协调 Agent 才能移入 `todo/`; Agent 建议不得冒充用户决策.

## 领取, 启动与协调

- 未指定任务且 `todo/` 有多张卡时列候选让用户选; 任务组按已确认依赖排序, 不逐卡询问. 开工条件不足时只报缺口, 不领取或退回 `backlog/`.
- 动代码前必须先取得 `working/` 中的唯一入口. 两种领取方式互斥:

```sh
# 委派给新执行 Agent: start 原子领取并启动
kanban start [--agent codex|claude|grok|cursor] [--launcher auto|tmux|tmux-session|herdr|foreground|console] <task-id>

# 用户明确要求当前 Agent 执行既有任务卡: 只迁移, 随后手工填写负责人和开始时间
kanban move <task-id> working
```

- `kanban move <task-id> working` 仅适用于用户明确要求当前 Agent 执行既有任务卡; 选择「确认计划并走看板」时必须用 `start`. 不得先 `move ... working` 再 `start`; `start` 只接受 `todo` 卡. 同文件系统上的入口迁移就是领取原语, 只有迁移成功者取得任务; 失败后重查, 不建替代卡, 不另加 lock 服务, 数据库或 ID 分配器.
- `start` 在启动前检查 Agent, launcher 和 TTY; `auto` 先按当前环境解析再走对应检查, `tmux` 要求已在 tmux session 内, `tmux-session` 只要求 tmux 可用并在此时选定项目专属 session 名, `herdr` 要求 `HERDR_ENV=1`、herdr 在 PATH 且有 `HERDR_WORKSPACE_ID`, `foreground` 要求三个标准流都是 TTY, `console` 要求原生 Windows. 前置检查失败不领取; 创建进程, tmux session, tmux window, herdr tab, herdr pane 就绪等待或 `pane run` 失败时恢复文档并迁回 `todo/`; herdr 就绪等待或 `pane run` 失败还须关闭本次新建的 tab, tmux/tmux-session 的 Codex 会话发现或 pane 会话标记写入失败还须关闭本次新建的 window. 新建 tab 的 shell 接管终端前送入的命令文本会被丢弃, 因此 `pane run` 必须在 pane 渲染出首帧输出之后, 就绪等待有上限, 超时按失败处理. tmux/tmux-session 只有在会话发现与 pane 标记写入成功后才算启动成功; 其他 launcher 在进程创建成功后即算启动成功, 后续退出不自动回滚. `console` 成功时输出 PID 后立即返回. 成功输出报告解析后的实际启动方式 (`herdr` 或 `tmux`), 而不是只写 `auto`.
- herdr 在 `pane run` 成功后即算启动成功; 后续会话身份上报和读回是 best-effort, 失败只告警, 不进入 `LaunchFailure` 的 tab 关闭与卡片回滚路径.
- `start` 的临时任务文件只写任务 ID 和固定要求, Agent 命令行只传一句读取该文件的指令. 执行 Agent 先读本规则, 卡片和项目规则, 再准备工作区并填写任务分支.
- 领取后只有执行负责人可修改或迁移 `working/` 入口; 协调和编排 Agent 只读督办. 明确交接后由新负责人接管, 不得并发写.
- 启动后的协调责任按启动方式分:
  - foreground 单卡: 启动者在 Agent 退出后检查结果, 直到任务完成或明确交接.
  - tmux、tmux-session 或 herdr 单卡: 执行 Agent 在独立 window 或 tab 直接向用户汇报, 启动者不巡检. 启动成功后立即告知用户本会话不跟踪该任务进度, 当前 session 可以结束, 下一个任务另开会话; `tmux-session` 还要一并给出 session 名和 attach 命令; `herdr` 还要给出 tab id 和 pane id. `auto` 解析为 `herdr` 或 `tmux` 后按对应单卡规则协调. 用户明确要求跟踪时改按 foreground 单卡协调.
  - console 单卡: 执行 Agent 在独立 Windows 控制台直接向用户汇报, 启动者不抓取输出. 启动成功后告知用户 PID 及本会话不跟踪进度; 该 PID 只用于只读判断进程是否仍存在, 不能用于 attach 或恢复输出. 用户明确要求由启动者跟踪时改按 foreground 单卡协调.
  - 任务组: 按「任务组编排」督办, 组级审核, 集成和收尾, 启动成功不解除该责任.

## 任务组编排

任务组采用组级审核: 执行 Agent 只负责开发, 验证, 提交和 push; 主控 (编排) Agent 决定何时审核, 把 finding 派回对应卡的原执行 Agent 修复, 审核通过后统一集成, 再把每张卡的收尾派回该卡的原执行 Agent. 主控负责结束组内每个执行 Agent 的任务: 集成成功后逐卡发出收尾通知, 并跟踪到卡片进入 `done/`; 整组完成并汇报后, 再询问用户是否退出这些执行 Agent 并关闭其终端容器. 单卡流程不变, 仍由执行 Agent 自行审核和合回.

### 角色与分工

- 用户启动任务组后, 启动者成为主控 Agent, 负责: 校验依赖, 创建组集成分支, 按顺序启动就绪卡, 订阅状态事件, 安排审核批次, 归属并派回 finding, 集成组分支, 逐卡派发收尾并跟踪其闭环, 删组分支和组级结论, 最后询问用户是否遣散负责各任务卡的执行 Agent. 主控不实现组内任务, 不直接修改子任务的代码, worktree, commit 或测试; 派回事项只调用一次 `kanban notify` 并检查退出码, 由命令选择直投或恢复通道; 非零退出即停止并报用户, 不绕过命令直接向窗口或会话发送按键, 消息或催促.
- 组内卡的执行 Agent 负责: 准备工作区, 实现, 验证, 按关注点提交并 push 任务分支, 按 `GIT-RULES.md`「组集成分支」ff 进组分支, 写好 `实施与验证` 和「完成总结」中的交付, 验收, 验证部分, 填 `任务分支`, 执行 `kanban move <task-id> review` 后退出. 执行 Agent 不触发审核, 不合回 `develop`, 收到收尾通知前不清理 worktree 与任务分支; 收到 `notify` 派回的 finding 时承担 `REVIEW-RULES.md`「主代理的核实义务」并修复; 收到派回的收尾通知时按「集成与收尾」完成本卡收尾, 汇报后退出.

### 启动与订阅

- 启动前读取全组卡片, 核对 ID, 依赖, 契约和修改范围, 解析每张卡的全部 `前置任务`, 并把直接引用分为组内卡、组外卡和组外任务组; 有缺失引用, 环或隔离冲突时不启动受影响卡. 将已确认的 `backlog` 卡片移入 `todo/`, 已处于后续状态的卡片保持原状.
- 有未满足的组外依赖时, 不创建组分支, 不启动任何卡, 全组保持在 `todo/`; 首次识别出这种状态时立即告知用户暂不开工的原因和被依赖的任务卡或任务组, 之后不主动打扰用户. 主控运行 `kanban subscribe <task-group> <全部成员 task-id>... --watch <组外 task-id|task-group-id>...` 阻塞等待, `--watch` 按直接引用传入并可重复使用. 组外卡进入 `done/`, 或组外任务组展开后的全部成员卡进入 `done/`, 才算满足; 理由是只有此时对方改动才已进入 `develop`. 任一被观测的组外卡进入 `archived/` 或 `trash/` 时, 立即停止等待并交用户决策, 不自行改判依赖已满足.
- 组外依赖等待期沿用订阅的状态事件和 15 分钟 heartbeat 语义: 状态变化时只定向复核相关依赖, heartbeat 时全组没有 `working` 成员可检查, 故只确认订阅仍在运行; 不运行全看板 `kanban check`, 不另行轮询, 也不因无输出或等待时间长而打扰用户.
- 组外依赖全部满足后停止等待订阅, 再按 `GIT-RULES.md`「组集成分支」基于当时最新的 `develop` 创建组分支, 记录其 base commit, 然后进入既有启动流程. 没有未满足的组外依赖时, 首卡启动前直接执行这一步.
- 每张就绪的 `todo` 卡都用 `kanban start <task-id>` 启动, Agent 按卡片规模取配置, launcher 默认取 Onevoke 配置, `--launcher` 只覆盖本次; 一个任务组内只用同一种 launcher, 所选 launcher 在当前平台不可用时报告阻塞. 首轮启动无组内前置任务的卡 (其组外前置已按上述流程全部满足), 之后只启动全部组内前置卡都在 `review/` 或 `done/` 的卡; 同时就绪且无资源冲突的卡并行启动, 禁越过依赖提前启动. `start` 的任务文件只传任务 ID; 执行 Agent 从卡片 `任务组` 字段和本规则得知自己走任务组流程.
- 首张卡启动成功后立即告知用户: 本会话是任务组主控, 须保留到全组按依赖顺序执行完毕, 不要结束当前 session; 提前结束会失去依赖校验, 顺序启动, 组级审核和集成. 主控会话持续到任务组成功或用户明确终止.
- 启动首批需监控的组内任务后, 主控运行 `kanban subscribe <task-group> <task-id>...` 并阻塞读取其逐行 JSON 输出. 命令先输出当前真实状态的 `snapshot`, 之后只在目标卡状态变化时输出 `state-change`; 事件包含任务组 ID, 变化卡的前后状态和全组当前快照. 主控按初始快照补齐订阅启动前已经发生的迁移, 不依赖历史事件.
- 收到 `state-change` 后, 主控只对变化卡以及可能因它进入 `review`/`done` 而解除依赖的直接后继卡运行 `kanban check <task-id>...`, 读取这些卡片并核对依赖; 新就绪卡仍按既定顺序用 `kanban start` 启动; 进入 `review/` 的卡进入待审核集合; 已派发收尾的卡进入 `done/` 即该卡收尾闭环. 新成员启动或经 `notify` 派回后重启订阅, 参数包含当前仍需监控的明确成员集合, 并以新的初始快照继续判断.
- 订阅无状态变化达 15 分钟时输出 `heartbeat`. 心跳只用于检查处于 `working` 的成员及对应执行 Agent 是否仍存活, 不运行全看板 `kanban check`, 不重新读取无关卡. Agent 消息或用户输入可触发同范围的额外存活检查, 不改变下一次心跳语义.
- 状态事件之间持续阻塞读取订阅输出, 禁止自行增加短周期轮询. 只有状态事件, heartbeat 或订阅进程明确失败/退出才触发处置; 无输出和等待时间长本身不构成异常.
- 状态事件后的定向检查或 heartbeat 存活检查中, launcher 提供只读输出通道时可查看对应执行 Agent 的输出. tmux 启动的卡用 `kanban start` 返回的 window id 执行 `tmux capture-pane -p -t <window-id>`, 需要时配合 `tmux list-windows` 确认窗口是否还在; `tmux-session` 启动的卡同样用返回的 window id, 列窗口时加 `-t <session>`. herdr 启动的卡用返回的 pane id 执行 `herdr pane read <pane-id>`. `console` 不提供输出抓取, 只用返回 PID 只读判断进程是否存在并结合卡片状态判断; 不读取, 控制或关闭独立控制台, 不把 PID 当作 tmux window id 或可恢复 session.
- 查看输出只读不交互: 不绕过 Onevoke 命令向执行 Agent 的窗口或会话发送按键, 消息, 催促或指令, 不中断, 恢复, 重启, 接管或改派子任务. 派回事项只用 `kanban notify`; 命令会自行选择直投或恢复, 非零退出时停止并报用户.

### 审核批次与派回

- 主控决定审核时机与批次: 把已进 `review/` 的卡按同一模块, 同一里程碑或依赖链分批, 一批的 diff 应能被 reviewer 精读; 不强制全组一次, 也不强制每卡一次. 批次的 CWD, base, task context 和角色流转按 `REVIEW-RULES.md`「任务组的组级审核」执行: 每批是独立的完整审核, 后一批的 base 是前一批通过的组分支 commit; 只有批内修复轮次做增量复审.
- finding 由主控按卡片的修改范围归属: 命中哪张卡的 `任务目标`/`不在本轮范围`/实际改动就派给哪张卡; 跨卡的集成类 finding 派给修改范围命中的卡, 都命不中时主控建一张小修复卡加入本组 (填 `任务组` 与 `前置任务`, `pick` 后 `start`).
- 派回只调用一次 `kanban notify <task-id> --message-file <findings>` 并检查退出码, 通道选择、恢复和状态回滚由命令内部完成; 非零退出即停下报用户. 文件写明 reviewer 角色, 档位, finding 原文和主控已知的事实, 不写主控自己的结论. 卡片迁回 `working/`, 原执行 Agent 带上下文继续: 逐条核实, 修复, 提交 push, rebase 到组分支头并 ff, 在 `实施与验证` 写上轮 finding 清单与处理结论, 再 `move review`. 主控收到 `state-change` 后汇总各卡清单组成 review context, 触发增量复审.
- 执行 Agent 判为不成立或超出契约的 finding 连同依据回到主控; 主控不得改写, 按 `REVIEW-RULES.md`「主代理的核实义务」纳入未处理项交用户复核.
- 一张卡经 `notify` 派回后未进入 `review/` 而执行 Agent 已退出, 或同一 finding 两轮未闭环, 按「异常恢复」记录现状并向用户报告, 不改派, 不由主控代修.

### 集成与收尾

- 全部组内卡进入 `review/` 且所有批次审核通过后, 主控按 `GIT-RULES.md`「组集成分支」把组分支 rebase 到最新 `develop`, 重做验证, fast-forward 合回并 push; `develop` 前进只重做 rebase 和验证, 沿用已通过审核结论. 组内任一卡仍在 `working/` 时不集成.
- 集成失败 (rebase, 验证, push 或 ff 未完成), 用户要求暂停或不合回时: 组内卡全部留在 `review/`, 保留组分支, 任务分支与 worktree, 主控记录阻塞及解除条件并按「完成报告」模板逐卡汇报, 末行状态写 `review (阻塞)`; 只有确实派回事项 (rebase 冲突, 审核 finding, 集成成功后的收尾) 时才用 `notify` 把对应卡迁回 `working/`, 主控不得为表示阻塞而手工迁移卡片.
- 集成成功后主控不自己逐卡收尾, 而是按依赖顺序 (无资源冲突的卡可并行) 只用 `kanban notify <task-id> --message-file <收尾通知>` 通知该卡的原执行 Agent 收尾. 通知文件写明: 组分支已 ff 合回 `develop` 及其完整 SHA, 该卡最终 commit, 各角色审核结论, 批次与修复轮次, 归属该卡的未处理项, 以及下一条的收尾清单. 主控不替执行 Agent 改写卡片正文, 派发后也不手工迁移该卡.
- 执行 Agent 收到收尾通知时卡片已被 `notify` 迁回 `working/`, 一张卡的收尾是一个整体: 先按 `GIT-RULES.md`「集成与清理」的清理前置确认本卡改动已进入 `develop` (`git merge-base --is-ancestor`), 未满足时不清理, 保留现场并回报主控; 满足后运行记忆合并, 删本卡 worktree 与本地/远端任务分支 (不删组分支), 在卡片「完成总结」补写审核和收尾两部分 (各角色结论, 修复轮次, 最终 commit, 组分支与 `develop` 集成结果), 填 `结果: completed`, 执行 `kanban move <task-id> done`, 按「完成报告」模板汇报后退出. 执行 Agent 是该卡的结束者; 它在迁 `review/` 前写好的交付, 验收, 验证部分不由主控改写.
- 无法派回或收尾未闭环时由主控代做: 卡片没有可用的 `窗口`/`会话` 记录, `notify` 非零退出, 或派回后执行 Agent 已退出而卡片仍未进 `done/` 时, 主控自己完成该卡的整套收尾并作为结束者汇报, 在该卡完成报告的 `收尾` 和组级汇总里写明「收尾由主控代做」及原因. 这是「审核批次与派回」中「不由主控代修」的明确例外, 只适用于收尾: 收尾不改代码, 只做清理和记录; 需要改代码的 finding 仍不得由主控代修.
- 清理中途失败 (某张卡的记忆合并或分支, worktree 删除失败) 时: 已完成收尾并进入 `done/` 的卡保持 `done/`, 不回退; 失败卡停在它当时的实际状态 (已派发收尾的在 `working/`, 未派发的在 `review/`), 只保留尚存的 worktree 与分支, 组分支保留; 逐卡按真实状态汇报, 失败卡写明失败步骤, 错误和解除条件, 状态写 `working (阻塞)` 或 `review (阻塞)`. 代码已在 `develop`, 因此不重新集成, 解除后只继续未完成的收尾.
- 全部组内卡进入 `done/` 后停止订阅, 删组分支, 运行一次不带目标的 `kanban check`; 完整检查通过才算成功. 任一卡进入 `archived/` 或 `trash/` 时, 须等待用户修改组契约或终止整组.
- 编排结束时汇总执行顺序, 并行情况, 审核批次与轮次, 集成结果, 再按卡片列出"未处理问题", 分类与记录要求按 `REVIEW-RULES.md`「结论与故障处置」的未处理项清单, 另加验证缺口和后续任务; 没有写"无", 每项另写任务 ID. 终止时按「完成报告」模板逐卡补发未完成卡的汇报, 再列终止决策.
- 只有全部组内卡已进入 `done/`, 组分支已删除, `kanban check` 已通过且组级汇总已完成时, 主控才一次性询问用户是否退出负责这些任务卡的执行 Agent 并关闭对应 herdr tab 或 tmux window. 未得到明确确认时不执行任何退出或关闭动作; 用户拒绝时保留全部会话和终端容器, 主控结束本次编排.
- 用户确认后, 主控对每张组内任务卡只调用一次 `kanban dismiss <task-id>`, 不直接向 Agent 发送退出指令, 不直接关闭 tab/window, 也不绕过命令的身份、拓扑和优雅退出门禁. 各卡的遣散结果相互独立: 某卡返回非零时保留其现场并记录原因, 继续处理其余组内卡; 全部尝试结束后汇总逐卡结果. 这是任务组完成后的可选终端清理, 失败不回退已完成卡片, 不恢复已删除的分支或 worktree, 也不改变已发出的完成报告和组级结论.

## 执行与完成

- 单卡在 `working/` 中按项目规则完成准备, 实现, 验证, 提交, push, 审核, 集成和清理; 任务组卡只做到提交, push 与 ff 进组分支, 然后迁 `review/`, 审核和集成由主控按「任务组编排」完成, 集成成功后主控把收尾派回本卡执行 Agent, 由它完成清理和收尾. 暂时失败或阻塞时保留原状态并记录阻塞及解除条件. 小任务写 `实施与验证`, 大任务按需维护 `plan.md`.
- 审核门槛和安全审核决策超时按 `REVIEW-RULES.md`, 本文件不另定. 未处理项先写入小卡 `实施与验证` 或大卡 `plan.md`, 再带入完成总结或 `report.md`.
- 默认完成顺序如下, 集成前不设验收环节, 不停下等用户确认:

```text
单卡:   实现与验证 -> 必要审核 -> 集成与清理 -> 写完成记录
        -> move done -> kanban check -> 最终完成报告
任务组: 实现与验证 -> 提交 push 并 ff 进组分支 -> 写实施与验证及交付/验收/验证
        -> move review -> (主控) 组级审核 -> notify 修复 -> (主控) 集成组分支
        -> (主控) notify 收尾 -> 记忆合并与清理 -> 补写审核/收尾 -> move done
        -> 完成报告 -> (主控) 删组分支 -> kanban check -> 组级汇总
        -> 询问是否遣散执行 Agent -> (用户确认后) 逐卡 dismiss -> 汇总遣散结果
```

- 合回时机取 `ONEVOKE-AGENTS.md`「看板任务完成」: 默认在验证和必要审核通过后, 按 `GIT-RULES.md`「集成与清理」直接 fast-forward 合回目标分支, 不等用户验收.
- 用户要求暂停或不合回, 必要审核未通过, 或集成, 清理失败时: 单卡不集成也不迁 `done/`, 留 `working/`, 保留分支与 worktree; 任务组卡在集成失败时全部留 `review/` 并保留资源, 集成成功后收尾中途失败时已进入 `done/` 的卡不回退, 其余卡停在实际状态 (`working/` 或 `review/`) 只保留尚存资源, 均按「任务组编排」的「集成与收尾」执行. 两种情形都记录阻塞及解除条件, 并按本文件「完成报告」模板向用户汇报. 用户明确要求的验收或集成确认不适用 15 分钟超时.
- 实现, 验证记录, 必要审核及适用的集成清理全部完成后, 才可填写完成总结或 `report.md` 和 `结果: completed`, 再执行 `kanban move <task-id> done` 和 `kanban check`. 非代码任务的不适用项写 `N/A`. 任务组卡在主控完成组集成并派发收尾后由该卡执行 Agent 执行这一步, 主控代做收尾时由主控执行; 执行 Agent 不得从 `review/` 直接迁 `done/`, 必须经 `notify` 回到 `working/` 后再迁.
- 用户在完成报告后测试发现的问题按新任务处理: 另建卡片并在 `讨论与决策` 指向原卡, 不把已进 `done/` 的卡退回 `working/`, 也不复用原卡继续改.

## 完成报告

- 任务卡本轮结束时一律按模板汇报一次, 不得用自由格式总结代替, 也不得只报一句完成或阻塞了事. 结束指卡片进入 `done/`, `archived/` 或 `trash/`, 以及阻塞后停在 `working/` 或 `review/` 交回用户; 卡片仍在推进时不发.
- 8 个字段不得省略也不得合并, 无内容写 `无` 或 `N/A`. 结束方式写进末行 `任务卡最终状态`, 取 `done`, `archived (<结果>)`, `trash`, `working (阻塞)` 或 `review (阻塞)`.
- 未进 `done/` 时, `任务` 用卡片当前所在状态目录下的绝对路径, `交付`, `验收`, `验证`, `审核`, `收尾` 只写已实际完成的部分, 未做项写 `未执行` 并注明原因; 不得因为任务没完成就留空, 省行或改用别的格式.
- 阻塞汇报在 `未处理问题` 里逐项列出剩余工作, 阻塞原因和解除条件, 并写明分支与 worktree 的保留现状; 终止汇报另写用户授权的结论 (`cancelled`, `duplicate`, `wontfix`) 和原因, `duplicate` 指向替代卡.
- 谁结束谁汇报: 交接后的接管 Agent 按同一模板汇报, 不因换会话或换执行者免除; 协调 Agent 的组级汇总不替代单卡汇报.
- 验证只写实际结果, 失败, 未执行或环境阻塞不得写成通过; 最终提交用完整 40 位 SHA. 收尾成功项可合并为"均完成", 异常须逐项说明.
- 未处理问题的分类与记录要求按 `REVIEW-RULES.md`「结论与故障处置」的未处理项清单, 另加验证缺口和后续任务; 同一根因只计一次. 安全审核超时项附发送时间和超时时间.

```markdown
# 看板任务完成报告

- 任务: [<task-id> - <标题>](<done 下任务入口绝对路径>)
- 交付: <用户可观察结果和关键改动>
- 验收: <已完成数>/<总数>; <逐条自检结论或用户接受的例外>
- 验证: <实际命令和结果; 失败或未执行项的原因, 影响和替代证据>
- 审核: <PM, CSA, Hacker, QA 的 reviewer, 状态和摘要; 审核期间修复>
- 收尾: <完整 SHA | N/A>; <集成结果 | N/A>; <主树同步, worktree, 分支, 临时审核文件, `kanban check`, memsearch 均完成或逐项异常>
- 未处理问题 (<N>): <无; 或逐项写 `[来源或类别][档位或状态] 问题; 影响: ...; 理由: ...`; 超时项附发送时间和超时时间>
- 总结: <一句话总结>; 代码分支: <代码最终所在分支 | N/A>; 任务卡最终状态: <done | archived (<结果>) | trash | working (阻塞) | review (阻塞)>
```

## 终止与清理

- 用户明确取消, 判定重复, 决定不修或接受替代方向后, 才可将 `backlog/`, `todo/` 或 `working/` 卡直接归档; 实现困难, 验证失败或暂时阻塞不算授权. 结果只能是 `cancelled`, `duplicate` 或 `wontfix`, 均写原因, `duplicate` 还须指向替代卡. `completed` 只用于 `done -> archived`.
- 卡片迁入 `archived/` 或 `trash/` 后, 执行或操作该卡的 Agent 按「完成报告」模板汇报一次, 末行状态写实际去向和结果.
- `done/` 保留近期完成项, 用户确认无需展示后再归档. 只有用户明确要求删除具体卡片时才移入 `trash/`; 迁移前写 `结果: trashed`, 原因和时间. 不自动清空或永久删除; 永久删除须逐项授权.

## 异常恢复

- `working/` 卡中断, 无负责人或长期无进展时, 协调 Agent 只用 `kanban notify <task-id> --message <现状与要求>` 通知原执行 Agent; 命令自行选择直投或恢复, 非零退出时由用户决定交接或终止. 其他 Agent 不得自行接管, 迁移或归档. 进程退出不改变 `working/`, 不退回 `todo/` 或再次 `start`.
- 出现重复 ID, 跨状态副本, 文件与目录同 ID, 大任务缺 `spec.md`, 目标冲突, 状态目录缺失或不可写时, 停止受影响操作并保留现场. 不通过删除, 改名或移动来绕过报错.
- 看板无 Git 历史; 误删先查 `trash/` 和本机备份, 不伪造内容.
