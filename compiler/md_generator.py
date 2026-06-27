"""Generate SKILL.md from compiled SKILL.json following the agentskills.io format.

Produces YAML frontmatter + Markdown with natural-language instructions.
No AX types, coordinates, or internal IDs are exposed to the reader.
"""

import asyncio
import json
import logging
import re

from config import LLM_MODEL
from state import state
from compiler.tool_manifest import load_tool_manifest, surface_tool_guidance, tool_details

logger = logging.getLogger(__name__)

MD_SYSTEM_PROMPT = """You are a technical documentation writer. Convert a compiled
skill definition (JSON) into a SKILL.md file following the agentskills.io standard
(https://agentskills.io/specification).

## FORMAT

Output ONLY the markdown file content. Use this exact structure:

```
---
name: skill-name
description: "What the skill does and when to use it. Include trigger keywords for implicit AI agent invocation."
---

# Human-Readable Skill Title

## When to use this
- Precondition 1 â€” describe the state the application must be in
- Precondition 2
- (Use the skill's preconditions as a starting point, but write them as natural prose)

## Inputs
- `variable_name`: Description of what to enter, including format and example value
  (e.g., "Date string in DD/MM/YYYY format, e.g. '23/06/2026'")
- `category`: The category to select from the dropdown â€” one of "Travel", "Meals", "Office Supplies"

## Steps
1. Step one â€” describe the element visually (color, position, label text), the action to
   take (click, type, select), and what value to enter/set using `variable_name`.
2. Step two â€” etc. Each step is a single paragraph with visual cues so an AI agent
   using Computer Use can locate and interact with the element.

## Verification
- What visible confirmation to check for after the workflow completes
- Derived from the verification checks â€” describe what the user sees (green banner,
  confirmation message, success text, etc.)
```

## RULES

- NEVER mention pixel coordinates, element_at_X_Y labels, AXButton/AXTextField types,
  or any internal IDs. These are implementation details hidden from the reader.
- Describe elements using what a HUMAN would see: color, label text, position
  ("in the top-right corner", "at the bottom of the form", "in the center"), and purpose.
- Variable placeholders use backtick notation: `variable_name`.
- Use the `target_label`, `visual_context`, and `expected_scene` fields from each step to describe
  what is visible on screen â€” element colors, positions, labels, surrounding context.
- Use `start_context` to describe where to begin. Treat its `locator` generically:
  it may be a URL, app name, file path, command, workspace, or other launch target.
- Use the `description` field from each input definition to explain what each variable
  is and what format it expects.
- The `description` frontmatter field should be 1-3 sentences. Front-load the key use
  case. Include keywords the agent can use for implicit invocation.
- Keep the skill under 500 lines. Be concise but thorough.
- Action-oriented language: "Click the...", "Type ... into the...", "Select `option` from the..."
- Match the Codex skill style: practical, playbook-like instructions, no fluff.
- Use valid UTF-8 text with plain ASCII punctuation. Do not output mojibake or mis-decoded character sequences such as "â€”", "â€™", "â€œ", "â€�", "â€¦", or "Â". Use "-", "'", '"', "...", and ordinary spaces instead."""


async def generate_skill_md(skill: dict) -> str:
    data = _extract_skill_data(skill)

    user_prompt = (
        f"Convert this skill definition into a SKILL.md file:\n\n"
        f"```json\n{json.dumps(data, indent=2)}\n```\n\n"
        f"Output ONLY the SKILL.md file content. No preamble, no explanation, no code fences."
    )

    full_prompt = MD_SYSTEM_PROMPT + "\n\n" + user_prompt
    try:
        resp = await asyncio.to_thread(
            state.coll.generate_text,
            prompt=full_prompt,
            model_name=LLM_MODEL,
            response_type="text",
        )
        output = resp.get("output", "")
        if isinstance(output, dict):
            output = output.get("content", "") or output.get("text", "") or json.dumps(output)
        content = output.strip()
        content = _strip_code_fences(content)
        content = _clean_mojibake(content)
        content = _validate_and_clean_md(content, skill)
        content = _append_execution_guidance_section(content, skill)
        content = _append_self_improvement_section(content)
        return content
    except Exception as e:
        logger.warning(f"VLM MD generation failed, using template fallback: {e}")
        return _template_fallback(skill)


