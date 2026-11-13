# DevPilot V9.8.1～V9.8.5 全部新增／修改文件函数索引

本表覆盖此次补丁所有新增 Python 文件的**全部函数及方法**，也为每个修改过的 Python 文件列出完整的函数目录。每个函数均用一句中文说明用途。
**`本次新增/修改`** 表示本补丁的重点接口；`原有兼容` 表示本次随文件一起保留的既有方法，不代表它被重新实现。测试函数同样包含在表中。

## 一、V9.8 新增文件

### `devpilot/runtime/workflow_recipes.py`

| 函数 / 方法 | 类型 | 一句话功能 |
|---|---|---|
| `analysis_workflow` | 本次新增/修改 | 为纯分析任务创建只读报告步骤，不创建 Worker 或写入仓库。 |
| `with_remote_delivery` | 本次新增/修改 | 在已验收的本地交付报告之后追加发布审批与远程 PR 创建步骤。 |
| `repair_workflow` | 本次新增/修改 | 将已审核的动态开发计划裁剪成独立的返工 Workflow，由 V8 在结束后重新验收。 |
| `repair_delivery_workflow` | 本次新增/修改 | 在返工重新验证通过后创建单独的人工交付审批，按需附加 GitHub PR。 |
| `RepairWorkflowAdapter.__init__` | 本次新增/修改 | 绑定同一套 V7 持久化状态和执行线程池。 |
| `RepairWorkflowAdapter.start` | 本次新增/修改 | 仅创建一条真实 V7 Workflow，不在审批完成前运行写入步骤。 |
| `RepairWorkflowAdapter.run_until_blocked` | 本次新增/修改 | 由原 V7 并行 Runner 将返工步骤推进到阻塞、完成或失败。 |

### `devpilot/runtime/component_audit.py`

| 函数 / 方法 | 类型 | 一句话功能 |
|---|---|---|
| `ComponentAudit.__init__` | 本次新增/修改 | 绑定仓库外审计目录和同进程互斥锁。 |
| `ComponentAudit.record` | 本次新增/修改 | 持久化实际进入运行时的组件事件，不记录提示词、源码或 API 密钥。 |
| `ComponentAudit.summary` | 本次新增/修改 | 汇总真正出现的组件和事件，并将没有出现的能力明确标成未观测。 |

### `examples/v9_8_acceptance.py`

| 函数 / 方法 | 类型 | 一句话功能 |
|---|---|---|
| `offline_suite` | 本次新增/修改 | 独立子进程逐组运行旧版与新增真实组件测试，避免后台线程影响其他测试组。 |
| `verify_live` | 本次新增/修改 | 通过真实 MCP 读取回执、DeepSeek 轨迹、V7/V8 状态和 GitHub PR 元数据核对在线交付。 |
| `main` | 本次新增/修改 | 提供 offline、live 和 verify 三个明确的真实验收入口，不自动跳过审批。 |

### `tests/test_v9_8_unified_integration.py`

| 函数 / 方法 | 类型 | 一句话功能 |
|---|---|---|
| `ScriptModel.__init__` | 本次新增/修改 | 保存模型身份和是否修复 Bug 的共享开关。 |
| `ScriptModel.complete` | 本次新增/修改 | 让实际 Agent 工具修改仓库文件，并按测试情形返回正确或错误代码。 |
| `_git` | 本次新增/修改 | 在测试中以独立 Git 参数列表创建真实提交与裸远端。 |
| `fixture` | 本次新增/修改 | 创建初始 pytest 会失败的 Python Git 项目，可选绑定本机裸 Git 远端。 |
| `make_factory` | 本次新增/修改 | 建立可在返工前切换输出的测试模型工厂。 |
| `_approve` | 本次新增/修改 | 在离线安全测试中模拟人类按当前 V7 request_id 批准。 |
| `test_workflow_recipes_are_valid_and_gate_pr` | 本次新增/修改 | 动态 Workflow 只能在已有 V8 交付报告后进行 PR 审批和发布。 |
| `test_readonly_analysis_runs_without_goal_or_worker` | 本次新增/修改 | 只读分析不创建 Worker 和 Goal，也不会更改 Git 提交。 |
| `test_pr_mode_requires_issue_origin_and_trust` | 本次新增/修改 | 没有明确 Issue URL、没有正确 Git origin 或分析任务请求发布均会被拒绝。 |
| `test_integrated_pr_needs_third_approval_and_never_pushes_main` | 本次新增/修改 | 真实 Worker/Goal 通过后只有第三次审批才推送修复分支，裸远端 main 不变。 |
| `test_v8_repair_loop_generates_new_approved_workflow` | 本次新增/修改 | 首次失败后 V8 生成 Feedback/RepairTicket，第二次真实 Worker 修复且重新验收并交付。 |
| `test_component_audit_does_not_invent_unobserved_capabilities` | 本次新增/修改 | 审计只能记录已触发组件，不会虚报 Memory、Skills、Cron 或 Context 压缩。 |
| `test_restricted_write_scope_rejects_model_extra_files` | 本次新增/修改 | 即使模型计划格式正确，超出用户允许的文件范围也不能进入 Workflow。 |
| `test_v8_failing_test_prepares_ticket_before_manual_dispatch` | 本次新增/修改 | Goal 测试失败会自动保存 V8 返工意图，但在人工审批前不再次修改代码。 |
| `test_v9_8_live_verifier_requires_real_model_trace_and_pr` | 本次新增/修改 | 即使本地报告伪称完成，没有实际模型轨迹和 GitHub PR 也不能冒充在线验收。 |
| `test_publish_network_uncertainty_never_silently_retries` | 本次新增/修改 | GitHub PR 创建响应丢失后 V7 应进入 recovery_required，不得重复远程写入。 |

