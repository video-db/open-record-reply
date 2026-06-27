<!-- PROJECT SHIELDS -->
[![Python][python-shield]][python-url]
[![MCP][mcp-shield]][mcp-url]
[![uv][uv-shield]][uv-url]
[![License][license-shield]][license-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![Website][website-shield]][website-url]

<br />
<p align="center">
  <a href="https://videodb.io/"><img src="https://videodb.io/assets/logos/wordmark-dark.png" alt="VideoDB" height="72"></a>
</p>

<h1 align="center">VideoDB Record & Replay</h1>

<p align="center">
  Record desktop workflows once. Replay them anywhere.
  <br />
  <br />
  <strong>Record &rarr; Compile &rarr; Replay</strong>
</p>

<p align="center">
  <a href="#installation">Install</a>
  ·
  <a href="#features">Features</a>
  ·
  <a href="#how-it-works">How It Works</a>
  ·
  <a href="https://docs.videodb.io"><strong>Docs</strong></a>
  ·
  <a href="https://github.com/video-db/open-record-reply/issues">Report Bug</a>
</p>

---

## What is Record & Replay?

An MCP server that gives AI agents the ability to watch, learn, and replay human desktop workflows. 

- **Record** — Captures every click, keystroke, and UI element through native accessibility APIs while simultaneously recording screen video to VideoDB for visual reference.
- **Compile** — An LLM transforms the event log and scene descriptions into reusable `SKILL.json` and human-readable `SKILL.md` files.
- **Replay** — Agents play back skills with variable substitution across macOS, Windows, and Linux.

Demonstrate a task once on screen, and the server produces a self-contained, versioned, agent-executable skill.

---

## How It Works

```
Human performs workflow
        │
        ▼
┌──────────────────────────────────────────────────────────────────┐
│  AX hooks ──► events.jsonl      (deterministic action log)       │
│  Capture SDK ──► video_id       (visual reference)               │
└──────────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────────┐
│  Compiler (LLM)                                                  │
│  events.jsonl + matched scene descriptions ──► SKILL.json        │
│  SKILL.json ──► SKILL.md        (agent-readable)                 │
└──────────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────────┐
│  Replay                                                          │
│  Read SKILL.json → substitute {{variables}} → native execution  │
│  Self-healing: AX re-lookup → frame delta → semantic search      │
└──────────────────────────────────────────────────────────────────┘
```

The camera records your screen. The accessibility hooks capture the deterministic truth. Together, they produce skills that are both precise and visually verifiable.

---

## Features

| Feature | Description |
|---------|-------------|
| **Dual recording** | Captures deterministic AX events + screen video simultaneously for precision and auditability |
| **LLM compilation** | VideoDB's VLM generates structured SKILL.json from event logs and scene descriptions |
| **Graceful degradation** | Falls back to events-only recording when screen capture is unavailable |
| **Cross-platform** | Native accessibility hooks for Windows (UIA), macOS (AX), and Linux (AT-SPI) |
| **Skill versioning** | Auto-increments on recompile; archives old versions as `SKILL.vN.json` |
| **Variable templating** | Detects search queries, dates, dropdown choices, and other reusable inputs |
| **Visual self-healing** | Stores scene descriptions alongside each step for future visual re-lookup |
| **Human-in-the-loop** | Recording is operator-driven, not agent-driven — the human demonstrates, the AI learns |

---

## Tools

| Tool | Parameters | Description |
|------|-----------|-------------|
| `request_capture_permissions_tool` | — | Request microphone and screen capture permissions before recording |
| `record_skill_tool` | `name: str`, `lead_in_seconds: float = 0.0` | Start a human-in-the-loop workflow recording |
| `stop_recording_tool` | `trim_end_seconds: float = 0.0` | Stop the active recording and export video to VideoDB |
| `compile_skill_tool` | `video_id: str`, `name: str` | Compile a recording into `SKILL.json` and `SKILL.md` |
| `list_skills_tool` | — | List all skills generated through this MCP |

### Resources

| Resource | Description |
|----------|-------------|
| `skills://list` | List all available skills as JSON |
| `skills://{name}/content` | Load a skill's `SKILL.md` into the agent context |

---

