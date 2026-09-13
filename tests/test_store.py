import numpy as np
import pytest

from memos.store import Store


class Encoder:
    def query(self, text):
        text = text.lower()
        return np.array([1 + text.count("sqlite"), 1 + text.count("network"), 1], dtype=np.float32)

    def passages(self, texts):
        return [self.query(text) for text in texts]


def test_post_search_rebuild(tmp_path):
    store = Store(tmp_path, Encoder())
    memo = store.post("SQLite fix", "Resolved sqlite locking", "/repo", "laptop.tail.ts.net")
    store.post("Network issue", "Unresolved network outage", "/other", "desktop.tail.ts.net")
    result = store.search("sqlite", project="/repo", device="laptop.tail.ts.net")
    assert len(result) == 1
    assert result[0]["id"] == memo["id"]
    assert "Resolved" in result[0]["body"]
    assert store.search("sqlite", device="missing") == []
    assert store.search("sqlite")[0]["id"] == memo["id"]
    assert store.reindex() == 2
    assert store.search("sqlite")[0]["id"] == memo["id"]
    (tmp_path / "memos" / f"{memo['id']}.md").unlink()
    assert store.reindex() == 1
    assert store.search("sqlite", project="/repo") == []


def test_validation_and_literal_queries(tmp_path):
    store = Store(tmp_path, Encoder())
    with pytest.raises(ValueError):
        store.post("", "body", "/repo", "device")
    with pytest.raises(ValueError):
        store.search("query", limit=100)
    with pytest.raises(ValueError):
        store.search(" ")
    store.post("Title -->", "body", "/repo", "device")
    assert store.reindex() == 1
    assert len(store.search('" OR * NEAR ()')) == 1
