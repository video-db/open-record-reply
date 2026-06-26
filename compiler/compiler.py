"""Compile a recording into a replayable SKILL.json."""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from videodb import SceneExtractionType

from config import (
    SCENE_INDEX_TIME_INTERVAL,
    SCENE_INDEX_FRAME_COUNT,
    LLM_MODEL,
    LLM_MAX_RETRIES,
)
from state import state
from compiler.prompts import build_prompt, COMPILATION_SYSTEM_PROMPT
from registry import save_skill

logger = logging.getLogger(__name__)


async def compile_skill(video_id: str, name: str) -> dict:
    video = state.coll.get_video(video_id)

    scene_index_id = None
    try:
        scene_index_id = video.index_scenes(
            extraction_type=SceneExtractionType.time_based,
            extraction_config={
                "time": SCENE_INDEX_TIME_INTERVAL,
                "frame_count": SCENE_INDEX_FRAME_COUNT,
            },
            prompt="Describe what the user is doing on screen at this moment: which element they are interacting with (clicking into a field, typing a value, selecting an option from a dropdown, choosing a radio button or checkbox, pressing a button), what value they are entering or selecting, and any visible changes that result from the interaction (e.g., dropdown closes with new value shown, radio button becomes selected, checkbox becomes checked, success message appears, new page loads).",
        )
    except Exception as e:
        msg = str(e)
        match = re.search(r"with id (\w+) already exists", msg)
        if match:
            old_id = match.group(1)
            try:
                video.delete_scene_index(old_id)
                logger.info(f"Deleted stale scene index {old_id}, creating fresh")
            except Exception:
                logger.warning(f"Could not delete stale index {old_id}")
            scene_index_id = video.index_scenes(
                extraction_type=SceneExtractionType.time_based,
                extraction_config={
                    "time": SCENE_INDEX_TIME_INTERVAL,
                    "frame_count": SCENE_INDEX_FRAME_COUNT,
                },
                prompt="Describe what the user is doing on screen at this moment: which element they are interacting with (clicking into a field, typing a value, selecting an option from a dropdown, choosing a radio button or checkbox, pressing a button), what value they are entering or selecting, and any visible changes that result from the interaction (e.g., dropdown closes with new value shown, radio button becomes selected, checkbox becomes checked, success message appears, new page loads).",
            )
        else:
            raise

    scenes = await _poll_scene_index(video, scene_index_id)
    logger.info(f"Scene index ready: {len(scenes)} scenes")
    transcript = _safe_get_transcript(video)

    sessions = sorted(Path.home().glob(f".mcp-videodb/sessions/*_{name}/metadata.json"))
    if not sessions:
        sessions = sorted(Path.home().glob(".mcp-videodb/sessions/*/metadata.json"))

    metadata = None
    events = []
    for mpath in sessions:
        meta = json.loads(mpath.read_text())
        if meta.get("video_id") == video_id:
            metadata = meta
            ep = mpath.parent / "events.jsonl"
            if ep.exists():
                events = [
                    json.loads(line)
                    for line in ep.read_text().strip().split("\n")
                    if line.strip()
                ]
            break

    if not metadata:
        raise RuntimeError(f"No session metadata found for video_id {video_id}")

    events = _prefilter_events(events)
    if not events:
        raise RuntimeError("No events remain after noise filtering")

    start_ms = metadata["recording_start_epoch_ms"]
    matched = []
    for event in events:
        if event.get("event") != "action":
            continue
        video_time = (event["ts"] - start_ms) / 1000.0
        scene_match = None
        for scene in scenes:
            if scene["start"] <= video_time <= scene["end"]:
                scene_match = scene
                break
        matched.append({
            "event": event,
            "scene_description": scene_match["description"] if scene_match else "(no scene match)",
            "video_time": video_time,
            "scene_start": scene_match["start"] if scene_match else None,
            "scene_end": scene_match["end"] if scene_match else None,
        })

    prompt = build_prompt(name, events, matched, transcript)
    skill_json = None

    for attempt in range(LLM_MAX_RETRIES):
        try:
            response = state.coll.generate_text(
                prompt=prompt, model_name=LLM_MODEL, response_type="json"
            )
            raw = response["output"]
            skill_json = raw if isinstance(raw, dict) else json.loads(raw)
            ts_err = _validate_timestamps_raw(skill_json)
            if ts_err:
                if attempt < LLM_MAX_RETRIES - 1:
                    prompt += f"\n\n{ts_err}"
                    continue
                logger.warning(f"Timestamp format wrong on final attempt — applying normalization")
            break
        except (json.JSONDecodeError, KeyError) as e:
            prompt += f"\n\nPREVIOUS OUTPUT WAS INVALID JSON. Error: {e}. Output valid JSON only."
        except Exception as e:
            if attempt < LLM_MAX_RETRIES - 1:
                await asyncio.sleep(5 * (2 ** attempt))
            else:
                raise

    if skill_json is None:
        raise RuntimeError(f"LLM compilation failed after {LLM_MAX_RETRIES} retries")

    skill_json = _normalize_llm_output(skill_json, name)
    skill_json["video_id"] = video_id
    skill_json["scene_index_id"] = scene_index_id
    skill_json["compiled_at"] = datetime.now(timezone.utc).isoformat()
    _attach_expected_scenes(skill_json, scenes, metadata)

    errors = _validate_skill(skill_json)
    if errors:
        prompt += f"\n\nVALIDATION ERRORS: {json.dumps(errors, indent=2)}"
        response = state.coll.generate_text(
            prompt=prompt, model_name=LLM_MODEL, response_type="json"
        )
        skill_json = (
            response["output"]
            if isinstance(response["output"], dict)
            else json.loads(response["output"])
        )
        skill_json = _normalize_llm_output(skill_json, name)
        skill_json["video_id"] = video_id
        skill_json["scene_index_id"] = scene_index_id
        skill_json["compiled_at"] = datetime.now(timezone.utc).isoformat()
        _attach_expected_scenes(skill_json, scenes, metadata)

    save_skill(skill_json)
    return skill_json


