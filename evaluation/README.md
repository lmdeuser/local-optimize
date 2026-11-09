# Retrieval acceptance

Run from the project root with the active user service. Reports and temporary files stay under the Git-ignored `artifacts/` directory:

```sh
mkdir -p artifacts/evaluation
export TMPDIR="$PWD/artifacts/evaluation"
.venv/bin/python evaluate_retrieval.py --output artifacts/evaluation/http-evaluation.json
```

The evaluator reads through the same RagClient/HTTP path as agents. It records IDs,
source paths, scores, source state and latency, never retrieved text. Positive
labels check source and terms; they are a reproducible proxy for relevance, not
proof of a complete or correct answer. The set includes ordinary Russian/English
questions, code identifiers, unrelated questions and misleading identifier matches.

The original 32 cases were used during development and are regression cases;
`split=heldout` denotes the original split, not an untouched final holdout. Five
`unseen` cases were added after the first model/policy pass. Documentation updates
are part of the tested corpus and must be disclosed when comparing runs. A label
correction on p19 accepts the operational documentation's actual worker contract.
Do not train or index this directory. Expand with independently judged real user
questions before making broader quality claims. No finite set proves perfection.
