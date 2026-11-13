"""V9.7 实际 CLI 的 DeepSeek 调用轨迹入口，普通运行模式仍使用原 CLI。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from devpilot.cli import main as cli_main
from devpilot.runtime.model_trace import TraceWriter, TracedDeepSeekModel


def main(argv: list[str] | None = None) -> int:
    """注入真实追踪模型执行 run/resume，或者从 JSONL 生成调用核对摘要。"""
    parser = argparse.ArgumentParser(description="DevPilot 真实 DeepSeek 调用轨迹")
    parser.add_argument("--trace-file", type=Path, required=True)
    parser.add_argument("--model", default=os.environ.get("DEVPILOT_MODEL", "deepseek-v4-flash"))
    args, forwarded = parser.parse_known_args(argv)
    if forwarded == ["inspect"]:
        writer = TraceWriter(args.trace_file)
        print(json.dumps(writer.summarize(), ensure_ascii=False, indent=2))
        return 0
    if not forwarded or forwarded[0] not in {"run", "resume"}:
        parser.error("请传入 run/resume 与原 devpilot.cli 参数，或传入 inspect")
    try:
        repo_index = forwarded.index("--repo")
        repo = Path(forwarded[repo_index + 1]).resolve()
    except (ValueError, IndexError):
        parser.error("run/resume 需要原 CLI 的 --repo 参数")
    trace_path = args.trace_file.expanduser().resolve()
    if trace_path == repo or trace_path.is_relative_to(repo):
        parser.error("追踪文件不能写进待修改的 Git 仓库")
    writer = TraceWriter(trace_path)
    writer.append("cli_start", command=forwarded[0], repo_name=repo.name)
    try:
        exit_code = cli_main(forwarded, model_factory=lambda role: TracedDeepSeekModel(args.model, role=role, trace=writer))
    except Exception as error:
        writer.append("cli_error", error_type=type(error).__name__)
        raise
    finally:
        summary = writer.summarize()
        print("\n=== DeepSeek 调用轨迹摘要 ===")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    writer.append("cli_end", exit_code=exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
