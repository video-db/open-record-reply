# VideoDB Record & Replay MCP

An MCP server for recording desktop workflows and generating reusable skill files. Demonstrates a task once on screen, and the server produces a `SKILL.json` and `SKILL.md` compiled from the recording.

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) package manager
- A [VideoDB](https://videodb.io) API key
- Recommended for replay: native accessibility permissions and system automation
  tooling for the current OS.
  - macOS: Accessibility API / AX, System Events/`osascript`, Finder clipboard,
    keyboard shortcuts, and `screencapture`.
  - Windows: UI Automation / UIA through `uiautomation`.
  - Linux: AT-SPI/accessibility APIs where available.

## Setup

### 1. Install dependencies

```powershell
uv sync
```

### 2. Set up recommended replay tooling

Replay is native-system-first. For browser and app workflows, control the
recorded visible app directly instead of launching a separate automation browser.
This keeps Safari, Chrome, Brave, Edge, Slack desktop, existing logins,
extensions, profile state, and OS dialogs aligned with what the user
demonstrated.

On macOS, Codex should use system commands and native automation such as
`osascript`/System Events, AX inspection, Finder clipboard file paste, keyboard
shortcuts, `screencapture`, and visual checks. On Windows use UI Automation /
UIA. On Linux use AT-SPI/accessibility APIs. Visual computer-use is a fallback
when structured controls are unavailable.

Generated skills should use the recorded visible app directly and should not use
separate browser automation sessions for normal replay.

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
| `record_skill_tool(name, lead_in_seconds=0)` | Start a human-operated workflow recording |
| `stop_recording_tool(trim_end_seconds=0)` | Stop recording after the operator says stop, get events + `video_id` |
| `compile_skill_tool(video_id, name)` | Generate `SKILL.json` + `SKILL.md` |

## Usage

```
record_skill_tool("my-workflow", lead_in_seconds=5)
    → agent tells the operator recording is active
    → operator performs actions on screen
    → operator says "stop"
    → agent calls stop_recording_tool(trim_end_seconds=10)
    → agent calls compile_skill_tool(video_id, "my-workflow")
```

Recording is human-in-the-loop. The agent starts recording, announces that
recording is active, tells the operator when to begin after the lead-in, then
waits. The human operator performs the actual UI workflow being captured. The
agent should not inspect the repo, drive the browser, click UI controls, or
otherwise automate the target workflow while recording is active
unless the user explicitly asks the agent to demonstrate the workflow itself.

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

- Browser workflows use the recorded visible browser app through native system
  automation.
- Desktop workflows use native accessibility and system commands.
- Hybrid workflows prefer native accessibility across browser, desktop, file
  picker, browser chrome, and OS-dialog steps.
- Visual computer-use is the fallback when structured controls are unavailable.
- Separate browser automation sessions are not generated replay fallbacks.

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
