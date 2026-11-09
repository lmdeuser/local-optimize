#!/usr/bin/env python3
"""Re-embed the corpus with the currently configured embedding model.

Swapping the embedding model is an operation, not an edit. Query and stored
vectors are only comparable when one model produced both, and mixing them raises
nothing at all — pgvector measures the cosine between two unrelated embedding
spaces perfectly happily and returns confident, well-scored nonsense. So there is
necessarily a window, between configuring the new model and finishing the
re-embed, where retrieval would be silently wrong.

`RagEngine.check_readiness` fails closed during that window, so the server
refuses to serve rather than answering from a half-converted store. Run this with
the server stopped and start it again afterwards.

Resumable: only rows that do not already match the configured model are touched,
so an interrupted run can simply be repeated. Content is never modified — only
the vector and the provenance columns describing which model produced it.

    python3 reembed_corpus.py --dry-run
    python3 reembed_corpus.py

"""
import argparse
import sys
import time

import psycopg2
from psycopg2.extras import execute_batch

from config import settings
from rag_engine import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL_NAME,
    EMBEDDING_VERSION,
    RagEngine,
    get_model_profile,
)


def pending_count(cur) -> int:
    cur.execute(
        "SELECT count(*) FROM public.project_knowledge "
        "WHERE content IS NOT NULL "
        "  AND (embedding IS NULL OR embedding_model IS DISTINCT FROM %s "
        "       OR embedding_dimension IS DISTINCT FROM %s)",
        (EMBEDDING_MODEL_NAME, EMBEDDING_DIM),
    )
    return int(cur.fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change and exit")
    args = parser.parse_args()

    profile = get_model_profile()
    print(f"configured model : {EMBEDDING_MODEL_NAME} (dim {EMBEDDING_DIM}, "
          f"version {EMBEDDING_VERSION})")
    print(f"passage prefix   : {profile['passage_prefix']!r}")
    print(f"cosine anchors   : lo={profile['cos_lo']} hi={profile['cos_hi']}")
    if profile is None or (profile["cos_lo"] == 0.0 and profile["cos_hi"] == 1.0
                           and EMBEDDING_MODEL_NAME not in ("all-MiniLM-L6-v2",)):
        print("\nREFUSING: this model has no measured profile. Run "
              "probes/derive_model_anchors.py and register it in MODEL_PROFILES "
              "first, or the calibrated relevance floor will be meaningless.",
              file=sys.stderr)
        return 2

    conn = psycopg2.connect(**settings.get_db_params())
    try:
        with conn.cursor() as cur:
            total = pending_count(cur)
            cur.execute("SELECT count(*) FROM public.project_knowledge")
            grand_total = int(cur.fetchone()[0])
        print(f"rows to re-embed : {total} of {grand_total}")
        if args.dry_run:
            return 0
        if total == 0:
            print("nothing to do — the store already matches the configured model")
            return 0

        engine = RagEngine()
        started = time.time()
        done = 0
        while True:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, content FROM public.project_knowledge "
                    "WHERE content IS NOT NULL "
                    "  AND (embedding IS NULL OR embedding_model IS DISTINCT FROM %s "
                    "       OR embedding_dimension IS DISTINCT FROM %s) "
                    "ORDER BY id LIMIT %s",
                    (EMBEDDING_MODEL_NAME, EMBEDDING_DIM, args.batch_size),
                )
                rows = cur.fetchall()
            if not rows:
                break

            updates = []
            for chunk_id, content in rows:
                vector = engine.encode(content, kind="passage").tolist()
                if len(vector) != EMBEDDING_DIM:
                    print(f"\nABORT: model produced {len(vector)} dims, expected "
                          f"{EMBEDDING_DIM}. The vector column would reject this.",
                          file=sys.stderr)
                    return 3
                updates.append((vector, EMBEDDING_MODEL_NAME, EMBEDDING_VERSION,
                                EMBEDDING_DIM, chunk_id))

            with conn.cursor() as cur:
                execute_batch(
                    cur,
                    "UPDATE public.project_knowledge "
                    "SET embedding = %s::vector, embedding_model = %s, "
                    "    embedding_version = %s, embedding_dimension = %s "
                    "WHERE id = %s",
                    updates,
                )
            conn.commit()
            done += len(rows)
            rate = done / max(1e-6, time.time() - started)
            print(f"\r  {done}/{total} rows  ({rate:.1f}/s)", end="", flush=True)

        print()
        engine.close()

        with conn.cursor() as cur:
            remaining = pending_count(cur)
            cur.execute(
                "SELECT embedding_model, embedding_dimension, count(*) "
                "FROM public.project_knowledge WHERE embedding IS NOT NULL "
                "GROUP BY 1, 2 ORDER BY 3 DESC"
            )
            populations = cur.fetchall()
        print(f"remaining        : {remaining}")
        for model, dim, count in populations:
            print(f"  {model} ({dim}d): {count} rows")
        if remaining:
            print("\nStore is NOT uniform — the server will refuse to serve. "
                  "Re-run to finish.", file=sys.stderr)
            return 1
        print("\nStore is uniform. Re-run probes/calibrate_threshold.py — the "
              "relevance floor was measured against the previous model.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
