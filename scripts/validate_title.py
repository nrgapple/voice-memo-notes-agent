#!/usr/bin/env python3
"""Validate a concise generated Voice Memo title."""

from __future__ import annotations

import argparse
import json
import re
import sys


DEFAULT_TRIGGERS = (
    "work note",
    "work notes",
    "for work",
    "work memo",
    "memo for work",
    "note for work",
)


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--trigger", action="append", default=[])
    parser.add_argument("--min-words", type=int, default=3)
    parser.add_argument("--max-words", type=int, default=8)
    parser.add_argument("--max-characters", type=int, default=60)
    args = parser.parse_args()

    title = normalize(args.title)
    errors = []
    if "\n" in args.title or "\r" in args.title:
        errors.append("title must be one line")
    if any(ord(character) < 32 for character in title):
        errors.append("title contains a control character")
    words = title.split()
    if not args.min_words <= len(words) <= args.max_words:
        errors.append(f"title must contain {args.min_words}-{args.max_words} words")
    if len(title) > args.max_characters:
        errors.append(f"title must be at most {args.max_characters} characters")
    triggers = args.trigger or list(DEFAULT_TRIGGERS)
    folded = re.sub(r"[^\w]+", " ", title.casefold()).strip()
    for trigger in triggers:
        normalized_trigger = re.sub(r"[^\w]+", " ", trigger.casefold()).strip()
        if re.search(rf"(?<!\w){re.escape(normalized_trigger)}(?!\w)", folded):
            errors.append(f"title must omit routing phrase: {trigger}")
            break

    print(json.dumps({"ok": not errors, "title": title, "errors": errors}, sort_keys=True))
    return 0 if not errors else 3


if __name__ == "__main__":
    sys.exit(main())
