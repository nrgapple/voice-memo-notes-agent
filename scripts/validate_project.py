#!/usr/bin/env python3
"""Validate repository metadata needed for a distributable Codex skill."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent


def fail(message: str) -> None:
    raise RuntimeError(message)


def main() -> int:
    skill_text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", skill_text, flags=re.DOTALL)
    if not match:
        fail("SKILL.md must start with YAML frontmatter")
    skill = yaml.safe_load(match.group(1))
    if skill.get("name") != "sync-voice-memos-to-notes":
        fail("SKILL.md has an unexpected skill name")
    if not str(skill.get("description") or "").strip():
        fail("SKILL.md requires a description")

    agent = yaml.safe_load((ROOT / "agents/openai.yaml").read_text(encoding="utf-8"))
    if agent.get("interface", {}).get("default_prompt") != "Use $sync-voice-memos-to-notes in scheduled-sync mode.":
        fail("agents/openai.yaml default prompt does not match the skill")

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
        fail("VERSION is not semantic-version shaped")

    required = [
        "LICENSE", "README.md", "SECURITY.md", "CONTRIBUTING.md",
        "CHANGELOG.md", "CODE_OF_CONDUCT.md", "RELEASING.md",
        "THIRD_PARTY_NOTICES.md",
    ]
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        fail(f"missing public project files: {', '.join(missing)}")
    print(f"project metadata valid ({version})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, yaml.YAMLError) as error:
        print(f"validate-project: {error}", file=sys.stderr)
        raise SystemExit(1)
