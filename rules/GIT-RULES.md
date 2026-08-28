# Git 工作流规则

本文件是 `BASE-RULES.md`「Git 工作流」的完整契约, 位于规则根的 `GIT-RULES.md`. 优先级见 `ONEVOKE-AGENTS.md`「优先级」. 只适用 Git 仓库; 非 Git 目录无分支, worktree, 审核, 集成, 直接改文件.

## 分支与 worktree

- 分支模型固定为 `main` + `develop`: `main` 是稳定分支, `develop` 是唯一集成分支. 不读取项目级分支取值, 不询问用户选择分支模型, 不回落到 `refs/remotes/origin/HEAD` 或其他分支.
- 初始化分支时, 有 `origin` 就先 fetch 并要求 `origin/main` 存在. `origin/develop` 不存在时: 本地也没有 `develop` 就从最新 `origin/main` 创建; 本地已有 `develop` 就先确认 `origin/main` 是它的祖先; 通过后普通 push 到 `origin/develop`, 不通过则停止并报告. 无 `origin` 或用户明确要求仅本地时, 要求本地 `main` 存在; 本地 `develop` 不存在时从 `main` 创建. `main` 不存在时停止并报告, 不猜测替代分支, 不重写历史.
- 除下述 Markdown 直改路径外, 所有改文件任务都用独立任务分支和 `<仓库根目录>/worktrees/<task-name>/` 专用 worktree. `<task-name>` 同分支名, 短 kebab-case; 任务分支不得是 `develop` 或 detached `HEAD`. 已在当前任务专用 worktree 和任务分支时直接复用.
- 有 `origin` 且用户未要求仅本地集成时, 先 fetch, 再基于最新 `origin/develop` 建任务分支; fetch 失败则停止创建并报告. 无 `origin` 或用户明确要求仅本地集成时, 基于本地 `develop` 建任务分支, 报告未同步远端.

## Markdown 直改路径

- 须同时满足: 任务只改 Markdown 文件; 当前分支是 `develop` 或用户明确指定的目标分支; 任务开始时工作树无未提交或未跟踪文件. 任一不满足用专用 worktree.
- 有 upstream 且用户未要求仅本地集成时, 改前必须 fetch 并 fast-forward, 确认 `HEAD` 等于 upstream. 本地领先, 分叉或无法同步时改用专用 worktree.
- 先完成验证和必要审核再普通 push; 此路径不走「集成与清理」流程. 审核 base 为改前 `HEAD`, 见 `REVIEW-RULES.md`.

## 本地改动保护

- 所有未提交的本地改动都视为用户资产, 包括已暂存, 未暂存和未跟踪文件. 绝对不允许用 `git restore`, `git checkout --`, `git reset --hard`, `git clean` 或任何等效方式丢弃, 覆盖或删除; 不因改动与当前任务无关而例外.
- 主树有未提交改动却必须执行 rebase, merge, fast-forward, 切换分支或其他要求干净工作树的主树操作时, 先用 `git stash push --include-untracked` 暂存全部改动, 确认新 stash 已创建后再操作; 不把这些改动提交进当前任务.
- 主树操作结束后立即运行 `git stash pop --index` 恢复原改动. 若产生冲突, 保留主树操作结果和原本地改动, 逐项解决冲突并恢复原暂存状态; 在确认全部改动已恢复前不得删除 stash, 清理文件, 宣告完成或离开现场. 恢复失败或无法无损解决时停止并报告用户.

## 提交与 push

- 每个已完成并通过对应验证的独立关注点单独提交, 不混无关改动. 提交 subject 默认中文动宾短语, 如 "修复登录竞态"; 项目规则另有格式从项目规则.
- 专用任务分支有可写 `origin` 且用户未要求仅本地集成时, 每个关注点提交后普通 push, 首次用 `git push -u origin <branch>`.
- 用户要求 push 时, 检查全部未提交和未 push 状态, 但只提交当前任务明确授权的改动, 保留并报告其他用户改动.
- 无 `origin` 或用户明确要求仅本地集成时, 保留本地提交, 跳过 push 并报告. 有 `origin` 但无法访问, 不可写或用户禁 push, 且用户未要求仅本地集成时, 保留任务分支和 worktree, 报告后停止集成.
- push 因 non-fast-forward 被拒时, 先 fetch 查远端改动, rebase 后重新验证. 集成流程内的重试按「集成与清理」的一次性门沿用已通过审核结论, 只重做 rebase 和验证, 不重审; 其余情形按「审核」确定的分册处理. 专用任务分支随后可用 `--force-with-lease`; Markdown 直改分支仍普通 push; `main` 和 `develop` 永不 force-push. 其他拒绝按项目 PR 流程处理, 无适用流程则停止并报告.
- 多人共享分支用 `--force-with-lease` 前先通知协作者. 不改写已合并, 已发布或正式 tag 锚定的历史.

## 审核

- 完整规则见 `REVIEW-RULES.md`; 触发审核前先读取该文件并遵循. reviewer 有 Codex, Claude 与 Grok 三个, 每个角色按该文件「Reviewer 选择」独立确定; 未指定时由命令根下的 `onevoke review` 读取当前作用域配置并分发.

## 集成与清理

