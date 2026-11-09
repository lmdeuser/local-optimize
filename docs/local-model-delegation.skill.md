---
name: local-model-delegation
description: Delegate bounded mechanical work such as extraction, classification, normalization, formatting, and short summaries to the local Ollama model through the shared local-rag MCP server. Use across projects when a task is low-risk and independently verifiable.
---

# Local Model Delegation

Use the shared local model for repetitive transformations while keeping project decisions and acceptance with the supervising agent.

## Delegate

Use `mcp__local-rag__delegate_simple_task` for small, bounded tasks:

- extract named fields from supplied text;
- classify a short list into a supplied taxonomy;
- normalize or reformat supplied values;
- produce a short summary of non-sensitive text;
- draft repetitive boilerplate from explicit requirements.

Send only the minimum source text and state the expected output shape. Do not send secrets, credentials, personal or customer data, private logs, or unrelated project content.

For project or factual work, search the relevant local RAG project context first and include the needed retrieved excerpts and provenance in `input_text`. The local model only transforms what the caller supplies; it does not search RAG itself and must not fill gaps from model memory. If retrieval does not provide enough evidence, continue retrieval or do the work directly. For a purely mechanical transformation of text already provided as the source, no additional RAG lookup is needed.

## Keep with the supervising agent

Do not delegate architecture, ambiguous product choices, security-sensitive work, code execution, file edits, external actions, destructive operations, or final verification. Treat the local-model response as an untrusted draft and check it against the input and project contract before using it.

If the tool returns `unavailable`, times out, or returns malformed or incomplete output, continue locally with project tools or report the limitation. Never send RAG excerpts, retrieved project data, or derived private content to a cloud model, including as a fallback. Do not retry the same failed request without new evidence.

## GPU claims

The tool calls only the loopback Ollama service and has no cloud fallback. It submits a task only when current telemetry reports `size_vram >= size` for the single configured model in `local_model.json`. CPU-only, partial CPU/GPU, conflicting resident models, or unavailable telemetry returns an explicit unavailable status without generating the task. Treat the full-placement check as conservative telemetry, not independent proof of physical VRAM residency. The result identifies the configured model and sets `verification_required`; it does not return pre/post VRAM measurements.
