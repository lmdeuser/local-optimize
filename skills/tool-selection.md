---
name: tool-selection
description: Select optimal CLI tools, subagents, and MCP tools efficiently with minimal resource overhead.
---

# Tool Selection Protocol

## Purpose
Guide effective tool choice and execution, prioritizing built-in utilities, targeted CLI commands, and appropriate MCP servers.

## Activation Triggers
- Deciding which command, script, or tool to run to solve a task.

## Rules
1. Use targeted commands with sensible output limits (e.g., limit logs or search output).
2. Check command options before running complex scripts.
3. Handle process execution safely; avoid long-running background loops without monitoring.

