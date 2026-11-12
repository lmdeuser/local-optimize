# Editor hooks

RAG injection into cloud-capable editor sessions is disabled. Claude Code may
send conversation history to Anthropic, so its hooks must never place retrieved
RAG results into that history.

The MCP configuration generator also refuses to expose RAG to its current
cloud-capable clients. Configure RAG only as a local LM Studio MCP connection;
do not switch that RAG-bearing chat to a cloud model. An isolated cloud handoff
is not available until it can pass only the original user request without chat
history, RAG tools, or project access.

## The scripts

| Event | Script | What it does |
| --- | --- | --- |
| `UserPromptSubmit` | `rag_user_prompt.py` | Disabled; no RAG request and no prompt injection. |
| `SessionStart` | `rag_session_start.py` | Disabled; no RAG request and no session injection. |
| `Stop` | `rag_harvest.py` | **Off by default.** Extracts durable facts from the transcript with a *local* model and queues them for review. |

Only the local harvester remains active when explicitly enabled. RAG retrieval
for local LM Studio is provided through its separate local MCP connection.

**Every failure is silent.** A timeout, a refused connection, malformed JSON, a
server that is down: all inject nothing and exit 0. A hook must never be the
reason a prompt fails.

## Install

```bash
python3 hooks/install.py --dry-run    # see the exact settings.json change
python3 hooks/install.py              # user scope: ~/.claude/settings.json
```

The installer backs up before writing, merges into existing hook arrays rather
than replacing them, and recognizes its own entries by path so re-running updates
them in place. `--uninstall` takes it back out; `--settings .claude/settings.json`
installs at project scope instead. Hooks are read at session start, so open a new
session afterwards.

## The harvester, and why it is off

Writing to a shared knowledge store without a human in the loop is how the store
fills with debugging trivia. So `rag_harvest.py` requires `RAG_HOOK_HARVEST=1`,
throttles to one run per 900 s per session, refuses any candidate under 40
characters or matching a secret-shaped pattern, and **queues** proposals to
`~/.local/state/local-optimize/hooks/harvest-queue.jsonl` rather than saving
them. It proposes; a human disposes.

Its extraction backend is pluggable, because the obvious choice did not work in
the historical 2026-10 probe: `qwen2.5-coder:1.5b` returned `[]` on a Russian
engineering conversation that plainly contained a durable decision, both with a
bare instruction and with a bilingual few-shot prompt. That measurement concerns
the former model, not the currently configured `qwen2.5-coder:7b`; it is not a
fresh quality claim about the current runtime.

The Claude Code session-start and user-prompt hooks are disabled because Claude
Code is cloud-capable and its conversation may be transmitted to Anthropic.
Harvesting is local-only. The transcript can contain retrieved RAG results, so
cloud harvest backends are blocked. Values other than `ollama` or `local` fail
closed and produce no candidates.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `RAG_HOOK_URL` | `http://127.0.0.1:8181` | RAG server |
| `RAG_HOOK_TIMEOUT` | `2.5` | seconds per request |
| `RAG_HOOK_DISABLE` | — | `1` disables every hook |
| `RAG_HOOK_DEBUG` | — | `1` writes diagnostics to stderr |
| `RAG_HOOK_LIMIT` | `4` | hits per injection |
| `RAG_HOOK_MAX_CHARS` | `1400` | injected characters per turn |
| `RAG_HOOK_SESSION_QUERY` | bilingual template | session-start question |
| `RAG_HOOK_HARVEST` | — | `1` enables the Stop harvester |
| `RAG_HOOK_HARVEST_BACKEND` | `ollama` | `ollama` or `local`; cloud values are blocked |
| `RAG_HOOK_HARVEST_INTERVAL` | `900` | seconds between harvests per session |

## Git isolation for Claude Code

The versioned `.claude/settings.json` forbids Git interaction by Claude Code in this project. Direct `git` and `gh` rules are backed by `hooks/deny_git.py`, a fail-closed `PreToolUse` guard for every tool. It also blocks shell/Python wrappers, Git-related MCP tools, `.git` metadata, Git libraries, and GitHub or `.git` URLs. Code searches and documentation about Git remain allowed.

The policy is loaded at session start; open a new Claude Code session after pulling it. `permissions.disableBypassPermissionsMode` is set to `disable`, and `.claude/settings.local.json` remains user-owned and unmodified. The hook command uses `${CLAUDE_PROJECT_DIR}` instead of a developer-specific absolute path, so it works after cloning into any directory, including paths with spaces.

This is tool-runtime enforcement, not an operating-system sandbox. It cannot prove that arbitrary code executed outside Claude Code's tool runtime has no Git access. Such a guarantee requires a separate OS-level sandbox with blocked executables, metadata, credentials, and network protocols.

## Not built, deliberately

A `PostToolUse` re-index hook. `watchdog-indexer.service` already indexes writes
inside the registered roots within a second — verified in its journal while this
was being built. A hook doing the same work would be a second, slower copy of a
mechanism that already works.
