"""Minimal MCP server for recording workflows and generating skill files."""

import asyncio

import videodb
from mcp.server.fastmcp import FastMCP
from capture.capture_client import CaptureClient

from capture.recorder import record_skill, stop_recording
from compiler.compiler import compile_skill, compile_skill_events_only
from config import API_KEY, BASE_URL, CAPTURE_ACTIVE_TIMEOUT_SECONDS, COLLECTION_NAME, SKILLS_ROOT
from registry import list_skills, save_skill_md
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



async def _request_capture_permission(kind: str) -> dict:
    """Request one Capture SDK permission with bounded startup/cleanup time."""
    token = state.conn.generate_client_token()
    client = CaptureClient(
        client_token=token,
        base_url=BASE_URL if BASE_URL else None,
    )
    try:
        granted = await asyncio.wait_for(
            client.request_permission(kind),
            timeout=CAPTURE_ACTIVE_TIMEOUT_SECONDS,
        )
        return {
            "permission": kind,
            "granted": bool(granted),
            "status": "granted" if granted else "denied",
        }
    except asyncio.TimeoutError:
        return {"permission": kind, "granted": False, "status": "timeout"}
    except Exception as e:
        return {
            "permission": kind,
            "granted": False,
            "status": "error",
            "error": str(e),
        }
    finally:
        try:
            await asyncio.wait_for(client.shutdown(), timeout=5.0)
        except Exception:
            pass


@mcp.tool()
async def request_capture_permissions_tool() -> dict:
    """Ask the system for VideoDB capture permissions before recording."""
    await _ensure_connected()
    results = []
    for permission in ("microphone", "screen_capture"):
        results.append(await _request_capture_permission(permission))
    return {
        "status": "ok",
        "permissions": results,
        "ready_for_full_capture": all(item["granted"] for item in results),
        "note": "If a permission prompt appeared, approve it and run this tool again to verify.",
    }

@mcp.tool()
async def record_skill_tool(name: str, lead_in_seconds: float = 0.0) -> dict:
    """Start a human-in-the-loop workflow recording.

    The agent should call this tool, tell the human operator when recording is
    active and when to begin after the lead-in, then wait. The human performs
    the UI actions being recorded. Do not drive the target app with browser,
    desktop, or shell automation during recording unless the user explicitly
    asks the agent to demonstrate the workflow.
    """
    await _ensure_connected()
    return await record_skill(name, lead_in_seconds=lead_in_seconds)


@mcp.tool()
async def stop_recording_tool(trim_end_seconds: float = 0.0) -> dict:
    """Stop the active human-operated recording after the user says stop."""
    return await stop_recording(trim_end_seconds=trim_end_seconds)


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


@mcp.tool()
async def list_skills_tool() -> dict:
    """List all skills generated through this MCP."""
    skills = list_skills()
    return {"skills": skills, "count": len(skills)}


@mcp.resource("skills://list")
async def list_skills_resource() -> str:
    """List all available skills (loaded into agent context when selected)."""
    import json
    return json.dumps(list_skills(), indent=2)


@mcp.resource("skills://{name}/content")
async def get_skill_resource(name: str) -> str:
    """Load a skill's SKILL.md into the agent context."""
    path = SKILLS_ROOT / name / "SKILL.md"
    if not path.exists():
        raise FileNotFoundError(f"Skill '{name}' not found at {path}")
    return path.read_text(encoding="utf-8")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
