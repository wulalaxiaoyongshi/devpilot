# V9.8 最终验收清单

- [x] V9.8.1 分析、本地修改、GitHub PR Workflow 的动态组合（离线真实工作流）。
- [x] V9.8.2 PR 发布只在独立人工审批后才执行，Git 临时远端 main 未修改，PR API 用可控替身。
- [x] V9.8.3 V8 Feedback、RepairTicket、Budget + 子 V7 Workflow 返工并重新采证。
- [x] V9.8.4 V2 Context、Todo、Skills；V3 Memory；V4 Mailbox 独立真实触发。
- [x] V9.8.5 五组离线专项共 51 passed；分组兼容回归已通过。
- [ ] Windows 上为新的 GitHub Issue 完成真实 DeepSeek + HTTP MCP 的新版统一 PR Workflow。
- [ ] Windows 上根据真实 PR URL 和模型调用轨迹执行 `--mode verify`。
- [ ] Windows 上执行 V9.8 与旧项目全量回归，确认 Python 3.14 兼容性。
- [ ] 独立在线验收 V8 失败自动返工、长上下文与复杂多 Worker 任务。

注意：`online_passed` 只有在 `examples/v9_8_acceptance.py --mode verify` 的实际证据核查通过后才能置 true。
