from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path

import numpy as np
import yaml

from .store import chunks, normalize

MAX_SKILL_BYTES = 256_000


class SkillLibrary:
    """Live folder catalog; embeddings are cached by content, only when searching."""

    def __init__(self, root: Path, encoder, lock=None):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.encoder = encoder
        # Share the memo store lock to serialize access to the embedding model.
        self.lock = lock if lock is not None else threading.RLock()
        self.cache = {}

    def _scan(self):
        skills, errors = {}, []
        for path in sorted(self.root.glob("*/SKILL.md")):
            skill_id = path.parent.name
            try:
                if not path.resolve().is_relative_to(self.root):
                    raise ValueError("symlinks outside the skills folder are not allowed")
                with path.open("rb") as file:
                    raw = file.read(MAX_SKILL_BYTES + 1)
                if len(raw) > MAX_SKILL_BYTES:
                    raise ValueError(f"SKILL.md exceeds {MAX_SKILL_BYTES} bytes")
                content = raw.decode("utf-8")
                lines = content.splitlines()
                if not lines or lines[0] != "---":
                    raise ValueError("missing YAML frontmatter")
                try:
                    end = lines.index("---", 1)
                except ValueError:
                    raise ValueError("unclosed YAML frontmatter") from None
                metadata = yaml.safe_load("\n".join(lines[1:end]))
                if not isinstance(metadata, dict):
                    raise ValueError("frontmatter must be a mapping")
                for field in ("name", "description"):
                    if not isinstance(metadata.get(field), str) or not metadata[field].strip():
                        raise ValueError(f"{field} must be a nonempty string")
                    if len(metadata[field]) > (200 if field == "name" else 4096):
                        raise ValueError(f"{field} is too long")
                triggers = metadata.get("triggers", [])
                if isinstance(triggers, str):
                    triggers = [triggers]
                if (not isinstance(triggers, list) or len(triggers) > 50
                        or any(not isinstance(t, str) or not t.strip() or len(t) > 1024 for t in triggers)):
                    raise ValueError("triggers must be a string or up to 50 nonempty strings (max 1024 characters each)")
                skills[skill_id] = {
                    "id": skill_id, "name": metadata["name"].strip(),
                    "description": metadata["description"].strip(),
                    "triggers": triggers, "content": content,
                    "version": hashlib.sha256(raw).hexdigest(),
                }
            except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
                errors.append({"id": skill_id, "error": str(exc)})
        self.cache = {key: value for key, value in self.cache.items() if key in skills}
        return skills, errors

    @staticmethod
    def _summary(skill):
        return {key: value for key, value in skill.items() if key != "content"}

    def list(self):
        with self.lock:
            skills, errors = self._scan()
            return {"skills": [self._summary(skill) for skill in skills.values()], "errors": errors}

    def get(self, skill_id: str):
        # Lookup by catalog ID, never by a client-supplied filesystem path.
        with self.lock:
            skills, errors = self._scan()
            if skill_id not in skills:
                detail = next((e["error"] for e in errors if e["id"] == skill_id), "not found")
                raise ValueError(f"Skill {skill_id!r}: {detail}")
            return dict(skills[skill_id])

    def search(self, query: str, limit: int = 5):
        if not query.strip() or len(query) > 4096:
            raise ValueError("query must contain 1–4096 characters")
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        with self.lock:
            skills, errors = self._scan()
            if not skills:
                return {"skills": [], "errors": errors}
            vector = normalize(self.encoder.query(query))
            terms = set(re.findall(r"\w+", query.casefold()))
            semantic, lexical = {}, {}
            for skill_id, skill in skills.items():
                text = "\n".join([skill_id, skill["name"], skill["description"],
                                  *skill["triggers"], skill["content"]])
                cached = self.cache.get(skill_id)
                if cached is None or cached[0] != skill["version"]:
                    vectors = np.stack([normalize(v) for v in self.encoder.passages(list(chunks(text)))])
                    cached = (skill["version"], vectors)
                    self.cache[skill_id] = cached
                semantic[skill_id] = float(np.max(cached[1] @ vector))
                words = set(re.findall(r"\w+", text.casefold()))
                matches = len(terms & words)
                if matches:
                    lexical[skill_id] = matches
            scores = {}
            for ranking in (sorted(semantic, key=semantic.get, reverse=True),
                            sorted(lexical, key=lexical.get, reverse=True)):
                for rank, skill_id in enumerate(ranking, 1):
                    scores[skill_id] = scores.get(skill_id, 0) + 1 / (60 + rank)
            return {
                "skills": [{**self._summary(skills[skill_id]), "score": round(scores[skill_id], 6)}
                           for skill_id in sorted(scores, key=scores.get, reverse=True)[:limit]],
                "errors": errors,
            }