def _extract_skill_data(skill: dict) -> dict:
    steps_for_prompt = []
    for step in skill.get("steps", []):
        s = {
            "id": step.get("id"),
            "action": step.get("action"),
            "target_type": step.get("target", {}).get("type", ""),
            "target_label": step.get("target", {}).get("label", ""),
            "visual_context": step.get("visual_context", ""),
            "expected_scene": step.get("expected_scene", ""),
        }
        value = step.get("value")
        if value:
            s["value"] = value
        steps_for_prompt.append(s)

    inputs_for_prompt = {}
    inputs = skill.get("inputs", {})
    if isinstance(inputs, dict):
        for iname, ispec in inputs.items():
            if isinstance(ispec, dict):
                inputs_for_prompt[iname] = {
                    "type": ispec.get("type", "string"),
                    "example": ispec.get("example", ""),
                    "format": ispec.get("format", ""),
                    "values": ispec.get("values", []),
                    "description": ispec.get("description", ""),
                }
            else:
                inputs_for_prompt[iname] = {"type": "string", "example": str(ispec)}
    elif isinstance(inputs, list):
        for item in inputs:
            if isinstance(item, dict):
                iname = item.get("name", "")
                inputs_for_prompt[iname] = {
                    "type": item.get("type", "string"),
                    "example": item.get("example", ""),
                    "format": item.get("format", ""),
                    "values": item.get("values", []),
                    "description": item.get("description", ""),
                }

    verification = []
    for v in skill.get("verification", []):
        verification.append({
            "type": v.get("type", ""),
            "check": v.get("check", ""),
        })

    return {
        "name": skill.get("name", ""),
        "description": skill.get("description", ""),
        "start_context": skill.get("start_context", {}),
        "execution_strategy": skill.get("execution_strategy", {}),
        "recorded_surface": skill.get("recorded_surface", {}),
        "preconditions": skill.get("preconditions", []),
        "inputs": inputs_for_prompt,
        "steps": steps_for_prompt,
        "verification": verification,
    }


def _clean_mojibake(content: str) -> str:
    replacements = {
        "\u00e2\u20ac\u201d": "-",
        "\u00e2\u20ac\u201c": "-",
        "\u00e2\u20ac\u00a6": "...",
        "\u00e2\u20ac\u02dc": "'",
        "\u00e2\u20ac\u2122": "'",
        "\u00e2\u20ac\u0153": '"',
        "\u00e2\u20ac\ufffd": '"',
        "\u00c2": "",
    }
    for bad, good in replacements.items():
        content = content.replace(bad, good)
    return content

