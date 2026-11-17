# DevPilot V9.8 · PR Recovery（完整可执行增量包）

## 0. 作用与安全边界

此包在你已有的 V9.8 上新增 `recover-pr`，并使用已有的 **V7 WorkflowStore / WorkflowEngine.reconcile_step、V8 验收报告、V9.5 OperationLedger、V9.7 PR Publisher**。**不重跑 DeepSeek/Worker、不重新改动源码、不自动合并 main**。

支持三条安全路径：

- `reconcile_only`：GitHub 已有同分支、同 SHA、同仓库、同 base 的 PR（例如你已经手动创建的 PR #4），只核实并更新仓库外持久记录，把 V7 原来的 `publish_pr` 失败状态正式对账为 `succeeded`，Workflow 自动变为 `completed`。
- `create_pr`：远程已存在已验收的修复分支、但没有 PR，重新运行可信 pytest 或 unittest，重新核对远程 main 不变，再发起 PR。
- `push_then_pr`：远端既没有分支也没有 PR，重新运行测试并核对基准；使用**期望空引用的 `--force-with-lease=refs/heads/<branch>:`** 只创建新分支（拒绝覆盖竞争对手新建分支），再创建 PR。

任何错误、网络结果未知、版本冲突、审批不符、已发布回执与远端不符、关闭未合并的 PR，都会拒绝自动恢复；没有 `--dry-run` 时也需要操作者亲自输入 `RECOVER <run_id>` 确认。

## 1. 安装增量包

先提交或备份 DevPilot V9.8 原有修改，在 **DevPilot 框架源码根目录**（不是 `devpilot_mcp_e2e_v98` 测试仓库）合并本 ZIP 中的相对路径。

| 文件 | 安装方式 |
|---|---|
| `devpilot/runtime/pr_recovery.py` | 新增 |
| `devpilot/runtime/pr_publisher.py` | 与原文件对比后覆盖：扩展 `build_pr_plan(..., allow_recovery=True)`，不改变正常发布默认行为 |
| `devpilot/cli.py` | 与原文件对比后覆盖：加入 `recover-pr` 和 `--git-http1.1` |
| `tests/test_v9_8_pr_recovery.py` | 新增 31 项离线回归测试 |
| `docs/V9_8_PR_RECOVERY_CN.md` | 使用教程 |
| `docs/V9_8_PR_RECOVERY_FUNCTIONS_CN.md` | 完整函数说明表 |

注意：**不要覆盖原 `state/workflows`、`state/sessions`、`state/reports`、`state/operations`，不能删它们或手改 JSON**。所有这些文件是你运行任务的真实证据。本包不包含 API Token 或任何 `.env`。

确保当前 `.venv` 已装好 pytest、GitHub CLI `gh` 登录状态有效。

```powershell
python -m devpilot.cli --help
python -m pytest tests/test_v9_8_pr_recovery.py -q -p no:cacheprovider `
  --basetemp="$env:USERPROFILE\devpilot_pr_recovery_pytest"
```

## 2. 对你当前的 Issue #3 / PR #4 进行真实恢复

**你已手动创建了 GitHub PR #4：**

- 真实 PR：https://github.com/wulalaxiaoyongshi/devpilot-mcp-e2e-lab/pull/4
- V7 run_id：`beb3886f1c764740b637f44388caf23b`
- 已验收 head：`97aef16fb59c0d2bc946f2fa3c6886228638dd48`
- GitHub Issue：https://github.com/wulalaxiaoyongshi/devpilot-mcp-e2e-lab/issues/3

在 DevPilot **框架根目录**的 PowerShell 执行：

```powershell
$repo = "$env:USERPROFILE\devpilot_mcp_e2e_v98"
$run  = "beb3886f1c764740b637f44388caf23b"

# 第一次：只读 GitHub / V7 / V8 / Git 检查，绝不推送
python -m devpilot.cli recover-pr `
  --repo $repo `
  --run-id $run `
  --dry-run
```

如果 PR #4 仍存在且提交没有被篡改，**预期**：

```json
{
  "run_id": "beb3886f1c764740b637f44388caf23b",
  "head_sha": "97aef16fb59c0d2bc946f2fa3c6886228638dd48",
  "pr_url": "https://github.com/wulalaxiaoyongshi/devpilot-mcp-e2e-lab/pull/4",
  "action": "reconcile_only",
  "will_push": false,
  "will_create_pr": false,
  "will_merge_main": false
}
```

这里是**预期字段示例，不代表已经在你的账号上实际执行通过**。如果打印 `conflict_*`、`uncertain_*` 或 GH 网络错误，直接停止，不能继续执行写操作。

核对 URL、Run ID、SHA 无误后，正式对账：

```powershell
python -m devpilot.cli recover-pr `
  --repo $repo `
  --run-id $run
```

按照提示手工输入：

```text
RECOVER beb3886f1c764740b637f44388caf23b
```

如果 PR #4 仍然匹配且你尚未修改源码，应该会输出 `"passed": true`、`"workflow": "completed"`、`"recovery_action": "reconcile_only"` 和 `"worker_rerun": false`。原 Worker 的成功状态和 `attempts` 不会发生变化。即使 PR 已由你合并且删除修复分支，也会通过已合并 PR 的真实 GitHub API 记录核实，返回 `"merged": true`。

完成后检查：

```powershell
python -m devpilot.cli status --repo $repo --run-id $run

gh pr view 4 --repo "wulalaxiaoyongshi/devpilot-mcp-e2e-lab" `
  --json url,state,headRefOid,baseRefName
