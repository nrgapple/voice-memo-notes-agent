#!/usr/bin/env python3
"""Check whether a transcript explicitly opts into the work-notes workflow."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


DEFAULT_PHRASES = (
    "work note",
    "work notes",
    "for work",
    "work memo",
    "memo for work",
    "note for work",
)


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.casefold())).strip()


def find_matching_phrase(value: str, phrases: tuple[str, ...] | list[str]) -> str | None:
    transcript = normalize(value)
    for phrase in phrases:
        candidate = normalize(phrase)
        if candidate and re.search(rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])", transcript):
            return phrase
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--phrase", action="append", dest="phrases")
    args = parser.parse_args()

    phrases = tuple(args.phrases or DEFAULT_PHRASES)
    matched = find_matching_phrase(args.transcript.read_text(encoding="utf-8"), phrases)
    print(json.dumps({"eligible": matched is not None, "matched_phrase": matched}))
    return 0 if matched is not None else 3


if __name__ == "__main__":
    sys.exit(main())