def _strip_code_fences(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        if len(lines) > 1:
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
        content = "\n".join(lines).strip()
    return content


def _validate_and_clean_md(content: str, skill: dict) -> str:
    name = skill.get("name", "")
    if not content.startswith("---") and f"name: {name}" not in content:
        content = f"---\nname: {name}\ndescription: \"{skill.get('description', '')}\"\n---\n\n{content}"
    content = re.sub(r'element_at_\d+_\d+', '', content)
    content = re.sub(r'\bAX(Button|TextField|PopUpButton|Checkbox|MenuItem|RadioButton|StaticText|Link|TextArea)\b', '', content)
    content = re.sub(r'\(no scene match\)', '', content, flags=re.IGNORECASE)
    content = re.sub(r'\n{3,}', '\n\n', content)
    return content.strip() + "\n"


def _append_execution_guidance_section(content: str, skill: dict) -> str:
    cleaned = content.strip()
    if re.search(r"^## Execution Guidance\s*$", cleaned, flags=re.MULTILINE):
        return cleaned + "\n"

    strategy = skill.get("execution_strategy") if isinstance(skill, dict) else {}
    if not isinstance(strategy, dict):
        strategy = {}
    surface = str(strategy.get("surface") or "unknown").strip() or "unknown"

    manifest = load_tool_manifest()
    manifest_guidance = surface_tool_guidance(surface, manifest)
    preferred_tools = _string_list(strategy.get("preferred_tools")) or manifest_guidance["preferred_tools"]
    fallback_tools = _string_list(strategy.get("fallback_tools")) or manifest_guidance["fallback_tools"]
    notes = _string_list(strategy.get("notes")) or manifest_guidance["guidance"]
    details = tool_details(_dedupe(preferred_tools + fallback_tools), manifest)
    recorded_surface = skill.get("recorded_surface") if isinstance(skill, dict) else {}

    lines = ["## Execution Guidance"]
    lines.append(f"- Surface: `{manifest_guidance['surface']}`.")
    surface_summary = _recorded_surface_summary(recorded_surface)
    if surface_summary:
        lines.append(f"- Recorded surface: {surface_summary}.")
        lines.append("- Before acting, bring this exact app/window type to the foreground. Do not switch to another app, browser, or native client unless the user approves.")
    if preferred_tools:
        lines.append(f"- Preferred tool path: {_tool_names_sentence(preferred_tools)}.")
    if fallback_tools:
        lines.append(f"- Fallback tool path: {_tool_names_sentence(fallback_tools)}.")
    for note in notes:
        lines.append(f"- {note}")
    lines.append("- Use native system automation for replay. On macOS, use osascript/System Events, AX inspection, Finder clipboard file paste, keyboard shortcuts, screencapture, and visual checks. On Windows use UI Automation / UIA. On Linux use AT-SPI/accessibility APIs.")
    lines.append("- For browser steps, control the recorded visible browser app as a desktop app. Keep the user's existing browser, profile, login, extensions, and window state.")
    lines.append("- Do not use any separate browser automation session for normal replay.")
    lines.append("- Prefer native accessibility and system commands for the existing app/window; use visual computer-use only when structured controls are unavailable.")
    lines.append("- Resolve targets by accessibility role, label, value, placeholder, and nearby visual context before using coordinates.")
    lines.append("- Use recorded relative positions only as a fallback inside the matching app/window, not as absolute screen coordinates.")
    lines.append("- Verify visible state after navigation, file pickers, submits, sends, deletes, or other high-impact actions before continuing.")
    lines.append("- Before upload/send/post/delete actions, verify the selected item, attachment preview, target conversation/page, and message text. If verification is unclear, ask the user instead of retrying.")
    lines.append("- Treat duplicate uploads, sends, posts, deletes, and submissions as high-risk. Do not repeat them unless there is clear evidence the previous attempt did not happen.")
    lines.append("- Treat these as recommended setup tools for replay, not optional afterthoughts.")
    lines.append("- Do not inspect local app storage, cookies, or tokens unless the user explicitly provides API credentials for that path.")
    preferred_names = _tool_display_names(preferred_tools, details)
    fallback_names = _tool_display_names(fallback_tools, details)
    if preferred_names:
        lines.append(f"- Preferred setup before replay: {', '.join(preferred_names)}.")
    if fallback_names:
        lines.append(f"- Fallback setup before replay: {', '.join(fallback_names)}.")

    return f"{cleaned}\n\n" + "\n".join(lines).strip() + "\n"


SELF_IMPROVEMENT_SECTION = """## Continuous Improvement
- Complete the user's requested task first. After the task succeeds, update this skill if the run revealed a reusable improvement.
- Add only durable guidance: missing inputs, better start-context checks, safer fallbacks, clearer verification, or tool-specific quirks that apply beyond one local machine.
- Do not add secrets, auth tokens, private message contents, one-off local paths, transient coordinates, or raw logs. Convert local details into named inputs instead.
- Keep edits concise, preserve the YAML frontmatter, and avoid duplicating existing instructions.
"""


def _append_self_improvement_section(content: str) -> str:
    cleaned = content.strip()
    if re.search(r"^## Continuous Improvement\s*$", cleaned, flags=re.MULTILINE):
        return cleaned + "\n"
    return f"{cleaned}\n\n{SELF_IMPROVEMENT_SECTION}"


def _template_fallback(skill: dict) -> str:
    name = skill.get("name", "unknown")
    description = skill.get("description", "")
    lines = []

    desc_text = description if description else f"Automated skill for {name.replace('-', ' ').title()}"
    lines.append("---")
    lines.append(f"name: {name}")
    lines.append(f"description: \"{desc_text}\"")
    lines.append("---")
    lines.append("")
    lines.append(f"# {name.replace('-', ' ').title()}")
    lines.append("")

    preconditions = skill.get("preconditions", [])
    start_context = skill.get("start_context", {})
    if preconditions or start_context:
        lines.append("## When to use this")
        if isinstance(start_context, dict) and start_context:
            label = start_context.get("label", "Starting application state")
            instructions = start_context.get("instructions", "")
            locator = start_context.get("locator", "")
            if locator:
                lines.append(f"- Open to {label}: {locator}")
            else:
                lines.append(f"- Start from {label}")
            if instructions and instructions not in ("Application is open and ready", ""):
                lines.append(f"- {instructions}")
        for p in preconditions:
            if p != "Application is open and ready":
                lines.append(f"- {p}")
        lines.append("")

    inputs = skill.get("inputs", {})
    if isinstance(inputs, dict) and inputs:
        lines.append("## Inputs")
        for iname, ispec in inputs.items():
            if isinstance(ispec, dict):
                parts = []
                typ = ispec.get("type", "string")
                fmt = ispec.get("format", "")
                ex = ispec.get("example", "")
                desc = ispec.get("description", "")
                vals = ispec.get("values", [])
                if desc:
                    parts.insert(0, desc)
                if fmt:
                    parts.append(f"Format: {fmt}")
                if vals:
                    parts.append(f"One of: {', '.join(str(v) for v in vals)}")
                if ex:
                    parts.append(f"Example: {ex}")
                if not parts:
                    parts.append(f"Type: {typ}")
                lines.append(f"- `{iname}`: {'; '.join(parts)}")
            else:
                lines.append(f"- `{iname}`: {ispec}")
        lines.append("")

    steps = skill.get("steps", [])
    if steps:
        lines.append("## Steps")
        for i, step in enumerate(steps, 1):
            action = step.get("action", "click")
            value = step.get("value", "")
            vctx = step.get("visual_context", "")
            expected = step.get("expected_scene", "")

            element_desc = _build_element_description(vctx, expected, step, i, len(steps))
            is_variable = isinstance(value, str) and value.startswith("{{") and value.endswith("}}")

            if action == "type" and value:
                if is_variable:
                    lines.append(f"{i}. {element_desc}, then type `{value}`.")
                else:
                    lines.append(f"{i}. {element_desc} and type the recorded text.")
            elif action == "select" and value:
                if is_variable:
                    lines.append(f"{i}. {element_desc}, then select `{value}` from the options that appear.")
                else:
                    lines.append(f"{i}. {element_desc} and select the option from the dropdown.")
            elif action == "click":
                if i == len(steps):
                    lines.append(f"{i}. {element_desc} to complete the workflow.")
                else:
                    lines.append(f"{i}. {element_desc}.")
            elif action == "wait":
                lines.append(f"{i}. Wait for the application to respond and the next state to load.")
            elif action == "navigate":
                lines.append(f"{i}. Navigate to the required view.")
            else:
                lines.append(f"{i}. {element_desc}.")
        lines.append("")

    verification = skill.get("verification", [])
    if verification:
        lines.append("## Verification")
        for v in verification:
            check = v.get("check", "")
            if check:
                lines.append(f"- {check}")
        lines.append("")

    lines.append("")
    return _append_self_improvement_section(
        _append_execution_guidance_section("\n".join(lines), skill)
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _tool_names_sentence(tool_names: list[str]) -> str:
    return ", ".join(f"`{name}`" for name in tool_names)


def _tool_display_names(tool_names: list[str], details: list[dict]) -> list[str]:
    detail_by_name = {detail.get("name"): detail for detail in details}
    names = []
    for tool_name in tool_names:
        detail = detail_by_name.get(tool_name, {})
        if detail.get("recommended_setup", True):
            names.append(detail.get("display_name") or tool_name.replace("_", " "))
    return names


def _recorded_surface_summary(surface: object) -> str:
    if not isinstance(surface, dict):
        return ""
    parts = []
    app_name = str(surface.get("app_name") or "").strip()
    if app_name:
        parts.append(app_name)
    window_title = str(surface.get("window_title") or "").strip()
    if window_title:
        parts.append(f'window "{window_title}"')
    platform = str(surface.get("platform") or "").strip()
    if platform:
        parts.append(f"on {platform}")
    return ", ".join(parts)


def _build_element_description(vctx: str, expected: str, step: dict, idx: int, total: int) -> str:
    desc = vctx or expected or ""
    desc = desc.strip().rstrip(".")

    action = step.get("action", "click")
    target = step.get("target", {})
    label = target.get("label", "")
    friendly = _friendly_type(target.get("type", ""))

    if desc and desc.lower() != "(no scene match)":
        if desc[0].isupper():
            desc = desc[0].lower() + desc[1:]
        if action == "type":
            if label and not label.startswith("element_at_"):
                return f"Click the \"{label}\" {friendly} and type into it"
            return f"Click into the text field ({desc})"
        elif action == "select":
            if label and not label.startswith("element_at_"):
                return f"Click the \"{label}\" {friendly} to open its options"
            return f"Click the dropdown or option list ({desc})"
        else:
            return f"Click {desc}"

    if label and not label.startswith("element_at_"):
        if action == "type":
            return f"Click the \"{label}\" {friendly} and type into it"
        elif action == "select":
            return f"Click the \"{label}\" {friendly} to open its options"
        return f"Click the \"{label}\" {friendly}"

    if action == "click" and idx == total:
        return "Click the confirmation or submit button"
    elif action == "click":
        return f"Click the control at step {idx}"
    elif action == "type":
        return "Click into the text field"
    elif action == "select":
        return "Click the dropdown or options list"
    return f"Interact with the control at step {idx}"


def _friendly_type(ax_type: str) -> str:
    mapping = {
        "AXButton": "button",
        "AXTextField": "text field",
        "AXPopUpButton": "dropdown",
        "AXCheckbox": "checkbox",
        "AXMenuItem": "menu item",
        "AXRadioButton": "radio button",
        "AXStaticText": "label",
        "AXLink": "link",
        "AXTextArea": "text area",
    }
    return mapping.get(ax_type, "element")
