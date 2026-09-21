# DevPilot Project State

## Current Stage

V2 — Context Runtime ✅ Complete

Next: V3 — Persistent Memory + Task Graph

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

---

## Current Architecture

DevPilot currently contains three main runtime layers:

1. Agent Kernel

   * Controls the model/tool execution loop.
   * Maintains conversation state.
   * Stops when the model returns no tool calls or the step limit is reached.

2. Tool Runtime

   * Exposes tools to the model.
   * Maps tool names to Python handlers.
   * Runs permission checks and hooks.
   * Executes workspace operations.

3. Context Runtime

   * Maintains Todo state.
   * Exposes Skills on demand.
   * Persists transcripts.
   * Controls active LLM context size.
   * Trims large or old tool outputs.
   * Semantically compacts old conversation history when required.

---

## Current Runtime Flow

User Request

→ Agent Loop

→ ContextManager

→ Todo / Skill / Context preparation

→ Context trimming or compaction when necessary

→ Dynamic System Prompt

→ Model

→ ToolCall

→ Tool Registry

→ PreToolUse Hooks

→ Permission Policy

→ Tool Handler

→ PostToolUse Hooks

→ Tool Result

→ Agent Loop continues

→ Final Assistant Response

---

## Current Design Decisions

* Python is the main implementation language.
* Core Agent mechanisms are implemented without LangChain, CrewAI, or AutoGen.
* Model providers are hidden behind the Model interface.
* Mock/Fake models are used for deterministic unit tests.
* LLMs make semantic decisions.
* Deterministic control remains inside the Runtime.
* Tools are injected into Agents rather than hard-coded into the Agent class.
* Tool execution is separated from tool selection.
* Runtime permissions are independent of model decisions.
* Skills are loaded on demand rather than permanently inserted into context.
* Transcript history is persisted separately from context compaction.
* Context compaction is allowed to be lossy for active model context.
* Transcript snapshots must preserve the original history rather than already-trimmed tool results.
* Tests should not require a real LLM unless explicitly testing provider integration.

---

## Persistent Runtime State

Current persistent project data may be stored under:

.devpilot/

including transcript and context-related runtime files.

Generated runtime state under `.devpilot/` should not be committed unless a file is intentionally used as a fixture or example.

---

## Not Started

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

## Next Step

Begin V3 — Persistent Memory + Task Graph.

First milestone:

Build a persistent Memory Store that can survive process restarts while remaining separate from the active Context Runtime.

---

## Last Completed Checkpoint

V2 — Context Runtime

Status: COMPLETE
