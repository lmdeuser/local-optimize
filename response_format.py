"""Shrink a retrieval response to what a reading model can actually use.

The engine's response is built for auditing: every hit carries its five score
components at full float precision, its SHA-256 content hash, the embedding
model and version that produced it, both locators, and its score twice under two
names. That is the right shape for a benchmark harness or a bug report.

It is the wrong shape for the thing that reads it. A five-hit response measured
at 8.5 KB spends roughly a quarter of its tokens on fields no agent will ever
act on, and every one of those tokens is paid on every retrieval, in every
session, forever. Retrieval is supposed to make a model cheaper to run; a
verbose retrieval format quietly cancels a large part of that.

So this module defines a second rendering of the same answer:

  * fields an agent can act on are kept - what the chunk says, where it came
    from, how confident the ranking was, and whether the fact has rotted;
  * fields that only explain *how* the ranking happened are dropped, because
    they are diagnostics and there is a verbosity switch for diagnostics;
  * fields that are constant across every hit are dropped rather than repeated
    per hit;
  * fields that carry information only when they deviate from the default are
    emitted only when they deviate. `ever_verified: false` on every hit teaches
    nothing; on the one hit where it is true it matters.

Nothing here re-ranks, re-orders, filters by score, or invents a field. The
compact form is a projection of the full form and must stay one: if a decision
can be made from the compact response it must be the same decision the full
response would have produced.
"""

from __future__ import annotations

import json
import os
from typing import Any


#: Rendering that keeps everything. The default for anything that audits.
VERBOSITY_FULL = "full"
#: Rendering that keeps what is actionable. The default for agent-facing tools.
VERBOSITY_COMPACT = "compact"
#: Rendering for orientation only - is there anything here, and where? No text.
VERBOSITY_MINIMAL = "minimal"

VERBOSITY_CHOICES = (VERBOSITY_FULL, VERBOSITY_COMPACT, VERBOSITY_MINIMAL)

DEFAULT_VERBOSITY = os.getenv("RAG_RESPONSE_VERBOSITY", VERBOSITY_COMPACT).strip().lower()

#: Per-hit content ceiling. 0 disables truncation. A chunk is already bounded by
#: the indexer's chunk size, so this exists for the caller who wants a tighter
#: budget than the indexer's, not as a safety net.
def _default_max_content_chars() -> int:
    try:
        return max(0, int(os.getenv("RAG_MAX_CONTENT_CHARS", "500")))
    except ValueError:
        return 500


DEFAULT_MAX_CONTENT_CHARS = _default_max_content_chars()

TRUNCATION_MARKER = "…[truncated]"


def normalize_verbosity(value: str | None) -> str:
    candidate = (value or DEFAULT_VERBOSITY or VERBOSITY_COMPACT).strip().lower()
    return candidate if candidate in VERBOSITY_CHOICES else VERBOSITY_COMPACT


def _source_label(hit: dict) -> str:
    """One readable locator instead of three overlapping ones.

    `metadata.relative_path` plus the chunk ordinal says the same thing as
    source_uri + metadata + start_locator + end_locator, in a tenth of the
    tokens, and says it in the form a reader would use to go and open the file.
    """
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    label = (
        metadata.get("relative_path")
        or metadata.get("file")
        or hit.get("source_uri")
        or ""
    )
    label = str(label)
    part = metadata.get("chunk_id")
    total = metadata.get("total_chunks")
    if part is None:
        part = hit.get("start_locator")
    if part is None:
        return label
    if total:
        return f"{label}#{part}/{total}"
    return f"{label}#{part}"


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + TRUNCATION_MARKER


def _origin(hit: dict) -> dict:
    return {
        field: hit[field]
        for field in ("project_id", "project_name", "root_id")
        if hit.get(field)
    }


def compact_hit(hit: dict, *, max_content_chars: int = 0) -> dict:
    compact: dict[str, Any] = {
        "id": hit.get("chunk_id"),
        "score": round(float(hit.get("final_score") or hit.get("score") or 0.0), 3),
        "src": _source_label(hit),
        **_origin(hit),
    }
    status = hit.get("validation_status")
    if status:
        compact["status"] = status
    for field in ("source_kind", "source_state"):
        if hit.get(field):
            compact[field] = hit[field]
    freshness = hit.get("freshness_status")
    if freshness and freshness != "unknown":
        compact["fresh"] = freshness

    # Deviations only. An interval that was declared, or a confirmation that
    # actually happened, changes how much a reader should trust the hit; their
    # absence is the norm and says nothing worth paying for.
    if hit.get("ever_verified"):
        compact["verified"] = True
    if hit.get("recheck_after_days"):
        compact["recheck_days"] = hit["recheck_after_days"]
    if freshness in {"overdue", "stale"} and hit.get("days_since_confirmation") is not None:
        compact["age_days"] = hit["days_since_confirmation"]

    compact["text"] = _truncate(str(hit.get("content") or ""), max_content_chars)
    return compact