def _safe_get_transcript(video) -> str:
    try:
        return video.get_transcript_text()
    except Exception:
        return ""


def _validate_skill(skill: dict) -> list[str]:
    import jsonschema
    schema_path = Path(__file__).parent.parent / "schema" / "skill.schema.json"
    schema = json.loads(schema_path.read_text())
    try:
        jsonschema.validate(instance=skill, schema=schema)
        return []
    except jsonschema.ValidationError as e:
        return [str(e)]


def _validate_timestamps_raw(skill_json: dict) -> str:
    """Check raw LLM output for absolute epoch timestamps before normalization.

    Returns an error message to append to the retry prompt, or empty string if OK.
    """
    bad_steps = []
    for step in skill_json.get("steps", []):
        ref = step.get("recording_ref", {})
        end = ref.get("end", 0)
        if isinstance(end, (int, float)) and end > 1_000_000_000:
            bad_steps.append(f"Step {step.get('id', '?')}: recording_ref.end={end}")
    if bad_steps:
        return (
            "TIMESTAMP FORMAT ERROR: recording_ref.end must be relative seconds from "
            "recording start, NOT absolute epoch timestamps.\n"
            "Example: 5.967 seconds is correct. 1782319061217 is WRONG.\n"
            f"Offending steps: {', '.join(bad_steps)}.\n"
            "Re-output with relative seconds only."
        )
    return ""