```

恢复后的新证据位置（**全部位于 Git 仓库之外**）：

```text
$env:USERPROFILE\.devpilot-v9-6-devpilot_mcp_e2e_v98\
  workflows\<run_id>.json            # V7 原生步骤恢复事件及 completed
  reports\<run_id>.json              # github_pr URL、SHA、merged、recovered
  reports\<run_id>.md                # 附加远程核实的 PR URL
  github_pr\<run_id>.json            # PR 发布回执
  pr_recovery\<run_id>.json          # 恢复阶段与审计结果
  operations\...                    # 如原账本 inflight，核实后会记录 succeeded
```

## 3. 未来的 PR 发布出现网络错误怎么办？

先运行 `recover-pr --dry-run` 查看是否 `reconcile_only` / `create_pr` / `push_then_pr`。

确实需要重新推 Git 分支且你已经验证使用 HTTP/1.1 可以访问 GitHub 时，在实际恢复命令后附加：

```powershell
python -m devpilot.cli recover-pr `
  --repo "$env:USERPROFILE\devpilot_mcp_e2e_v98" `
  --run-id "实际的run_id" `
  --git-http1.1
```

`--git-http1.1` **只影响本次 Git push**，不会修改全局或项目 Git 配置。恢复流程使用 GitHub `gh api` 查询远程 ref/PR，因此即使系统 Git HTTP 通道偶发超时也可以安全确认已存在的 PR。

对于远程写入之前的检查：工作区必须干净，HEAD 等于已验收提交；GitHub origin 必须与原始 Issue 一致；所有 V7 审批、Worker Job、V8 Goal、必要测试必须真实通过；`main` 未被移动；任何相同 PR 或分支的冲突必须人工处理。

## 4. 哪些状态不会自动重试？

| 预览 `action` | 含义 | 系统行为 |
|---|---|---|
| `reconcile_only` | 已确认相同 PR | 只补状态与报告，无远程写入 |
| `create_pr` | 分支已存在且 SHA 正确，但没找到 PR | 重新跑测试、再次审查基准后创建 PR |
| `push_then_pr` | 分支及 PR 都不存在 | 重新跑测试、原子创建新分支及 PR |
| `conflict_main` | 原始 `main` 已变化 | 拒绝写入，先重新集成与验收 |
| `conflict_branch` | 同名远程分支 SHA 冲突 | 拒绝覆盖 |
| `uncertain_original_publish` | 原 V9.5 账本仍显示 inflight 而未查到 PR | 停止，人工确认原操作的远程结果 |
| `uncertain_push` | 推送曾发出但无法确认成功 | 不自动二次推送 |
| `uncertain_pr_create` | PR 创建曾发出但远程未确认 | 不自动二次创建 |
| `missing_published_pr` | 曾存回执但 GitHub 现在找不到 PR | 停止，防止虚假补发 |
| `missing_confirmed_branch` | 曾确认存在的分支现在消失 | 停止，人工确认 |

**PR 已 CLOSED 且没有合并**不会自动算成功；PR 已 MERGED 则可以恢复原交付记录，但不会再次调用 `gh pr merge`。

如果 V7 因其它步骤失败（非 `publish_pr`）、审批缺失、Goal 未 verified、文件修改范围变化或 GitHub API 返回错误，本命令会直接拒绝恢复。

## 5. 离线验收与限制

本增量包采用真实本地 Git 提交/裸远端、真实 V7 Store / Engine.reconcile_step、真实 V9.5 OperationLedger，**以离线可控的 `gh` 假实现模拟远程 GitHub API**。该测试不会替你在线创建任何 PR。

在交付环境中分组运行：

```powershell
python -m pytest tests/test_v9_8_pr_recovery.py -q -p no:cacheprovider `
  --basetemp="$env:USERPROFILE\devpilot_pr_recovery_pytest"
python -m pytest tests/test_v9_8_unified_integration.py -q -p no:cacheprovider `
  --basetemp="$env:USERPROFILE\devpilot_v98_pytest"
python -m pytest tests/test_v9_5_security.py -q -p no:cacheprovider `
  --basetemp="$env:USERPROFILE\devpilot_v95_pytest"
python -m pytest tests/test_v7_4_recovery.py -q -p no:cacheprovider `
  --basetemp="$env:USERPROFILE\devpilot_v74_pytest"
```

核心限制：

1. **真实 GitHub 私有仓库恢复必须在你的 Windows 本机验收**；本环境没有你的 Token，不能声称 PR #4 的在线恢复已成功。
2. 当前恢复器一次只处理一条 Workflow、一个 GitHub Issue、一个 PR；不支持多 PR 批处理。
3. 当前没有跨机器租约服务，采用仓库外排他锁；进程异常崩溃后，残留 `.lock` 需要先人工核实进程和 GitHub 状态，不能自动删除。
4. 任何尚未确认的 GitHub 写操作不能因为“暂时查不到 PR”就自动重复提交；此类状态需人工介入后进一步恢复。
5. GitHub CLI 的身份与 MCP 的只读 Token 是独立的；PR 写入依赖 `gh auth login` 的权限。
6. 如果你本地对 `cli.py` 或 `pr_publisher.py` 做过独立修补，请先对比增量 ZIP；不要盲目覆盖最新自定义代码。

**设计原则：只允许“真实远端已核实成功”推进已失败的 V7 `publish_pr` 步骤，不直接伪造完成状态。**
