from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

MODEL = "BAAI/bge-small-en-v1.5"

KINDS = {"note", "fact", "preference", "decision", "procedure", "incident", "open_question"}
SCOPES = {"global", "repository", "project", "device"}
STATUSES = {"active", "unverified", "expired", "superseded", "contradicted", "retracted"}
RELATION_TYPES = {"supersedes", "supports", "contradicts", "derived_from", "related_to"}

DEFAULT_CONFIDENCE = 0.5
DEFAULT_IMPORTANCE = 0.5
MAX_BODY_BYTES = 32_000
MAX_FIELD_BYTES = 1024

SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk|pk)-(?:live|test|proj)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"(?i)\b(?:password|passwd|api[_ -]?key|access[_ -]?token|client[_ -]?secret)\s*[:=]\s*[^\s]{8,}"
    ),
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def _words(text: str) -> set[str]:
    return set(re.findall(r"[\w.-]+", text.casefold(), flags=re.UNICODE))


def _content_hash(text: str) -> str:
    normalized = " ".join(re.findall(r"\w+", text.casefold(), flags=re.UNICODE))
    return hashlib.sha256(normalized.encode()).hexdigest()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class Store:
    def __init__(self, root: Path, encoder=None):
        self.root = root.expanduser().resolve()
        self.files = self.root / "memos"
        self.files.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.root / "audit.jsonl"
        self.lock = threading.RLock()
        self.encoder = encoder or Encoder(self.root / "models")
        self.db = sqlite3.connect(self.root / "index.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=OFF;
            CREATE TABLE IF NOT EXISTS memos (
                id TEXT PRIMARY KEY, title TEXT, device TEXT, project TEXT,
                created_at TEXT, body TEXT, path TEXT,
                updated_at TEXT, kind TEXT, scope TEXT, status TEXT,
                confidence REAL, importance REAL, expires_at TEXT, source TEXT,
                evidence TEXT, content_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS chunks (memo_id TEXT, vector BLOB);
            CREATE VIRTUAL TABLE IF NOT EXISTS memo_fts USING fts5(id UNINDEXED, text);
            CREATE TABLE IF NOT EXISTS relationships (
                source_id TEXT, relation TEXT, target_id TEXT, created_at TEXT,
                PRIMARY KEY (source_id, relation, target_id)
            );
            CREATE TABLE IF NOT EXISTS lifecycle_events (
                event_id TEXT PRIMARY KEY, memory_id TEXT, event_type TEXT,
                created_at TEXT, actor TEXT, source_interaction TEXT, reason TEXT,
                prior_state TEXT, resulting_state TEXT
            );
            CREATE TABLE IF NOT EXISTS retrieval_traces (
                trace_id TEXT PRIMARY KEY, created_at TEXT, task_hash TEXT,
                candidates TEXT, injected TEXT, ignored TEXT, outcome TEXT,
                useful_memory_ids TEXT, latency_ms REAL
            );
            CREATE TABLE IF NOT EXISTS metrics (
                name TEXT PRIMARY KEY, value REAL NOT NULL DEFAULT 0
            );
        """)
        self._migrate_legacy_schema()
        self._worker_stop = threading.Event()
        self._worker: threading.Thread | None = None

    def _migrate_legacy_schema(self):
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(memos)")}
        additions = {
            "updated_at": "TEXT", "kind": "TEXT", "scope": "TEXT", "status": "TEXT",
            "confidence": "REAL", "importance": "REAL", "expires_at": "TEXT",
            "source": "TEXT", "evidence": "TEXT", "content_hash": "TEXT",
        }
        with self.db:
            for name, sql_type in additions.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE memos ADD COLUMN {name} {sql_type}")

    @staticmethod
    def _metadata_defaults(metadata: dict[str, Any], body: str) -> dict[str, Any]:
        created_at = metadata.get("created_at") or utcnow()
        relationships = metadata.get("relationships", [])
        if not isinstance(relationships, list):
            relationships = []
        return {
            **metadata,
            "updated_at": metadata.get("updated_at") or created_at,
            "kind": metadata.get("kind") or "note",
            "scope": metadata.get("scope") or "project",
            "status": metadata.get("status") or "active",
            "confidence": float(metadata.get("confidence", DEFAULT_CONFIDENCE)),
            "importance": float(metadata.get("importance", DEFAULT_IMPORTANCE)),
            "expires_at": metadata.get("expires_at"),
            "source": metadata.get("source") or "legacy",
            "evidence": metadata.get("evidence") or [],
            "relationships": relationships,
            "content_hash": metadata.get("content_hash") or _content_hash(body),
        }

    @staticmethod
    def _validate_relationships(relationships: list[dict[str, str]]):
        for relation in relationships:
            if not isinstance(relation, dict):
                raise ValueError("relationships must contain mappings")
            if relation.get("type") not in RELATION_TYPES:
                raise ValueError(f"unsupported relationship type: {relation.get('type')!r}")
            target = relation.get("target_id")
            if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target):
                raise ValueError("relationship target_id is invalid")

    @staticmethod
    def _validate_metadata(metadata: dict[str, Any]):
        if not isinstance(metadata["kind"], str) or metadata["kind"] not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}")
        if not isinstance(metadata["scope"], str) or metadata["scope"] not in SCOPES:
            raise ValueError(f"scope must be one of {sorted(SCOPES)}")
        if not isinstance(metadata["status"], str) or metadata["status"] not in STATUSES:
            raise ValueError(f"status must be one of {sorted(STATUSES)}")
        for field in ("confidence", "importance"):
            if not 0 <= float(metadata[field]) <= 1:
                raise ValueError(f"{field} must be between 0 and 1")
        if metadata.get("expires_at") and _parse_time(metadata["expires_at"]) is None:
            raise ValueError("expires_at must be an ISO-8601 timestamp")
        Store._validate_relationships(metadata.get("relationships", []))

    def _index(self, metadata, body, path):
        metadata = self._metadata_defaults(metadata, body)
        self._validate_metadata(metadata)
        relation_text = " ".join(
            f"{rel['type']} {rel['target_id']}" for rel in metadata["relationships"]
        )
        text = "\n".join([
            metadata["title"], metadata["device"], metadata["project"], metadata["kind"],
            metadata["scope"], metadata["status"], relation_text, body,
        ])
        vectors = self.encoder.passages(list(chunks(text)))
        memo_id = metadata["id"]
        with self.db:
            self.db.execute("DELETE FROM chunks WHERE memo_id=?", (memo_id,))
            self.db.execute("DELETE FROM memo_fts WHERE id=?", (memo_id,))
            self.db.execute("DELETE FROM relationships WHERE source_id=?", (memo_id,))
            self.db.execute(
                """INSERT OR REPLACE INTO memos
                   (id, title, device, project, created_at, body, path, updated_at, kind,
                    scope, status, confidence, importance, expires_at, source, evidence, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    memo_id, metadata["title"], metadata["device"], metadata["project"],
                    metadata["created_at"], body, str(path), metadata["updated_at"],
                    metadata["kind"], metadata["scope"], metadata["status"],
                    metadata["confidence"], metadata["importance"], metadata["expires_at"],
                    metadata["source"], json.dumps(metadata["evidence"], ensure_ascii=True),
                    metadata["content_hash"],
                ),
            )
            self.db.execute("INSERT INTO memo_fts VALUES (?, ?)", (memo_id, text))
            self.db.executemany(
                "INSERT INTO chunks VALUES (?, ?)",
                [(memo_id, normalize(vector).tobytes()) for vector in vectors],
            )
            self.db.executemany(
                "INSERT OR REPLACE INTO relationships VALUES (?, ?, ?, ?)",
                [
                    (memo_id, relation["type"], relation["target_id"], metadata["created_at"])
                    for relation in metadata["relationships"]
                ],
            )

    def _increment_metric(self, name: str, amount: float = 1):
        self.db.execute(
            """INSERT INTO metrics(name, value) VALUES (?, ?)
               ON CONFLICT(name) DO UPDATE SET value=value+excluded.value""",
            (name, amount),
        )

    def _append_event(
        self,
        memory_id: str,
        event_type: str,
        *,
        actor: str = "agent-memos",
        source_interaction: str | None = None,
        reason: str = "",
        prior_state: Any = None,
        resulting_state: Any = None,
    ) -> dict[str, Any]:
        event = {
            "event_id": uuid.uuid4().hex,
            "memory_id": memory_id,
            "event_type": event_type,
            "created_at": utcnow(),
            "actor": actor,
            "source_interaction": source_interaction,
            "reason": reason,
            "prior_state": prior_state,
            "resulting_state": resulting_state,
        }
        with self.audit_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=True) + "\n")
        self.db.execute(
            "INSERT OR REPLACE INTO lifecycle_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event["event_id"], memory_id, event_type, event["created_at"], actor,
                source_interaction, reason, json.dumps(prior_state, ensure_ascii=True),
                json.dumps(resulting_state, ensure_ascii=True),
            ),
        )
        return event

    def post(
        self,
        title: str,
        body: str,
        project: str,
        device: str,
        *,
        kind: str = "note",
        scope: str = "project",
        status: str = "active",
        confidence: float = DEFAULT_CONFIDENCE,
        importance: float = DEFAULT_IMPORTANCE,
        expires_at: str | None = None,
        source: str = "manual",
        evidence: list[str] | str | None = None,
        relationships: list[dict[str, str]] | None = None,
        actor: str = "agent",
        source_interaction: str | None = None,
        reason: str = "manual post",
    ):
        values = {"title": title, "body": body, "project": project, "device": device}
        for key, value in values.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must not be empty")
            if len(value.encode()) > (MAX_BODY_BYTES if key == "body" else MAX_FIELD_BYTES):
                raise ValueError(f"{key} is too long")
        evidence_values = [evidence] if isinstance(evidence, str) else list(evidence or [])
        if (
            len(evidence_values) > 50
            or any(not isinstance(item, str) or len(item) > 4096 for item in evidence_values)
        ):
            raise ValueError("evidence must contain up to 50 strings of 4096 characters")
        if not isinstance(source, str) or not source.strip() or len(source.encode()) > MAX_FIELD_BYTES:
            raise ValueError("source must be a nonempty string up to 1024 bytes")
        if len(relationships or []) > 100:
            raise ValueError("relationships must contain at most 100 entries")
        secret_text = "\n".join([title, body, project, device, source, *evidence_values])
        if contains_secret(secret_text):
            raise ValueError("memory appears to contain a credential or secret-like value")
        created_at = utcnow()
        metadata = self._metadata_defaults(
            {
                "id": uuid.uuid4().hex,
                "title": title.strip(),
                "project": project.strip(),
                "device": device.strip(),
                "created_at": created_at,
                "updated_at": created_at,
                "kind": kind,
                "scope": scope,
                "status": status,
                "confidence": confidence,
                "importance": importance,
                "expires_at": expires_at,
                "source": source.strip() or "manual",
                "evidence": evidence_values,
                "relationships": relationships or [],
            },
            body,
        )
        self._validate_metadata(metadata)
        path = self.files / f"{metadata['id']}.md"
        document = (
            "<!-- memos: "
            + json.dumps(metadata, ensure_ascii=True).replace("-->", "\\u002d\\u002d>")
            + " -->\n\n"
            + f"# {metadata['title']}\n\n{body}\n"
        )
        with self.lock:
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
                    self.db.execute("DELETE FROM relationships WHERE source_id=?", (metadata["id"],))
                raise
            with self.db:
                self._append_event(
                    metadata["id"], "created", actor=actor,
                    source_interaction=source_interaction, reason=reason,
                    resulting_state={key: value for key, value in metadata.items() if key != "content_hash"},
                )
                self._increment_metric("memory_writes")
        return {**metadata, "path": str(path)}

    def _read_document(self, path: Path):
        if not path.resolve().is_relative_to(self.files):
            raise ValueError(f"Memo symlink escapes the data directory: {path}")
        text = path.read_text(encoding="utf-8")
        try:
            header, content = text.split("\n", 1)
        except ValueError:
            raise ValueError(f"Invalid memo metadata: {path}") from None
        if not header.startswith("<!-- memos: ") or not header.endswith(" -->"):
            raise ValueError(f"Invalid memo metadata: {path}")
        metadata = json.loads(header[len("<!-- memos: "):-len(" -->")])
        required = ("id", "title", "project", "device", "created_at")
        if (
            not isinstance(metadata, dict)
            or any(not isinstance(metadata.get(field), str) or not metadata[field] for field in required)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", metadata["id"])
        ):
            raise ValueError(f"Invalid memo metadata: {path}")
        body = content.lstrip().removeprefix(f"# {metadata['title']}\n").lstrip().rstrip("\n")
        return self._metadata_defaults(metadata, body), body

    def _replay_audit(self):
        self.db.execute("DELETE FROM lifecycle_events")
        if not self.audit_path.exists():
            return
        for line_number, line in enumerate(self.audit_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                self.db.execute(
                    "INSERT OR REPLACE INTO lifecycle_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event["event_id"], event["memory_id"], event["event_type"],
                        event["created_at"], event.get("actor", "unknown"),
                        event.get("source_interaction"), event.get("reason", ""),
                        json.dumps(event.get("prior_state"), ensure_ascii=True),
                        json.dumps(event.get("resulting_state"), ensure_ascii=True),
                    ),
                )
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid audit event at line {line_number}") from exc

    def reindex(self):
        # Markdown and the append-only audit log remain authoritative.
        with self.lock:
            documents = []
            for path in sorted(self.files.glob("*.md")):
                metadata, body = self._read_document(path)
                documents.append((metadata, body, path))
            with self.db:
                self.db.execute("DELETE FROM memos")
                self.db.execute("DELETE FROM chunks")
                self.db.execute("DELETE FROM memo_fts")
                self.db.execute("DELETE FROM relationships")
            for document in documents:
                self._index(*document)
            with self.db:
                self._replay_audit()
        return len(documents)

    def _relationship_state(self) -> tuple[set[str], set[str]]:
        now = datetime.now(timezone.utc)
        live_sources = {
            row["id"]
            for row in self.db.execute("SELECT id, status, expires_at FROM memos")
            if row["status"] == "active"
            and (_parse_time(row["expires_at"]) is None or _parse_time(row["expires_at"]) > now)
        }
        superseded, contradicted = set(), set()
        for row in self.db.execute("SELECT source_id, relation, target_id FROM relationships"):
            if row["source_id"] not in live_sources:
                continue
            if row["relation"] == "supersedes":
                superseded.add(row["target_id"])
            elif row["relation"] == "contradicts":
                contradicted.add(row["target_id"])
        return superseded, contradicted

    def _rows(
        self,
        *,
        project: str | None = None,
        device: str | None = None,
        scopes: Iterable[str] | None = None,
        kinds: Iterable[str] | None = None,
        historical: bool = False,
    ) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self.db.execute(
            "SELECT * FROM memos WHERE (? IS NULL OR project=?) AND (? IS NULL OR device=?)",
            (project, project, device, device),
        )]
        scope_set, kind_set = set(scopes or []), set(kinds or [])
        if scope_set:
            invalid = scope_set - SCOPES
            if invalid:
                raise ValueError(f"unsupported scopes: {sorted(invalid)}")
            rows = [row for row in rows if row["scope"] in scope_set]
        if kind_set:
            invalid = kind_set - KINDS
            if invalid:
                raise ValueError(f"unsupported kinds: {sorted(invalid)}")
            rows = [row for row in rows if row["kind"] in kind_set]
        now = datetime.now(timezone.utc)
        superseded, contradicted = self._relationship_state()
        for row in rows:
            effective = row["status"]
            if row["id"] in superseded:
                effective = "superseded"
            elif row["id"] in contradicted:
                effective = "contradicted"
            elif _parse_time(row["expires_at"]) is not None and _parse_time(row["expires_at"]) <= now:
                effective = "expired"
            row["effective_status"] = effective
        if historical:
            return rows
        return [row for row in rows if row["effective_status"] == "active"]

    @staticmethod
    def _scope_score(row: dict[str, Any], project: str | None, device: str | None) -> float:
        scope = row["scope"]
        if scope == "global":
            return 0.7 if project or device else 1.0
        if scope == "repository":
            return 0.85 if project and row["project"] == project else 0.45
        if scope == "project":
            return 0.9 if project and row["project"] == project else 0.35
        if scope == "device":
            return 1.0 if device and row["device"] == device else 0.1
        return 0.0

    @staticmethod
    def _freshness(row: dict[str, Any]) -> float:
        updated = _parse_time(row.get("updated_at")) or _parse_time(row.get("created_at"))
        if updated is None:
            return 0.5
        age_days = max((datetime.now(timezone.utc) - updated).total_seconds() / 86400, 0)
        half_life = 30 if row["kind"] == "incident" else 365
        return math.exp(-math.log(2) * age_days / half_life)

    @staticmethod
    def _verification_score(row: dict[str, Any]) -> float:
        evidence = json.loads(row.get("evidence") or "[]")
        source = (row.get("source") or "").casefold()
        if evidence:
            return 1.0
        if "user" in source or "direct" in source:
            return 0.9
        if "test" in source or "verified" in source:
            return 0.9
        if "infer" in source or "agent" in source:
            return 0.35
        return 0.55

    def _rank(
        self,
        query: str,
        rows: list[dict[str, Any]],
        *,
        project: str | None,
        device: str | None,
        candidate_limit: int,
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        candidates = {row["id"]: row for row in rows}
        vector = normalize(self.encoder.query(query))
        semantic: dict[str, float] = {}
        for row in self.db.execute("SELECT memo_id, vector FROM chunks"):
            memo_id = row["memo_id"]
            if memo_id in candidates:
                score = float(vector @ np.frombuffer(row["vector"], dtype=np.float32))
                semantic[memo_id] = max(semantic.get(memo_id, -1.0), score)

        terms = _words(query)
        lexical_rank: list[str] = []
        if terms:
            expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in terms)
            try:
                lexical_rank = [
                    row[0] for row in self.db.execute(
                        "SELECT id FROM memo_fts WHERE memo_fts MATCH ? ORDER BY rank", (expression,)
                    ) if row[0] in candidates
                ]
            except sqlite3.OperationalError:
                lexical_rank = []
        lexical = {
            memo_id: len(terms & _words(row["title"] + "\n" + row["body"])) / max(len(terms), 1)
            for memo_id, row in candidates.items()
        }

        # Stage one: broad hybrid candidate generation using semantic and lexical rankings.
        semantic_rank = sorted(semantic, key=semantic.get, reverse=True)
        broad_ids: list[str] = []
        for memo_id in [*semantic_rank[:candidate_limit], *lexical_rank[:candidate_limit]]:
            if memo_id not in broad_ids:
                broad_ids.append(memo_id)
        if len(broad_ids) < min(candidate_limit, len(candidates)):
            for memo_id in semantic_rank:
                if memo_id not in broad_ids:
                    broad_ids.append(memo_id)
                if len(broad_ids) >= candidate_limit:
                    break

        reinforced = {
            row["memory_id"]: row["count"]
            for row in self.db.execute(
                """SELECT memory_id, COUNT(*) AS count FROM lifecycle_events
                   WHERE event_type='reinforced' GROUP BY memory_id"""
            )
        }
        ranked = []
        for memo_id in broad_ids:
            row = candidates[memo_id]
            semantic_score = max(semantic.get(memo_id, 0), 0)
            lexical_score = lexical.get(memo_id, 0)
            scope_score = self._scope_score(row, project, device)
            confidence = float(row["confidence"] or DEFAULT_CONFIDENCE)
            importance = float(row["importance"] or DEFAULT_IMPORTANCE)
            freshness = self._freshness(row)
            verification = self._verification_score(row)
            reinforcement = min(reinforced.get(memo_id, 0) / 5, 1)
            score = (
                0.40 * semantic_score + 0.20 * lexical_score + 0.14 * scope_score
                + 0.07 * confidence + 0.06 * importance + 0.04 * freshness
                + 0.06 * verification + 0.03 * reinforcement
            )
            reasons = []
            if lexical_score:
                reasons.append(f"matched {round(lexical_score * 100)}% of query terms")
            if semantic_score >= 0.6:
                reasons.append("strong semantic match")
            if scope_score >= 1:
                reasons.append(f"matched {row['scope']} scope")
            if verification >= 0.9:
                reasons.append("supported by direct or verified evidence")
            if reinforced.get(memo_id):
                reasons.append(f"reinforced {reinforced[memo_id]} time(s)")
            ranked.append({
                **row,
                "score": round(score, 6),
                "semantic_score": round(semantic_score, 6),
                "lexical_score": round(lexical_score, 6),
                "relevance_reasons": reasons or ["nearest hybrid candidate"],
            })
        return sorted(ranked, key=lambda row: (row["score"], row["created_at"]), reverse=True)

    @staticmethod
    def _diversify(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        for row in rows:
            words = _words(row["title"] + "\n" + row["body"])
            duplicate = False
            for existing in selected:
                other = _words(existing["title"] + "\n" + existing["body"])
                overlap = len(words & other) / max(len(words | other), 1)
                if row["content_hash"] == existing["content_hash"] or overlap > 0.82:
                    duplicate = True
                    break
            if not duplicate:
                selected.append(row)
            if len(selected) >= limit:
                break
        return selected

    def search(
        self,
        query: str,
        project: str | None = None,
        device: str | None = None,
        limit: int = 5,
        *,
        historical: bool = False,
        scopes: Iterable[str] | None = None,
        kinds: Iterable[str] | None = None,
        min_relevance: float | None = None,
    ):
        if not query.strip() or len(query) > 4096:
            raise ValueError("query must contain 1-4096 characters")
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        start = time.perf_counter()
        with self.lock:
            rows = self._rows(
                project=project, device=device, scopes=scopes, kinds=kinds, historical=historical
            )
            ranked = self._rank(
                query, rows, project=project, device=device,
                candidate_limit=max(limit * 8, 40),
            )
            if min_relevance is not None:
                ranked = [
                    row for row in ranked
                    if row["score"] >= min_relevance
                    and (row["lexical_score"] > 0 or row["semantic_score"] >= 0.58)
                ]
            result = self._diversify(ranked, limit)
            with self.db:
                self._increment_metric("retrievals")
                self._increment_metric("retrieval_latency_ms", (time.perf_counter() - start) * 1000)
            return result

    @staticmethod
    def _excerpt(body: str, query: str, max_chars: int = 700) -> str:
        terms = _words(query)
        passages = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
        if not passages:
            return ""
        passage = max(passages, key=lambda part: len(terms & _words(part)))
        if len(passage) <= max_chars:
            return passage
        return passage[:max_chars].rsplit(" ", 1)[0] + "..."

    def _relations_for(self, memory_ids: Iterable[str]) -> list[dict[str, str]]:
        ids = set(memory_ids)
        return [
            dict(row) for row in self.db.execute(
                "SELECT source_id, relation, target_id, created_at FROM relationships"
            ) if row["source_id"] in ids or row["target_id"] in ids
        ]

    def recall_context(
        self,
        task: str,
        project: str | None = None,
        device: str | None = None,
        scopes: list[str] | None = None,
        kinds: list[str] | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        start = time.perf_counter()
        with self.lock:
            # Scope is independent of provenance: global memories remain eligible even
            # when they were learned in another checkout or on another device.
            rows = self._rows(scopes=scopes, kinds=kinds)
            rows = [
                row for row in rows
                if row["scope"] == "global"
                or (row["scope"] in {"repository", "project"}
                    and (project is None or row["project"] == project))
                or (row["scope"] == "device"
                    and (device is None or row["device"] == device))
            ]
            ranked = self._rank(
                task, rows, project=project, device=device,
                candidate_limit=max(limit * 10, 50),
            )
            relevant = [
                row for row in ranked
                if row["score"] >= 0.38
                and (row["lexical_score"] > 0 or row["semantic_score"] >= 0.58)
            ]
            selected = self._diversify(relevant, limit)

            # Expand one useful supporting/derivation edge without flooding context.
            selected_ids = {row["id"] for row in selected}
            for relation in self._relations_for(selected_ids):
                if relation["relation"] not in {"supports", "derived_from"}:
                    continue
                related_id = relation["target_id"] if relation["source_id"] in selected_ids else relation["source_id"]
                related = next((row for row in ranked if row["id"] == related_id), None)
                if related and related_id not in selected_ids and len(selected) < limit:
                    related["relevance_reasons"] = [f"expanded through {relation['relation']} relationship"]
                    selected.append(related)
                    selected_ids.add(related_id)

            trace_id = uuid.uuid4().hex
            latency_ms = (time.perf_counter() - start) * 1000
            candidate_payload = [{"id": row["id"], "score": row["score"]} for row in ranked[:50]]
            ignored = [row["id"] for row in ranked[:50] if row["id"] not in selected_ids]
            with self.db:
                self.db.execute(
                    "INSERT INTO retrieval_traces VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                    (
                        trace_id, utcnow(), hashlib.sha256(task.encode()).hexdigest(),
                        json.dumps(candidate_payload), json.dumps(list(selected_ids)),
                        json.dumps(ignored), latency_ms,
                    ),
                )
                self._increment_metric("recall_context_calls")
                self._increment_metric("recall_context_injected", len(selected))
                self._increment_metric("recall_context_latency_ms", latency_ms)
            memories = [
                {
                    "id": row["id"], "title": row["title"],
                    "excerpt": self._excerpt(row["body"], task),
                    "kind": row["kind"], "scope": row["scope"],
                    "status": row["effective_status"],
                    "confidence": row["confidence"], "importance": row["importance"],
                    "project": row["project"], "device": row["device"],
                    "updated_at": row["updated_at"], "expires_at": row["expires_at"],
                    "source": row["source"], "score": row["score"],
                    "relevance_reasons": row["relevance_reasons"], "untrusted": True,
                }
                for row in selected
            ]
            return {
                "memories": memories,
                "relationships": self._relations_for(selected_ids),
                "trace_id": trace_id,
                "cutoff": 0.38,
                "message": None if memories else "no useful memory found",
            }

    @staticmethod
    def _classify(content: str, source: str) -> tuple[str, str]:
        lowered = content.casefold()
        if "?" in content or any(term in lowered for term in ("unknown", "needs investigation", "open question")):
            return "open_question", "active"
        if any(term in lowered for term in ("prefer", "preference", "always use", "never use")):
            return "preference", "active"
        if any(term in lowered for term in ("decided", "decision", "we will", "chosen")):
            return "decision", "active"
        if any(term in lowered for term in ("steps", "procedure", "run ", "workflow")):
            return "procedure", "active"
        if any(term in lowered for term in ("incident", "outage", "failure", "error")):
            return "incident", "active"
        if "infer" in source.casefold() or "guess" in source.casefold():
            return "open_question", "unverified"
        return "fact", "active"

    @staticmethod
    def _evidence_confidence(source: str, evidence: list[str]) -> float:
        lowered = source.casefold()
        evidence_text = " ".join(evidence).casefold()
        if "user" in lowered or "direct" in lowered:
            return 0.95
        if evidence and any(term in evidence_text for term in ("test", "verified", "observed", "log", "command")):
            return 0.9
        if evidence:
            return 0.75
        if "infer" in lowered or "agent" in lowered or "guess" in lowered:
            return 0.3
        return 0.55

    @staticmethod
    def _title_from_content(content: str) -> str:
        first = next((line.strip(" #\t") for line in content.splitlines() if line.strip()), "Observation")
        return first[:160]

    @staticmethod
    def _contradicts(left: str, right: str) -> bool:
        negations = {"not", "never", "no", "cannot", "can't", "won't", "without"}
        left_words, right_words = _words(left), _words(right)
        left_negated = bool(left_words & negations)
        right_negated = bool(right_words & negations)
        left_core, right_core = left_words - negations, right_words - negations
        overlap = len(left_core & right_core) / max(min(len(left_core), len(right_core)), 1)
        return left_negated != right_negated and overlap >= 0.65

    def record_observation(
        self,
        content: str,
        source: str,
        project: str,
        device: str,
        evidence: list[str] | str | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        for field, value in {
            "content": content, "source": source, "project": project, "device": device,
        }.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must not be empty")
            if len(value.encode()) > (MAX_BODY_BYTES if field == "content" else MAX_FIELD_BYTES):
                raise ValueError(f"{field} is too long")
        if scope is not None and scope not in SCOPES:
            raise ValueError(f"scope must be one of {sorted(SCOPES)}")
        evidence_values = [evidence] if isinstance(evidence, str) else list(evidence or [])
        if (
            len(evidence_values) > 50
            or any(not isinstance(item, str) or len(item) > 4096 for item in evidence_values)
        ):
            raise ValueError("evidence must contain up to 50 strings of 4096 characters")
        if contains_secret("\n".join([content, source, project, device, *evidence_values])):
            with self.db:
                self._increment_metric("automatic_writes_rejected_secret")
            raise ValueError("observation appears to contain a credential or secret-like value")
        kind, status = self._classify(content, source)
        confidence = self._evidence_confidence(source, evidence_values)
        if kind == "fact" and confidence < 0.5:
            kind, status = "open_question", "unverified"
        selected_scope = scope or ("device" if kind == "incident" else "project")
        fingerprint = _content_hash(content)
        with self.lock:
            duplicate = self.db.execute(
                "SELECT * FROM memos WHERE content_hash=? ORDER BY created_at DESC LIMIT 1",
                (fingerprint,),
            ).fetchone()
            if duplicate:
                with self.db:
                    self._append_event(
                        duplicate["id"], "reinforced", actor=source,
                        reason="equivalent observation was recorded",
                        resulting_state={"evidence": evidence_values, "confidence": confidence},
                    )
                    self._increment_metric("observations_deduplicated")
                return {"memory_id": duplicate["id"], "deduplicated": True, "kind": duplicate["kind"]}

            relationships = []
            active_rows = self._rows(project=project, device=None)
            for row in active_rows:
                if self._contradicts(content, row["body"]):
                    relationships.append({"type": "contradicts", "target_id": row["id"]})
                    break
            expires_at = None
            if kind == "incident":
                expires_at = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
            elif kind == "fact" and selected_scope == "device":
                expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
            memory = self.post(
                self._title_from_content(content), content, project, device,
                kind=kind, scope=selected_scope, status=status, confidence=confidence,
                importance=0.6 if kind in {"preference", "decision"} else 0.5,
                expires_at=expires_at, source=source, evidence=evidence_values,
                relationships=relationships, actor=source, source_interaction=source,
                reason="automatic observation capture",
            )
            with self.db:
                self._increment_metric("observations_recorded")
                if relationships:
                    self._increment_metric("contradictions_detected")
            return {
                "memory_id": memory["id"], "deduplicated": False, "kind": kind,
                "status": status, "confidence": confidence, "relationships": relationships,
            }

    def consolidate_memories(
        self, memory_ids: list[str] | None = None, dry_run: bool = False
    ) -> dict[str, Any]:
        with self.lock:
            superseded, contradicted = self._relationship_state()
            rows = [
                row for row in self._rows(historical=True)
                if row["status"] == "active"
                and row["id"] not in superseded
                and row["id"] not in contradicted
            ]
            if memory_ids:
                requested = set(memory_ids)
                rows = [row for row in rows if row["id"] in requested]
                missing = requested - {row["id"] for row in rows}
                if missing:
                    raise ValueError(f"active memories not found: {sorted(missing)}")
            now = datetime.now(timezone.utc)
            merge_rows = [
                row for row in rows
                if _parse_time(row["expires_at"]) is None or _parse_time(row["expires_at"]) > now
            ]
            actions = []
            used: set[str] = set()
            for index, left in enumerate(merge_rows):
                if left["id"] in used:
                    continue
                left_words = _words(left["title"] + "\n" + left["body"])
                for right in merge_rows[index + 1:]:
                    if right["id"] in used:
                        continue
                    right_words = _words(right["title"] + "\n" + right["body"])
                    overlap = len(left_words & right_words) / max(len(left_words | right_words), 1)
                    if left["content_hash"] == right["content_hash"] or overlap >= 0.82:
                        preferred = max(
                            (left, right),
                            key=lambda row: (float(row["confidence"]), float(row["importance"]), row["created_at"]),
                        )
                        actions.append({
                            "type": "merge", "memory_ids": [left["id"], right["id"]],
                            "preferred_id": preferred["id"], "reason": "duplicate or near-duplicate content",
                        })
                        used.update((left["id"], right["id"]))
                        break
                    if self._contradicts(left["body"], right["body"]):
                        actions.append({
                            "type": "contradiction", "memory_ids": [left["id"], right["id"]],
                            "reason": "similar claims have opposite polarity",
                        })
                        used.update((left["id"], right["id"]))
                        break
            for row in rows:
                expiry = _parse_time(row["expires_at"])
                already_recorded = self.db.execute(
                    """SELECT 1 FROM lifecycle_events
                       WHERE memory_id=? AND event_type='expired' LIMIT 1""", (row["id"],)
                ).fetchone()
                if expiry and expiry <= now and not already_recorded:
                    actions.append({"type": "expiry", "memory_ids": [row["id"]], "reason": "expiry elapsed"})

            if dry_run:
                return {"dry_run": True, "actions": actions, "created_memory_ids": []}

            created = []
            for action in actions:
                originals = [self.db.execute("SELECT * FROM memos WHERE id=?", (memo_id,)).fetchone()
                             for memo_id in action["memory_ids"]]
                originals = [dict(row) for row in originals if row]
                if action["type"] == "merge":
                    preferred = next(row for row in originals if row["id"] == action["preferred_id"])
                    result = self.post(
                        f"Consolidated: {preferred['title']}", preferred["body"],
                        preferred["project"], preferred["device"], kind=preferred["kind"],
                        scope=preferred["scope"], confidence=max(float(row["confidence"]) for row in originals),
                        importance=max(float(row["importance"]) for row in originals),
                        source="automatic consolidation",
                        evidence=json.loads(preferred["evidence"] or "[]"),
                        relationships=[{"type": "supersedes", "target_id": row["id"]} for row in originals],
                        actor="consolidation-worker",
                        source_interaction=",".join(action["memory_ids"]), reason=action["reason"],
                    )
                    created.append(result["id"])
                elif action["type"] == "contradiction":
                    left, right = originals
                    body = (
                        "Conflicting memories require verification.\n\n"
                        f"- {left['id']}: {left['title']}\n"
                        f"- {right['id']}: {right['title']}"
                    )
                    result = self.post(
                        f"Conflict: {left['title']}", body, left["project"], left["device"],
                        kind="open_question", scope=left["scope"], confidence=0.2,
                        importance=max(float(left["importance"]), float(right["importance"])),
                        source="automatic consolidation",
                        relationships=[{"type": "contradicts", "target_id": row["id"]} for row in originals],
                        actor="consolidation-worker",
                        source_interaction=",".join(action["memory_ids"]), reason=action["reason"],
                    )
                    created.append(result["id"])
                elif action["type"] == "expiry":
                    with self.db:
                        self._append_event(
                            action["memory_ids"][0], "expired", actor="consolidation-worker",
                            reason=action["reason"], resulting_state={"status": "expired"},
                        )
            with self.db:
                self._increment_metric("consolidation_runs")
                self._increment_metric("consolidation_actions", len(actions))
            return {"dry_run": False, "actions": actions, "created_memory_ids": created}

    def get_memory_history(self, memory_id: str) -> dict[str, Any]:
        with self.lock:
            origin = self.db.execute("SELECT * FROM memos WHERE id=?", (memory_id,)).fetchone()
            if origin is None:
                raise ValueError(f"memory {memory_id!r} not found")
            discovered = {memory_id}
            frontier = [memory_id]
            relations = []
            while frontier:
                current = frontier.pop()
                for row in self.db.execute(
                    """SELECT source_id, relation, target_id, created_at FROM relationships
                       WHERE source_id=? OR target_id=?""", (current, current)
                ):
                    relation = dict(row)
                    if relation not in relations:
                        relations.append(relation)
                    for related in (row["source_id"], row["target_id"]):
                        if related not in discovered:
                            discovered.add(related)
                            frontier.append(related)
            placeholders = ",".join("?" for _ in discovered)
            memories = [dict(row) for row in self.db.execute(
                f"SELECT * FROM memos WHERE id IN ({placeholders})", tuple(discovered)
            )]
            superseded, contradicted = self._relationship_state()
            now = datetime.now(timezone.utc)
            for memory in memories:
                effective = memory["status"]
                if memory["id"] in superseded:
                    effective = "superseded"
                elif memory["id"] in contradicted:
                    effective = "contradicted"
                elif (_parse_time(memory["expires_at"]) is not None
                      and _parse_time(memory["expires_at"]) <= now):
                    effective = "expired"
                memory["effective_status"] = effective
            events = [
                {**dict(row), "prior_state": json.loads(row["prior_state"] or "null"),
                 "resulting_state": json.loads(row["resulting_state"] or "null")}
                for row in self.db.execute(
                    f"SELECT * FROM lifecycle_events WHERE memory_id IN ({placeholders}) ORDER BY created_at",
                    tuple(discovered),
                )
            ]
            return {"memory_id": memory_id, "memories": memories, "relationships": relations, "events": events}

    def rollback_memory(self, memory_id: str, target_revision: str | int) -> dict[str, Any]:
        history = self.get_memory_history(memory_id)
        ordered = sorted(history["memories"], key=lambda row: row["created_at"])
        if isinstance(target_revision, int):
            if not 0 <= target_revision < len(ordered):
                raise ValueError("target_revision index is out of range")
            target = ordered[target_revision]
        else:
            target = next((row for row in ordered if row["id"] == target_revision), None)
            if target is None:
                raise ValueError("target_revision is not in this memory history")
        superseded, contradicted = self._relationship_state()
        active = [
            row for row in ordered
            if row["status"] == "active" and row["id"] not in superseded and row["id"] not in contradicted
        ]
        relationships = [{"type": "derived_from", "target_id": target["id"]}]
        relationships.extend({"type": "supersedes", "target_id": row["id"]} for row in active)
        result = self.post(
            f"Rollback: {target['title']}", target["body"], target["project"], target["device"],
            kind=target["kind"], scope=target["scope"], confidence=float(target["confidence"]),
            importance=float(target["importance"]), source="rollback",
            evidence=json.loads(target["evidence"] or "[]"), relationships=relationships,
            actor="rollback", source_interaction=memory_id,
            reason=f"restored revision {target['id']}",
        )
        with self.db:
            self._append_event(
                result["id"], "rollback", actor="rollback",
                reason=f"restored {target['id']} from history of {memory_id}",
                prior_state={"active_ids": [row["id"] for row in active]},
                resulting_state={"active_id": result["id"], "restored_id": target["id"]},
            )
            self._increment_metric("rollbacks")
        return {"memory_id": result["id"], "restored_revision": target["id"], "superseded": [row["id"] for row in active]}

    def record_retrieval_feedback(
        self, trace_id: str, outcome: str, useful_memory_ids: list[str] | None = None
    ) -> dict[str, Any]:
        if outcome not in {"success", "partial", "failure", "unknown"}:
            raise ValueError("outcome must be success, partial, failure, or unknown")
        useful = useful_memory_ids or []
        with self.lock, self.db:
            trace = self.db.execute(
                "SELECT injected FROM retrieval_traces WHERE trace_id=?", (trace_id,)
            ).fetchone()
            if trace is None:
                raise ValueError("retrieval trace not found")
            injected = set(json.loads(trace["injected"]))
            if not set(useful).issubset(injected):
                raise ValueError("useful_memory_ids must have been injected by this trace")
            self.db.execute(
                "UPDATE retrieval_traces SET outcome=?, useful_memory_ids=? WHERE trace_id=?",
                (outcome, json.dumps(useful), trace_id),
            )
            self._increment_metric(f"retrieval_feedback_{outcome}")
            self._increment_metric("retrieval_memories_useful", len(useful))
        return {"trace_id": trace_id, "outcome": outcome, "useful_memory_ids": useful}

    def metrics_snapshot(self) -> dict[str, Any]:
        with self.lock:
            counters = {row["name"]: row["value"] for row in self.db.execute("SELECT * FROM metrics")}
            traces = self.db.execute(
                "SELECT COUNT(*) AS count, AVG(latency_ms) AS average FROM retrieval_traces"
            ).fetchone()
            return {
                "counters": counters,
                "retrieval_traces": int(traces["count"]),
                "average_recall_latency_ms": round(float(traces["average"] or 0), 3),
            }

    def start_consolidation_worker(self, interval_seconds: int = 3600):
        if interval_seconds <= 0 or (self._worker and self._worker.is_alive()):
            return
        self._worker_stop.clear()

        def work():
            while not self._worker_stop.wait(interval_seconds):
                try:
                    self.consolidate_memories()
                except Exception:
                    with self.lock, self.db:
                        self._increment_metric("consolidation_failures")

        self._worker = threading.Thread(target=work, name="memo-consolidation", daemon=True)
        self._worker.start()

    def close(self):
        self._worker_stop.set()
        if self._worker:
            self._worker.join(timeout=2)
        self.db.close()
