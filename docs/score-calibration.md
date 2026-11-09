# Score calibration

## Current serving policy (2026-10-29)

The measurements and formula below now describe candidate selection only.
Final serving relevance is scored by the local Intel-XPU Qwen reranker against
the original query. The versioned policy is `retrieval_policy.json`: minimum 0.5,
identifier-property gate 0.05, at most 24 candidates plus up to 8 reserved document candidates, 768 tokens per pair.
Queries beyond the 384-token question budget fail closed instead of losing their
trailing constraints. A score is not a calibrated probability that a claim is true.
Changing policy requires the positive and adversarial evaluation set, not just
verbatim snippets from the corpus. Exact-only caching preserves that decision.
The previous 96.7% figure is historical and is not a current quality claim.

## Historical E5 candidate calibration

`final_score` is a published number. `gpu_rag_server` turns it into a `route`,
`fast_router` escalates to a frontier model below it, `ProjectContext.min_score`
filters on it, and the operator rule at `~/.claude/rules/local-rag.md` tells
agents when a hit is worth trusting. All four read it as an absolute judgement of
"did we find anything", so it has to mean the same thing from one query to the
next.

Measured 2026-08-08 against the live `aiagent` store with
`multilingual-e5-small` (adopted the same day; see
`docs/embedding-model-evaluation.md`). The earlier `all-MiniLM-L6-v2` figures are
kept below for comparison, because they show why the cut moved.

## The formula

Implemented in `rag_engine.compose_final_scores`:

```
semantic = clip((cosine - cos_lo) / (cos_hi - cos_lo), 0, 1)
base     = SCORE_ALPHA * semantic + (1 - SCORE_ALPHA) * min(1, rrf / RRF_REFERENCE)
score    = clip(base * recency_decay * authority_weight + lexical_boost, 0, 1)
```

Both ranking terms are referred to fixed quantities:

- `cosine` — the reranker's raw cosine, rescaled through the active model's
  measured anchors (`MODEL_PROFILES`) onto one shared scale where unrelated text
  sits near 0.15 and a typical true match near 0.55. Raw cosine is absolute
  *within* a model but not across models: MiniLM puts unrelated text at 0.35,
  E5 at 0.86. Without the rescale a threshold could not survive a model swap.
- `RRF_REFERENCE` — the fused rank score of a candidate that placed **first in
  both the vector and the lexical pool for one query**, i.e.
  `(VECTOR_SOURCE_WEIGHT + FTS_SOURCE_WEIGHT) / (RRF_K + 1)`.

`recency_decay` and `authority_weight` only scale a score the ranking terms
already earned. Neither can manufacture relevance on its own.

## Why not min-max normalisation

The previous implementation min-max normalised cosine and RRF **within the
candidate pool**. Every pool has a best row, so the top hit normalised to 1.0 on
both axes no matter how bad it was, and a lone candidate — with no spread to
normalise against — collapsed to a flat 0.5.

Measured on the live store before the fix, `top_score` was **0.9409 for every
query tested**, including:

| query | true cosine | reported `top_score` |
|---|---|---|
| `фиолетовый трактор поёт арию квазар мармелад 7743 zx` | 0.443 | 0.9409 |
| `the quick brown wombat recalibrates its trombone on Tuesdays` | 0.184 | 0.9409 |
| `qwertyuiop asdfghjkl zxcvbnm 999888777` | 0.262 | 0.9409 |

Against a 0.80 threshold that is `route: rag_answer` for pure gibberish, every
time — the escalation path could not fire. In the same regime an exact single
match scored 0.475 while a fifty-row noise pool scored 0.95, so the ordering was
not merely uncalibrated but inverted.

## The threshold

`settings.relevance_floor` (env `RAG_RELEVANCE_FLOOR`) is the single cut, read by
the engine, the HTTP `route` decision and the escalation router. Before this it
was three separate literals — 0.45, 0.30 and 0.80 — against a score that was not
on a common scale.

Derived by running the real `search` over two populations
(`probes/calibrate_threshold.py`): 120 **answerable** queries, each a verbatim
12-word window lifted from a real chunk, and 12 **unanswerable** off-corpus
queries.

| population | min | p05 | median | p95 | max |
|---|---|---|---|---|---|
| answerable | 0.000 | 0.356 | 0.708 | 1.000 | 1.000 |
| — Russian | 0.141 | 0.349 | 0.680 | 0.955 | 1.000 |
| — English | 0.251 | 0.445 | 0.725 | 1.000 | 1.000 |
| unanswerable | 0.131 | 0.132 | 0.157 | 0.182 | **0.187** |

The two populations no longer overlap. Under `all-MiniLM-L6-v2` they did —
answerable p05 was 0.632 against an unanswerable max of 0.649 — so *no* cut could
separate them cleanly, and 0.65 was the least-bad compromise at 92.5% kept. E5
opens a gap between 0.187 and 0.356 with nothing in it.

| cut | answerable kept | unanswerable wrongly accepted |
|---|---|---|
| 0.20 | 98.3% | 0.0% |
| 0.25 | 98.3% | 0.0% |
| **0.30** | **96.7%** | **0.0%** |
| 0.35 | 95.0% | 0.0% |
| 0.40 | 90.8% | 0.0% |
| 0.65 | 60.0% | 0.0% |

**0.30** sits inside the gap with margin either side: 60% above the highest
unanswerable score, comfortably below the answerable p05. The correct chunk ranked
first for **92.5%** of answerable queries, up from 83.3% under MiniLM.

## Changing it

The threshold is meaningful only on the scale it was measured against. Re-run
`probes/calibrate_threshold.py` (kept in
`~/.local/state/local_optimize/probes/`) after any change to the formula, the
fusion weights, or the embedding model, and update the tables above with what it
prints. `tests/unit/test_search_score_calibration.py` guards the properties the
scale depends on; it does not know the corpus, so it cannot notice that the
numbers went stale.