async def compile_skill_events_only(name: str) -> dict:
    from registry import save_skill
    sessions = sorted(Path.home().glob(f".mcp-videodb/sessions/*_{name}/metadata.json"))
    if not sessions:
        sessions = sorted(Path.home().glob(".mcp-videodb/sessions/*/metadata.json"))

    metadata = None
    events = []
    for mpath in reversed(sessions):
        meta = json.loads(mpath.read_text())
        if meta.get("skill_name") == name:
            metadata = meta
            ep = mpath.parent / "events.jsonl"
            if ep.exists():
                events = [
                    json.loads(line)
                    for line in ep.read_text().strip().split("\n")
                    if line.strip()
                ]
            break

    if not metadata:
        raise RuntimeError(f"No session metadata found for skill '{name}'")

    if not events:
        raise RuntimeError("No events recorded")

    action_events = [e for e in events if e.get("event") == "action"]

    prompt = COMPILATION_SYSTEM_PROMPT + "\n\n" + _build_events_only_prompt(
        name, action_events, metadata
    )

    skill_json = None
    for attempt in range(LLM_MAX_RETRIES):
        try:
            response = state.coll.generate_text(
                prompt=prompt, model_name=LLM_MODEL, response_type="json"
            )
            raw = response["output"]
            skill_json = raw if isinstance(raw, dict) else json.loads(raw)
            break
        except (json.JSONDecodeError, KeyError) as e:
            prompt += f"\n\nPREVIOUS OUTPUT WAS INVALID JSON. Error: {e}. Output valid JSON only."
        except Exception as e:
            if attempt < LLM_MAX_RETRIES - 1:
                await asyncio.sleep(5 * (2 ** attempt))
            else:
                raise

    if skill_json is None:
        raise RuntimeError(f"LLM compilation failed after {LLM_MAX_RETRIES} retries")

    skill_json = _normalize_llm_output(skill_json, name)
    skill_json["video_id"] = "v_events_only"
    skill_json["scene_index_id"] = ""
    skill_json["compiled_at"] = datetime.now(timezone.utc).isoformat()

    errors = _validate_skill(skill_json)
    if errors:
        raise RuntimeError(f"Schema validation failed: {errors}")

    save_skill(skill_json)
    return skill_json


async def _poll_scene_index(video, scene_index_id: str, max_attempts: int = 20) -> list[dict]:
    """Poll scene index until processing completes (count stabilizes or max attempts)."""
    prev_count = -1
    stable = 0
    for i in range(max_attempts):
        try:
            scenes = video.get_scene_index(scene_index_id)
            count = len(scenes)
            if count > 0 and count == prev_count:
                stable += 1
                if stable >= 3:
                    logger.info(f"Scene index stable at {count} scenes after {i+1} polls")
                    return scenes
            else:
                stable = 0
            prev_count = count
        except Exception as e:
            logger.warning(f"Scene index poll error (attempt {i+1}): {e}")
        await asyncio.sleep(1.5)
    scenes = video.get_scene_index(scene_index_id)
    logger.warning(f"Scene index poll exhausted — got {len(scenes)} scenes")
    return scenes or []


def _to_relative_seconds(end_time: float, metadata: dict) -> float:
    """Normalize recording_ref.end to relative seconds from recording start.

    VLM may output one of three formats:
    1. Relative seconds: e.g. 5.967
    2. Absolute epoch milliseconds: e.g. 1782319061217 (> 1e12)
    3. Absolute epoch seconds: e.g. 1782319061.716 (> 1e9, < start_ms)

    Detect and convert to relative seconds.
    """
    start_ms = metadata.get("recording_start_epoch_ms", 0)
    if end_time > 1000000000000:
        return (end_time - start_ms) / 1000.0
    if start_ms > 0 and end_time > 1000000000:
        start_sec = start_ms / 1000.0
        return end_time - start_sec
    return end_time


def _attach_expected_scenes(skill_json: dict, scenes: list[dict], metadata: dict) -> None:
    max_scene_end = max((s.get("end", 0) for s in scenes), default=0)
    steps_no_match = 0
    for step in skill_json.get("steps", []):
        ref = step.get("recording_ref", {})
        end_time = _to_relative_seconds(ref.get("end", 0), metadata)
        best = ""
        for scene in scenes:
            if scene.get("start", 0) <= end_time <= scene.get("end", 0):
                desc = scene.get("description", "")
                if desc:
                    best = desc
                    break
        if not best:
            if end_time > max_scene_end and scenes:
                last_desc = scenes[-1].get("description", "")
                step["expected_scene"] = f"[AFTER RECORDING END] Last visible: {last_desc}"
                steps_no_match += 1
            else:
                step["expected_scene"] = best
            continue
        step["expected_scene"] = best
    if steps_no_match > 0:
        logger.warning(
            f"Recording scenes only cover up to {max_scene_end:.1f}s — "
            f"{steps_no_match} step(s) beyond this range have no expected_scene. "
            f"Re-record the full workflow for complete step-by-step VLM guidance."
        )


