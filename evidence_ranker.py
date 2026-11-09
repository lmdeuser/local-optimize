"""Local Intel XPU evidence scoring. No generation, cloud calls or CPU fallback."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from pathlib import Path

from query_analysis import query_facet

ROOT = Path(__file__).resolve().parent
POLICY = json.loads((ROOT / "retrieval_policy.json").read_text())
POLICY_HASH = hashlib.sha256(json.dumps(POLICY, sort_keys=True).encode()).hexdigest()


def select_candidates(hits: list[dict]) -> list[dict]:
    """Reserve recall for documentation without ignoring the main ranked pool."""
    selected = hits[:int(POLICY["candidate_limit"])]
    documents = [hit for hit in hits if hit.get("source_type") == "document"]
    return list({hit["chunk_id"]: hit for hit in selected + documents[:int(POLICY["document_candidates"]) ]}.values())


class EvidenceRanker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model = None
        self._tokenizer = None

    def warm_up(self) -> None:
        self.rank("What is a cache?", [{"chunk_id": 0, "content": "A cache reuses stored results."}], 1, 0)

    def _load(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if not torch.xpu.is_available():
            raise RuntimeError("Intel XPU is required for evidence ranking")
        path = (ROOT / POLICY["model_path"]).resolve(strict=True)
        if not path.is_relative_to(ROOT / "models"):
            raise ValueError("Evidence model must be a local project model")
        tokenizer = AutoTokenizer.from_pretrained(
            path, padding_side="left", local_files_only=True, trust_remote_code=False
        )
        model = AutoModelForCausalLM.from_pretrained(
            path, dtype=torch.float16, attn_implementation="sdpa",
            local_files_only=True, trust_remote_code=False,
        ).to("xpu").eval()
        if any(tensor.device.type != "xpu" for tensor in (*model.parameters(), *model.buffers())):
            raise RuntimeError("Evidence model is not fully on Intel XPU")
        self._prefix = tokenizer.encode(
            '<|im_start|>system\nJudge whether the Document meets the requirements based on '
            'the Query and the Instruct provided. Note that the answer can only be "yes" '
            'or "no".<|im_end|>\n<|im_start|>user\n', add_special_tokens=False,
        )
        self._suffix = tokenizer.encode(
            '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n', add_special_tokens=False,
        )
        self._labels = [tokenizer.convert_tokens_to_ids(label) for label in ("no", "yes")]
        self._tokenizer, self._model = tokenizer, model

    def _scores(self, query: str, hits: list[dict], deadline: float) -> list[float]:
        import torch

        rows = []
        if len(self._tokenizer.encode(query, add_special_tokens=False)) > 384:
            raise ValueError("Question exceeds the evidence model token budget")
        for hit in hits:
            text = (
                '<Instruct>: Given a technical question, retrieve passages that provide evidence '
                'answering it. Related names alone are not an answer.\n'
                f'<Query>: {query}\n<Document>: {hit["content"]}'
            )
            ids = self._tokenizer.encode(
                text, add_special_tokens=False, truncation=True,
                max_length=int(POLICY["max_length"]) - len(self._prefix) - len(self._suffix),
            )
            rows.append(self._prefix + ids + self._suffix)
        scores = []
        batch_size = int(POLICY["batch_size"])
        with torch.inference_mode():
            for start in range(0, len(rows), batch_size):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Evidence ranking deadline exceeded")
                batch = self._tokenizer.pad(
                    {"input_ids": rows[start:start + batch_size]}, padding=True, return_tensors="pt"
                ).to("xpu")
                logits = self._model(**batch, use_cache=False, logits_to_keep=1).logits[:, -1, self._labels].float()
                scores.extend(torch.softmax(logits, dim=-1)[:, 1].cpu().tolist())
        if any(not math.isfinite(score) for score in scores):
            raise RuntimeError("Non-finite evidence score")
        return scores

    def rank(self, query: str, hits: list[dict], limit: int, min_score: float) -> list[dict]:
        """Scores estimate relevance, never independent verification of a claim."""
        if not re.search(r"\b(?:test\w*|тест\w*)", query, flags=re.IGNORECASE):
            hits = [hit for hit in hits if hit.get("source_kind") != "test_fixture"]
        if not hits:
            return []
        deadline = time.monotonic() + float(POLICY["deadline_seconds"])
        if not self._lock.acquire(timeout=float(POLICY["deadline_seconds"])):
            raise TimeoutError("Evidence ranking is occupied")
        try:
            self._load()
            candidates = select_candidates(hits)
            scores = self._scores(query, candidates, deadline)
            threshold = max(float(POLICY["relevance_threshold"]), min_score)
            selected = [(score, hit) for score, hit in zip(scores, candidates) if score >= threshold]
            facet = query_facet(query)
            facet_scores = self._scores(facet, [hit for _, hit in selected], deadline) if facet and selected else [1.0] * len(selected)
            results = []
            for (score, hit), facet_score in zip(selected, facet_scores):
                if facet_score < float(POLICY["facet_threshold"]):
                    continue
                item = dict(hit)
                item["candidate_score"] = item.get("final_score", 0.0)
                item["final_score"] = item["score"] = round(score, 4)
                item["match_method"] = "query_and_facet" if facet else "query"
                item["facet_score"] = round(facet_score, 4) if facet else None
                results.append(item)
            if time.monotonic() > deadline:
                raise TimeoutError("Evidence ranking deadline exceeded")
            return sorted(results, key=lambda hit: hit["final_score"], reverse=True)[:limit]
        finally:
            self._lock.release()

    def runtime_status(self) -> dict:
        if self._model is not None and any(p.device.type != "xpu" for p in (*self._model.parameters(), *self._model.buffers())):
            raise RuntimeError("Evidence model left Intel XPU")
        return {"backend": "torch-xpu", "loaded": self._model is not None,
                "policy": POLICY["version"], "cpu_fallback": False}
