#!/usr/bin/env python3
"""Validate repository metadata needed for a distributable Codex skill."""

from __future__ import annotations

import re
import struct
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
        "assets/README.md", "assets/VoiceMemoAgent.icon.png", "assets/VoiceMemoAgent.icns",
        "scripts/build_app.sh", "scripts/build_icon.sh", "scripts/build_release.sh",
    ]
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        fail(f"missing public project files: {', '.join(missing)}")

    executable_scripts = [
        "scripts/build_app.sh", "scripts/build_icon.sh", "scripts/build_release.sh",
    ]
    not_executable = [name for name in executable_scripts if not (ROOT / name).stat().st_mode & 0o111]
    if not_executable:
        fail(f"build scripts are not executable: {', '.join(not_executable)}")

    png = (ROOT / "assets/VoiceMemoAgent.icon.png").read_bytes()
    if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
        fail("icon source is not a PNG")
    width, height = struct.unpack(">II", png[16:24])
    if (width, height) != (1024, 1024):
        fail(f"icon source must be 1024x1024 (found {width}x{height})")
    if (ROOT / "assets/VoiceMemoAgent.icns").read_bytes()[:4] != b"icns":
        fail("compiled app icon is not an ICNS file")

    for workflow in (ROOT / ".github/workflows").glob("*.yml"):
        for line_number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
            uses = re.search(r"\buses:\s*[^\s]+@([^\s#]+)", line)
            if uses and not re.fullmatch(r"[0-9a-f]{40}", uses.group(1)):
                fail(f"{workflow.relative_to(ROOT)}:{line_number} action is not SHA-pinned")
    print(f"project metadata valid ({version})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, yaml.YAMLError) as error:
        print(f"validate-project: {error}", file=sys.stderr)
        raise SystemExit(1)
