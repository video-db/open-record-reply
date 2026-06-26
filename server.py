"""Minimal MCP server for recording workflows and generating skill files."""

import videodb
from mcp.server.fastmcp import FastMCP

from capture.recorder import record_skill, stop_recording
from compiler.compiler import compile_skill, compile_skill_events_only
from config import API_KEY, BASE_URL, COLLECTION_NAME
from registry import save_skill_md
from state import state

mcp = FastMCP("videodb-record-replay")

_initialized = False


async def _ensure_connected() -> None:
    """Connect to VideoDB once for recording and compilation."""
    global _initialized
    if _initialized and state.conn and state.coll:
        return

    connect_kwargs = {"api_key": API_KEY}
    if BASE_URL:
        connect_kwargs["base_url"] = BASE_URL

    state.conn = videodb.connect(**connect_kwargs)
    collections = state.conn.get_collections()
    existing = next((c for c in collections if c.name == COLLECTION_NAME), None)
    if existing:
        state.coll = state.conn.get_collection(existing.id)
    else:
        state.coll = state.conn.create_collection(
            name=COLLECTION_NAME,
            description="MCP Record & Replay recordings",
        )
    ws = state.conn.connect_websocket()
    ws_conn = await ws.connect()
    state.ws_connection_id = ws_conn.connection_id
    _initialized = True


@mcp.tool()
async def record_skill_tool(name: str) -> dict:
    """Start recording a workflow that will later be compiled into a skill."""
    await _ensure_connected()
    return await record_skill(name)


@mcp.tool()
async def stop_recording_tool() -> dict:
    """Stop the active recording and return its event log and VideoDB video id."""
    return await stop_recording()


@mcp.tool()
async def compile_skill_tool(video_id: str, name: str) -> dict:
    """Compile a recording into SKILL.json and SKILL.md files."""
    await _ensure_connected()
    if not video_id or video_id.strip() == "" or video_id.lower() == "none":
        skill = await compile_skill_events_only(name)
    else:
        skill = await compile_skill(video_id, name)
    md_path = await save_skill_md(skill)
    skill["skill_md_path"] = str(md_path)
    return skill


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()