### `tests/test_v9_8_components_on_demand.py`

| 函数 / 方法 | 类型 | 一句话功能 |
|---|---|---|
| `NoNetworkModel.complete` | 本次新增/修改 | 在需要历史摘要时返回固定摘要，证明压缩路径已触发。 |
| `test_v2_large_tool_output_offloaded_without_losing_original` | 本次新增/修改 | V2 真正把过大的工具结果写到磁盘，并向模型提供截断预览。 |
| `test_v2_todo_tool_and_skill_on_demand` | 本次新增/修改 | V2 Todo 与 load_skill 通过真实工具 handler 操作各自数据。 |
| `test_v3_memory_persisted_and_recalled` | 本次新增/修改 | V3 记忆能够跨 Manager 实例持久保存，并由 memory_search 工具按关键词检索。 |
| `test_v4_mailbox_between_worker_identities` | 本次新增/修改 | V4 Mailbox 支持真实的 Worker 发消息、另一个 Worker 读取并持久化已读状态。 |


## 二、V9.8 修改文件（含保留的原有方法）

### `devpilot/runtime/general_coder.py`

| 函数 / 方法 | 类型 | 一句话功能 |
|---|---|---|
| `git` | 本次新增/修改 | 通过无 Shell 参数列表读取可信 Git 状态并拒绝执行失败。 |
| `test_argv` | 原有兼容 | 仅由宿主选择固定的 pytest 或 unittest 命令，不接受模型生成命令。 |
| `GeneralCodingAgent.__init__` | 本次新增/修改 | 绑定当前 Git 仓库与独立状态目录，并复用 V7、V8 的原有存储和执行器。 |
| `GeneralCodingAgent._session_path` | 原有兼容 | 只允许使用真实 V7 Workflow UUID 对应的会话 JSON 路径。 |
| `GeneralCodingAgent._save_session` | 原有兼容 | 用同目录临时文件原子保存计划与对应 Goal、代码版本和验收命令。 |
| `GeneralCodingAgent.session` | 本次新增/修改 | 读取持久化会话并确认它确实属于当前仓库及本阶段可信测试策略。 |
| `GeneralCodingAgent._analyze` | 本次新增/修改 | 经真实 V0～V3 Agent Loop 分析仓库，并可选通过经授权的 V6 MCP 只读工具取资料。 |
| `GeneralCodingAgent.create` | 本次新增/修改 | 按宿主选定的只读分析、本地修复或 GitHub PR 交付模式编译动态 V7 工作流。 |
| `GeneralCodingAgent._analysis_report` | 本次新增/修改 | 只把已采集的代码分析保存到仓库外，不创建 Worker 或修改源码。 |
| `GeneralCodingAgent._publish_pr` | 本次新增/修改 | 以当前 V7 发布审批为唯一授权，复用 PR Publisher 并用 V9.5 账本防止重复推送。 |
| `GeneralCodingAgent._build_repair_workflow` | 本次新增/修改 | 用 V8 真实失败反馈生成新的受限 Worker 计划，并强制人工批准后再执行。 |
| `GeneralCodingAgent.prepare_repair` | 本次新增/修改 | 经 V8 FeedbackGenerator 与预算核查生成持久化返工单，不自动修改源码。 |
| `GeneralCodingAgent.dispatch_repair` | 本次新增/修改 | 将 V8 持久修复单交给 V7 启动全新审批工作流，而非重放失败的旧步骤。 |
| `GeneralCodingAgent._finish_repair` | 本次新增/修改 | 返工 Workflow 完成后让 V8 再次采证验收，并在成功时创建独立交付审批。 |
| `GeneralCodingAgent._ensure_team` | 原有兼容 | 在计划审批之后按各 Worker 的文件范围创建真实 V4 团队和 V5 后台运行时。 |
| `GeneralCodingAgent._worker` | 本次新增/修改 | 通过真实 V4 TeamManager 和 V5 JobStore 执行单个 Worker，并记录防重复提交账本。 |
| `GeneralCodingAgent._merge` | 本次新增/修改 | 核实真实 V5 完成通知已交给 Lead 后，再合并已验证的 Worker Git 分支。 |
| `GeneralCodingAgent._verify` | 本次新增/修改 | 用 V8 真实测试与 Git 证据验收全部必要条件，失败时拒绝进入交付审批。 |
| `GeneralCodingAgent._report` | 本次新增/修改 | 在最终审批后再次检查 V8 证据新鲜度并保存关联真实 Git 差异的交付报告。 |
| `GeneralCodingAgent.status` | 本次新增/修改 | 从 V7 和 V8 持久化状态生成 CLI 可读取的进度及待审批编号。 |
| `GeneralCodingAgent.approve` | 原有兼容 | 只使用 V7 的带请求编号审批接口推进流程，不在 CLI 中擅自跳过审批。 |
| `GeneralCodingAgent.advance` | 本次新增/修改 | 使用 V7.5 并行调度器推进已授权步骤，遇到下一次审批或故障就停止。 |
| `GeneralCodingAgent.close` | 原有兼容 | 等待 V7 执行线程与 V5 Worker 完成后关闭本次会话持有的所有资源。 |
| `GeneralCodingAgent.__enter__` | 原有兼容 | 让用户可使用 with 管理真实模型运行时的资源生命周期。 |
| `GeneralCodingAgent.__exit__` | 原有兼容 | 退出上下文时持久化运行结果并关闭线程池。 |