def _prefilter_events(events: list[dict], screen_h: int = 1080, screen_w: int = 1920) -> list[dict]:
    noise_values = {"stop", "done", "stop recording", "stoprecording", "stoptherecording"}
    filtered = []
    skip_next = False
    for i, e in enumerate(events):
        if e.get("event") != "action":
            continue
        val = (e.get("value") or "").strip().lower()
        if val and val in noise_values:
            if (e.get("action") in ("type", "select") and filtered and
                    filtered[-1].get("target", {}).get("label") == e.get("target", {}).get("label")):
                filtered.pop()
            skip_next = False
            continue
        pos = e.get("position", {})
        if pos:
            x, y = pos.get("x", 0), pos.get("y", 0)
            if y > screen_h - 40:
                continue
            if y < 25:
                continue
            if x > screen_w - 40:
                continue
        filtered.append(e)
    return filtered


def _build_events_only_prompt(name: str, events: list[dict], metadata: dict) -> str:
    import compiler.prompts as p
    start_ms = metadata["recording_start_epoch_ms"]
    events_text = "\n".join(
        json.dumps({"ts": round((e["ts"] - start_ms) / 1000.0, 3),
                     "action": e["action"], "target": e.get("target"),
                     "value": e.get("value")})
        for e in events
    )
    return p.build_user_prompt(
        name, events_text,
        [{"event": e, "scene_description": "(no video — events only)",
          "video_time": (e["ts"] - start_ms) / 1000.0,
          "scene_start": None, "scene_end": None} for e in events],
        "(no narration)"
    )


def _normalize_llm_output(skill: dict, name: str) -> dict:
    if not isinstance(skill, dict):
        skill = {}

    skill["name"] = _slugify_skill_name(skill.get("name") or name)
    if not skill.get("description"):
        skill["description"] = f"Automated skill: {skill['name'].replace('-', ' ')}"

    if not isinstance(skill.get("version"), int):
        try:
            skill["version"] = int(float(skill.get("version", 1)))
        except (TypeError, ValueError):
            skill["version"] = 1

    if "variables" in skill and "inputs" not in skill:
        skill["inputs"] = skill.pop("variables")
    skill["inputs"] = _normalize_inputs(skill.get("inputs", {}))

    preconditions = skill.get("preconditions")
    if isinstance(preconditions, str):
        preconditions = [preconditions]
    skill["preconditions"] = preconditions or ["Application is open and ready"]
    skill["start_context"] = _normalize_start_context(
        skill.get("start_context"),
        skill["preconditions"],
    )

    normalized_steps = []
    for i, raw_step in enumerate(skill.get("steps", []) or []):
        if not isinstance(raw_step, dict):
            continue
        step = dict(raw_step)
        action = _normalize_action(step.get("action") or step.get("type"))
        target = _normalize_target(step.get("target"))
        recording_ref = _normalize_recording_ref(step.get("recording_ref") or step.get("time"), i)

        try:
            step_id = int(step.get("id", i + 1))
        except (TypeError, ValueError):
            step_id = i + 1

        expected = step.get("expected_scene") or ""
        visual_context = step.get("visual_context") or expected
        if not visual_context or str(visual_context).strip().lower() == "(no scene match)":
            visual_context = _fallback_visual_context(action, target)

        clean_step = {
            "id": step_id,
            "action": action,
            "target": target,
            "recording_ref": recording_ref,
            "visual_context": str(visual_context),
        }
        if expected and str(expected).strip().lower() != "(no scene match)":
            clean_step["expected_scene"] = str(expected)
        if "value" in step:
            clean_step["value"] = _redact_sensitive_value(step.get("value"))
        normalized_steps.append(clean_step)

    skill["steps"] = normalized_steps

    norm_verification = []
    for raw_check in skill.get("verification", []) or []:
        if isinstance(raw_check, str):
            check_type = "ax_element"
            check = raw_check
        elif isinstance(raw_check, dict):
            check_type = raw_check.get("type", "ax_element")
            if check_type == "element_present":
                check_type = "ax_element"
            if check_type not in {"ax_element", "visual", "transcript"}:
                check_type = "ax_element"
            check = raw_check.get("check") or raw_check.get("description") or ""
        else:
            continue

        check = str(check).strip()
        if check and not _is_generic_verification_check(check):
            norm_verification.append({"type": check_type, "check": check})

    skill["verification"] = norm_verification or _synthesize_verification(skill)

    return skill


