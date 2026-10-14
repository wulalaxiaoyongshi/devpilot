# DevPilot Project State
---

## Completed

### V0 — Agent Kernel

* Project skeleton
* Message model
* Model abstraction
* MockModel
* DeepSeek model provider
* Real LLM integration
* Structured ModelResponse
* ToolCall representation
* Agent Loop
* Tool result feedback loop
* Maximum step protection

### V1 — Tool Runtime

* Tool abstraction
* Tool schema generation
* Tool Registry
* Tool Dispatcher
* PreToolUse hooks
* PostToolUse hooks
* Permission policy
* Built-in read_file tool
* Built-in write_file tool
* Built-in edit_file tool
* Built-in glob tool
* Built-in bash tool
* Workspace path protection
* Tool execution tests
* Agent → Tool Runtime integration

### V2 — Context Runtime

* TodoManager
* todo_write tool
* SkillCatalog
* load_skill tool
* On-demand Skill loading
* Transcript persistence
* ContextManager
* Dynamic system prompt construction
* Todo state injection
* Skill catalog injection
* Large tool-result offloading
* Old tool-result trimming
* Context size estimation
* Semantic context compaction
* Compaction transcript snapshot
* Preservation of original history when creating transcript snapshots
* V2 Agent integration

### V3 — Persistent Memory + Task Graph

* Persistent Memory Store
* Memory Recall
* Memory Extraction
* Memory Consolidation
* Persistent Task Store
* Dependency-aware Task Graph
* Runnable-task scheduling

### V4 — Multi-Agent Runtime

* Subagent
* Persistent Teammate
* Task ownership
* Mailbox
* Git worktree isolation

### V5 — Event Runtime

* Background tasks
* Event bus
* Cron
* Async event handling
---


## Not Started
### V6 — MCP

* MCP client
* Dynamic tool discovery
* MCP tool adapter
* MCP permission policy

### V7 — Workflow Runtime

* Deterministic workflows
* Parallel
* Pipeline
* Journal
* Resume / replay
* Budget

### V8 — Goal Runtime

* Goal evaluator
* Stop gate
* Completion evidence
* Full coding-agent demo

---