### `devpilot/runtime/general_plan.py`

| 函数 / 方法 | 类型 | 一句话功能 |
|---|---|---|
| `checked_python_path` | 原有兼容 | 只接受仓库内真实且不经过符号链接的 Python 目标文件路径。 |
| `parse_coding_plan` | 本次新增/修改 | 将模型 JSON 计划校验为有限的 Worker、任务与互不重叠的 Python 文件集合。 |
| `build_general_workflow` | 原有兼容 | 用 V9.5 PlanGuard 验证动态 Worker 步骤，并添加合并、验收和交付审批关卡。 |

### `devpilot/runtime/pr_publisher.py`

| 函数 / 方法 | 类型 | 一句话功能 |
|---|---|---|
| `_process` | 原有兼容 | 用独立参数执行可信 Git/GitHub CLI 命令，并将输出固定解码为 UTF-8。 |
| `_git` | 原有兼容 | 在指定仓库执行 Git 并返回标准输出，避免调用 shell。 |
| `_gh` | 原有兼容 | 通过当前 GitHub CLI 登录身份执行 GitHub 操作。 |
| `_issue_identity` | 原有兼容 | 严格解析 GitHub Issue URL，拒绝非 GitHub 域名和伪造路径。 |
| `_assert_origin` | 原有兼容 | 确保将被推送的 origin 属于 Issue 对应的 GitHub 仓库。 |
| `_read_json` | 原有兼容 | 读取持久化 JSON 证据并确认顶层是对象。 |
| `_remote_sha` | 原有兼容 | 查询 GitHub 远程分支 SHA，避免覆盖其他人的提交。 |
| `_git_changes` | 原有兼容 | 重新计算 Git 提交范围，检查实际改动文件与交付报告一致。 |
| `build_pr_plan` | 本次新增/修改 | 读取 V7/V8 的最终交付证据并确认 Git HEAD、远程基准及修改范围。 |
| `rerun_tests` | 原有兼容 | 发布前使用当前 Python 环境重新运行宿主限定的真实测试命令。 |
| `_list_prs` | 原有兼容 | 从 GitHub 查询同一修复提交的 PR，防止因重试创建重复 PR。 |
| `_check_pr_match` | 原有兼容 | 只接受与已验收提交、目标 main 和指定仓库完全一致的 PR。 |
| `_save_receipt` | 原有兼容 | 在仓库外以原子替换保存发布记录，后续重复调用可以核对历史。 |
| `publish_pr` | 原有兼容 | 取得真人确认后只推送新的修复分支、创建 PR 并持久化回执，永不合并 main。 |

