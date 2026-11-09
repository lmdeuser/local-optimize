"""Read-only HTTP evaluation; reports never contain retrieved document text."""
import argparse
import json
import math
import time
from pathlib import Path

from config import settings
from project_context import build_default_project_context
from rag_client import RagClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path(__file__).parent / "evaluation/retrieval_cases.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    cases_path = args.cases.resolve(strict=True)
    output_path = args.output.resolve()
    if not cases_path.is_relative_to(root) or not output_path.is_relative_to(root / "artifacts"):
        parser.error("Cases must be inside the project; reports must be under artifacts/")
    cases = json.loads(cases_path.read_text())
    if len(cases) > 100:
        parser.error("At most 100 cases per bounded pass")
    context = build_default_project_context(settings, agent_id="retrieval-evaluation",
                                            session_id="retrieval-evaluation-20261029", read_scope="shared")
    rows = []
    for case in cases:
        start = time.monotonic()
        response = RagClient(context).search(case["query"], limit=5)
        hits = response.get("results", [])
        correct = any(
            "/tests/" not in hit.get("source_uri", "")
            and any(path in hit.get("source_uri", "") for path in case["paths"])
            and all(term in hit.get("content", "").casefold() for term in case["terms"])
            for hit in hits
        )
        rows.append({"id": case["id"], "split": case["split"],
                     "answerable": case["answerable"], "status": response.get("status"),
                     "expected_match": correct, "accepted": len(hits),
                     "seconds": round(time.monotonic() - start, 4),
                     "sources": [{"id": hit.get("chunk_id"), "source": hit.get("source_uri"),
                                  "score": hit.get("final_score"), "state": hit.get("source_state")}
                                 for hit in hits]})
        print(json.dumps({key: rows[-1][key] for key in ("id", "status", "expected_match", "accepted", "seconds")} ), flush=True)
    positive = [row for row in rows if row["answerable"]]
    negative = [row for row in rows if not row["answerable"]]
    durations = sorted(row["seconds"] for row in rows)
    summary = {"positive_hits": sum(row["expected_match"] for row in positive),
               "positive_total": len(positive),
               "negative_abstentions": sum(row["accepted"] == 0 and row["status"] == "success" for row in negative),
               "negative_total": len(negative),
               "p50_seconds": durations[len(durations) // 2],
               "p95_seconds": durations[math.ceil(len(durations) * .95) - 1]}
    output_path.write_text(json.dumps({"summary": summary, "cases": rows}, ensure_ascii=False, indent=2))
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
