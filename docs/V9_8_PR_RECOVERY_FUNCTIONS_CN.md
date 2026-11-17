# DevPilot V9.8 PR Recovery：逐文件函数对照表

所有文件均包含在本次补丁中。以下按文件列出主要函数及方法，每条以一句中文说明职责。

## `devpilot/runtime/pr_recovery.py`

| 函数/方法 | 一句话职责 |
|---|---|
| `RecoveryPreview.as_dict` | 输出不包含 Token 的预览，供操作者决定是否恢复远程发布。 |
| `_atomic_json` | 以 UTF-8 原子替换保存可信恢复状态，支持崩溃后再次读取。 |
| `_recovery_journal` | 在仓库外为每条 Workflow 指定单一恢复记录路径。 |
| `_write_journal` | 记录远程操作前后阶段，使未知响应不会被下一次运行自动重放。 |
| `_exclusive_recovery` | 对同一 Run 创建排他文件锁，禁止两个恢复进程同时执行 GitHub 写入。 |
| `_remote_ref` | 用已认证 gh API 查询精确 Git ref，避免将 Git 网络失败误当作分支不存在。 |
| `_query_pr` | 按 owner:branch 过滤 GitHub PR，并逐项校验仓库、源提交及 main 目标。 |
| `inspect_recovery` | 只读核对本地 V7/V8 与 GitHub API，在未知结果时绝不猜测远端状态。 |
| `_verify_before_write` | 在任何远程写操作之前重新运行可信测试并核对 Git HEAD、工作区及 main 基准。 |
| `_push_verified_branch` | 使用期望空引用的 Git lease 原子创建新分支，禁止竞争条件下覆盖已有分支。 |
| `_create_verified_pr` | 创建一次 GitHub PR，响应丢失时按远端查询结果核实而不立即重复提交。 |
| `_record_delivered` | 在 V7 状态变更前原子保存 PR 回执及报告，以便崩溃后安全补齐。 |
| `_reconcile_ledger` | 仅在 GitHub 已确认 PR 后将原操作账本中的未知结果核实为成功。 |
| `_reconcile_v7` | 使用 V7 原生 reconcile_step 写入真实远端证据，使失败的发布步骤合法完成。 |
| `recover_pr` | 持排他锁核对远端后完成缺失发布动作，再原生核实 V7 与账本，不重跑 Worker。 |

## `devpilot/runtime/pr_publisher.py`

| 函数/方法 | 一句话职责 |
|---|---|
| `_process` | 用独立参数执行可信 Git/GitHub CLI 命令，并将输出固定解码为 UTF-8。 |
| `_git` | 在指定仓库执行 Git 并返回标准输出，避免调用 shell。 |
| `_gh` | 通过当前 GitHub CLI 登录身份执行 GitHub 操作。 |
| `_issue_identity` | 严格解析 GitHub Issue URL，拒绝非 GitHub 域名和伪造路径。 |
| `_assert_origin` | 确保将被推送的 origin 属于 Issue 对应的 GitHub 仓库。 |
| `_read_json` | 读取持久化 JSON 证据并确认顶层是对象。 |
| `_remote_sha` | 查询 GitHub 远程分支 SHA，避免覆盖其他人的提交。 |
| `_git_changes` | 重新计算 Git 提交范围，检查实际改动文件与交付报告一致。 |
| `build_pr_plan` | 读取 V7/V8 的最终交付证据并确认 Git HEAD、远程基准及修改范围。 |
| `rerun_tests` | 发布前使用当前 Python 环境重新运行宿主限定的真实测试命令。 |
| `_list_prs` | 从 GitHub 查询同一修复提交的 PR，防止因重试创建重复 PR。 |
| `_check_pr_match` | 只接受与已验收提交、目标 main 和指定仓库完全一致的 PR。 |
| `_save_receipt` | 在仓库外以原子替换保存发布记录，后续重复调用可以核对历史。 |
| `publish_pr` | 取得真人确认后只推送新的修复分支、创建 PR 并持久化回执，永不合并 main。 |

## `devpilot/cli.py`

| 函数/方法 | 一句话职责 |
|---|---|
| `build_parser` | 统一定义 run、status、approve 和 resume 命令及受信任的启动配置。 |
| `default_state` | 默认将持久化状态放在 Git 仓库外部，避免污染代码验收。 |
| `main` | 调用真实模型和已有 Runtime，并在每个审批或失败边界展示实际持久化状态。 |

## `tests/test_v9_8_pr_recovery.py`