- 不做直接集成的情形: 项目要求 PR 或发布门禁; 用户要求暂停或不合回; 非看板任务的 Bug 修复未获用户验证确认. 看板任务的合回时机取 `ONEVOKE-AGENTS.md`「看板任务完成」, 默认审核通过即集成, 不等用户验收, Bug 卡同样适用.
- 审核是集成前一次性门: 进集成流程前, 基于当时审核 base 完成验证, 并完成审核且通过, 或因未命中审核白名单而跳过审核且已告知用户. 集成流程 (rebase 到最新 `develop`, push, ff 同步) 一旦开始, `develop` 前进只重做 rebase 和验证, 沿用已通过审核结论, 不再审核. 审核进行期间同样不追 `develop`: base 冻结, 见 `REVIEW-RULES.md`「前置条件与执行」. 例外: rebase 引入实质代码冲突并由本人手动解决, 或用户明确要求时重审. 实质代码冲突指冲突落在代码文件且需手动解决; rebase 无冲突, 或冲突只出现在 `*.md` 与 `*.markdown` 时不重审.
- 集成路径与审核 base: 远端路径在任务 worktree fetch 后 rebase 到最新 `origin/develop`, 该远端 commit 即审核 base; 无 `origin` 或用户明确要求仅本地集成时 rebase 到本地 `develop`, 其当前 commit 即审核 base. 验证和审核通过后进集成流程. 已 push 的任务分支审核通过后仅用 `git push --force-with-lease` 更新; lease 失败则停止, 不覆盖远端改动.
- 集成流程内再查 `develop`; 若已前进, 按一次性门规则重复 rebase 和验证, 未前进才允许集成.
- 远端直接集成用非 force 的 `git push origin <最终任务 commit>:refs/heads/develop`, push 成功后 fetch, 再在主树 `develop` 跑 `git merge --ff-only origin/develop`. 本地直接集成在主树 `develop` 跑 `git merge --ff-only <任务分支>`, 并报告未 push. 集成 push 被 non-fast-forward 拒绝或本地 ff 失败时, 保持主树不变, 回同步和验证流程; 其他拒绝按项目 PR 流程处理, 无适用流程则停止并报告. 任何路径都不得产生 merge commit.
- 主树 `git merge --ff-only` 前按「本地改动保护」暂存并在操作后恢复主树中的未提交改动. 主树 ff 因本地领先或分叉失败时只报告未同步的具体原因和恢复办法, 不阻塞清理. 禁 reset, 丢弃或提交主树里的用户改动.
- 用 PR 时先 push 当前任务分支. PR 必须说明改了什么, 为何改, 如何验证; 可见 UI 变更附截图; 测试或快照变更列实际命令. 等 CI 全通过后, 按仓库策略 squash 或 rebase 合并, 仓库未指定默认 squash. 仓库未配 CI 先问用户, 不自动合并.
- 清理的唯一前置是任务改动已进入 `develop`: 直接集成用 `git merge-base --is-ancestor <最终任务 commit> <origin/develop 或本地 develop>` 判定, 远端路径先 fetch; PR 路径因 squash 或 rebase 合并会重写 commit, 改以 PR 已标记为 merged 且目标分支是 `develop` 为准. 判不出来或判定为否时不清理, 保留 worktree 和分支并报告.
- 满足清理前置后, POSIX 先跑 `<命令根>/merge-worktree-memory.py --source <worktree-path>`, 原生 Windows 人工输入普通参数时在 PowerShell 跑 `& "$env:USERPROFILE\.local\bin\merge-worktree-memory.cmd" --source "<worktree-path>"`. 项目安装把其中的全局命令根换成当前作用域命令根, 例如 `& "<命令根>\merge-worktree-memory.cmd"`. Windows 自动化不得固定假设 `py -3` 可用: 必须排除当前工作目录中的同名程序, 按 `系统 py.exe -3`、`PATH 中其他 py.exe -3`、`PATH 中 python.exe` 的顺序实际验证并选择原生 Python 3 绝对路径, 再用进程 API argv 数组传入 `-X`, `utf8`, 命令根下的 `merge-worktree-memory.py`, `--source` 和 worktree 路径; 选中 `py.exe` 时在 `-X` 前另传 `-3`. 含引号或边界反斜杠等特殊参数也必须走该 argv 数组, 禁拼接 shell 命令字符串或经 `.cmd` 重解析. 两条路径进入同一 Python 实现; 合并用平台独占锁保护, POSIX 为 `flock`, Windows 为 `LockFileEx`, 不得无锁执行. Windows 必须逐级拒绝来源和目标边界中的 reparse point, 用固定句柄读取/追加, 并把目标记忆目录、锁和文件迁移为当前用户独占的受保护 DACL. 源 worktree 没有 `.memsearch/memory` (未装 memsearch, 或尚未产生记忆) 时该命令是空操作并以 0 退出, 照常执行, 不跳过也不据此报错; 来源在合并期间仍被写入且无法证明稳定时, 脚本必须失败. 脚本失败则保留 worktree 和分支. 项目安装必须使用该命令根绝对入口, 禁止改用 PATH 中的全局同名命令.
- 脚本成功后, 删对应 worktree, 本地任务分支及仅为该 worktree 建的临时或预览 tag; 非本地集成还须删远端任务分支. 禁删正式发布 tag.
