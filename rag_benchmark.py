#!/usr/bin/env python3
"""
RAG Quality Benchmark Framework v1.0
Gold dataset & empirical evaluation for Universal RAG Infrastructure.
"""

import sys
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from rag_engine import RagEngine
from config import settings
from project_context import build_default_project_context

GOLD_DATASET = [
    {
        "query": "Как устроен MCP Server over stdio?",
        "category": "architecture",
        "expected_terms": ["stdio", "jsonrpc", "mcp_server.py"],
        "expect_found": True
    },
    {
        "query": "Какие таблицы существуют в базе PostgreSQL aiagent?",
        "category": "sql_schema",
        "expected_terms": ["project_knowledge", "kb_entities", "kb_relations", "agent_messages"],
        "expect_found": True
    },
    {
        "query": "How is HNSW index created for vector embeddings?",
        "category": "english_text",
        "expected_terms": ["hnsw", "vector", "embedding"],
        "expect_found": True
    },
    {
        "query": "Что делает функция dequeue_message с SKIP LOCKED?",
        "category": "specific_function",
        "expected_terms": ["dequeue_message", "skip locked", "agent_messages"],
        "expect_found": True
    },
    {
        "query": "Какая модель квантования используется на космодроме Марс-15?",
        "category": "missing_answer",
        "expected_terms": [],
        "expect_found": False
    }
]


def is_correct_abstention(result: dict) -> bool:
    """A failed search or an accepted irrelevant hit must never count as a pass."""
    if result.get("status") != "success":
        return False
    return not any(
        hit.get("final_score", hit.get("score", 0.0)) >= settings.relevance_floor
        for hit in result.get("results", [])
    )

def run_benchmark(k: int = 5) -> dict:
    engine = RagEngine()
    context = build_default_project_context(
        settings,
        working_directory=str(Path.cwd()),
        agent_id="rag-benchmark",
        session_id="benchmark",
        write_visibility="session-private",
        read_scope="shared",
    )
    total_queries = len(GOLD_DATASET)
    recalls = []
    precisions = []
    mrrs = []
    correct_abstentions = 0

    print(f"--- Starting RAG Quality Benchmark (Gold Dataset size={total_queries}) ---")

    for idx, item in enumerate(GOLD_DATASET, 1):
        q = item["query"]
        expected = item["expected_terms"]
        expect_found = item["expect_found"]

        res = engine.search(context, q, limit=k)
        results = res.get("results", [])

        if not expect_found:
            if is_correct_abstention(res):
                correct_abstentions += 1
                recalls.append(1.0)
                precisions.append(1.0)
                mrrs.append(1.0)
            else:
                recalls.append(0.0)
                precisions.append(0.0)
                mrrs.append(0.0)
            continue

        matched_ranks = []
        found_expected_count = 0

        for r_idx, r in enumerate(results, 1):
            content_lower = r["content"].lower()
            match = any(t.lower() in content_lower for t in expected)
            if match:
                matched_ranks.append(r_idx)
                found_expected_count += 1

        hit = len(matched_ranks) > 0
        recall = 1.0 if hit else 0.0
        precision = (found_expected_count / len(results)) if results else 0.0
        mrr = (1.0 / matched_ranks[0]) if matched_ranks else 0.0

        recalls.append(recall)
        precisions.append(precision)
        mrrs.append(mrr)

        print(f"[{idx}/{total_queries}] Query: '{q}' | Recall@{k}: {recall:.2f} | MRR: {mrr:.2f} | TopScore: {res.get('top_score', 0.0):.4f}")

    avg_recall = float(sum(recalls) / total_queries) if total_queries else 0.0
    avg_precision = float(sum(precisions) / total_queries) if total_queries else 0.0
    avg_mrr = float(sum(mrrs) / total_queries) if total_queries else 0.0
    abstention_rate = float(correct_abstentions / max(1, sum(1 for d in GOLD_DATASET if not d["expect_found"])))

    metrics = {
        f"Recall@{k}": round(avg_recall, 4),
        f"Precision@{k}": round(avg_precision, 4),
        "MRR": round(avg_mrr, 4),
        "correct_abstention_rate": round(abstention_rate, 4),
        "total_evaluated": total_queries
    }
    engine.close()
    return metrics

if __name__ == "__main__":
    report = run_benchmark(k=5)
    print("\n=== Benchmark Final Results ===")
    print(json.dumps(report, indent=2, ensure_ascii=False))
