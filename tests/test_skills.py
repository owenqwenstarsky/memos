import asyncio

import numpy as np
import pytest
from mcp.server.fastmcp import FastMCP

from memos.server import register_skill_tools
from memos.skills import MAX_SKILL_BYTES, SkillLibrary


class Encoder:
    def __init__(self):
        self.calls = 0

    def query(self, text):
        return np.array([1 + text.lower().count("sqlite"), 1 + text.lower().count("network"), 1])

    def passages(self, texts):
        self.calls += 1
        return [self.query(text) for text in texts]


def put(root, name="database", description="Fix sqlite problems", extra="", body="Investigate sqlite."):
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: Example\ndescription: {description}\n{extra}---\n\n{body}\n")
    return path


def test_live_catalog_and_search(tmp_path):
    encoder = Encoder()
    library = SkillLibrary(tmp_path, encoder)
    assert library.list() == {"skills": [], "errors": []}
    path = put(tmp_path, extra="triggers:\n  - database locked\n")
    put(tmp_path, "network", "Fix network outages", body="Check network connectivity.")
    catalog = library.list()
    assert len(catalog["skills"]) == 2
    assert catalog["skills"][0]["triggers"] == ["database locked"]
    assert "content" not in catalog["skills"][0]
    assert encoder.calls == 0  # Listing and retrieval never require inference.
    assert "Investigate sqlite" in library.get("database")["content"]
    assert encoder.calls == 0
    assert library.search("sqlite")["skills"][0]["id"] == "database"
    assert encoder.calls == 2
    library.search("sqlite")
    assert encoder.calls == 2
    old_version = library.get("database")["version"]
    put(tmp_path, description="New sqlite guidance", extra="triggers: database\n")
    assert library.get("database")["version"] != old_version
    library.search("sqlite")
    assert encoder.calls == 3
    path.unlink()
    assert [s["id"] for s in library.list()["skills"]] == ["network"]
    assert "database" not in library.cache
    with pytest.raises(ValueError, match="not found"):
        library.get("database")


def test_bad_files_do_not_break_catalog(tmp_path):
    put(tmp_path)
    put(tmp_path, "invalid", extra="triggers: [12]\n")
    path = put(tmp_path, "no-header")
    path.write_text("# not a valid skill")
    put(tmp_path, "unsafe-yaml", description="!!python/object:builtins.object {}")
    path = put(tmp_path, "large")
    path.write_bytes(b"x" * (MAX_SKILL_BYTES + 1))
    library = SkillLibrary(tmp_path, Encoder())
    catalog = library.list()
    assert len(catalog["skills"]) == 1
    assert len(catalog["errors"]) == 4
    assert len(library.search("sqlite")["errors"]) == 4
    with pytest.raises(ValueError, match="triggers"):
        library.get("invalid")
    for skill_id in ("../secret", "/etc/passwd", "database/SKILL.md"):
        with pytest.raises(ValueError):
            library.get(skill_id)


def test_external_symlinks_rejected(tmp_path):
    root = tmp_path / "skills"
    outside = tmp_path / "outside"
    put(outside)
    root.mkdir()
    (root / "linked").symlink_to(outside / "database", target_is_directory=True)
    library = SkillLibrary(root, Encoder())
    assert not library.list()["skills"]
    assert "symlinks" in library.list()["errors"][0]["error"]


def test_validation(tmp_path):
    library = SkillLibrary(tmp_path, Encoder())
    for query, limit in (("", 5), (" " , 5), ("x" * 4097, 5), ("sqlite", 0), ("sqlite", 21)):
        with pytest.raises(ValueError):
            library.search(query, limit)


def test_mcp_tools(tmp_path):
    put(tmp_path)
    mcp = FastMCP("test")
    register_skill_tools(mcp, SkillLibrary(tmp_path, Encoder()))

    async def check():
        tools = await mcp.list_tools()
        assert {tool.name for tool in tools} == {"list_skills", "search_skills", "get_skill"}
        for name, arguments in (("list_skills", {}), ("search_skills", {"query": "sqlite"}),
                                ("get_skill", {"skill_id": "database"})):
            result = await mcp.call_tool(name, arguments)
            assert result

    asyncio.run(check())
