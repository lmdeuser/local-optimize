# Knowledge lifecycle

How a fact enters this store, stays trustworthy, and leaves.

The store exists so agents stop re-deriving knowledge every session. That only
pays off if what comes back is right: a stale hit costs more than a miss, because
the agent acts on it confidently and the rework burns more than the lookup saved.
Everything below exists to keep that true as the store grows.

## States

A chunk's `validation_status` is its standing; its verification timestamps are its
age. They are independent — a fact can be authoritative and overdue.

| status | meaning | retrievable |
|---|---|---|
| `unverified` | written, never confirmed | yes (`authority_weight` 0.95) |
| `validated` | confirmed by tests run or behaviour observed | yes (1.1) |
| `authoritative` | canonical | yes (1.2) |
| `deprecated` | was true, superseded | **no** |
| `invalidated` | never true, or its source document was removed | **no** |

`freshness_status`, returned on every hit, is computed from
`COALESCE(verified_at, created_at)`:

| value | meaning |
|---|---|
| `fresh` | confirmed within 7 days |
| `recent` | within 30 days |
| `stale` | older than 30 days |
| `overdue` | past its own declared `recheck_after_days` |
| `unknown` | no timestamp at all |

## Operations

**`save`** writes a fact. Identical content lands on one row via `dedup_key`
(`project_id | content_hash | visibility | owner_agent | owner_session` — note
`validation_status` is deliberately *not* in the key). A re-save updates the
status in place, except over a retired fact: retirement is sticky, because agents
re-save what they learn every session and an unconditional overwrite would give a
correction a shelf life of exactly one routine save.

**`deprecate`** retires a fact, addressing the row that already exists. Saving a
correction cannot do this — the corrected fact is worded differently, so it hashes
differently, so it lands as a new row while the wrong one keeps being retrieved
and outranks the correction whenever it is older or carries higher standing.
Reports `updated: 0` when nothing matched; believing a retraction landed when it
did not is the failure the operation exists to prevent.

**`verify`** records that a fact was re-checked and still holds, as of now. Age is
measured from the last confirmation, so a fact re-checked today stops being
demoted for having been written months ago. `recheck_after_days` declares how
long the confirmation is good for.

**`stale_report`** lists what is overdue, worst first, plus optionally an
`ageing` population that never declared an interval but is old and important
enough to be worth a look.

**`memory_optimize`** reports near-duplicate pairs by cosine similarity, bounded
by `DUPLICATE_NEIGHBOURS` per row and `DUPLICATE_PAIR_LIMIT` overall. With
`apply=true` it retires the later copy as `deprecated` with a `superseded_by`
link. It never deletes — provenance survives.

## Why expiry is declared, not detected

A large class of stored facts describe *other people's* systems: DOM selectors,
undocumented endpoints, UI behaviour. Their expiry is invisible from this
machine — nothing here can observe that a vendor shipped a redesign. So it cannot
be detected; it can only be declared in advance and then re-confirmed.

Nothing expires automatically. An old fact is not a wrong fact, and silently
dropping or demoting it would trade a stale hit for a silent miss, which is
harder to notice and harder to debug. `overdue` raises a flag the caller can see
and act on; the hit is still returned.

## Ranking interaction

`recency_decay` and `authority_weight` scale a score the ranking terms already
earned — they cannot manufacture relevance. Neither notices that a fact stopped
being true, which is exactly why `deprecate` filters rather than down-ranks:
down-ranking a wrong fact still returns it, just slightly lower.

See `docs/score-calibration.md` for the score itself.
