from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from .store import Store, normalize


def rrf_baseline(store: Store, query: str, project: str | None, limit: int) -> list[str]:
    """Reproduce the v1 nearest-neighbor RRF behavior for comparison."""
    rows = store.db.execute(
        "SELECT id FROM memos WHERE (? IS NULL OR project=?)", (project, project)
    ).fetchall()
    candidates = {row["id"] for row in rows}
    if not candidates:
        return []
    vector = normalize(store.encoder.query(query))
    semantic = {}
    for row in store.db.execute("SELECT memo_id, vector FROM chunks"):
        if row["memo_id"] in candidates:
            score = float(vector @ np.frombuffer(row["vector"], dtype=np.float32))
            semantic[row["memo_id"]] = max(semantic.get(row["memo_id"], -1), score)
    tokens = re.findall(r"\w+", query)
    lexical = []
    if tokens:
        expression = " OR ".join(f'"{token}"' for token in tokens)
        lexical = [
            row[0] for row in store.db.execute(
                "SELECT id FROM memo_fts WHERE memo_fts MATCH ? ORDER BY rank", (expression,)
            ) if row[0] in candidates
        ]
    scores = {}
    for ranking in (sorted(semantic, key=semantic.get, reverse=True), lexical):
        for rank, memory_id in enumerate(ranking, 1):
            scores[memory_id] = scores.get(memory_id, 0) + 1 / (60 + rank)
    return sorted(scores, key=scores.get, reverse=True)[:limit]


def evaluate_corpus(store: Store, corpus: dict[str, Any]) -> dict[str, Any]:
    title_to_id = {}
    for document in corpus["memories"]:
        memo = store.post(
            document["title"], document["body"], document.get("project", "/evaluation"),
            document.get("device", "evaluation-device"), kind=document.get("kind", "note"),
            scope=document.get("scope", "project"), confidence=document.get("confidence", 0.8),
            importance=document.get("importance", 0.5), source="retrieval evaluation corpus",
            evidence=["checked-in expected relevance judgment"],
        )
        title_to_id[document["title"]] = memo["id"]

    cases = []
    recalls = precisions = duplicate_rates = context_chars = 0.0
    baseline_recalls = baseline_precisions = 0.0
    for case in corpus["queries"]:
        result = store.recall_context(
            case["task"], project=case.get("project"), device=case.get("device"),
            limit=case.get("limit", 5),
        )
        returned = [memory["id"] for memory in result["memories"]]
        baseline = rrf_baseline(store, case["task"], case.get("project"), case.get("limit", 5))
        expected = {title_to_id[title] for title in case.get("relevant_titles", [])}
        found = set(returned) & expected
        recall = len(found) / max(len(expected), 1) if expected else float(not returned)
        precision = len(found) / max(len(returned), 1) if expected else float(not returned)
        duplicate_rate = 0.0 if not returned else 1 - len(set(returned)) / len(returned)
        chars = sum(len(memory["excerpt"]) for memory in result["memories"])
        recalls += recall
        precisions += precision
        duplicate_rates += duplicate_rate
        context_chars += chars
        baseline_found = set(baseline) & expected
        baseline_recall = len(baseline_found) / max(len(expected), 1) if expected else float(not baseline)
        baseline_precision = len(baseline_found) / max(len(baseline), 1) if expected else float(not baseline)
        baseline_recalls += baseline_recall
        baseline_precisions += baseline_precision
        cases.append({
            "task": case["task"], "returned_titles": [
                next(title for title, memo_id in title_to_id.items() if memo_id == returned_id)
                for returned_id in returned
            ],
            "recall": round(recall, 3), "precision": round(precision, 3),
            "duplicate_rate": round(duplicate_rate, 3), "context_chars": chars,
            "baseline_recall": round(baseline_recall, 3),
            "baseline_precision": round(baseline_precision, 3),
        })
    count = max(len(cases), 1)
    summary = {
        "mean_recall": round(recalls / count, 3),
        "mean_precision": round(precisions / count, 3),
        "mean_duplicate_rate": round(duplicate_rates / count, 3),
        "mean_context_chars": round(context_chars / count, 1),
    }
    baseline_summary = {
        "mean_recall": round(baseline_recalls / count, 3),
        "mean_precision": round(baseline_precisions / count, 3),
    }
    return {
        "cases": cases,
        "summary": summary,
        "v1_rrf_baseline": baseline_summary,
        "improvement": {
            "recall": round(summary["mean_recall"] - baseline_summary["mean_recall"], 3),
            "precision": round(summary["mean_precision"] - baseline_summary["mean_precision"], 3),
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate the memory retrieval pipeline")
    parser.add_argument(
        "corpus", nargs="?",
        default=Path(__file__).resolve().parent / "eval" / "retrieval_cases.json",
        type=Path,
    )
    args = parser.parse_args()
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="agent-memos-eval-") as directory:
        store = Store(Path(directory))
        try:
            print(json.dumps(evaluate_corpus(store, corpus), indent=2))
        finally:
            store.close()


if __name__ == "__main__":
    main()
