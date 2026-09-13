import asyncio

import numpy as np
import pytest
from mcp.server.fastmcp import FastMCP

from memos.server import register_skill_tools
from memos.skills import MAX_RESOURCE_BYTES, MAX_SKILL_BYTES, SkillLibrary


class Encoder:
    def __init__(self):
        self.calls = 0

    def query(self, text):
        return np.array([
            1 + text.lower().count("sqlite"),
            1 + text.lower().count("network"),
            1 + text.lower().count("deploy"),
        ])

    def passages(self, texts):
        self.calls += 1
        return [self.query(text) for text in texts]


def put(root, name="database", description="Fix sqlite problems", extra="", body="Investigate sqlite."):
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n{body}\n")
    return path


def test_live_catalog_search_and_version_negotiation(tmp_path):
    encoder = Encoder()
    library = SkillLibrary(tmp_path, encoder)
    empty = library.list()
    assert empty["skills"] == []
    assert empty["errors"] == []
    assert empty["catalog_version"]
    path = put(tmp_path, extra="triggers:\n  - database locked\npriority: 20\n")
    put(tmp_path, "network", "Fix network outages", body="Check network connectivity.")
    catalog = library.list(empty["catalog_version"])
    assert len(catalog["skills"]) == 2
    assert catalog["changes"]["added"] == ["database", "network"]
    assert catalog["skills"][0]["triggers"] == ["database locked"]
    assert catalog["skills"][0]["priority"] == 20
    assert "content" not in catalog["skills"][0]
    assert encoder.calls == 0
    assert library.list(catalog["catalog_version"])["unchanged"] is True
    assert "Investigate sqlite" in library.get("database")["content"]
    assert encoder.calls == 0
    assert library.search("sqlite")["skills"][0]["id"] == "database"
    assert encoder.calls == 2
    library.search("sqlite")
    assert encoder.calls == 2
    old_version = library.get("database")["version"]
    put(tmp_path, description="New sqlite guidance", extra="triggers: database\n")
    assert library.get("database")["version"] != old_version
    changed = library.list(catalog["catalog_version"])
    assert changed["changes"]["changed"] == ["database"]
    library.search("sqlite")
    assert encoder.calls == 3
    path.unlink()
    assert [skill["id"] for skill in library.list()["skills"]] == ["network"]
    assert "database" not in library.cache
    with pytest.raises(ValueError, match="not found"):
        library.get("database")


def test_manifest_matching_resources_and_runner(tmp_path):
    skill_dir = tmp_path / "deploy"
    skill_dir.mkdir()
    (skill_dir / "reference.md").write_text("Use the production deployment checklist.")
    put(
        tmp_path, "deploy", "Deploy services safely",
        extra=(
            "triggers:\n  - deploy the service\n"
            "required_tools:\n  - railway\n"
            "optional_resources:\n  - reference.md\n"
            "priority: 50\n"
            "exclusions:\n  - do not deploy\n"
            "compatibility:\n  platforms: [macos, linux]\n"
            "tests:\n"
            "  - task: deploy the service\n"
            "    should_match: true\n"
            "    available_tools: [railway]\n"
            "  - task: write a poem\n"
            "    should_match: false\n"
            "    available_tools: [railway]\n"
        ),
        body="Follow the deployment reference.",
    )
    library = SkillLibrary(tmp_path, Encoder())
    matched = library.match("Please deploy the service", ["railway"])
    assert matched["matches"][0]["id"] == "deploy"
    assert "matched trigger" in matched["matches"][0]["match_reasons"][0]
    missing = library.match("Please deploy the service", [])
    assert missing["matches"] == []
    assert "missing required tools" in missing["excluded"][0]["reasons"][0]
    excluded = library.match("do not deploy the service", ["railway"])
    assert "matched exclusion" in excluded["excluded"][0]["reasons"][0]

    resource = library.get_resources("deploy", ["reference.md"])["resources"][0]
    assert "production deployment" in resource["content"]
    with pytest.raises(ValueError, match="not declared"):
        library.get_resources("deploy", ["SKILL.md"])
    with pytest.raises(ValueError):
        library.get_resources("deploy", ["../reference.md"])
    results = library.run_tests("deploy")
    assert results["passed"] == 2
    assert results["failed"] == 0
    assert all(item["estimated_tokens"] > 0 for item in results["results"])


def test_bad_files_conflicts_and_oversized_resources(tmp_path):
    put(tmp_path)
    put(tmp_path, "invalid", extra="triggers: [12]\n")
    path = put(tmp_path, "no-header")
    path.write_text("# not a valid skill")
    put(tmp_path, "unsafe-yaml", description="!!python/object:builtins.object {}")
    path = put(tmp_path, "large")
    path.write_bytes(b"x" * (MAX_SKILL_BYTES + 1))
    put(tmp_path, "duplicate-name", extra="triggers: something unique\n")
    text = (tmp_path / "duplicate-name" / "SKILL.md").read_text().replace(
        "name: duplicate-name", "name: database"
    )
    (tmp_path / "duplicate-name" / "SKILL.md").write_text(text)
    put(tmp_path, "missing-resource", extra="optional_resources: [missing.md]\n")
    oversized = tmp_path / "oversized"
    oversized.mkdir()
    (oversized / "huge.md").write_bytes(b"x" * (MAX_RESOURCE_BYTES + 1))
    put(tmp_path, "oversized", extra="optional_resources: [huge.md]\n")
    library = SkillLibrary(tmp_path, Encoder())
    catalog = library.list()
    assert catalog["skills"] == []
    assert len(catalog["errors"]) == 8
    assert len(library.search("sqlite")["errors"]) == 8
    with pytest.raises(ValueError, match="triggers"):
        library.get("invalid")
    for skill_id in ("../secret", "/etc/passwd", "database/SKILL.md"):
        with pytest.raises(ValueError):
            library.get(skill_id)


def test_conflicting_triggers_and_external_symlinks_rejected(tmp_path):
    root = tmp_path / "skills"
    outside = tmp_path / "outside"
    put(outside)
    root.mkdir()
    (root / "linked").symlink_to(outside / "database", target_is_directory=True)
    put(root, "one", extra="triggers: same trigger\n")
    put(root, "two", extra="triggers: same trigger\n")
    library = SkillLibrary(root, Encoder())
    catalog = library.list()
    assert catalog["skills"] == []
    assert any("symlinks" in error["error"] for error in catalog["errors"])
    assert sum("conflicting trigger" in error["error"] for error in catalog["errors"]) == 2


def test_validation(tmp_path):
    library = SkillLibrary(tmp_path, Encoder())
    for query, limit in (("", 5), (" ", 5), ("x" * 4097, 5), ("sqlite", 0), ("sqlite", 21)):
        with pytest.raises(ValueError):
            library.search(query, limit)


def test_mcp_tools(tmp_path):
    put(tmp_path)
    mcp = FastMCP("test")
    register_skill_tools(mcp, SkillLibrary(tmp_path, Encoder()))

    async def check():
        tools = await mcp.list_tools()
        assert {tool.name for tool in tools} == {
            "list_skills", "search_skills", "get_skill", "match_skills", "get_skill_resources"
        }
        for name, arguments in (
            ("list_skills", {}), ("search_skills", {"query": "sqlite"}),
            ("get_skill", {"skill_id": "database"}),
            ("match_skills", {"task": "sqlite"}),
        ):
            result = await mcp.call_tool(name, arguments)
            assert result

    asyncio.run(check())
