from pathlib import Path

from scripts.ops.normalize_catalogs import normalize

PROJECT = "telegram-bot-template"
RAW = f"""\
# Russian translations for messages.
# Copyright (C) 2026 ORGANIZATION
# This file is distributed under the same license as the messages project.
# FIRST AUTHOR <EMAIL@ADDRESS>, 2026.
#
msgid ""
msgstr ""
"Project-Id-Version: {PROJECT} 1\\n"
"Report-Msgid-Bugs-To: EMAIL@ADDRESS\\n"
"Last-Translator: FULL NAME <EMAIL@ADDRESS>\\n"
"Language-Team: ru <LL@li.org>\\n"
"Language: ru\\n"

"""


def test_normalize_scrubs_placeholders_and_trailing_blank_lines() -> None:
    result = normalize(RAW, PROJECT)
    assert "ORGANIZATION" not in result
    assert "FIRST AUTHOR" not in result
    assert "EMAIL" not in result
    assert f'"Project-Id-Version: {PROJECT}\\n"' in result
    assert '"Language: ru\\n"' in result
    assert result.endswith('"Language: ru\\n"\n')


def test_normalize_is_idempotent() -> None:
    once = normalize(RAW, PROJECT)
    assert normalize(once, PROJECT) == once


def test_committed_catalogs_are_already_normalized() -> None:
    root = Path(__file__).resolve().parents[4] / "locales"
    for po in root.glob("*/LC_MESSAGES/*.po"):
        text = po.read_text(encoding="utf-8")
        assert normalize(text, PROJECT) == text, po
