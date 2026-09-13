from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

MODEL = "BAAI/bge-small-en-v1.5"


class Encoder:
    """Lazy loading keeps imports cheap; inference is local after the first download."""

    def __init__(self, cache: Path):
        from fastembed import TextEmbedding

        self.model = TextEmbedding(model_name=MODEL, cache_dir=str(cache))

    def passages(self, texts):
        return list(self.model.passage_embed(texts))

    def query(self, text):
        return next(iter(self.model.query_embed(text)))


def chunks(text: str):
    # Short overlapping chunks fit comfortably in the model's 512-token window.
    for start in range(0, len(text), 700):
        yield text[start:start + 900]


def normalize(vector):
    vector = np.asarray(vector, dtype=np.float32)
    return vector / max(float(np.linalg.norm(vector)), 1e-12)


class Store:
    def __init__(self, root: Path, encoder=None):
        self.root = root.expanduser().resolve()
        self.files = self.root / "memos"
        self.files.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.encoder = encoder or Encoder(self.root / "models")
        self.db = sqlite3.connect(self.root / "index.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS memos (
                id TEXT PRIMARY KEY, title TEXT, device TEXT, project TEXT,
                created_at TEXT, body TEXT, path TEXT
            );
            CREATE TABLE IF NOT EXISTS chunks (memo_id TEXT, vector BLOB);
            CREATE VIRTUAL TABLE IF NOT EXISTS memo_fts USING fts5(id UNINDEXED, text);
        """)

    def _index(self, metadata, body, path):
        text = f"{metadata['title']}\n{metadata['device']}\n{metadata['project']}\n{body}"
        vectors = self.encoder.passages(list(chunks(text)))
        memo_id = metadata["id"]
        with self.db:
            self.db.execute("DELETE FROM chunks WHERE memo_id=?", (memo_id,))
            self.db.execute("DELETE FROM memo_fts WHERE id=?", (memo_id,))
            self.db.execute(
                "INSERT OR REPLACE INTO memos VALUES (?, ?, ?, ?, ?, ?, ?)",
                (memo_id, metadata["title"], metadata["device"], metadata["project"],
                 metadata["created_at"], body, str(path)),
            )
            self.db.execute("INSERT INTO memo_fts VALUES (?, ?)", (memo_id, text))
            self.db.executemany("INSERT INTO chunks VALUES (?, ?)",
                                [(memo_id, normalize(v).tobytes()) for v in vectors])

    def post(self, title: str, body: str, project: str, device: str):
        values = {"title": title, "body": body, "project": project, "device": device}
        for key, value in values.items():
            if not value.strip():
                raise ValueError(f"{key} must not be empty")
            if len(value) > (32_000 if key == "body" else 1024):
                raise ValueError(f"{key} is too long")
        metadata = dict(id=uuid.uuid4().hex, title=title.strip(), project=project.strip(),
                        device=device.strip(), created_at=datetime.now(timezone.utc).isoformat())
        path = self.files / f"{metadata['id']}.md"
        document = ("<!-- memos: " + json.dumps(metadata, ensure_ascii=True).replace("-->", "\\u002d\\u002d>")
                    + " -->\n\n" + f"# {metadata['title']}\n\n{body}\n")
        with self.lock:
            # Index first; remove the entry if writing the canonical file fails.
            self._index(metadata, body, path)
            try:
                temp = path.with_suffix(".tmp")
                temp.write_text(document, encoding="utf-8")
                temp.replace(path)
            except Exception:
                with self.db:
                    self.db.execute("DELETE FROM memos WHERE id=?", (metadata["id"],))
                    self.db.execute("DELETE FROM chunks WHERE memo_id=?", (metadata["id"],))
                    self.db.execute("DELETE FROM memo_fts WHERE id=?", (metadata["id"],))
                raise
        return {**metadata, "path": str(path)}

    def reindex(self):
        # Startup rebuild makes Markdown authoritative, including manual edits/deletions.
        with self.lock:
            documents = []
            for path in sorted(self.files.glob("*.md")):
                header, content = path.read_text(encoding="utf-8").split("\n", 1)
                if not header.startswith("<!-- memos: ") or not header.endswith(" -->"):
                    raise ValueError(f"Invalid memo metadata: {path}")
                metadata = json.loads(header[len("<!-- memos: "):-len(" -->")])
                body = content.lstrip().removeprefix(f"# {metadata['title']}\n").lstrip()
                documents.append((metadata, body, path))
            with self.db:
                self.db.execute("DELETE FROM memos")
                self.db.execute("DELETE FROM chunks")
                self.db.execute("DELETE FROM memo_fts")
            for document in documents:
                self._index(*document)
        return len(documents)

    def search(self, query: str, project: str | None = None,
               device: str | None = None, limit: int = 5):
        if not query.strip() or len(query) > 4096:
            raise ValueError("query must contain 1–4096 characters")
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM memos WHERE (? IS NULL OR project=?) AND (? IS NULL OR device=?)",
                (project, project, device, device),
            ).fetchall()
            if not rows:
                return []
            candidates = {row["id"]: dict(row) for row in rows}
            vector = normalize(self.encoder.query(query))
            semantic = {}
            for row in self.db.execute("SELECT memo_id, vector FROM chunks"):
                memo_id = row["memo_id"]
                if memo_id in candidates:
                    score = float(vector @ np.frombuffer(row["vector"], dtype=np.float32))
                    semantic[memo_id] = max(semantic.get(memo_id, -1.0), score)
            tokens = re.findall(r"\w+", query, flags=re.UNICODE)
            lexical = []
            if tokens:
                expression = " OR ".join('"' + token + '"' for token in tokens)
                lexical = [row[0] for row in self.db.execute(
                    "SELECT id FROM memo_fts WHERE memo_fts MATCH ? ORDER BY rank", (expression,)
                ) if row[0] in candidates]
            # Reciprocal rank fusion: semantic recall plus exact names/error strings.
            scores = {}
            for ranking in (sorted(semantic, key=semantic.get, reverse=True), lexical):
                for rank, memo_id in enumerate(ranking, 1):
                    scores[memo_id] = scores.get(memo_id, 0) + 1 / (60 + rank)
            return [{**candidates[memo_id], "score": round(scores[memo_id], 6)}
                    for memo_id in sorted(scores, key=scores.get, reverse=True)[:limit]]
