"""Tests for compiler/md_generator.py."""

from compiler.md_generator import (
    _append_execution_guidance_section,
    _append_self_improvement_section,
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
            "preferred_tools": ["playwright"],
            "fallback_tools": ["native_accessibility", "visual_computer_use"],
            "notes": ["Use browser automation for web-page steps."],
        }
    }

    result = _append_execution_guidance_section("# Example", skill)

    assert "## Execution Guidance" in result
    assert "Surface: `web_browser`" in result
    assert "Preferred tool path: `playwright`" in result
    assert "Playwright or browser automation" in result
    assert "file pickers" in result


def test_appends_desktop_platform_execution_guidance():
    skill = {
        "execution_strategy": {
            "surface": "desktop_app",
            "preferred_tools": ["native_accessibility"],
            "fallback_tools": ["visual_computer_use"],
        }
    }

    result = _append_execution_guidance_section("# Example", skill)

    assert "macOS Accessibility API / AX" in result
    assert "Windows UI Automation / UIA" in result
    assert "Linux AT-SPI" in result


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
