import asyncio

import numpy as np
from mcp.server.fastmcp import FastMCP

from memos.server import register_memory_tools
from memos.store import Store


class Encoder:
    def query(self, text):
        lowered = text.lower()
        return np.array([1 + lowered.count("sqlite"), 1 + lowered.count("network")])

    def passages(self, texts):
        return [self.query(text) for text in texts]


def test_memory_mcp_tools_include_legacy_and_v2_interfaces(tmp_path):
    store = Store(tmp_path, Encoder())
    mcp = FastMCP("test")
    register_memory_tools(mcp, store)

    async def check():
        tools = await mcp.list_tools()
        assert {tool.name for tool in tools} == {
            "post_memo", "search_memos", "recall_context", "record_observation",
            "consolidate_memories", "record_retrieval_feedback", "get_memory_history",
            "rollback_memory", "get_memory_metrics",
        }
        posted = await mcp.call_tool("post_memo", {
            "title": "SQLite note", "body": "sqlite WAL", "project": "/repo", "device": "device",
        })
        assert posted
        assert await mcp.call_tool("search_memos", {"query": "sqlite"})
        assert await mcp.call_tool("recall_context", {"task": "sqlite"})
        assert await mcp.call_tool("get_memory_metrics", {})

    asyncio.run(check())
