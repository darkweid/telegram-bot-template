"""Scrub the .po catalogs that pybabel writes, so `make i18n-update` is idempotent.

Babel fills the header with placeholders (ORGANIZATION, FIRST AUTHOR,
EMAIL@ADDRESS, a trailing VERSION) and ends the file with a blank line. This
script blanks the personal header fields, drops the placeholder comment lines
and leaves exactly one newline at the end. Running it twice changes nothing.
"""

from __future__ import annotations

import argparse
from pathlib import Path

_PLACEHOLDER_COMMENTS = ("# Copyright (C) ", "# FIRST AUTHOR ")
_BLANKED_FIELDS = ("Report-Msgid-Bugs-To", "Last-Translator", "Language-Team")


def normalize(text: str, project: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith(_PLACEHOLDER_COMMENTS):
            continue
        if line.startswith('"Project-Id-Version:'):
            line = f'"Project-Id-Version: {project}\\n"'
        else:
            for field in _BLANKED_FIELDS:
                if line.startswith(f'"{field}:'):
                    line = f'"{field}: \\n"'
        lines.append(line)
    return "\n".join(lines).rstrip("\n") + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrub pybabel placeholders from the .po catalogs."
    )
    parser.add_argument("locales_dir", type=Path)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    for path in sorted(args.locales_dir.glob("*/LC_MESSAGES/*.po")):
        original = path.read_text(encoding="utf-8")
        updated = normalize(original, args.project)
        if updated != original:
            path.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    main()
