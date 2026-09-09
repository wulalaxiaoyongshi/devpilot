# DevPilot Project State

## Current Stage

V0 — Agent Kernel

## Current Goal

Build the smallest testable Agent kernel.

## Completed

- Project architecture roadmap

## In Progress

- V0.0 Project skeleton
- V0.1 Model abstraction
- V0.2 Minimal Agent

## Not Started

- Tool calling
- Tool runtime
- Context runtime
- Memory
- Task graph
- Multi-agent
- Event runtime
- MCP
- Workflow
- Goal evaluator

## Current Design Decisions

- Python is the main implementation language.
- Core Agent mechanisms will be implemented without LangChain, CrewAI, or AutoGen.
- Model providers will be hidden behind a Model interface.
- Tests should not require a real LLM whenever possible.

## Next Step

Implement the minimal Model interface and Agent class.