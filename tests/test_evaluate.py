import json
from pathlib import Path

import numpy as np

from memos.evaluate import evaluate_corpus
from memos.store import Store


class Encoder:
    TERMS = ("sqlite", "badge", "railway", "deploy", "import", "photosynthesis")

    def query(self, text):
        lowered = text.lower()
        vector = np.array([lowered.count(term) for term in self.TERMS], dtype=np.float32)
        if not vector.any():
            vector[0] = 0.01
        return vector

    def passages(self, texts):
        return [self.query(text) for text in texts]


def test_checked_in_retrieval_corpus(tmp_path):
    corpus_path = Path(__file__).resolve().parents[1] / "memos" / "eval" / "retrieval_cases.json"
    corpus = json.loads(corpus_path.read_text())
    store = Store(tmp_path, Encoder())
    result = evaluate_corpus(store, corpus)
    assert result["summary"]["mean_recall"] >= 0.75
    assert result["summary"]["mean_precision"] >= 0.75
    assert result["summary"]["mean_duplicate_rate"] == 0
    assert result["summary"]["mean_context_chars"] < 1000
    assert result["improvement"]["recall"] > 0
    assert result["improvement"]["precision"] > 0
