# DevPilot V9.8（1～5 一次性交付）：统一 Workflow、PR、V8 返工、组件审计与验收

## 0. 交付边界

这是基于已跑通的 V9.7 源码做的 **增量补丁**；没有重写 V0 Agent、V1 ToolDispatcher、V4 Worktree、V5 Job/Event、V6 MCP 或 V7 Engine。

- **V9.8.1 业务工作流动态组合**：`--mode analysis` 只读分析；`--mode edit --delivery local` 本地 Coding Agent；`--mode edit --delivery github-pr` 受控 GitHub PR 流程。模型决定 1～3 个 Worker 的目标文件与任务，宿主负责固定安全门禁。
- **V9.8.2 PR 内建 V7**：`preapproval → workers → merge → verify → delivery_review → report → publish_review → publish_pr`，发布只经 `gh` + Git 推送修复分支，不允许自动合并 `main`。
- **V9.8.3 失败返工**：V8 验收有明确 FAIL 时自动准备 Feedback/RepairTicket；用户通过 `repair dispatch` 创建新的 V7 受控返工 Workflow，重新执行 Worker 和 V8 证据验收，通过后生成新的交付审批及可选 PR 流程。轮次通过 V8 Budget 限制。
- **V9.8.4 按需组件**：保留原 Memory/Skills/Context/Mailbox 的真实实现，增加 `devpilot audit` 组件运行事件和独立能力触发测试。**未观察到的模块会标为未观察，不会虚报每条业务都用了全部组件。**
- **V9.8.5 验收**：新增测试；离线与真实模型/MCP/PR 的结论严格分开。新增 `examples/v9_8_acceptance.py`。

### 设计图

```text
CLI / GitHub Issue Bridge
  ├─ analysis ─> Lead read-only analysis ─> V7 analysis_report ─> report
  └─ edit ─> Lead + DeepSeek ─> model plan ─> PlanGuard ─> V7 preapproval
                 ├─ V4/V5 Workers in distinct Git Worktrees
                 └─ merge ─> V8 real pytest + GoalVerifier
                      ├─ PASS ─> V7 delivery_review ─> report
                      │             └─ (optional) publish_review ─> publish_pr ─> GitHub PR
                      └─ FAIL ─> V8 Feedback + RepairTicket + Budget
                                    └─ V7 new repair Workflow / human approval
                                          └─ merge ─> V8 re-verify ─> new delivery Workflow
```

## 1. 安装（Windows PowerShell）

先提交或备份你已经在用的 DevPilot V9.7 项目，再将本 ZIP **按同名相对路径合并**。不要把 ZIP 解压到新的嵌套 `devpilot/devpilot/` 目录。对于你以前手工修改过的 `devpilot/cli.py` 等文件，先用 IDE 对比差异再覆盖。

本补丁依赖已安装的 V9.7：**DeepSeek Trace Addon、Direct HTTP MCP Addon、GitHub Issue Bridge 和 Safe Auto GitHub PR Addon** 都应已并入；你之前跑通了这些项目。

```powershell
python -m pip install -e ".[mcp,test]"
python -m devpilot.cli --help
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
```

如果 setuptools 报 `Multiple top-level packages discovered: ['skills','devpilot']`，请在现有 `pyproject.toml` 添加（不要重复定义已有段落）：

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["."]
include = ["devpilot", "devpilot.*"]
exclude = ["skills", "skills.*", "tests", "tests.*", "examples", "examples.*"]
```

## 2. V9.8.1：按业务决定 Workflow

仅分析代码，不写仓库，也不创建 Worker / Goal：

```powershell
python -m devpilot.cli run --repo "$env:USERPROFILE\devpilot_mcp_e2e_work" --task "分析折扣计算模块，不要改文件" --mode analysis
```

真实本地开发（原 CLI 兼容，仍需两次审批）：

```powershell
python -m devpilot.cli run --repo "$env:USERPROFILE\devpilot_playground" --task "给 calculator.py 添加一个函数并写测试" --mode edit --delivery local --test pytest --trace-file "$env:USERPROFILE\devpilot_trace\v98_local.jsonl"
```

模型只能提出 Python 文件目标，`--allow-file` 由可信宿主限制更改范围；不传时仍要经过计划审核和人工审批。**`--delivery github-pr` 需要显式提供匹配 `origin` 的真实 Issue URL。**

## 3. V9.8.2：真实 GitHub Issue → PR（推荐创建新 Issue #2）

**你之前的 Issue #1 已经合并，不能重复拿旧 HEAD 做发布测试。** 在既有私有 GitHub 测试仓库 `wulalaxiaoyongshi/devpilot-mcp-e2e-lab` 新建 Issue：

```powershell
gh issue create --repo "wulalaxiaoyongshi/devpilot-mcp-e2e-lab" --title "feat: add tax calculator and regression tests" --body-file ".\docs\V9_8_ISSUE2_BODY.md"
```

记下真实的新 Issue URL。因为之前合并可能是 squash merge，**建议重新克隆一份干净的工作区**，而不是在旧修复分支直接做新任务：

```powershell
git clone https://github.com/wulalaxiaoyongshi/devpilot-mcp-e2e-lab.git "$env:USERPROFILE\devpilot_mcp_e2e_v98"
git -C "$env:USERPROFILE\devpilot_mcp_e2e_v98" status --short
gh auth status
```

确保当前 PowerShell 中仍有先前创建的**只读** GitHub MCP PAT，且该 token 允许访问测试仓库；PR 发布将由 `gh auth login` 已保存的另一个账号授权完成。

**从真实 HTTP MCP 读取新 Issue，再用 DeepSeek 开始修复和自动 PR Workflow：**

```powershell
python -m examples.v9_8_acceptance --mode live `
  --repo "$env:USERPROFILE\devpilot_mcp_e2e_v98" `
  --issue-url "这里替换为新 Issue 的真实 URL" `
  --config examples/github_mcp_http_readonly.json `
  --trace-file "$env:USERPROFILE\devpilot_trace\v98_issue_pr.jsonl" `
  --allow-file shop/pricing.py `
  --allow-file tests/test_pricing.py
