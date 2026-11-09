# Local Model Delegation

## Purpose

Use the configured local Ollama service for bounded mechanical work while the supervising agent retains planning, file changes, source validation, and final acceptance. This is not a separate compact-model policy: every MCP inference path reads the same `local_model.json`.

## Current contract

- `delegate_simple_task` supports extraction, classification, normalization, formatting, and short summaries.
- The shared model is `qwen2.5-coder:7b` with an 8,192-token context. Conflicting model or context overrides fail closed.
- Instructions are limited to 500 characters, input to 6,000, and the output contract to 300. The default end-to-end timeout is 150 seconds, bounded to 180; output is capped at 256 tokens.
- The tool calls direct loopback Ollama only. A nonblocking cross-process lock permits one generation, with no queue, CPU fallback, or cloud fallback.
- Generation requires current Ollama telemetry showing one resident configured model, the expected context, and `size_vram >= size`. This is a conservative placement check, not independent proof of physical VRAM residency.
- Results include `status`, `task_type`, `execution_backend`, `gpu_execution`, `configured_model`, `verification_required`, `elapsed_ms`, and correlation data; successful calls also include `response`. The tool does not return pre/post VRAM measurements.
- A timeout, busy model, placement conflict, transport failure, or empty response is returned explicitly as `unavailable`/`error`.

## Safety boundary

Do not send secrets, credentials, private user or customer data, or sensitive logs. Do not delegate architecture, security judgments, external actions, file edits, code execution, destructive operations, or final verification.

For project or factual work, first retrieve relevant RAG material and pass only the needed excerpts and provenance in `input_text`. The local model uses only that input and must return `INSUFFICIENT_DATA` when it lacks necessary evidence. It is not a RAG substitute. Treat all output as an untrusted draft and verify it against the source and project contract.

If local inference fails, continue with project tools or report the limitation. Never send RAG excerpts, retrieved project data, or derived private content to a cloud model as fallback.

## Client isolation

The full `local-rag` MCP server is intended for approved local-only clients. Cloud-capable clients, including Kimi Code running a cloud model, must not register it directly, because retrieved RAG content would enter the cloud conversation. The separate `lmstudio_rag_mcp.py` wrapper exposes read-only search for a local LM Studio chat; do not switch that chat to a cloud model.
