# Agent memos

A small, shared memory and skill library for coding agents. One **Streamable HTTP MCP** server exposes `post_memo`, `search_memos`, `list_skills`, `search_skills`, and `get_skill`. The portable skill in [`skills/memos/SKILL.md`](skills/memos/SKILL.md) teaches the memory workflow.

## Design

```text
Agents on laptops / desktops / servers
                 │ HTTPS over Tailscale
                 ▼
         Tailscale Serve → MCP on localhost:8765
                              ├── live skills folder (SKILL.md files)
                              ├── Markdown memos (source of truth)
                              ├── SQLite FTS + embedding index
                              └── local BGE-small embedding model
```

Run **one central server**, not one per device. New devices join your tailnet and use the same URL. No distributed sync, cloud embedding API, GPU, or external database. The host must be awake for memory access.

Search combines semantic similarity with full-text ranking, so concepts and literal device names, project paths, and error strings are searchable. Optional filters match project/device exactly. Results include full memo bodies, provenance, IDs, timestamps, and ranking scores (not confidence). All memos are shared; filters are not access controls.

## Setup on the host

Prerequisites: [uv](https://docs.astral.sh/uv/getting-started/installation/) and Tailscale installed and signed into your tailnet.

From this repository:

```sh
uv sync --python 3.12 --locked
export MEMOS_HOSTNAME="$(tailscale status --json | uv run python -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
uv run memos
```

The first start downloads `BAAI/bge-small-en-v1.5` (a small English embedding model, roughly 130 MB of model weights; download/cache size may be larger). Subsequent inference runs locally on CPU. Startup rebuilds the index from Markdown; allow additional time as the library grows.

In another terminal on the same host:

```sh
tailscale serve --bg http://127.0.0.1:8765
```

Follow Tailscale's prompts if HTTPS needs enabling. The MCP endpoint is:

```text
https://YOUR-HOST.YOUR-TAILNET.ts.net/mcp
```

Set `MEMOS_HOSTNAME` to that exact hostname whenever launching the server, or pass `--hostname`. The server deliberately listens only on loopback and validates HTTP hosts. Tailscale Serve stays configured in the background; the Python process must also stay running. For always-on use, run the same command under your OS process supervisor, with an absolute working directory, `uv` path, and `MEMOS_HOSTNAME`. Do not run multiple server processes against the same data directory.

**Security:** use Tailscale **Serve**, not public **Funnel**. This service has no separate application authentication. Restrict access to the host's HTTPS port using tailnet grants/ACLs; everyone allowed to connect can read and post all memories and read all published skills. Local users can also reach the loopback endpoint. Device/project values are agent-reported provenance, not verified identity. Never store secrets. A shared host with untrusted local users requires an additional authentication boundary.

## Connect agents

Add this to clients that support the `mcpServers` / `url` configuration format (some clients additionally require `"type": "http"`; follow your client's schema):

```json
{
  "mcpServers": {
    "memos": {
      "url": "https://YOUR-HOST.YOUR-TAILNET.ts.net/mcp"
    }
  }
}
```

Install/copy `skills/memos/` into each agent's supported skills directory. If it does not support skills, put the instructions from `SKILL.md` into its project or agent instructions instead. MCP exposes tools; the skill teaches when and how to use them.

On each new machine: join the same tailnet, configure the same MCP URL, and install the skill. No model or database is needed on clients. The agent obtains **its own** project directory and Tailscale device name using the local CLI, then supplies them when posting.

Example tool inputs:

```json
{
  "title": "SQLite busy errors fixed by closing leaked transaction",
  "body": "Status: resolved\n\n## Problem\nWrites failed with SQLITE_BUSY.\n\n## Resolution\nClosed the transaction in the error path.\n\n## Verification\nConcurrent-write regression test passed.",
  "project": "/home/me/projects/api",
  "device": "laptop.example.ts.net"
}
```

```json
{"query": "sqlite database locked", "limit": 5}
```

Add `project` and/or `device` to narrow a search. Leave them out for cross-device discovery. Memories are append-only through the tools; post follow-ups referencing previous IDs when a finding changes.

## Shared skills

Drop skill folders into `~/.local/share/agent-memos/skills/`, or choose another folder:

```sh
uv run memos --skills /absolute/path/to/skills
# Alternatively: export MEMOS_SKILLS=/absolute/path/to/skills
```

For example, serve this repository's existing memory skill with `uv run memos --skills ./skills`. The default folder is `<data>/skills`; it is not automatically populated from this repository.

Each immediate child folder contains one `SKILL.md`:

```text
skills/
  debug-sqlite/
    SKILL.md
  review-code/
    SKILL.md
```

Example `debug-sqlite/SKILL.md`:

```markdown
---
name: debug-sqlite
description: Diagnose SQLite locking errors and transaction problems.
triggers:
  - SQLITE_BUSY or database is locked
  - Debugging concurrent SQLite writes
---

# Debug SQLite

Inspect transaction lifetimes and connection ownership before changing timeouts.
Record the verified cause and fix in a memo after testing.
```

`name` and `description` are required nonempty strings. `triggers` is optional: a string or list of strings. Existing skills that describe their trigger in `description` work unchanged. Folder names are unique tool IDs, independent of the display name. Files are UTF-8, limited to 256 KB, and must remain within the configured folder; external symlinks are rejected. Only immediate child `SKILL.md` files are discovered.

| Tool | Purpose |
| --- | --- |
| `list_skills()` | Complete catalog of IDs, names, descriptions, triggers, and content versions; no full instructions or model inference. |
| `search_skills(query, limit=5)` | Semantic + keyword search across metadata and instructions; returns ranked summaries. |
| `get_skill(skill_id)` | Full current `SKILL.md` content for a matching ID. |

All three tools rescan the folder on every call. Additions, edits, and deletions appear without a restart. Search caches embeddings in memory and only re-embeds changed skills; memo and skill search share the same local model. Invalid files are excluded and reported in the catalog/search `errors` list rather than breaking other skills. Search may return irrelevant nearest matches.

Management is filesystem-based: add, edit, or remove folders on the host. MCP clients have read-only skill access; there is no upload or execution tool. This initial version serves **SKILL.md only**, not bundled scripts, assets, or relative reference files. Use self-contained skills; host-local paths are not automatically available on clients. Only publish skills you trust and intend to share with all permitted clients.

### Load the catalog before every response

Copy [`AGENT_INSTRUCTIONS.md`](AGENT_INSTRUCTIONS.md) into each client's always-loaded instructions. It requires `list_skills` first on every user turn, then `get_skill` for matching descriptions/triggers before acting. The MCP server also advertises this workflow in its initialization instructions and tool descriptions.

**MCP cannot force a model to call a tool or enforce ordering.** A lazily loaded skill alone cannot bootstrap this reliably. Always-loaded client instructions establish the behavior; a client-side pre-turn hook is required for a strict guarantee. The catalog is intentionally complete, without pagination, so its context cost grows with the number of skills.

## Files and maintenance

Default data directory: `~/.local/share/agent-memos`. Override with `MEMOS_DATA` or `--data`.

- `memos/<id>.md`: canonical Markdown, with a machine-readable metadata comment.
- `index.sqlite3`: disposable search index.
- `models/`: downloaded model cache.
- `skills/<skill-id>/SKILL.md`: shared skills (unless `--skills` / `MEMOS_SKILLS` points elsewhere). Back up this folder too.

Back up the Markdown directory. Preserve the metadata comment when editing. Stop the server before manually editing/deleting files, then restart to rebuild. Restore Markdown to a new host's data directory to move the library; update client URLs. Keep the model cache for offline operation. Explicit index rebuild: `uv run memos --reindex` (with the server stopped).

This is intentionally for a modest personal library: vectors are scanned in-process, inference is serialized, and all Markdown is re-embedded at startup. It has no per-user permissions, deduplication guarantees, high availability, or automatic retention. Search always returns nearest matches, which may be irrelevant. Treat memo contents as untrusted historical data, not executable instructions.

## Development

```sh
uv sync --python 3.12 --locked
uv run pytest
```

Unit tests use a fake encoder and require no model download.