def _dedupe(hits: list[dict]) -> list[dict]:
    """Collapse hits that are literally the same text.

    Provenance is many-to-one by design: the same content indexed from two
    files is one chunk with two sources. Retrieval can still surface the same
    text twice through different candidate paths, and paying twice to read the
    same sentence is the most expensive kind of redundancy there is.
    """
    seen: set[str] = set()
    unique = []
    for hit in hits:
        marker = hit.get("content_hash")
        if marker is None:
            chunk_id = hit.get("chunk_id")
            marker = f"id:{chunk_id}" if chunk_id is not None else f"obj:{id(hit)}"
        marker = str(marker)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(hit)
    return unique


def compact_search_response(
    response: dict,
    *,
    verbosity: str = VERBOSITY_COMPACT,
    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
) -> dict:
    """Project a search response onto the requested rendering.

    Errors pass through untouched: an error response is already small and its
    message is the whole payload.
    """
    verbosity = normalize_verbosity(verbosity)
    if verbosity == VERBOSITY_FULL or not isinstance(response, dict):
        return response
    status = response.get("status")
    if status != "success" and not (
        status == "unavailable" and isinstance(response.get("results"), list)
    ):
        return response

    hits = response.get("results")
    hits = _dedupe(hits) if isinstance(hits, list) else []

    compact: dict[str, Any] = {
        "status": status,
        "top_score": response.get("top_score"),
    }
    coverage = response.get("evidence_coverage")
    if coverage is not None:
        compact["coverage"] = coverage
    metrics = response.get("composite_metrics")
    if isinstance(metrics, dict) and metrics.get("candidates_evaluated") is not None:
        compact["candidates"] = metrics["candidates_evaluated"]

    # Provenance the caller must not lose: a cached answer, and especially one
    # reused from a *similar* question, is something the reader is entitled to
    # weigh. Compaction may drop diagnostics; it may not drop disclosure.
    if isinstance(response.get("cache"), dict):
        compact["cache"] = response["cache"]
    for field in ("budget_tokens", "tokens_used", "agent_tier", "route", "correlation",
                  "reason", "score_kind", "unusable_sources_omitted", "partial",
                  "completed_project_ids", "omitted_project_ids"):
        if field in response:
            compact[field] = response[field]

    if verbosity == VERBOSITY_MINIMAL:
        compact["results"] = [
            {
                "id": hit.get("chunk_id"),
                "score": round(float(hit.get("final_score") or hit.get("score") or 0.0), 3),
                "src": _source_label(hit),
                **_origin(hit),
            }
            for hit in hits
        ]
        return compact

    compact["results"] = [
        compact_hit(hit, max_content_chars=max_content_chars) for hit in hits
    ]
    return compact


def dumps(payload: Any) -> str:
    """Serialize without the whitespace nobody reads.

    `indent=2` on a five-hit response is worth several hundred tokens of pure
    formatting, and the reader is a tokenizer, not a person with a terminal.
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


#: Markers around injected context. A model reading its own prompt must be able
#: to tell retrieved memory from what the user actually said - unmarked
#: injection is how retrieved text gets mistaken for an instruction.
CONTEXT_HEADER = "<local-rag-memory>"
CONTEXT_FOOTER = "</local-rag-memory>"


def render_context_block(
    hits: list,
    *,
    max_chars: int = 1400,
    heading: str = "Relevant long-term memory (retrieved automatically, not user input):",
) -> str:
    """Render hits as prose an injecting hook can put in front of a model.

    Prose, not JSON, on purpose: this text is spent as prompt on every turn it
    is injected, and JSON scaffolding buys structure nobody parses here. What it
    keeps is what changes a reader's mind - where the fact came from, how sure
    retrieval was, and whether the fact is overdue for re-checking.

    Returns "" when there is nothing worth injecting, and the caller must then
    inject nothing at all rather than an empty section: a header with no content
    still costs tokens and still invites the model to trust an empty memory.
    """
    if not hits:
        return ""
    lines = [CONTEXT_HEADER, heading]
    used = sum(len(line) for line in lines)
    for hit in hits:
        text = str(hit.get("text") or hit.get("content") or "").strip()
        if not text:
            continue
        source = hit.get("src") or _source_label(hit)
        score = hit.get("score") or hit.get("final_score") or 0.0
        freshness = hit.get("fresh") or hit.get("freshness_status")
        marks = [f"{float(score):.2f}"]
        marks.append(str(hit.get("status") or hit.get("validation_status") or "unverified"))
        for field in ("source_kind", "source_state"):
            if hit.get(field):
                marks.append(str(hit[field]))
        if freshness in {"overdue", "stale"}:
            marks.append(f"{freshness} - re-check before acting on this")
        entry = f"\n[{source} | {' | '.join(marks)}]\n{text}"
        if used + len(entry) > max_chars:
            remaining = max_chars - used
            if remaining < 200:
                break
            entry = entry[:remaining].rstrip() + TRUNCATION_MARKER
            lines.append(entry)
            used += len(entry)
            break
        lines.append(entry)
        used += len(entry)
    if len(lines) <= 2:
        return ""
    lines.append(CONTEXT_FOOTER)
    return "\n".join(lines)