def _is_generic_verification_check(check: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", check.lower()).strip()
    generic_checks = {
        "task completed successfully",
        "completed successfully",
        "workflow completed successfully",
        "success",
        "done",
        "task complete",
    }
    return normalized in generic_checks or len(normalized) < 8


def _synthesize_verification(skill: dict) -> list[dict]:
    checks = []

    for step in reversed(skill.get("steps", []) or []):
        action = step.get("action")
        value = step.get("value")
        if action in {"type", "select"} and value and value != "[REDACTED]":
            value_text = str(value)
            if value_text.startswith("{{") and value_text.endswith("}}"):
                variable = value_text.strip("{}")
                if action == "select":
                    checks.append({"type": "ax_element", "check": f"Verify the `{variable}` option is selected or visibly applied."})
                else:
                    checks.append({"type": "ax_element", "check": f"Verify the field for `{variable}` contains the entered value."})
            elif action == "select":
                checks.append({"type": "ax_element", "check": f"Verify '{value_text}' is selected or visibly applied."})
            else:
                checks.append({"type": "ax_element", "check": f"Verify the field contains '{value_text}'."})
            if len(checks) >= 2:
                break

    status_keywords = (
        "selected", "saved", "confirmed", "submitted", "complete", "uploaded",
        "uploading", "progress", "visible", "shows", "appears", "title",
        "filename", "modal", "dialog", "radio", "checked", "step", "status",
    )
    for step in reversed(skill.get("steps", []) or []):
        context = str(step.get("expected_scene") or step.get("visual_context") or "").strip()
        if not context or context.lower() == "(no scene match)":
            continue
        sentence = _best_verification_sentence(context, status_keywords)
        if sentence:
            check = f"Verify {sentence}"
            if all(existing["check"] != check for existing in checks):
                checks.append({"type": "ax_element", "check": check})
        if len(checks) >= 3:
            break

    if checks:
        return checks[:3]
    return [{"type": "ax_element", "check": "Verify the final screen still shows the expected workflow state from the last recorded step."}]


def _best_verification_sentence(context: str, keywords: tuple[str, ...]) -> str:
    cleaned = re.sub(r"\s+", " ", context.replace("**", "")).strip()
    parts = re.split(r"(?<=[.!?])\s+|\n+| - ", cleaned)
    for part in parts:
        part = part.strip(" -")
        if not part:
            continue
        lowered = part.lower()
        if any(keyword in lowered for keyword in keywords):
            part = part[0].lower() + part[1:] if part[:1].isupper() else part
            return part[:220].rstrip(" ,;:") + "."
    if cleaned:
        cleaned = cleaned[0].lower() + cleaned[1:] if cleaned[:1].isupper() else cleaned
        return cleaned[:220].rstrip(" ,;:") + "."
    return ""

def _slugify_skill_name(value: object) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", str(value).lower().replace("_", "-"))
    return slug.strip("-") or "recorded-skill"


def _normalize_action(action: object) -> str:
    action = str(action or "click").lower().strip()
    aliases = {
        "input": "type",
        "enter": "type",
        "text": "type",
        "dropdown": "select",
        "choose": "select",
        "open": "click",
        "press": "click",
    }
    action = aliases.get(action, action)
    return action if action in {"click", "type", "select", "navigate", "wait"} else "click"


def _normalize_target(target: object) -> dict:
    if isinstance(target, str):
        return {"type": "element", "label": target}
    if not isinstance(target, dict):
        return {"type": "element", "label": "target element"}
    target_type = target.get("type") or target.get("role") or target.get("kind") or "element"
    label = target.get("label") or target.get("name") or target.get("text") or target.get("id") or "target element"
    return {"type": str(target_type), "label": str(label)}


def _normalize_recording_ref(ref: object, index: int) -> dict:
    if not isinstance(ref, dict):
        return {"start": float(index), "end": float(index + 1)}
    try:
        start = float(ref.get("start", index))
    except (TypeError, ValueError):
        start = float(index)
    try:
        end = float(ref.get("end", start + 1.0))
    except (TypeError, ValueError):
        end = start + 1.0
    start = max(0.0, start)
    end = max(start, end)
    return {"start": start, "end": end}


def _fallback_visual_context(action: str, target: dict) -> str:
    label = target.get("label", "target element")
    target_type = target.get("type", "element")
    if label.startswith("element_at_"):
        return f"The {target_type} used for the {action} action in the recorded workflow."
    return f"The {target_type} labeled '{label}' used for the {action} action."


def _normalize_start_context(start_context: object, preconditions: list[str]) -> dict:
    if not isinstance(start_context, dict):
        instructions = "; ".join(str(p) for p in preconditions if p) or "Application is open and ready"
        return {
            "kind": "unknown",
            "label": "Starting application state",
            "instructions": instructions,
            "evidence": "No structured start context was produced during compilation.",
        }

    allowed_kinds = {
        "web",
        "desktop_app",
        "file",
        "terminal",
        "workspace",
        "screen_state",
        "unknown",
    }
    kind = str(start_context.get("kind") or "unknown").strip().lower()
    aliases = {
        "website": "web",
        "browser": "web",
        "app": "desktop_app",
        "application": "desktop_app",
        "desktop": "desktop_app",
        "page": "screen_state",
        "screen": "screen_state",
    }
    kind = aliases.get(kind, kind)
    if kind not in allowed_kinds:
        kind = "unknown"

    label = str(start_context.get("label") or "Starting application state").strip()
    instructions = str(start_context.get("instructions") or "").strip()
    if not instructions:
        instructions = "; ".join(str(p) for p in preconditions if p) or "Application is open and ready"

    normalized = {
        "kind": kind,
        "label": label,
        "instructions": instructions,
    }
    locator = str(start_context.get("locator") or "").strip()
    if locator:
        normalized["locator"] = locator
    evidence = str(start_context.get("evidence") or "").strip()
    if evidence:
        normalized["evidence"] = evidence
    return normalized


def _redact_sensitive_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    sensitive = [
        r"sk-[A-Za-z0-9_-]+",
        r"Bearer\s+[A-Za-z0-9._-]+",
        r"eyJ[A-Za-z0-9._-]+",
        r"\b\d{13,19}\b",
        r"\b\d{3}-\d{2}-\d{4}\b",
    ]
    if any(re.search(pattern, value) for pattern in sensitive):
        return "[REDACTED]"
    return value

def _normalize_inputs(inputs) -> dict:
    normalized = {}
    if isinstance(inputs, dict) and not isinstance(inputs, list):
        iterable = inputs.items()
    elif isinstance(inputs, list):
        iterable = []
        for item in inputs:
            if isinstance(item, dict):
                key = item.get("name") or item.get("key") or str(len(iterable) + 1)
                iterable.append((key, item))
            else:
                iterable.append((str(item), {"type": "string", "example": str(item)}))
    else:
        iterable = []

    for key, spec in iterable:
        input_name = re.sub(r"[^a-zA-Z0-9_]+", "_", str(key)).strip("_") or f"input_{len(normalized) + 1}"
        if not isinstance(spec, dict):
            spec = {"type": "string", "example": str(spec)}
        input_type = str(spec.get("type", "string")).lower()
        if input_type not in {"string", "number", "enum"}:
            input_type = "string"
        clean_spec = {
            "type": input_type,
            "example": spec.get("example", ""),
        }
        if spec.get("format"):
            clean_spec["format"] = str(spec["format"])
        if spec.get("description"):
            clean_spec["description"] = str(spec["description"])
        values = spec.get("values")
        if values:
            clean_spec["values"] = [str(value) for value in values]
        normalized[input_name] = clean_spec
    return normalized