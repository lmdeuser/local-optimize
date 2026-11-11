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

## Selection Matrix
- Known path or exact text: use the dedicated Read, Glob, or Grep capability; use `rg` only when no dedicated tool fits.
- Structured transformation or multi-step execution: use a bounded shell command with explicit inputs and expected output.
- Project history or prior decisions not present in current files: on an approved local-only client, use project RAG; otherwise inspect primary sources.
- Version-sensitive third-party API behavior: query current owning documentation through the configured documentation route.
- Parallel isolated work: delegate only when the user or project instructions authorize it and the subtask has an independently checkable result.
- Browser interaction: use the browser route designated by the host, not an assumed external automation dependency.

Prefer the smallest tool that can produce decisive evidence. Do not replace a direct file read with semantic search or a documentation lookup with model recollection.