| 函数/方法 | 一句话职责 |
|---|---|
| `_git` | 以真实 Git CLI 执行测试仓库操作，不依赖网络。 |
| `FakeGitHub.__init__` | 保存本地裸远端的查询状态和调用次数。 |
| `FakeGitHub._sha` | 通过真实 Git 协议从本地裸远端获取精确分支提交。 |
| `FakeGitHub.add_pr` | 构造符合 GitHub REST 响应格式的同仓库 PR 元数据。 |
| `FakeGitHub.__call__` | 响应匹配 refs、按 head 过滤 pulls 和 create 请求，拒绝所有其他操作。 |
| `site` | 创建真实 Git 提交、独立裸远端与完整 V7/V8 交付证据。 |
| `inspect` | 用当前真实本地状态和测试 GitHub 服务执行只读恢复诊断。 |
| `recover` | 执行测试实例的恢复，仍采用真实 Git push 与真实 V7 reconcile_step。 |
| `test_existing_manual_pr_reconciles_original_failed_v7` | 用户手工创建的 PR 必须只核对、补证据并将原 Workflow 变成 completed。 |
| `test_missing_branch_creates_only_verified_branch_and_pr` | 远程没有分支和 PR 时，只执行一次真实 Git push，再创建 PR，不推送 main。 |
| `test_branch_exists_but_pr_missing_creates_pr_without_push` | 已推送的正确分支只创建 PR，禁止重复推送。 |
| `test_pr_response_lost_but_remote_exists_is_confirmed` | GitHub 已接收 PR 但回执丢失，重新查询确认证据后不重复创建。 |
| `test_unconfirmed_pr_create_blocks_automatic_replay` | PR 创建响应丢失且 API 未确认时，记录未知状态并阻断自动重复提交。 |
| `test_changed_main_blocks_new_remote_write` | 远程 main 前移且 PR 不存在时，阻止继续创建 PR。 |
| `test_existing_pr_can_be_reconciled_after_main_advanced` | PR 已经存在后即使 main 前移，仍可核对历史提交并修复旧 Workflow 状态。 |
| `test_remote_branch_conflict_does_not_force_push` | 同名远程分支指向其他 SHA 时，恢复必须停止且不得覆盖。 |
| `test_missing_publish_approval_is_rejected_before_network` | 即使远端 PR 存在，没有 V7 publish_review 审批也不能将原任务标成成功。 |
| `test_stale_local_head_fails_closed` | 本地 HEAD 或实际提交与 V8 报告不一致时，拒绝 PR 恢复。 |
| `test_dirty_worktree_fails_closed` | 工作区含未提交修改时，恢复拒绝沿用旧 Goal 证据。 |
| `test_goal_verification_failed_blocks` | V8 Goal 未 verified 或验收条件未 pass 时，不能通过恢复绕过 Goal。 |
| `test_operation_ledger_inflight_reconciled_only_after_remote_pr` | V9.5 已提交但结果未知的操作，只能用真实远端 PR 证据核实为成功。 |
| `test_api_network_failure_never_treated_as_missing` | GitHub API 网络失败不得误判为缺失远程 PR 或分支。 |
| `test_closed_unmerged_pr_cannot_be_marked_delivered` | 已关闭但未合并的 PR 不构成成功交付，防止错误状态回填。 |
| `test_already_merged_pr_reconciles_without_merging_again` | 旧 PR 已由维护者合并且远程分支删除，也能只读核实并正确记 merged。 |
| `test_mismatched_pr_head_blocks` | PR 分支名相同但 head SHA 与原 Goal 报告不同，必须终止。 |
| `test_stale_lock_blocks_second_recovery` | 排他锁存在时禁止另一个恢复进程同时操作远端。 |
| `test_issue_identity_mismatch_blocks` | 不能把其它 Issue 的 PR 挂到当前已审批的开发任务。 |
| `test_original_inflight_without_remote_confirmation_is_blocked` | 原 V9.5 OperationLedger 有 inflight 却没有 PR 时，不允许自动再次发布。 |
| `test_receipt_exists_but_remote_pr_missing_is_conflict` | 保存过发布成功回执而 GitHub 查不到 PR 时，不允许静默重建。 |
| `test_confirmed_branch_missing_is_not_silently_repushed` | 曾记录成功推送但远端分支已消失，需人工解释，不能自动推新分支。 |
| `test_cli_dry_run_is_readonly` | recover-pr --dry-run 仅检查 GitHub 实际状态，绝不推进 V7 或推送分支。 |
| `test_cli_can_reconcile_manually_created_pr_without_model` | 恢复已存在 PR 只需输入 RECOVER 口令，CLI 不初始化或调用 DeepSeek。 |
| `test_cli_wrong_confirmation_does_not_modify_remote` | 恢复口令错误时终止，不运行 Git Push 或 gh pr create。 |
| `test_old_not_executed_ledger_keeps_history_when_recovery_publishes` | 原发布已确认未执行时，Recovery 新建 PR 后不得篡改原操作的历史结果。 |
| `test_saved_report_with_missing_remote_pr_is_blocked` | 报告已记 PR 而远端 PR 丢失时，不得忽略历史证据重新发布。 |
| `test_recovery_required_step_uses_native_v7_reconciliation` | V7 原生 recovery_required 状态通过同一个 reconcile_step 核实远端完成。 |
| `test_http11_option_only_changes_this_git_push` | HTTP/1.1 仅用于恢复时的 Git push，不会更改系统或仓库全局 Git 配置。 |
| `test_open_pr_with_missing_remote_branch_requires_human_check` | OPEN PR 若其源分支消失则拒绝在原 Workflow 写入错误成功状态。 |
| `test_concurrent_remote_branch_creation_does_not_move_it` | 其他人刚创建同名远端分支时，期望空引用的 lease 禁止覆盖其 Git SHA。 |

