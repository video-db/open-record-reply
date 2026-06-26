# VideoDB Record & Replay MCP

An MCP server for recording desktop workflows and generating reusable skill files. Demonstrates a task once on screen, and the server produces a `SKILL.json` and `SKILL.md` compiled from the recording.

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) package manager
- A [VideoDB](https://videodb.io) API key

## Setup

### 1. Install dependencies

```powershell
uv sync
```

### 2. Create `.env`

```
VIDEODB_API_KEY=sk-your_api_key_here
```

### 3. Configure your MCP client

**Claude Desktop** / **VS Code** — add to your MCP config:

```json
{
  "mcpServers": {
    "videodb-record-replay": {
      "command": "uv",
      "args": ["run", "python", "server.py"],
      "cwd": "/path/to/Record_Replay"
    }
  }
}
```

### 4. Restart your client

Three tools should appear:

| Tool | Description |
|------|-------------|
| `record_skill_tool(name)` | Start recording a workflow |
| `stop_recording_tool()` | Stop recording, get events + `video_id` |
| `compile_skill_tool(video_id, name)` | Generate `SKILL.json` + `SKILL.md` |

## Usage

```
record_skill_tool("my-workflow")
    → perform actions on screen
    → stop_recording_tool()
    → compile_skill_tool(video_id, "my-workflow")
```

Compiled skills land in `~/.mcp-videodb/skills/<name>/SKILL.json` and `SKILL.md`.
