"""Tests for compiler/md_generator.py."""

from compiler.md_generator import (
    _append_execution_guidance_section,
    _append_self_improvement_section,
    _recorded_surface_summary,
    _template_fallback,
)


def test_appends_self_improvement_section():
    content = """---
name: example
description: "Example skill"
---

# Example

## Steps
1. Do the task.
"""

    result = _append_self_improvement_section(content)

    assert "## Continuous Improvement" in result
    assert "Complete the user's requested task first" in result
    assert "Do not add secrets" in result


def test_does_not_duplicate_self_improvement_section():
    content = """# Example

## Continuous Improvement
- Existing guidance.
"""

    result = _append_self_improvement_section(content)

    assert result.count("## Continuous Improvement") == 1
    assert "Existing guidance." in result


def test_template_fallback_includes_self_improvement_section():
    skill = {
        "name": "example-skill",
        "description": "Example skill",
        "preconditions": [],
        "inputs": {},
        "steps": [],
        "verification": [],
    }

    result = _template_fallback(skill)

    assert "## Continuous Improvement" in result
    assert "Keep edits concise" in result


def test_appends_browser_execution_guidance():
    skill = {
        "execution_strategy": {
            "surface": "web_browser",
            "preferred_tools": ["native_accessibility"],
            "fallback_tools": ["visual_computer_use"],
            "notes": ["Replay the recorded visible browser app directly with native desktop automation."],
        }
    }

    result = _append_execution_guidance_section("# Example", skill)

    assert "## Execution Guidance" in result
    assert "Surface: `web_browser`" in result
    assert "Preferred tool path: `native_accessibility`" in result
    assert "Fallback tool path: `visual_computer_use`" in result
    assert "recorded visible browser app" in result
    assert "Do not use any separate browser automation session" in result
    assert "Before upload/send/post/delete actions" in result
    assert "Do not repeat them unless" in result
    assert "Preferred setup before replay: Native accessibility controls." in result
    assert "Fallback setup before replay: Visual computer-use." in result


def test_appends_desktop_platform_execution_guidance():
    skill = {
        "execution_strategy": {
            "surface": "desktop_app",
            "preferred_tools": ["native_accessibility"],
            "fallback_tools": ["visual_computer_use"],
        }
    }

    result = _append_execution_guidance_section("# Example", skill)

    assert "osascript/System Events" in result
    assert "UI Automation / UIA" in result
    assert "AT-SPI" in result


def test_appends_recorded_surface_guidance():
    skill = {
        "recorded_surface": {
            "platform": "darwin",
            "app_name": "Safari",
            "window_title": "Example Page",
        },
        "execution_strategy": {
            "surface": "web_browser",
            "preferred_tools": ["native_accessibility"],
            "fallback_tools": ["visual_computer_use"],
        },
    }

    result = _append_execution_guidance_section("# Example", skill)

    assert 'Recorded surface: Safari, window "Example Page", on darwin.' in result
    assert "bring this exact app/window type" in result
    assert "Do not switch to another app, browser, or native client" in result
    assert "Resolve targets by accessibility role" in result
    assert "relative positions only as a fallback" in result


def test_recorded_surface_summary_omits_missing_fields():
    summary = _recorded_surface_summary({"app_name": "Slack", "platform": "darwin"})

    assert summary == "Slack, on darwin"


def test_does_not_duplicate_execution_guidance_section():
    content = """# Example

## Execution Guidance
- Existing guidance.
"""

    result = _append_execution_guidance_section(content, {})

    assert result.count("## Execution Guidance") == 1
    assert "Existing guidance." in result


def test_template_fallback_orders_execution_before_continuous_improvement():
    skill = {
        "name": "example-skill",
        "description": "Example skill",
        "execution_strategy": {"surface": "desktop_app"},
        "preconditions": [],
        "inputs": {},
        "steps": [],
        "verification": [],
    }

    result = _template_fallback(skill)

    assert result.index("## Execution Guidance") < result.index("## Continuous Improvement")