```

它**不会自动批准**。运行后复制真实 `run_id` 和 `request_id`，按下面三个审批门禁依次执行：

```powershell
$repo = "$env:USERPROFILE\devpilot_mcp_e2e_v98"
$run = "填入本次 run_id"
$trace = "$env:USERPROFILE\devpilot_trace\v98_issue_pr.jsonl"

# 第一次：同意 Worker 修改文件
python -m devpilot.cli status --repo $repo --run-id $run
python -m devpilot.cli approve --repo $repo --run-id $run --step preapproval --request-id "实际审批 ID" --decision approve
python -m devpilot.cli resume --repo $repo --run-id $run --trace-file $trace --timeout 600

# 第二次：Goal verified 后审查本地交付
python -m devpilot.cli status --repo $repo --run-id $run
python -m devpilot.cli approve --repo $repo --run-id $run --step delivery_review --request-id "新审批 ID" --decision approve
python -m devpilot.cli resume --repo $repo --run-id $run --trace-file $trace --timeout 600

# 第三次：授权推送修复分支并创建 PR；不授权合并 main
python -m devpilot.cli status --repo $repo --run-id $run
python -m devpilot.cli approve --repo $repo --run-id $run --step publish_review --request-id "发布审批 ID" --decision approve
python -m devpilot.cli resume --repo $repo --run-id $run --trace-file $trace --timeout 600
```

每次批准时 CLI 会要求你输入 `APPROVE 实际REQUEST_ID`，这是你亲自确认该步，而不是模型自行越权。**审批人标识只是本地 CLI 文本，不是多用户身份认证。**

完成后预期 `workflow=completed`、`goal=verified`、步骤 `publish_pr=succeeded`，且 `reports/<run_id>.json` 中的 `github_pr.url` 对应真实 GitHub PR。GitHub 上 `main` 不会被 DevPilot 合并；维护者应亲自审核 PR 后决定是否合并。

验证真实在线证据（其中包括 GitHub 实际 PR 元数据检查）：

```powershell
python -m examples.v9_8_acceptance --mode verify `
  --repo "$env:USERPROFILE\devpilot_mcp_e2e_v98" `
  --run-id "这次的 run_id" `
  --issue-url "这次的新 Issue URL" `
  --trace-file "$env:USERPROFILE\devpilot_trace\v98_issue_pr.jsonl"