Changing the embedding model needs one extra step before that: re-derive its
anchors with `probes/derive_model_anchors.py` and register them in
`rag_engine.MODEL_PROFILES`. An unregistered model falls back to the identity
map, which passes raw cosine straight through — and since raw cosine sits in a
completely different band per model, the published cut would then mean nothing.
`tests/unit/test_model_profile.py` asserts that the configured model is
registered and that noise and true matches land either side of the floor for
every registered model, so this fails loudly rather than silently.

## Cross-encoder reranking: measured, and rejected (2026-08-21)

The reranker this engine ships with computes cosine between the same two vectors
candidate selection already compared. It cannot disagree with the selection and
adds no information to it — the name does more work than the code. Replacing it
with a cross-encoder, which reads `(query, passage)` jointly, is the obvious
upgrade. It was built, measured against this store, and removed.

**Recorded because the first measurement said the opposite.** Calibrating with
this repository's usual method — a "true" pair being a verbatim 12-word window
lifted out of the passage — `bge-reranker-base` reported noise p95 `0.0000`
against a true median of `0.9987`. A separation of 0.999 where the embedding
manages 0.028 looks like a decisive win. It was a measurement of copied text: a
cross-encoder can recognize repeated words perfectly while being unable to
connect a question to the paragraph that answers it. **A verbatim-window
population is a valid probe for an embedding and a trap for a reranker.**

`probes/rerank_paraphrase_probe.py` asks the question production asks: a query
somebody would type, against the chunk that answers it, worded differently, with
same-domain wrong answers as the hard negatives. On 16 such pairs from this
store's own content:

| scorer | worst relevant | best irrelevant | margin |
|---|---|---|---|
| multilingual-e5-small (cosine) | 0.7685 | 0.7959 | −0.027 |
| bge-reranker-base | 0.0000 | 0.0002 | −0.0001 |
| bge-reranker-v2-m3 | 0.0000 | 0.0001 | −0.0001 |

Both rerankers collapse every relevant pair to near zero. The invocation is not
at fault — checked against pairs from their own training distribution, where they
behave exactly as advertised, in Russian as well as English:

| pair | bge-reranker-base | bge-reranker-v2-m3 |
|---|---|---|
| "what is a panda" / panda definition | 0.9973 | 0.9107 |
| "что такое панда" / определение панды | 0.9863 | 0.8448 |
| "what is a panda" / unrelated text | 0.0001 | 0.0002 |
| "how is the search cache invalidated" / the paragraph explaining generation-counter invalidation | **0.0010** | **0.0035** |

That last row is the whole finding. These models are trained on MS MARCO-style
web factoids — "what is X" answered by a passage that states what X is. This
store is engineering documentation and design rationale, where the questions are
"how" and "why" and the answering paragraph shares almost no vocabulary with the
question. It is the case they are weakest on, and it is the only case this store
has.

So the feature is gone rather than shipped disabled: a dormant, measurably
harmful path behind a flag is debt, not optionality. What is kept is the
instrument — `probes/rerank_paraphrase_probe.py` and
`probes/calibrate_reranker.py` — so that the next candidate model is judged by
the question production asks rather than by the one that flatters it.

Worth trying next, in this order: a reranker trained on technical or instruction
data rather than web search; a bi-encoder reranker fine-tuned on this corpus;
or, cheapest of all, leaving retrieval alone and spending the same effort on the
chunking, since the cosine baseline overlaps here too (−0.027) and that overlap
is upstream of any reranker.

## Re-derivation, 2026-08-21

`probes/calibrate_threshold.py` was referenced by this document but absent from
the repository; it is reconstructed from the method recorded above. Run on the
current store (100 answerable windows, 12 unanswerable, cosine ranking):

| population | min | p05 | median | p95 | max |
|---|---|---|---|---|---|
| answerable | 0.255 | 0.384 | 0.708 | 1.000 | 1.000 |
| unanswerable | 0.148 | 0.151 | 0.160 | 0.193 | **0.238** |

| cut | answerable kept | unanswerable wrongly accepted |
|---|---|---|
| 0.20 | 100.0% | 8.3% |
| 0.25 | 100.0% | 0.0% |
| **0.30** | **97.0%** | **0.0%** |
| 0.40 | 91.0% | 0.0% |

The gap is (0.238, 0.384) and **0.30 still sits inside it**, keeping 97.0% of
answerable queries against 96.7% at the original measurement. The correct chunk
ranked first for 86.0%. So nothing in the caching, formatting or invalidation
work moved the scale — which is what this run was for.

**The floor has not been re-derived against the cross-encoder scale.** Enabling
it requires running `probes/calibrate_threshold.py` again with it active. The
arithmetic says the gap should widen rather than close — noise maps to the same
0.150 either way, while answerable p05 rises from 0.384 to roughly 0.535·0.7 —
but "should" is not a measurement.

### A search that a query could kill

Found while re-deriving the floor, and worth recording because the trigger is
ordinary: `websearch_to_tsquery` compiles into a conjunction with a bounded parse
stack, and a 12-word window lifted from an indexed shell script containing a rule
of seventy dashes overflowed it. `tsquery stack too small` aborts the **whole**
hybrid search, not just its lexical half, so the query returned `status: error`.

Anyone pasting a log line or a separator row would have hit it. Fixed in two
places, because prevention and containment are different things:
`sanitize_fts_query` collapses runs of three or more identical punctuation
characters and caps the term count at `RAG_FTS_MAX_TERMS` (48 — a conjunction of
fifty terms matches nothing anyway), and a lexical pool that still fails is
caught, rolled back and skipped so the vector pool answers alone.