### `devpilot/cli.py`

| 函数 / 方法 | 类型 | 一句话功能 |
|---|---|---|
| `build_parser` | 本次新增/修改 | 统一定义 run、status、approve 和 resume 命令及受信任的启动配置。 |
| `default_state` | 原有兼容 | 默认将持久化状态放在 Git 仓库外部，避免污染代码验收。 |
| `main` | 本次新增/修改 | 调用真实模型和已有 Runtime，并在每个审批或失败边界展示实际持久化状态。 |

### `examples/v9_7_github_issue_coding.py`

| 函数 / 方法 | 类型 | 一句话功能 |
|---|---|---|
| `parse_issue_url` | 原有兼容 | 只接受 github.com 的标准 Issue URL，并提取仓库身份与 Issue 编号。 |
| `ensure_matching_git_repo` | 原有兼容 | 确保待修改目录是与 Issue 一致的干净 Git 克隆仓库。 |
| `exact_issue_read_allowed` | 原有兼容 | 明确只批准读取此仓库的此条 Issue，拒绝其他 MCP 工具和参数。 |
| `read_issue_via_mcp` | 原有兼容 | 通过真实 V6 MCP Server、V9.5 动态授权及 V1 Dispatcher 读取 Issue 正文。 |
| `build_issue_task` | 本次新增/修改 | 将只读 MCP 返回内容标明为不可信任务资料，并附上宿主约束。 |
| `run_bridge` | 本次新增/修改 | 先核对 GitHub Issue 和本地仓库，再选择仅检查或启动真实 DeepSeek 开发会话。 |

### `devpilot/workspace/worktree.py`

| 函数 / 方法 | 类型 | 一句话功能 |
|---|---|---|
| `WorktreeManager.__init__` | 原有兼容 | 绑定 Git 主仓库和 Worktree 执行目录，为 Worker 隔离代码工作区。 |
| `WorktreeManager.create` | 原有兼容 | 创建独立 Git Worktree，让不同 Worker 在各自分支内修改代码。 |
| `WorktreeManager.commit_all` | 原有兼容 | 检查 Worker 工作目录并把已授权修改提交到独立 Git 分支。 |
| `WorktreeManager.merge_branch` | 原有兼容 | 将 Worker 分支提交受控合并到指定 Git 工作目录。 |
| `WorktreeManager._ensure_git_repo` | 本次新增/修改 | 用 UTF-8 容错解码确认当前 Worktree 根目录是 Git 仓库。 |
| `WorktreeManager._safe_name` | 原有兼容 | 将 Worker 名称限制为安全的 Git 分支及目录名称。 |
| `WorktreeManager._run` | 本次新增/修改 | 执行受控 Git 参数列表并用 UTF-8 解码结果，异常时报告错误。 |

### `devpilot/runtime/diagnostics.py`

| 函数 / 方法 | 类型 | 一句话功能 |
|---|---|---|
| `_module_available` | 原有兼容 | 只验证 Python SDK 的实际导入能力，不发送任何网络请求。 |
| `_has_deepseek_key` | 原有兼容 | 只检查进程环境或当前工作目录 .env 中是否配置密钥，绝不输出其内容。 |
| `environment_report` | 本次新增/修改 | 只读取仓库、运行时和配置状态，区分离线可运行与真实模型在线就绪。 |

### `devpilot/goal/collector.py`

