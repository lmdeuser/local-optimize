# Embedding model evaluation

> Historical candidate-selection measurements. The 0.30 floor below belongs to the
> E5/RRF candidate score scale used in 2026-08. Current serving applies the
> versioned Qwen evidence policy and a 0.50 default floor; these figures are not
> a current quality or threshold claim.

Measured 2026-08-08 against the live `aiagent` store: 566 retrievable chunks,
302 Russian / 264 English. Harness:
`~/.local/state/local_optimize/probes/embedding_bakeoff.py` (read-only).

The question was §4.1 of the audit brief: `all-MiniLM-L6-v2` is English-trained
and this corpus is heavily Russian, and two near-verbatim retrievals had come
back with cosines of 0.514 and 0.375, which looked low.

## Method

Two label-free probes, both derived from the corpus so nothing had to be hand
annotated:

- **verbatim** — a contiguous 12-word window lifted out of a chunk is the query,
  and the correct answer is that same chunk, ranked against all 566. This is the
  ceiling.
- **neighbour** — a whole chunk is the query, and the correct answer is the
  adjacent chunk of the same document. Related content, different words; this
  stands in for the paraphrase case, which is the one that actually matters when
  the asker words a thing differently from whoever saved it.

`document_id` is NULL on every row in this store, so adjacency comes from
`file_path` with rows in ingestion order.

## Results

Verbatim, recall@1 / recall@5:

| model | dim | RU | EN |
|---|---|---|---|
| all-MiniLM-L6-v2 | 384 | 0.582 / 0.749 | 0.655 / 0.870 |
| paraphrase-multilingual-MiniLM-L12-v2 | 384 | 0.525 / 0.699 | 0.195 / 0.364 |
| **multilingual-e5-small** | 384 | **0.682 / 0.833** | **0.778 / 0.954** |

Neighbour (paraphrase proxy), recall@1 / recall@10:

| model | RU | EN |
|---|---|---|
| all-MiniLM-L6-v2 | 0.086 / 0.460 | 0.174 / 0.674 |
| paraphrase-multilingual-MiniLM-L12-v2 | 0.122 / 0.597 | 0.141 / 0.696 |
| **multilingual-e5-small** | **0.259 / 0.687** | **0.342 / 0.859** |

## Findings

**The obvious swap is a regression.** `paraphrase-multilingual-MiniLM-L12-v2` —
the candidate the brief named as nearly free because it keeps 384 dimensions —
loses to the incumbent on Russian verbatim recall and collapses on English
(0.195 vs 0.655 recall@1). Adopting it on intuition would have made retrieval
worse in both languages.

**The observed cosines were not low.** For `all-MiniLM-L6-v2` on this corpus a
true verbatim pair averages 0.525 (RU) and a random pair averages 0.197. A
near-verbatim match at 0.514 is therefore a *normal strong* match, not a weak
one. The brief read those numbers on the wrong scale.

**`multilingual-e5-small` wins everything.** Better in both languages on both
probes, and 3x better on Russian paraphrase recall@1 — which is the metric
closest to the failure the store actually suffers from. It is 384-dimensional, so
`vector(384)` and migration 0001 are untouched.

## Adopted 2026-08-08

`multilingual-e5-small` is now the configured model. The full sequence, in the
order it had to happen:

1. **Per-model profiles** (`rag_engine.MODEL_PROFILES`) carrying each model's
   asymmetric prefixes and its measured cosine anchors:

   | model | noise p95 | true median | `cos_lo` | `cos_hi` |
   |---|---|---|---|---|
   | all-MiniLM-L6-v2 | 0.3506 | 0.5401 | 0.2795 | 0.7533 |
   | multilingual-e5-small | 0.8639 | 0.8921 | 0.8533 | 0.9238 |

   `compose_final_scores` rescales cosine through these onto one shared scale
   (noise → 0.15, typical true match → 0.55), which is what lets a single
   calibrated threshold survive a model swap at all.

2. **Asymmetric prefixes.** `encode(text, kind=...)` applies `query: ` or
   `passage: `. Only three call sites needed marking: `save` embeds passages,
   `search` embeds queries.

3. **The safety guard.** `embedding_consistency()` compares stored
   `embedding_model` / `embedding_dimension` against the configured model, and
   `check_readiness` fails closed on a mismatch. Deliberately *not* wired into
   `_ensure_schema`: engine construction must still succeed mid-re-embed, since
   `reembed_corpus.py` is the thing that resolves the mismatch. Conflating the
   two made the repair path unreachable — found by hitting it.

4. **The re-embed.** `reembed_corpus.py` re-encoded all 1152 rows at ~12 rows/s
   with the server stopped. Resumable: it only touches rows that do not already
   match the configured model.

5. **Re-calibration.** The relevance floor moved from 0.65 to **0.30**, because
   the score scale changed with the model. See `docs/score-calibration.md`.

### What it bought

| | all-MiniLM-L6-v2 | multilingual-e5-small |
|---|---|---|
| correct chunk ranked first | 83.3% | **92.5%** |
| answerable p05 | 0.632 | 0.356 |
| unanswerable max | 0.649 | **0.187** |
| populations overlap? | **yes** | no |

The overlap line is the important one. Under MiniLM the worst answerable query
scored *below* the best unanswerable one, so no threshold could separate them and
any cut had to trade real recall for false confidence. Under E5 there is a clean
gap with nothing in it.

Measured after the swap on the live service: an on-corpus query that MiniLM
scored 0.6025 (escalating to a frontier model) now scores 0.617 against a 0.30
floor and is answered from the store; pure gibberish scores 0.161.