```

只有所有 checks 为 true 时 `online_passed` 才可能为 true；这要求真实的 DeepSeek 请求轨迹、MCP 读取回执、V7/V8 验收、PR URL 和 GitHub 在线 PR 元数据均一致。

## 4. V9.8.3：测试失败后的受控自动返工

`verify` 遇到 **FAIL** 时，原 Workflow 会停止在 `failed`，V8 自动准备 `RepairTicket` 和失败反馈，不会自动重复写代码。可以在任意后续新进程执行：

```powershell
$repo = "本次工作的 Git 仓库"
$original = "测试失败的原始 run_id"
python -m devpilot.cli repair --repo $repo --run-id $original --repair-action prepare
```

记录 `ticket_id`；此命令如果已自动生成单据，会**复用同一个待处理单据**，不消耗新一轮预算。

```powershell
python -m devpilot.cli repair --repo $repo --run-id $original --repair-action dispatch --ticket-id "实际 ticket_id" --trace-file "$env:USERPROFILE\devpilot_trace\v98_repair.jsonl"
```

会返回新的 `repair run_id` 和 `preapproval`，此时还没有执行 Worker。人工批准并 `resume` 后，新 Worker 将尝试修复。**新的修复 Workflow 完成后，V8 再次运行真实验收；只有通过才生成新的 `next_run_id` 交付审批工作流。** 如仍 FAIL，继续受 V8 `GoalBudget(max_rounds=2)` 约束；失败必须展示，不得冒充成功。

## 5. V9.8.4：组件使用审计与按需触发

```powershell
python -m devpilot.cli audit --repo $repo --run-id "实际 run_id"
python -m pytest tests/test_v9_8_components_on_demand.py -v
```

`component_audit` 是可信宿主在 V7、V8、V4/V5 等实际执行边界记录的组件事件；没有观察到的部分会列在 `not_observed_components`。独立专项测试使用真实 V2 ContextCompactor（大输出卸载）、SkillCatalog + TodoTool、V3 MemoryStore + Recall 和 V4 MailboxStore。**这些模块能单独正常工作，不等于每个普通 Issue 任务都触发了它们。**

V6 MultiServer、Resources/Prompts、V5 Cron、跨进程分布式恢复等暂不冒充已经完成真实在线联合验收，审计中明确标出未验证项。

## 6. V9.8.5：离线/在线验收

```powershell
python -m examples.v9_8_acceptance --mode offline
python -m pytest tests/test_v9_8_unified_integration.py tests/test_v9_8_components_on_demand.py -v --basetemp="$env:USERPROFILE\devpilot_pytest_tmp" -p no:cacheprovider
```

本地离线验证可能使用 ScriptModel，只验证真实组件/真实 Git，不代表 DeepSeek 在线通过。在线模式需要你在 Windows 本机使用真实 DeepSeek API、GitHub MCP PAT、`gh` 登录和新的 Issue，最终 `--mode verify` 取得证据。

## 7. 重要安全限制和未覆盖点

1. 当前 Coding Agent 主要支持 **Python 目标文件 + 可信宿主执行 pytest/unittest**，尚不是支持任意项目语言的通用 Codex。
2. GitHub PR 写入通过**本机 Git 与 `gh` CLI**，不是 GitHub MCP 写工具；已有只读 MCP Token 不需要提高权限。
3. PR 发布前需要真实 V7 `publish_review` 审批，只有 `publish_pr` 一步具有 GitHub 写权限；**绝不自动合并或强推 main**。PR 网络结果不确定时转入 `recovery_required`，必须先核实 GitHub 状态。
4. V8 Budget 当前可靠限制的是 **修复轮数**；Token/美元额度需要宿主真实用量事件 `record_usage` 后才可断言，不能假设模型费用已经精确计量。
5. 已有 V7/RLocks 主要是进程内协调，尚无生产级跨进程/分布式事务、安全沙箱、强身份认证，也无法保证恶意仓库测试执行的安全。只在可信测试仓库或隔离环境执行实际代码。
6. `V9.8` 的新真实 GitHub PR 交付路径仍需在你的账号上跑新的 Issue 才能宣称在线成功；此前 V9.7 的在线证据不能替代。
7. 工作流目前是“模型动态规划 Worker 任务 + 宿主从受信任配方组合动作”，而不是允许模型任意生成和执行所有 Workflow Action；这是一项有意的安全边界。
8. 运行过程中出现 GBK 警告时，已给本次修改涉及的 Git/Goal 输出显式 UTF-8 解码；其他历史子进程路径仍可能需要逐一兼容测试。

## 8. 文件清单

- `devpilot/runtime/workflow_recipes.py`：V7 动态模板、返工和 PR 交付编排。
- `devpilot/runtime/component_audit.py`：可追溯的组件使用事件。
- `devpilot/runtime/general_coder.py`：统一 Agent、PR 发布 Action、V8 修复及分析模式。
- `devpilot/runtime/pr_publisher.py`：在正常 V7 Workflow 的 `publish_pr` Action 内允许经过审批的 PR 发布，并检查修复来源。
- `devpilot/runtime/general_plan.py`：保留更多受信任的步骤 ID 避免模型冲突。
- `devpilot/cli.py`：`--mode`、`--delivery`、`--allow-file`、`--trace-file`、`repair`、`audit`。
- `examples/v9_7_github_issue_coding.py`：真实 Issue → 受限任务 → 新 PR Workflow 的桥接。
- `examples/v9_8_acceptance.py`：离线多组测试及在线交付证据核查。
- `devpilot/workspace/worktree.py`、`devpilot/goal/{collector,verifier,delivery}.py`、`devpilot/runtime/diagnostics.py`：Windows 子进程 UTF-8 防护。
- `tests/test_v9_8_unified_integration.py`、`tests/test_v9_8_components_on_demand.py`：新增验收。
- `docs/V9_8_FUNCTIONS_CN.md`：逐文件函数和一句话中文用途。