| 函数 / 方法 | 类型 | 一句话功能 |
|---|---|---|
| `EvidenceCollector.__init__` | 原有兼容 | 绑定 GoalStore、证据目录和可信工作区，并设置命令白名单及采集上限。 |
| `EvidenceCollector._now` | 原有兼容 | 生成带 UTC 时区的采集时间，供证据追溯。 |
| `EvidenceCollector._git_state` | 本次新增/修改 | 读取当前 Git HEAD 和工作区脏状态，若不是 Git 仓库则返回未知。 |
| `EvidenceCollector._safe_file` | 原有兼容 | 将 Goal 中的文件路径限制在工作区内，拒绝通过符号链接跳出目录。 |
| `EvidenceCollector._file_facts` | 原有兼容 | 只读取指定文件的存在性、大小、哈希及目标文本命中情况。 |
| `EvidenceCollector._read_tail_capped` | 原有兼容 | 只保留日志末尾的有限字节，同时计算完整日志的 SHA256 摘要。 |
| `EvidenceCollector._command_facts` | 原有兼容 | 仅执行可信宿主预先允许的完整 argv，记录退出码、耗时和有界日志。 |
| `EvidenceCollector.collect` | 原有兼容 | 读取指定验收条件的真实事实并追加证据，不修改 Goal 的验收状态。 |
| `EvidenceCollector.collect_all` | 原有兼容 | 按 GoalSpec 顺序采集全部自动验收条件，明确跳过需要人工证明的条件。 |

### `devpilot/goal/verifier.py`

| 函数 / 方法 | 类型 | 一句话功能 |
|---|---|---|
| `GoalVerifier.__init__` | 原有兼容 | 绑定目标、证据和可信代码目录，为验收创建独立的调用锁。 |
| `GoalVerifier._now` | 原有兼容 | 读取带时区的 UTC 时间，作为验收记录的可信时间来源。 |
| `GoalVerifier._git_state` | 本次新增/修改 | 读取 Git HEAD 和未提交修改状态，无法确认时统一视为未知。 |
| `GoalVerifier._spec_hash` | 原有兼容 | 按照 V8.2 相同的序列化规则计算 GoalSpec 指纹。 |
| `GoalVerifier._since` | 原有兼容 | 找出最近一轮验证开始的时间，避免直接沿用上轮的旧证据。 |
| `GoalVerifier._provenance_problem` | 原有兼容 | 确认一份证据对应当前 Goal、条件、工作区、验收轮次和干净的 Git 版本。 |
| `GoalVerifier._compare` | 原有兼容 | 按照明确的验收规则比较采集事实，而不让模型自行判定成功。 |
| `GoalVerifier._verify_one` | 原有兼容 | 只用当前验收项最新一份证据判断通过、失败或未知。 |
| `GoalVerifier.verify` | 原有兼容 | 读取最新证据、对照 Git 和验收标准，保存逐项结论并只在全部必要条件通过时认证 Goal。 |

### `devpilot/goal/delivery.py`

| 函数 / 方法 | 类型 | 一句话功能 |
|---|---|---|
| `GoalDeliveryBuilder.__init__` | 原有兼容 | 保存统一 Goal 入口，并设置是否强制要求 V7 有真实审批记录。 |
| `GoalDeliveryBuilder._fresh_verification` | 原有兼容 | 重新核对所有必要证据的来源、Git 版本和最新结果，防止旧结论误交付。 |
| `GoalDeliveryBuilder._approval_actor` | 原有兼容 | 只认可最后一轮已完成修复所关联的 V7 审批，不复用上轮批准。 |
| `GoalDeliveryBuilder._changed_files` | 本次新增/修改 | 从真实 Git 中核对修复步骤上报的 Commit 并收集实际修改的文件名。 |
| `GoalDeliveryBuilder.build` | 原有兼容 | 重新核对代码和审批，再保存一份能够解释不予交付原因的验收快照。 |
| `GoalDeliveryBuilder.render_markdown` | 原有兼容 | 将已检查的事实整理成适合用户阅读的 Markdown，而不注入执行指令。 |
| `GoalReportStore.__init__` | 原有兼容 | 创建可信报告存储目录，并保证一个实例内的写入互斥。 |
| `GoalReportStore._path` | 原有兼容 | 检查文件编号，只允许在指定 Goal 的报告目录下生成文件。 |
| `GoalReportStore._atomic_new` | 原有兼容 | 先落盘同目录临时文件再发布新报告，不允许覆盖已有同名记录。 |
| `GoalReportStore.append` | 原有兼容 | 追加不可覆盖的 JSON 快照，并导出同编号 Markdown 供用户阅读。 |
| `GoalReportStore.get` | 原有兼容 | 从持久化 JSON 恢复历史交付报告并核对记录所属的目标。 |
| `GoalReportStore.list_for_goal` | 原有兼容 | 按生成时间读取某个目标的历史报告，让 UI 展示审计记录。 |

