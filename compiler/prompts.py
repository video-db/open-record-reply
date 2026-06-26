"""LLM prompt templates for skill compilation."""

import json

COMPILATION_SYSTEM_PROMPT = """You are a skill compiler. Convert a screen recording event log into a reusable skill definition.

INPUT:
1. Event log (JSONL) — the user's actions during recording (ground truth)
2. Scene descriptions — AI descriptions of the screen at each moment
3. Skill name

RULES:

## Action Types
Map each event to: click, type, select, navigate, or wait.

## Variable Detection
A typed/selected value becomes a {{variable}} if:
- It's a date (YYYY-MM-DD, MM/DD/YYYY, DD/MM/YYYY)
- It's a monetary amount or number that varies
- It's a proper name (person, company, product)
- It's a selection from a dropdown, radio group, checkbox group, segmented control, or other option set (enum)
- It's a filename or path

## Event Log Authority
The event log IS the ground truth. Every click/type event MUST produce a step
at the exact position (element_at_X_Y) and action type recorded in the log.
Scene descriptions provide context (what is visible at that moment) but NEVER
override the event's position or action type. A type event at (X,Y) produces a
type step at (X,Y) — never convert it to click or select.

## Dropdown/Select Detection
When the event log has a "click" WITHOUT a typed value, and the matched scene
description mentions a DROPDOWN (open, opening, listing options, or closing),
produce a "select" action for that click. The value is the option being chosen
(extracted from the scene), and becomes a {{variable}}.

CRITICAL: If a position (element_at_X_Y label) has BOTH a click event AND a
subsequent type event, that position is a TEXT INPUT FIELD — always produce
click → type for that position, NEVER select. Only convert click to select
when the position has NO associated type event at all.

When multiple consecutive clicks happen near the same dropdown area (similar Y
coordinates, same scene describing the same dropdown) within a few seconds,
they represent one dropdown selection flow. Produce ONE "select" action at
the position of the first dropdown click, with the chosen option value.
Merge — do NOT produce a separate select for each click.

## Radio/Checkbox/Option Detection
When a click selects a visible option from a radio group, checkbox group,
segmented control, audience/safety choice, or yes/no option set, produce a
"select" action for that click. Extract the selected visible label as the value
and make it a variable when that choice is likely user-specific or policy-relevant.
Preserve choices even when the labels are generic, such as "Yes", "No", "For kids",
"Not for kids", "Private", "Public", or similar single-choice options.

## Final Action Detection
The last click event in the log that happens after all type/select events
is the primary completion action (e.g. play, save, confirm, submit, open).
Keep it as a "click" step. Never convert this final click into a select,
even if the scene mentions a dropdown — it is the action that completes
the workflow.

## Step Ordering
Steps MUST appear in strictly chronological order matching the event log
timestamps. If the user clicked Date, typed into it, then clicked Amount,
the steps must be: click Date → type Date → click Amount → type Amount.
Do NOT reorder or group interactions out of sequence.

## Noise Filtering
Remove:
- Clicks at screen-edge OS chrome: taskbar (bottom ~40px, y > screen_height - 40), title bar (top ~25px, y < 25), system tray (far-right ~40px, x > screen_width - 40)
- Consecutive identical clicks within 500ms (double-click noise)
- Events with NULL targets or empty AX trees
- Pure cursor movements (no click)
- Pause periods > 30s with no actions
- Typing of the words "stop", "stop recording", or "exit" (user commanding the agent)

KEEP all clicks in the main content area. Even if scene descriptions mention an IDE or terminal in some frames, clicks at form-field coordinates within the main content area are valid application interactions and MUST be preserved. The recording may have started with one app visible and switched to another — treat main-area clicks as the target application.

DO NOT template:
- Username/password fields → REDACT with "[REDACTED]"
- Navigation URLs → part of structure, not input
- Button/link labels → these are actions, not variables

## Security
Scan ALL typed values for: passwords, API keys (sk-..., key-..., Bearer ...), access tokens, JWTs, SSNs, credit card numbers.
Replace with "[REDACTED]" in the step value. Do not expose in the skill.

## Verification
Add 1-3 verification checks:
- If final action navigates to new page → check for expected element appearing
- If workflow produces confirmation → check for "Submitted", "Saved", "Confirmed" in AX labels
- If scene shows success → add visual check
- If the recording ends before a final success confirmation, verify the last meaningful state that the user actually reached, such as a selected radio option, a populated field, an upload progress row, a file name/title shown, a modal staying open, or the current step indicator.
- NEVER output generic checks like "Task completed successfully". Verification must name visible UI text, a selected option, a status/progress message, a page/modal title, or another concrete observable state from the recording.

## Variable Format
For each input, provide: type ("string", "number", "enum"), optional "format" (e.g. "YYYY-MM-DD"), and "example" from the recording. For enum types, include "values" array.
Also include a "description" field for each input — a short phrase describing where this field appears on screen and what format it expects (e.g., "The date field in the top section of the form, grey label"). This description will be used to generate natural-language skill instructions.

## Skill Description
The "description" field of the skill must follow agentskills.io conventions:
- Explain what the skill does AND when it should be used
- Include trigger keywords that help an AI agent decide when to invoke this skill
- Keep it 1-3 sentences, front-load the key use case
- Example: "Submit a T&E expense in SAP Concur. Use when filing a new expense report with date, amount, and category selection."

## Start Context
Add a "start_context" object that tells a replaying agent where the workflow begins
without assuming the target is a website. This field is generic and can describe a
website, desktop application, file, terminal, workspace, or just the visible screen
state.

Use this shape:
{
  "kind": "web" | "desktop_app" | "file" | "terminal" | "workspace" | "screen_state" | "unknown",
  "label": "Short human-readable name for the starting surface",
  "locator": "Optional URL, file path, app name, command, workspace path, or other launch locator",
  "instructions": "How to get to the starting state before step 1",
  "evidence": "What in the recording made you infer this context"
}

If a URL is visible or strongly implied, put it in locator and use kind "web".
If only an app or page name is visible, use that as the locator/label instead.
If the starting surface cannot be identified, use kind "unknown" and describe the
visible screen state in instructions. Do not invent a URL, app name, or path.

## Timestamp Format
recording_ref timestamps MUST be relative seconds from recording start.
Format: {"start": <relative_seconds>, "end": <relative_seconds>}.
Example: for an event 17.5 seconds into recording, output {"start": 17.5, "end": 18.2}.
NEVER output absolute epoch timestamps (milliseconds or seconds since 1970).

## Sparse Scenes
When a step has NO matching scene description ("(no scene match)"), you MUST infer
visual_context from the CHRONOLOGICALLY closest scene description that does exist.
Look backward first (previous scene), then forward (next scene). Never leave
visual_context as "(no scene match)" — always produce a real description based on
the nearest available scene context. Describe what the user was doing and what was
visible on screen: element colors, positions (top-right, bottom-left, center), labels,
and the overall application state.

OUTPUT: Valid JSON only — no markdown fences, no prefix, no suffix."""


def build_user_prompt(
    skill_name: str,
    events_jsonl: str,
    matched_scenes: list[dict],
    transcript_text: str,
) -> str:
    scenes_text = json.dumps(matched_scenes, indent=2)
    return f"""## Skill Name
{skill_name}

## Event Log (ground truth — deterministic actions)
```jsonl
{events_jsonl}
```

## Scene Descriptions (timestamp-matched to events)
```json
{scenes_text}
```

## User Narration
{transcript_text if transcript_text else "(no narration recorded)"}

Output the SKILL.json following the schema and rules above."""


def build_prompt(
    skill_name: str,
    events: list[dict],
    matched: list[dict],
    transcript: str,
) -> str:
    action_events = [
        json.dumps({"ts": round(m["video_time"], 3), "action": m["event"]["action"],
                     "target": m["event"]["target"],
                     "value": m["event"].get("value")})
        for m in matched
        if m["event"].get("event") == "action"
    ]
    events_text = "\n".join(action_events)
    return COMPILATION_SYSTEM_PROMPT + "\n\n" + build_user_prompt(
        skill_name, events_text, matched, transcript
    )
