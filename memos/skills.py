from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np
import yaml

from .store import chunks, normalize

MAX_SKILL_BYTES = 256_000
MAX_RESOURCE_BYTES = 256_000
MAX_CONTEXT_BYTES = 512_000
MAX_RESOURCES = 50
SAFE_RESOURCE_SUFFIXES = {
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".csv", ".tsv",
    ".j2", ".jinja", ".jinja2", ".tmpl", ".mustache",
}
COMPATIBILITY_KEYS = {"min_server", "max_server", "platforms", "clients"}


def _string_list(value: Any, field: str, limit: int = 50) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if (
        not isinstance(value, list) or len(value) > limit
        or any(not isinstance(item, str) or not item.strip() or len(item) > 1024 for item in value)
    ):
        raise ValueError(f"{field} must be a string or up to {limit} nonempty strings")
    return [item.strip() for item in value]


class SkillLibrary:
    """Live validated skill catalog with content-addressed embedding caches."""

    def __init__(self, root: Path, encoder, lock=None):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.encoder = encoder
        self.lock = lock if lock is not None else threading.RLock()
        self.cache: dict[str, tuple[str, np.ndarray]] = {}
        self.catalog_history: dict[str, dict[str, str]] = {}

    def _resource_path(self, skill_dir: Path, relative: str) -> Path:
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            raise ValueError(f"unsafe resource path: {relative!r}")
        path = (skill_dir / Path(*pure.parts)).resolve()
        if not path.is_relative_to(skill_dir.resolve()):
            raise ValueError(f"resource escapes skill folder: {relative!r}")
        if path.suffix.casefold() not in SAFE_RESOURCE_SUFFIXES:
            raise ValueError(f"unsupported resource type: {relative!r}")
        if not path.is_file():
            raise ValueError(f"missing resource: {relative!r}")
        if path.stat().st_size > MAX_RESOURCE_BYTES:
            raise ValueError(f"resource exceeds {MAX_RESOURCE_BYTES} bytes: {relative!r}")
        return path

    def _load_skill(self, path: Path) -> dict[str, Any]:
        skill_id = path.parent.name
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

        triggers = _string_list(metadata.get("triggers"), "triggers")
        required_tools = _string_list(metadata.get("required_tools"), "required_tools")
        resources = _string_list(metadata.get("optional_resources"), "optional_resources", MAX_RESOURCES)
        exclusions = _string_list(metadata.get("exclusions"), "exclusions")
        priority = metadata.get("priority", 0)
        if not isinstance(priority, int) or not -100 <= priority <= 100:
            raise ValueError("priority must be an integer between -100 and 100")
        compatibility = metadata.get("compatibility", {})
        if isinstance(compatibility, str):
            compatibility = {"clients": [compatibility]}
        if not isinstance(compatibility, dict) or set(compatibility) - COMPATIBILITY_KEYS:
            raise ValueError(f"compatibility supports only {sorted(COMPATIBILITY_KEYS)}")
        for key in ("platforms", "clients"):
            if key in compatibility:
                compatibility[key] = _string_list(compatibility[key], f"compatibility.{key}")
        for key in ("min_server", "max_server"):
            if key in compatibility and not isinstance(compatibility[key], str):
                raise ValueError(f"compatibility.{key} must be a string")

        tests = metadata.get("tests", [])
        if isinstance(tests, dict):
            scenarios = []
            for task in _string_list(tests.get("positive"), "tests.positive"):
                scenarios.append({"task": task, "should_match": True})
            for task in _string_list(tests.get("negative"), "tests.negative"):
                scenarios.append({"task": task, "should_match": False})
            tests = scenarios
        if not isinstance(tests, list) or len(tests) > 100:
            raise ValueError("tests must be a list of up to 100 scenarios")
        for scenario in tests:
            if (
                not isinstance(scenario, dict)
                or not isinstance(scenario.get("task"), str)
                or not isinstance(scenario.get("should_match"), bool)
            ):
                raise ValueError("each test needs task and boolean should_match")
            if "available_tools" in scenario:
                scenario["available_tools"] = _string_list(
                    scenario["available_tools"], "tests.available_tools"
                )

        skill_dir = path.parent.resolve()
        resource_entries = []
        total_bytes = len(raw)
        for relative in resources:
            resource_path = self._resource_path(skill_dir, relative)
            resource_raw = resource_path.read_bytes()
            try:
                resource_raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError(f"resource must be UTF-8 text: {relative!r}") from None
            total_bytes += len(resource_raw)
            resource_entries.append({
                "path": relative,
                "size": len(resource_raw),
                "version": hashlib.sha256(resource_raw).hexdigest(),
            })
        if total_bytes > MAX_CONTEXT_BYTES:
            raise ValueError(f"skill context exceeds {MAX_CONTEXT_BYTES} bytes")
        version_input = raw + b"".join(
            entry["path"].encode() + entry["version"].encode() for entry in resource_entries
        )
        return {
            "id": skill_id,
            "name": metadata["name"].strip(),
            "description": metadata["description"].strip(),
            "triggers": triggers,
            "compatibility": compatibility,
            "required_tools": required_tools,
            "optional_resources": resource_entries,
            "priority": priority,
            "exclusions": exclusions,
            "tests": tests,
            "content": content,
            "context_bytes": total_bytes,
            "estimated_tokens": (total_bytes + 3) // 4,
            "version": hashlib.sha256(version_input).hexdigest(),
        }

    def _scan(self):
        skills: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, str]] = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            skill_id = path.parent.name
            try:
                skills[skill_id] = self._load_skill(path)
            except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
                errors.append({"id": skill_id, "error": str(exc)})

        invalid_ids: set[str] = set()
        names: dict[str, list[str]] = {}
        triggers: dict[str, list[str]] = {}
        for skill_id, skill in skills.items():
            names.setdefault(skill["name"].casefold(), []).append(skill_id)
            for trigger in skill["triggers"]:
                triggers.setdefault(trigger.casefold(), []).append(skill_id)
        for ids in names.values():
            if len(ids) > 1:
                invalid_ids.update(ids)
                for skill_id in ids:
                    errors.append({"id": skill_id, "error": f"duplicate skill name shared by {ids}"})
        for trigger, ids in triggers.items():
            if len(ids) > 1:
                invalid_ids.update(ids)
                for skill_id in ids:
                    errors.append({"id": skill_id, "error": f"conflicting trigger {trigger!r} shared by {ids}"})
        for skill_id in invalid_ids:
            skills.pop(skill_id, None)

        self.cache = {key: value for key, value in self.cache.items() if key in skills}
        return skills, errors

    @staticmethod
    def _summary(skill):
        return {key: value for key, value in skill.items() if key not in {"content", "tests"}}

    @staticmethod
    def _catalog_version(skills: dict[str, dict[str, Any]], errors: list[dict[str, str]]) -> str:
        payload = [(skill_id, skill["version"]) for skill_id, skill in sorted(skills.items())]
        payload += [(error["id"], error["error"]) for error in errors]
        return hashlib.sha256(repr(payload).encode()).hexdigest()

    def list(self, known_catalog_version: str | None = None):
        with self.lock:
            skills, errors = self._scan()
            version = self._catalog_version(skills, errors)
            snapshot = {skill_id: skill["version"] for skill_id, skill in skills.items()}
            previous = self.catalog_history.get(known_catalog_version or "")
            self.catalog_history[version] = snapshot
            if known_catalog_version == version:
                return {
                    "skills": [], "errors": errors, "catalog_version": version,
                    "unchanged": True, "changes": {"added": [], "changed": [], "deleted": []},
                }
            changes = None
            if previous is not None:
                changes = {
                    "added": sorted(set(snapshot) - set(previous)),
                    "changed": sorted(
                        skill_id for skill_id in set(snapshot) & set(previous)
                        if snapshot[skill_id] != previous[skill_id]
                    ),
                    "deleted": sorted(set(previous) - set(snapshot)),
                }
            return {
                "skills": [self._summary(skill) for skill in skills.values()],
                "errors": errors,
                "catalog_version": version,
                "unchanged": False,
                "changes": changes,
            }

    def get(self, skill_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", skill_id):
            raise ValueError("skill_id is invalid")
        with self.lock:
            skills, errors = self._scan()
            if skill_id not in skills:
                detail = next((e["error"] for e in errors if e["id"] == skill_id), "not found")
                raise ValueError(f"Skill {skill_id!r}: {detail}")
            return dict(skills[skill_id])

    def _vectors(self, skill: dict[str, Any]) -> np.ndarray:
        cached = self.cache.get(skill["id"])
        if cached is None or cached[0] != skill["version"]:
            text = "\n".join([
                skill["id"], skill["name"], skill["description"], *skill["triggers"],
                *skill["exclusions"], skill["content"],
            ])
            vectors = np.stack([normalize(vector) for vector in self.encoder.passages(list(chunks(text)))])
            cached = (skill["version"], vectors)
            self.cache[skill["id"]] = cached
        return cached[1]

    @staticmethod
    def _lexical_match(skill: dict[str, Any], task: str) -> tuple[float, list[str]]:
        lowered = task.casefold()
        terms = set(re.findall(r"\w+", lowered))
        metadata = " ".join([skill["id"], skill["name"], skill["description"], *skill["triggers"]])
        words = set(re.findall(r"\w+", metadata.casefold()))
        score = len(terms & words) / max(len(terms), 1)
        reasons = []
        matching_triggers = [trigger for trigger in skill["triggers"] if trigger.casefold() in lowered]
        if matching_triggers:
            score = max(score, 0.9)
            reasons.append(f"matched trigger: {matching_triggers[0]}")
        elif score:
            reasons.append(f"matched {round(score * 100)}% of task terms")
        return score, reasons

    def search(self, query: str, limit: int = 5):
        if not query.strip() or len(query) > 4096:
            raise ValueError("query must contain 1-4096 characters")
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        with self.lock:
            skills, errors = self._scan()
            if not skills:
                return {"skills": [], "errors": errors}
            vector = normalize(self.encoder.query(query))
            scores = {}
            for skill_id, skill in skills.items():
                semantic = float(np.max(self._vectors(skill) @ vector))
                lexical, _ = self._lexical_match(skill, query)
                scores[skill_id] = 0.65 * max(semantic, 0) + 0.35 * lexical + skill["priority"] / 1000
            return {
                "skills": [
                    {**self._summary(skills[skill_id]), "score": round(scores[skill_id], 6)}
                    for skill_id in sorted(scores, key=scores.get, reverse=True)[:limit]
                ],
                "errors": errors,
            }

    def match(
        self,
        task: str,
        available_tools: list[str] | None = None,
        catalog_version: str | None = None,
    ) -> dict[str, Any]:
        if not task.strip() or len(task) > 4096:
            raise ValueError("task must contain 1-4096 characters")
        available = set(available_tools or [])
        with self.lock:
            skills, errors = self._scan()
            current_version = self._catalog_version(skills, errors)
            vector = normalize(self.encoder.query(task))
            matches, excluded = [], []
            for skill in skills.values():
                exclusion = next(
                    (rule for rule in skill["exclusions"] if rule.casefold() in task.casefold()), None
                )
                missing_tools = sorted(set(skill["required_tools"]) - available) if available_tools is not None else []
                lexical, reasons = self._lexical_match(skill, task)
                semantic = float(np.max(self._vectors(skill) @ vector))
                score = 0.55 * max(semantic, 0) + 0.35 * lexical + 0.10 * ((skill["priority"] + 100) / 200)
                if exclusion or missing_tools:
                    excluded.append({
                        "id": skill["id"],
                        "reasons": ([f"matched exclusion: {exclusion}"] if exclusion else [])
                        + ([f"missing required tools: {', '.join(missing_tools)}"] if missing_tools else []),
                    })
                elif lexical >= 0.15 or semantic >= 0.62:
                    if semantic >= 0.62:
                        reasons.append("semantic similarity exceeded the calibrated threshold")
                    if skill["priority"]:
                        reasons.append(f"manifest priority {skill['priority']}")
                    matches.append({
                        **self._summary(skill), "score": round(score, 6),
                        "match_reasons": reasons,
                    })
            matches.sort(key=lambda item: item["score"], reverse=True)
            return {
                "matches": matches,
                "excluded": excluded,
                "errors": errors,
                "catalog_version": current_version,
                "catalog_changed": catalog_version is not None and catalog_version != current_version,
            }

    def get_resources(self, skill_id: str, paths: list[str]) -> dict[str, Any]:
        if not isinstance(paths, list) or not 1 <= len(paths) <= 20:
            raise ValueError("paths must contain between 1 and 20 resource paths")
        with self.lock:
            skill = self.get(skill_id)
            declared = {resource["path"] for resource in skill["optional_resources"]}
            resources = []
            for relative in paths:
                if relative not in declared:
                    raise ValueError(f"resource is not declared by the skill: {relative!r}")
                path = self._resource_path((self.root / skill_id).resolve(), relative)
                raw = path.read_bytes()
                resources.append({
                    "path": relative,
                    "content": raw.decode("utf-8"),
                    "size": len(raw),
                    "version": hashlib.sha256(raw).hexdigest(),
                })
            return {"skill_id": skill_id, "resources": resources}

    def run_tests(self, skill_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            skills, errors = self._scan()
            if skill_id:
                if skill_id not in skills:
                    raise ValueError(f"Skill {skill_id!r} not found")
                skills = {skill_id: skills[skill_id]}
            results = []
            for skill in skills.values():
                for scenario in skill["tests"]:
                    available_tools = scenario.get("available_tools", [])
                    match_result = self.match(scenario["task"], available_tools)
                    actual = any(item["id"] == skill["id"] for item in match_result["matches"])
                    excluded_result = next(
                        (item for item in match_result["excluded"] if item["id"] == skill["id"]),
                        None,
                    )
                    missing = set(skill["required_tools"]) - set(available_tools)
                    results.append({
                        "skill_id": skill["id"], "task": scenario["task"],
                        "expected": scenario["should_match"], "actual": actual,
                        "passed": actual == scenario["should_match"],
                        "missing_tools": sorted(missing),
                        "exclusion_reasons": excluded_result["reasons"] if excluded_result else [],
                        "estimated_tokens": skill["estimated_tokens"],
                    })
            return {
                "results": results,
                "passed": sum(item["passed"] for item in results),
                "failed": sum(not item["passed"] for item in results),
                "errors": errors,
            }
