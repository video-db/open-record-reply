# VideoDB Record & Replay MCP

An MCP server for recording desktop workflows and generating reusable skill files. Demonstrates a task once on screen, and the server produces a `SKILL.json` and `SKILL.md` compiled from the recording.

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) package manager
- A [VideoDB](https://videodb.io) API key
- Recommended for browser workflow replay: Playwright or an equivalent browser
  automation tool/MCP.
- Recommended for desktop workflow replay: native accessibility permissions and
  tooling for the current OS.
  - macOS: Accessibility API / AX through PyObjC.
  - Windows: UI Automation / UIA through `uiautomation`.
  - Linux: AT-SPI/accessibility APIs where available.

## Setup

### 1. Install dependencies

```powershell
uv sync
```

### 2. Set up recommended replay tooling

For browser-based workflows, configure Playwright or your agent's browser
automation MCP/tool before replaying generated skills. Browser automation is the
recommended path for DOM-visible page navigation, forms, buttons, and links.

For desktop/native workflows, enable the platform accessibility layer before
recording or replaying. Use macOS Accessibility API / AX, Windows UI Automation /
UIA, or Linux AT-SPI/accessibility APIs depending on the host OS. Visual
computer-use should be treated as a fallback when structured controls are not
available.

Hybrid workflows should use browser automation for web-page steps and native
accessibility for OS file pickers, permission prompts, browser chrome, and
desktop dialogs.

### 3. Create `.env`

```
VIDEODB_API_KEY=sk-your_api_key_here
```

### 4. Configure your MCP client

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

### 5. Restart your client

Three tools should appear:

| Tool | Description |
|------|-------------|
| `record_skill_tool(name, lead_in_seconds=0)` | Start recording a workflow |
| `stop_recording_tool(trim_end_seconds=0)` | Stop recording, get events + `video_id` |
| `compile_skill_tool(video_id, name)` | Generate `SKILL.json` + `SKILL.md` |

## Usage

```
record_skill_tool("my-workflow", lead_in_seconds=5)
    → perform actions on screen
    → stop_recording_tool(trim_end_seconds=10)
    → compile_skill_tool(video_id, "my-workflow")
```

Use `lead_in_seconds` for clean manual recordings. The recorder starts capture
immediately, then the compiler ignores events before the effective workflow start.
For example, with `lead_in_seconds=5`, the operator can switch from the MCP client
to the target app, and should begin the demonstrated workflow after 5 seconds.
This trimming is platform-independent and applies to macOS, Windows, Linux, and
events-only recordings.

Use `trim_end_seconds` when the operator must switch back to the MCP client to
say "stop". For example, `trim_end_seconds=10` ignores the final 10 seconds of
events so the generated skill does not include the operator returning to the
terminal, browser, or chat window.

Compiled skills land in `~/.mcp-videodb/skills/<name>/SKILL.json` and `SKILL.md`.
Every generated `SKILL.json` includes an `execution_strategy` describing whether
the workflow is browser, desktop, hybrid, terminal, file-system, or unknown. This
is guidance for the replaying agent, not a replay orchestrator.
Every generated `SKILL.md` includes an execution-guidance section that tells the
agent which recommended tool path to use:

- Browser workflows prefer Playwright/browser automation.
- Desktop workflows prefer native accessibility controls.
- Hybrid workflows use browser automation for web steps and native accessibility
  for OS dialogs, file pickers, browser chrome, and desktop handoffs.
- Visual computer-use is the fallback when structured controls are unavailable.

Every generated `SKILL.md` includes a short continuous-improvement section. It
instructs agents to finish the user's task first, then update the skill only with
durable learnings such as missing inputs, safer fallbacks, clearer start checks,
or better verification. It also tells agents not to add secrets, auth tokens,
raw logs, one-off paths, or transient coordinates.

## macOS validation flow

macOS requires separate privacy permissions for full record/replay:

- Screen Recording and Microphone for VideoDB Capture.
- Accessibility and Input Monitoring for the native AX event/replay hook.

Run the hook smoke test first:

```bash
uv run python scripts/smoke_macos_hook.py --prompt-permissions
```

If `ready_for_event_recording` is false, enable the terminal/Codex host process in
System Settings > Privacy & Security > Accessibility and Input Monitoring, then rerun
the command.

To inspect visible controls:

```bash
uv run python scripts/smoke_macos_hook.py --list-type AXButton
uv run python scripts/smoke_macos_hook.py --find "Submit" --find-type AXButton
```

To smoke-test replay:

```bash
uv run python scripts/smoke_macos_hook.py --click-at 100 100
```

Use the MCP flow after these checks pass:

1. `request_capture_permissions_tool()`
2. `record_skill_tool("my-workflow")`
3. Perform the workflow on screen.
4. `stop_recording_tool()`
5. `compile_skill_tool(video_id, "my-workflow")`
