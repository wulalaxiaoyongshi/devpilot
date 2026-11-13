"""V9.6：面向不同 Python Git 项目的通用 Coding Agent 命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from devpilot.core.model import DeepSeekModel
from devpilot.runtime.general_coder import GeneralCodingAgent, git


def build_parser() -> argparse.ArgumentParser:
    """统一定义 run、status、approve 和 resume 命令及受信任的启动配置。"""
    parser = argparse.ArgumentParser(prog="devpilot", description="通用 Python Coding Agent（真实模型默认、分阶段人工审批）")
    parser.add_argument("command", choices=("run", "status", "approve", "resume", "doctor", "publish-pr", "repair", "audit"))
    parser.add_argument("--repo", type=Path, required=True, help="真实 Python Git 仓库根目录")
    parser.add_argument("--state", type=Path, default=None, help="必须放在 Git 仓库以外的状态目录")
    parser.add_argument("--task", help="用户自己的自然语言开发任务，仅 run 使用")
    parser.add_argument("--run-id", help="已存在的 Workflow Run ID")
    parser.add_argument("--test", choices=("pytest", "unittest"), default=None, help="由宿主选定的测试工具")
    parser.add_argument("--model", default=os.environ.get("DEVPILOT_MODEL", "deepseek-v4-flash"))
    parser.add_argument("--trace-file", type=Path, help="保存真实 DeepSeek 请求元信息的仓库外 JSONL 文件")
    parser.add_argument("--mcp-config", type=Path, default=None, help="可信的真实 MCP Server 配置")
    parser.add_argument("--mcp-read-alias", action="append", default=[], help="显式信任该 Server 上经策略识别的只读工具")
    parser.add_argument("--step", help="需要审批的工作流步骤")
    parser.add_argument("--request-id", help="当前审批单的唯一编号")
    parser.add_argument("--decision", choices=("approve", "reject"))
    parser.add_argument("--actor", default="reviewer", help="本地审核人标识（不等于真实身份认证）")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--require-live", action="store_true", help="doctor 要求本机 DeepSeek 和 MCP 环境就绪")
    parser.add_argument("--issue-url", help="真实 GitHub Issue 标准 URL，用于绑定自动 PR 发布范围")
    parser.add_argument("--mode", choices=("edit", "analysis"), default="edit", help="动态业务模式：代码修改或只读分析")
    parser.add_argument("--delivery", choices=("local", "github-pr"), default="local", help="本地交付或 V7 内受控 PR 交付")
    parser.add_argument("--allow-file", action="append", default=[], help="可信宿主限制 Worker 只能修改的 Python 相对路径，可多次传入")
    parser.add_argument("--repair-action", choices=("prepare", "dispatch"), help="V8 反馈修复：准备返工单或创建受审批的返工流程")
    parser.add_argument("--ticket-id", help="V8 返工单编号")
    parser.add_argument("--dry-run", action="store_true", help="只检查 PR 发布条件，不执行远程写入")
    return parser


def default_state(repo: Path) -> Path:
    """默认将持久化状态放在 Git 仓库外部，避免污染代码验收。"""
    root = repo.resolve(strict=True)
    return root.parent / (".devpilot-v9-6-" + root.name)


def main(argv: list[str] | None = None, *, model_factory: Any = None) -> int:
    """调用真实模型和已有 Runtime，并在每个审批或失败边界展示实际持久化状态。"""
    args = build_parser().parse_args(argv)
    try:
        repo = args.repo.resolve(strict=True)
        state = (args.state or default_state(repo)).resolve()
        if args.command == "publish-pr":
            from devpilot.runtime.pr_publisher import build_pr_plan, publish_pr
            if not args.run_id or not args.issue_url:
                raise ValueError("publish-pr 需要 --run-id 和 --issue-url")
            plan = build_pr_plan(repo, state, args.run_id, args.issue_url)
            preview = {"run_id": plan.run_id, "github_repo": plan.github_repo,
                       "issue_url": plan.issue_url, "base_branch": plan.base_branch,
                       "publish_branch": plan.publish_branch, "head_sha": plan.head_sha,
                       "changed_files": plan.changed_files, "title": plan.title,
                       "dry_run": args.dry_run, "will_merge_main": False}
            print(json.dumps(preview, ensure_ascii=False, indent=2))
            if args.dry_run:
                return 0
            def confirm_publish(value):
                """展示发布目标并要求真人输入本次任务的唯一确认口令。"""
                print("仅推送修复分支并创建 PR，不会合并 main；请先核对上面的仓库、SHA 和改动文件。")
                return input(f"输入 PUBLISH {value.run_id} 才允许发布：").strip() == f"PUBLISH {value.run_id}"
            result = publish_pr(plan, state, confirm_publish)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "doctor":
            from devpilot.runtime.diagnostics import environment_report
            report = environment_report(repo, state, require_live=args.require_live,
                                        mcp_config=args.mcp_config, test_mode=args.test or "pytest")
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ready"] else 2
        if args.command != "run" and not args.run_id:
            raise ValueError("status/approve/resume/repair/audit require --run-id")
        if args.command == "run" and not args.task:
            raise ValueError("run requires --task")
        if args.command == "approve" and (not args.step or not args.request_id or not args.decision):
            raise ValueError("approve requires --step, --request-id and --decision")
        test_mode = args.test or "pytest"
        if args.command != "run":
            session_file = state / "sessions" / f"{args.run_id}.json"
            stored = json.loads(session_file.read_text(encoding="utf-8"))
            test_mode = stored["test_mode"]
        if model_factory is not None:
            create_model = model_factory
        elif args.trace_file is not None:
            from devpilot.runtime.model_trace import TraceWriter, TracedDeepSeekModel
            trace_path = args.trace_file.expanduser().resolve()
            if trace_path == repo or trace_path.is_relative_to(repo):
                raise ValueError("DeepSeek trace must be stored outside the Git repository")
            trace = TraceWriter(trace_path)
            create_model = lambda role: TracedDeepSeekModel(args.model, role=role, trace=trace)
        else:
            create_model = lambda _id: DeepSeekModel(model=args.model)
        with GeneralCodingAgent(repo, state, create_model, test_mode=test_mode,
                                mcp_config=args.mcp_config, mcp_read_aliases=tuple(args.mcp_read_alias),
                                job_timeout=args.timeout,
                                allowed_files=tuple(args.allow_file)) as app:
            if args.command == "run":
                result = app.create(args.task, mode=args.mode, delivery=args.delivery, issue_url=args.issue_url)
            elif args.command == "audit":
                result = app.audit.summary(args.run_id)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            elif args.command == "repair":
                if args.repair_action == "prepare":
                    result = app.prepare_repair(args.run_id)
                elif args.repair_action == "dispatch" and args.ticket_id:
                    result = app.dispatch_repair(args.run_id, args.ticket_id)
                else:
                    raise ValueError("repair requires --repair-action prepare or dispatch --ticket-id")
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            elif args.command == "status":
                result = app.status(args.run_id)
            elif args.command == "resume":
                result = app.advance(args.run_id, timeout_seconds=args.timeout)
            else:
                current = app.status(args.run_id)
                if not any(item["step"] == args.step and item["request_id"] == args.request_id for item in current["approvals"]):
                    raise ValueError("No matching pending approval request")
                if args.decision == "approve":
                    print("\n你即将批准真实代码改动或交付，请检查仓库和 Workflow 内容。")
                    if args.step == "preapproval":
                        print(json.dumps(app.session(args.run_id)["plan"], ensure_ascii=False, indent=2))
                    elif args.step == "publish_review":
                        print("即将向 GitHub 推送独立修复分支并创建 PR；不会修改或合并远端 main。")
                        print("Issue：" + str(app.session(args.run_id).get("issue_url")))
                        print("Git HEAD：" + git(repo, "rev-parse", "HEAD"))
                    else:
                        print("Git 变更文件：\n" + git(repo, "show", "--format=", "--name-only", "HEAD"))
                    if input(f"输入 APPROVE {args.request_id} 才会批准：").strip() != f"APPROVE {args.request_id}":
                        raise PermissionError("Approval confirmation did not match")
                result = app.approve(args.run_id, args.step, args.request_id,
                                     actor=args.actor, approved=args.decision == "approve")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result.get("approvals"):
                print("需要人工审批：请复制上面的 step 和 request_id，再运行 devpilot approve。")
            elif result["workflow"] in {"failed", "recovery_required", "rejected", "approval_expired"}:
                print("执行被阻止：请查询 state/workflows 和 Goal 证据，禁止盲目重放。", file=sys.stderr)
                return 2
            return 0
    except (OSError, ValueError, RuntimeError, PermissionError, KeyError) as error:
        print(f"DevPilot: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
