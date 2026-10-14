from pathlib import Path

import pytest

from scripts.ops.sort_requirements_in import (
    main,
    requirement_sort_key,
    sort_requirements_in_content,
    sort_requirements_in_file,
)


def test_an_unsorted_file_is_rewritten_sorted(tmp_path: Path) -> None:
    path = tmp_path / "base.in"
    path.write_text("redis\naiogram>=3.31\nbabel\n", encoding="utf-8")

    assert sort_requirements_in_file(path) is True
    assert path.read_text(encoding="utf-8") == "aiogram>=3.31\nbabel\nredis\n"


def test_a_sorted_file_is_left_untouched(tmp_path: Path) -> None:
    path = tmp_path / "base.in"
    path.write_text("aiogram>=3.31\nbabel\nredis\n", encoding="utf-8")
    before = path.stat().st_mtime_ns

    assert sort_requirements_in_file(path) is False
    assert path.read_text(encoding="utf-8") == "aiogram>=3.31\nbabel\nredis\n"
    assert path.stat().st_mtime_ns == before


def test_the_sort_key_ignores_extras_specifiers_case_and_underscores() -> None:
    assert requirement_sort_key("SQLAlchemy[asyncio]>=2") == "sqlalchemy"
    assert requirement_sort_key("pydantic_settings") == "pydantic-settings"
    assert requirement_sort_key("mako>=1.4.1  # security") == "mako"


def test_extras_do_not_change_the_position() -> None:
    content = "uuid6\nsqlalchemy[asyncio]\nredis\n"

    assert sort_requirements_in_content(content) == (
        "redis\nsqlalchemy[asyncio]\nuuid6\n"
    )


def test_a_comment_travels_with_the_requirement_below_it() -> None:
    content = "# the driver\nasyncpg\n# the framework\naiogram\n"

    assert sort_requirements_in_content(content) == (
        "# the framework\naiogram\n# the driver\nasyncpg\n"
    )


def test_an_inline_comment_stays_on_its_line() -> None:
    content = "mako>=1.4.1  # security\naiohttp>=3.14.3  # security\n"

    assert sort_requirements_in_content(content) == (
        "aiohttp>=3.14.3  # security\nmako>=1.4.1  # security\n"
    )


def test_a_directive_stays_above_the_requirements() -> None:
    content = "pytest\n-r base.in\nblack\n"

    assert sort_requirements_in_content(content) == "-r base.in\n\nblack\npytest\n"


def test_blank_line_separated_blocks_are_sorted_independently() -> None:
    content = "-r base.in\n\nmypy\nblack\n\npytest-cov\npytest\n"

    assert sort_requirements_in_content(content) == (
        "-r base.in\n\nblack\nmypy\n\npytest\npytest-cov\n"
    )


def test_a_trailing_comment_stays_last() -> None:
    content = "redis\naiogram\n# kept at the end\n"

    assert sort_requirements_in_content(content) == (
        "aiogram\nredis\n\n# kept at the end\n"
    )


def test_main_sorts_every_given_file_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 0 is intentional: pre-commit detects the rewritten file itself."""
    first = tmp_path / "first.in"
    second = tmp_path / "second.in"
    first.write_text("b\na\n", encoding="utf-8")
    second.write_text("d\nc\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["sort_requirements_in", str(first), str(second)])

    assert main() == 0
    assert first.read_text(encoding="utf-8") == "a\nb\n"
    assert second.read_text(encoding="utf-8") == "c\nd\n"
