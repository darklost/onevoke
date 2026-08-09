# Git 工作流规则

本文件是 `~/.agents/BASE-RULES.md`「Git 工作流」的完整契约, 装在 `~/.agents/GIT-RULES.md`. 优先级: 当前任务明确用户指令 > 项目级 `AGENTS.md` 或 `CLAUDE.md` > `~/.agents/ONEVOKE-AGENTS.md`「默认取值」 > 本文件.

本文件只适用 Git 仓库. 非 Git 目录无分支, worktree, 审核, 集成, 直接改文件.

## 分支与 worktree

- 默认集成分支由项目规则指定; 项目未指定时取 `~/.agents/ONEVOKE-AGENTS.md`「分支」的取值, 该取值指的分支在仓库不存在时用 `refs/remotes/origin/HEAD` 指向的分支. 都无且本地仓库无法唯一确定时先问用户.
- 除下述 Markdown 直改路径外, 所有改文件任务都用独立任务分支和 `<仓库根目录>/worktrees/<task-name>/` 专用 worktree. `<task-name>` 同分支名, 短 kebab-case; 任务分支不得是默认集成分支或 detached `HEAD`. 已在当前任务专用 worktree 和任务分支时直接复用.
- 有 `origin` 且用户未要求仅本地集成时, 先 fetch, 再基于最新 `origin/<默认集成分支>` 建任务分支; fetch 失败则停止创建并报告. 无 `origin` 或用户明确要求仅本地集成时, 基于本地默认集成分支建任务分支, 报告未同步远端.
- Markdown 直改路径须同时满足: 任务只改 Markdown 文件; 当前分支是默认集成分支或用户明确指定目标分支; 任务开始时工作树无未提交或未跟踪文件. 任一不满足用专用 worktree.
- Markdown 直改分支有 upstream 且用户未要求仅本地集成时, 改前必须 fetch 并 fast-forward, 确认 `HEAD` 等于 upstream. 本地领先, 分叉或无法同步时改用专用 worktree.

## 提交与 push

- 每个已完成并通过对应验证的独立关注点单独提交, 不混无关改动. 提交 subject 默认中文动宾短语, 如 "修复登录竞态"; 项目规则另有格式从项目规则.
- 专用任务分支有可写 `origin` 且用户未要求仅本地集成时, 每个关注点提交后普通 push, 首次用 `git push -u origin <branch>`.
- Markdown 直改路径先完成验证和必要审核再普通 push; 此路径不走「集成与清理」流程.
- 用户要求 push 时, 检查全部未提交和未 push 状态, 但只提交当前任务明确授权的改动, 保留并报告其他用户改动.
- 无 `origin` 或用户明确要求仅本地集成时, 保留本地提交, 跳过 push 并报告. 有 `origin` 但无法访问, 不可写或用户禁 push, 且用户未要求仅本地集成时, 保留任务分支和 worktree, 报告后停止集成.
- push 因 non-fast-forward 被拒时, 先 fetch 查远端改动, rebase 后重新验证, 审核按本文件「审核」确定的分册处理. 专用任务分支随后可用 `--force-with-lease`; Markdown 直改分支仍普通 push; 默认集成分支永不 force-push. 其他拒绝按项目 PR 流程处理, 无适用流程则停止并报告.
- 多人共享分支用 `--force-with-lease` 前先通知协作者. 不改写已合并, 已发布或正式 tag 锚定的历史.

## 审核

- 完整规则见 `~/.agents/REVIEW-RULES.md`; 触发审核前先读取该文件并遵循. reviewer 有 Codex 与 Grok 两个, 按该文件「Reviewer 选择」确定用哪个, 未指定时用 Codex; 同一任务不得混用两个 reviewer 的阶段结论.

## 集成与清理

- 项目要求 PR 或发布门禁, 用户要求暂停或不合回, 或 Bug 修复未获用户验证确认时, 不做直接集成.
- 看板任务的合回时机另受 `~/.agents/ONEVOKE-AGENTS.md`「看板任务完成」约束; 该取值要求先等用户确认时, 未确认不进集成流程.
- 审核是集成前一次性门: 进集成流程前, 基于当时审核 base 完成验证, 并完成审核且通过, 或按适用审核分册的豁免条件跳过审核且已告知用户. 集成流程 (rebase 到最新集成分支, push, ff 同步) 一旦开始, 集成分支前进只重做 rebase 和验证, 沿用已通过审核结论, 不再审核. 例外: rebase 引入实质代码冲突并由本人手动解决, 或用户明确要求时重审.
- 远端集成路径: 在任务 worktree fetch, rebase 到最新 `origin/<默认集成分支>`, 该远端 commit 记为审核 base, 再验证和审核, 通过后进集成流程. 已 push 的任务分支审核通过后仅用 `git push --force-with-lease` 更新; lease 失败则停止, 不覆盖远端改动.
- 本地集成路径: 无 `origin` 或用户明确要求仅本地集成时, rebase 到本地默认集成分支, 其当前 commit 记为审核 base, 再验证和审核, 通过后进集成流程.
- 集成流程内再查默认集成分支; 若已前进, 按一次性门规则重复 rebase 和验证, 未前进才允许集成.
- 远端直接集成用非 force 的 `git push origin <最终任务 commit>:refs/heads/<默认集成分支>`. non-fast-forward 时保持主树不变, 回同步和验证流程; 其他拒绝按项目 PR 流程处理, 无适用流程则停止并报告. push 成功后 fetch, 再在主树默认集成分支跑 `git merge --ff-only origin/<默认集成分支>`; 此处 ff 失败只报告, 按「清理」规则继续.
- 本地直接集成在主树默认集成分支跑 `git merge --ff-only <任务分支>`, 并报告未 push; fast-forward 失败回同步和验证流程. 任何路径都不得产生 merge commit.
- 用 PR 时先 push 当前任务分支. PR 必须说明改了什么, 为何改, 如何验证; 可见 UI 变更附截图; 测试或快照变更列实际命令. 等 CI 全通过后, 按仓库策略 squash 或 rebase 合并, 仓库未指定默认 squash. 仓库未配 CI 先问用户, 不自动合并.
- 清理的唯一前置是任务改动已进入集成分支. 远端直接集成先 fetch 再用 `git merge-base --is-ancestor <最终任务 commit> origin/<默认集成分支>` 判定; 本地直接集成同法核对本地默认集成分支. PR 路径不用这个判定, 因为 squash 或 rebase 合并会重写 commit, 以 PR 已标记为 merged 且目标分支就是默认集成分支为准. 判不出来或判定为否时不清理, 保留 worktree 和分支并报告.
- 主树 `git merge --ff-only` 失败不阻塞清理. 上条判定通过即照常清理, 同时向用户报告主树未同步的具体原因 (未提交改动, 本地领先, 已分叉) 和恢复办法; 禁为了同步主树擅自 stash, reset, 丢弃或提交主树里的用户改动.
- 满足清理前置后, 先跑 `~/.local/bin/merge-worktree-memory.py --source <worktree-path>`. 未装 `memsearch` 时该命令是空操作并正常返回, 照常执行, 不跳过也不据此报错.
- `merge-worktree-memory.py` 成功后, 删对应 worktree, 本地任务分支及仅为该 worktree 建的临时或预览 tag; 非本地集成还须删远端任务分支. 脚本失败则保留 worktree 和分支. 禁删正式发布 tag.
