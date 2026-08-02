#!/usr/bin/env python3
"""Resolve the journal date for a Voice Memo recording."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta


def parse_recorded_at(value: str) -> date:
    normalized = value.strip()
    if "T" not in normalized:
        return date.fromisoformat(normalized)
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    return datetime.fromisoformat(normalized).date()


def resolve_journal_date(recorded_date: date) -> date:
    days_to_monday = {5: 2, 6: 1}.get(recorded_date.weekday(), 0)
    return recorded_date + timedelta(days=days_to_monday)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Map weekend Voice Memos to the following Monday's journal."
    )
    parser.add_argument("--recorded-at", required=True, help="ISO-8601 recording timestamp or date")
    args = parser.parse_args()

    try:
        recorded_date = parse_recorded_at(args.recorded_at)
    except ValueError as error:
        parser.error(f"invalid ISO-8601 --recorded-at value: {error}")

    journal_date = resolve_journal_date(recorded_date)
    print(
        json.dumps(
            {
                "journal_date": journal_date.isoformat(),
                "recorded_date": recorded_date.isoformat(),
                "shifted_to_monday": journal_date != recorded_date,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