## Installation

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) package manager
- A [VideoDB](https://console.videodb.io) API key (free)

### 1. Clone and install

```bash
git clone https://github.com/video-db/open-record-reply.git
cd open-record-reply
uv sync
```

### 2. Set your API key

Create a `.env` file in the project root:

```env
VIDEODB_API_KEY=sk-your_api_key_here
```

### 3. Configure your MCP client

Add to your MCP config (`claude_desktop_config.json`, VS Code MCP settings, etc.):

```json
{
  "mcpServers": {
    "videodb-record-replay": {
      "command": "uv",
      "args": ["run", "python", "server.py"],
      "cwd": "/path/to/open-record-reply"
    }
  }
}
```

### 4. Restart your client

Five tools and two resources will appear. You're ready to record.

<details>
<summary><strong>Platform-specific setup</strong></summary>

**macOS** — Requires Screen Recording and Accessibility permissions. Run the smoke test first:

```bash
uv run python scripts/smoke_macos_hook.py --prompt-permissions
```

If `ready_for_event_recording` is false, enable the terminal process in **System Settings > Privacy & Security > Accessibility** and **Input Monitoring**, then rerun.

**Windows** — Uses UI Automation. No additional setup required beyond the standard install.

**Linux** — Uses AT-SPI. Ensure `at-spi2-core` is installed and your desktop environment has accessibility enabled.

</details>

---

## Usage

Recording is human-in-the-loop. The agent starts recording, announces that recording is active, then waits. The human operator performs the UI workflow being captured.

```
record_skill_tool("my-workflow", lead_in_seconds=5)
    → Agent tells the operator recording is active
    → Operator switches to the target app and performs the workflow
    → Operator returns to the MCP client and says "stop"
    → Agent calls stop_recording_tool(trim_end_seconds=10)
    → Agent calls compile_skill_tool(video_id, "my-workflow")
```

<details>
<summary><strong>lead_in_seconds</strong></summary>

The recorder starts capture immediately, then the compiler ignores events before the effective workflow start. With `lead_in_seconds=5`, the operator can switch from the MCP client to the target app and should begin the demonstrated workflow after 5 seconds.

</details>

<details>
<summary><strong>trim_end_seconds</strong></summary>

Discards events at the tail of the recording. Use when the operator must switch back to the MCP client to say "stop". For example, `trim_end_seconds=10` ignores the final 10 seconds so the generated skill does not include the operator returning to the terminal or chat window.

</details>

<details>
<summary><strong>Events-only mode</strong></summary>

If VideoDB screen capture is unavailable, the system falls back to recording AX events only. Call `compile_skill_tool` with `video_id=""` or `video_id="none"` to compile from events alone.

</details>

---

## Skill Output

Compiled skills land in `~/.mcp-videodb/skills/<name>/`:

| File | Purpose |
|------|---------|
| `SKILL.json` | Structured skill definition with steps, inputs, verification, execution strategy |
| `SKILL.md` | Human and agent-readable markdown following the agentskills.io standard |
| `SKILL.vN.json` | Archived previous versions on recompile |

Every generated `SKILL.json` includes an `execution_strategy` — `web_browser`, `desktop_app`, `hybrid`, `terminal`, `file_system`, or `unknown` — so the replaying agent knows which tool path to use. Every `SKILL.md` includes an execution guidance section and a continuous improvement section.

---

## Architecture

```
open-record-reply/
├── server.py                 # FastMCP entry point, tool and resource definitions
├── state.py                  # Shared server state singleton
├── config.py                 # Constants, .env loading
├── registry.py               # Skill CRUD + versioning
│
├── capture/
│   ├── recorder.py           # Records AX events + VideoDB capture simultaneously
│   ├── ax_client.py          # JSONL IPC wrapper for native AX companion
│   ├── capture_client.py     # VideoDB Capture SDK wrapper
│   └── native/
│       ├── ax_hook_win32.py   # Windows: UI Automation + keyboard polling + TCP IPC
│       ├── ax_hook_darwin.py  # macOS: Accessibility API + pynput + pipe IPC
│       └── ax_hook_linux.py   # Linux: AT-SPI + pynput + pipe IPC
│
├── compiler/
│   ├── compiler.py           # LLM compilation: index scenes → match events → prompt → normalize
│   ├── prompts.py            # LLM system prompt for structured skill generation
│   ├── md_generator.py       # Converts SKILL.json to agent-readable SKILL.md
│   ├── tool_manifest.py      # Surface-to-tool mapping for replay guidance
│   └── recommended_tools.json
│
├── schema/
│   └── skill.schema.json     # JSON Schema (draft-07) for SKILL.json validation
│
├── scripts/
│   ├── smoke_macos_hook.py   # macOS AX hook smoke test
│   └── test_native_desktop.py  # Windows desktop recorder and inspector (standalone)
│
└── tests/
    ├── conftest.py
    ├── test_recorder.py
    ├── test_compiler.py
    ├── test_ax_client.py
    ├── test_ax_hook_win32.py
    ├── test_ax_hook_darwin.py
    ├── test_md_generator.py
    └── test_tool_manifest.py
```

---

## Development

### Run tests

```bash
uv run pytest
```

Tests use `pytest` with `pytest-asyncio` (`asyncio_mode = "auto"`). All VideoDB API calls are mocked.

### Notebook

A Jupyter notebook (`notebook.ipynb`) provides a step-by-step walkthrough of the full record→compile pipeline, useful for understanding the flow without setting up an MCP client.

---

## Troubleshooting

<details>
<summary><strong>Recording won't start</strong></summary>

- Verify `VIDEODB_API_KEY` is set in `.env` and is valid
- Run `request_capture_permissions_tool` and approve any permission prompts
- On macOS, check Screen Recording and Accessibility permissions
- Check that no other application is using the accessibility hook

</details>

<details>
<summary><strong>Compilation fails or returns empty steps</strong></summary>

- Ensure the recording has meaningful UI interactions (not just idle time)
- Try events-only compilation (`video_id=""`) if video indexing is slow
- The LLM may need a retry — compilation automatically retries up to 2 times

</details>

<details>
<summary><strong>Permission prompts not appearing on macOS</strong></summary>

```bash
# Reset permissions and try again
uv run python scripts/smoke_macos_hook.py --prompt-permissions
```

If `ready_for_event_recording` is false, manually enable the terminal in **System Settings > Privacy & Security > Accessibility** and **Input Monitoring**.

</details>

<details>
<summary><strong>Windows: no keyboard events recorded</strong></summary>

- Ensure the app being recorded has UI Automation support (most modern apps do)
- Run `uv run python scripts/test_native_desktop.py` to verify UIA data quality

</details>

---

## Community & Support

- **Docs**: [docs.videodb.io](https://docs.videodb.io)
- **Issues**: [GitHub Issues](https://github.com/video-db/open-record-reply/issues)
- **Discord**: [Join the VideoDB community](https://discord.gg/py9P639jGz)
- **API Key**: [console.videodb.io](https://console.videodb.io)

---

<p align="center">Made with ❤️ by the <a href="https://videodb.io">VideoDB</a> team</p>

<!-- MARKDOWN LINKS & IMAGES -->
[python-shield]: https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white
[python-url]: https://www.python.org/
[mcp-shield]: https://img.shields.io/badge/MCP-1.0+-000000?style=for-the-badge&logo=anthropic&logoColor=white
[mcp-url]: https://modelcontextprotocol.io/
[uv-shield]: https://img.shields.io/badge/uv-package_manager-DE5FE2?style=for-the-badge&logo=astral&logoColor=white
[uv-url]: https://docs.astral.sh/uv/
[license-shield]: https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge
[license-url]: https://opensource.org/licenses/MIT
[stars-shield]: https://img.shields.io/github/stars/video-db/open-record-reply.svg?style=for-the-badge
[stars-url]: https://github.com/video-db/open-record-reply/stargazers
[issues-shield]: https://img.shields.io/github/issues/video-db/open-record-reply.svg?style=for-the-badge
[issues-url]: https://github.com/video-db/open-record-reply/issues
[website-shield]: https://img.shields.io/website?url=https%3A%2F%2Fvideodb.io%2F&style=for-the-badge&label=videodb.io
[website-url]: https://videodb.io/
