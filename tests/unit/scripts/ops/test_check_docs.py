from pathlib import Path
import subprocess

import pytest

from scripts.ops import check_docs
from scripts.ops.check_docs import (
    check,
    check_links,
    check_make_calls,
    check_paths,
    has_unterminated_fence,
    heading_anchors,
    main,
    makefile_targets,
    repository_files,
    split_code,
)

FILES = {"README.md", "docs/llm.md", "src/core/llm/client.py", "Makefile"}


@pytest.fixture(autouse=True)
def _clean_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A git hook exports GIT_DIR or GIT_INDEX_FILE; the throwaway repositories
    below would otherwise talk to the real one."""
    for name in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        monkeypatch.delenv(name, raising=False)


def test_headings_become_github_anchors() -> None:
    text = "# Quick start (polling)\n## `make run`\n## Roles\n## Roles\n"
    assert heading_anchors(text) == {
        "quick-start-polling",
        "make-run",
        "roles",
        "roles-1",
    }


def test_a_heading_inside_a_code_block_is_no_anchor() -> None:
    assert heading_anchors("```bash\n# not a heading\n```\n") == set()


def test_code_is_split_from_prose() -> None:
    prose, code = split_code("See `make test`.\n```bash\nmake lint\n```\n[x](a.md)\n")
    assert "make lint" not in prose
    assert "make test" not in prose
    assert "[x](a.md)" in prose
    assert code == ["make test", "make lint\n"]


def test_a_link_to_a_missing_file_is_reported(tmp_path: Path) -> None:
    problems = check_links(tmp_path, "README.md", "[LLM](docs/gone.md)", FILES)
    assert problems == ["README.md: link to docs/gone.md, which is not in the tree"]


def test_a_link_to_a_missing_heading_is_reported(tmp_path: Path) -> None:
    """A section moved out of the README keeps its old anchors in other files."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/llm.md").write_text("# LLM\n## Pricing\n", encoding="utf-8")
    assert check_links(tmp_path, "README.md", "[p](docs/llm.md#pricing)", FILES) == []
    assert check_links(tmp_path, "README.md", "[p](docs/llm.md#fallback)", FILES) == [
        "README.md: link to docs/llm.md#fallback, which has no such heading"
    ]


def test_an_anchor_in_the_same_file_is_checked(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# Top\n[x](#top) [y](#nope)\n", encoding="utf-8"
    )
    assert check_links(tmp_path, "README.md", "[x](#top) [y](#nope)", FILES) == [
        "README.md: link to #nope, which has no such heading"
    ]


def test_external_links_and_directories_pass(tmp_path: Path) -> None:
    text = "[a](https://example.com/x) [b](mailto:a@b.c) [c](src/core/llm/)"
    assert check_links(tmp_path, "README.md", text, FILES) == []


def test_a_link_out_of_a_subdirectory_resolves_from_that_directory(
    tmp_path: Path,
) -> None:
    assert check_links(tmp_path, "docs/llm.md", "[r](../README.md)", FILES) == []
    assert check_links(tmp_path, "docs/llm.md", "[r](README.md)", FILES) == [
        "docs/llm.md: link to README.md, which is not in the tree"
    ]


def test_an_unknown_make_target_is_reported() -> None:
    targets = {"run-dev", "logs", "migration", "i18n-compile", "restart", "cert-issue"}
    code = [
        "make run-dev",
        "make logs s=bot",
        'make migration m="add notes table"',
        "make i18n-compile restart s=bot",
        "make cert-issue DOMAIN=<host of WEBHOOK_BASE_URL>",
        "cd infra && make run-dev",
        "make deploy",
    ]
    assert check_make_calls("docs/x.md", code, targets) == [
        "docs/x.md: make deploy names no Makefile target"
    ]


def test_the_word_make_in_a_sentence_is_no_call() -> None:
    """A layout tree in a code block says "helpers behind make targets"."""
    code = ["├── scripts/      helpers behind make targets and pre-commit hooks\n"]
    assert check_make_calls("README.md", code, {"lint"}) == []


def _git_tree(root: Path, ignored: str = "") -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".gitignore").write_text(ignored, encoding="utf-8")


def test_a_missing_repository_path_is_reported(tmp_path: Path) -> None:
    _git_tree(tmp_path)
    code = [
        "src/core/llm/client.py",
        "src/core/llm/",
        "src/core/gone.py",
        "src/<module>/di.py",
        "locales/<locale>/LC_MESSAGES/messages.po",
        "infra/requirements/*.in",
    ]
    assert check_paths(tmp_path, "docs/x.md", code, FILES) == [
        "docs/x.md: src/core/gone.py is not in the tree"
    ]


def test_a_path_relative_to_its_document_exists(tmp_path: Path) -> None:
    """infra/ansible/README.md names tests/ci/check_mode.sh beside itself."""
    _git_tree(tmp_path)
    files = {"infra/ansible/README.md", "infra/ansible/tests/ci/check_mode.sh"}
    code = ["tests/ci/check_mode.sh"]
    assert check_paths(tmp_path, "infra/ansible/README.md", code, files) == []


def test_an_ignored_path_is_a_build_artefact_not_a_missing_file(tmp_path: Path) -> None:
    """A virtualenv or a collections directory the docs tell you to create."""
    _git_tree(tmp_path, ".venv/\n")
    code = ["infra/ansible/.venv", "infra/ansible/.venv/bin/python"]
    assert check_paths(tmp_path, "infra/ansible/README.md", code, set()) == []


def test_badge_links_and_titled_links_are_checked(tmp_path: Path) -> None:
    prose = (
        "[![License](https://img.shields.io/x.svg)](LICENSE) "
        '[guide](docs/gone.md "Guide")'
    )
    assert check_links(tmp_path, "README.md", prose, {"README.md"}) == [
        "README.md: link to LICENSE, which is not in the tree",
        "README.md: link to docs/gone.md, which is not in the tree",
    ]


def test_makefile_targets_are_read_from_rule_lines(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text(
        "X = 1\n.PHONY: run\nrun: ## Run\n\techo run\ntest-all: test\n",
        encoding="utf-8",
    )
    assert makefile_targets(tmp_path) == {"run", "test-all"}


def test_untracked_but_unignored_files_count_and_ignored_ones_do_not(
    tmp_path: Path,
) -> None:
    """A link to a local-only file passes on the developer's machine and fails
    in CI, so an ignored file is not in the tree."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("private.md\n", encoding="utf-8")
    (tmp_path / "new.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "private.md").write_text("x\n", encoding="utf-8")
    assert repository_files(tmp_path) == {".gitignore", "new.md"}


def test_a_path_inside_a_command_is_checked(tmp_path: Path) -> None:
    """`pytest tests/unit/gone.py` breaks as silently as a bare path."""
    _git_tree(tmp_path)
    code = [
        "pytest tests/unit/gone.py",
        "tests/unit/gone2.py::test_x",
        "src/gone3.py:12",
        "python scripts/ops/gone4.py\nmake lint\n",
    ]
    assert check_paths(tmp_path, "docs/x.md", code, FILES) == [
        "docs/x.md: tests/unit/gone.py is not in the tree",
        "docs/x.md: tests/unit/gone2.py is not in the tree",
        "docs/x.md: src/gone3.py is not in the tree",
        "docs/x.md: scripts/ops/gone4.py is not in the tree",
    ]


def test_things_that_are_not_repository_paths_pass(tmp_path: Path) -> None:
    _git_tree(tmp_path)
    code = [
        "git clone https://github.com/o/r/tree/main/src/gone.py",
        "docker pull ghcr.io/org/src/app:latest",
        "ls /etc/src/gone.py",
        "curl localhost:8000/docs/gone",
        "python -m src.core.gone",
        "\u251c\u2500\u2500 src/gone.py\n\u2514\u2500\u2500 tests/gone.py\n",
        "tests/unit/<name>/*.py src/{a,b}.py",
    ]
    assert check_paths(tmp_path, "docs/x.md", code, FILES) == []


def test_make_is_a_command_after_a_prefix() -> None:
    code = ["run: make nope1", "sudo make nope2", "time make nope3", "then make nope4"]
    assert check_make_calls("docs/x.md", code, {"lint"}) == [
        "docs/x.md: make nope1 names no Makefile target",
        "docs/x.md: make nope2 names no Makefile target",
        "docs/x.md: make nope3 names no Makefile target",
        "docs/x.md: make nope4 names no Makefile target",
    ]


def test_an_option_value_is_not_a_make_target() -> None:
    code = ["make -C infra up", "make -f build.mk up", "make --directory infra up"]
    assert check_make_calls("docs/x.md", code, {"up"}) == []


def test_mermaid_blocks_are_not_shell() -> None:
    """An edge label `-->|make sure|` is no pipe into make."""
    text = "```mermaid\nA -->|make sure| B\nA --> src/gone.py\n```\n"
    prose, code = split_code(text)
    assert code == []
    assert check_make_calls("README.md", code, {"lint"}) == []


def test_reference_definitions_and_single_quoted_titles_are_checked(
    tmp_path: Path,
) -> None:
    prose = "[a][ref] [b](gone2.md 'Title')\n\n[ref]: gone1.md\n"
    assert check_links(tmp_path, "README.md", prose, {"README.md"}) == [
        "README.md: link to gone2.md, which is not in the tree",
        "README.md: link to gone1.md, which is not in the tree",
    ]


def test_a_link_to_the_repository_root_passes(tmp_path: Path) -> None:
    text = "[a](../) [b](.) [c](./)"
    assert check_links(tmp_path, "docs/llm.md", text, FILES) == []


def test_a_fence_closes_only_on_the_same_character_and_length() -> None:
    text = "````md\n```\n# inner\n```\n````\n# Real\n~~~\n# tilde\n~~~\n"
    assert heading_anchors(text) == {"real"}
    assert not has_unterminated_fence(text)


def test_an_unterminated_fence_is_reported(tmp_path: Path) -> None:
    """The rest of the file would otherwise be dropped without a word."""
    _git_tree(tmp_path)
    (tmp_path / "Makefile").write_text("lint:\n\ttrue\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("```bash\nmake lint\n", encoding="utf-8")
    assert has_unterminated_fence("```bash\nmake lint\n")
    assert check(tmp_path) == ["README.md: a fenced code block is never closed"]


def test_setext_headings_have_anchors() -> None:
    text = "Quick start\n===========\n\nRoles\n-----\n\nText\n\n---\n"
    assert heading_anchors(text) == {"quick-start", "roles"}


def test_punctuation_is_dropped_from_an_anchor() -> None:
    assert heading_anchors("# Why? (and how)\n") == {"why-and-how"}


def test_a_link_to_a_file_deleted_from_the_working_tree_is_reported(
    tmp_path: Path,
) -> None:
    """It is still in the index, so the tree lists it but it cannot be read."""
    files = {"README.md", "docs/gone.md"}
    assert check_links(tmp_path, "README.md", "[a](docs/gone.md#top)", files) == [
        "README.md: link to docs/gone.md#top, which is not in the tree"
    ]


def _documented_tree(root: Path, readme: str) -> None:
    _git_tree(root)
    (root / "Makefile").write_text("lint:\n\ttrue\n", encoding="utf-8")
    (root / "README.md").write_text(readme, encoding="utf-8")


def test_check_collects_links_make_calls_and_paths(tmp_path: Path) -> None:
    _documented_tree(
        tmp_path, "[a](docs/gone.md)\n\n```bash\nmake nope\n```\n\n`src/gone.py`\n"
    )
    assert check(tmp_path) == [
        "README.md: link to docs/gone.md, which is not in the tree",
        "README.md: make nope names no Makefile target",
        "README.md: src/gone.py is not in the tree",
    ]


def test_main_exits_1_and_prints_each_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _documented_tree(tmp_path, "`src/gone.py`\n")
    monkeypatch.setattr(check_docs, "ROOT", tmp_path)
    assert main() == 1
    assert capsys.readouterr().out == "README.md: src/gone.py is not in the tree\n"


def test_main_exits_0_on_a_clean_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _documented_tree(tmp_path, "`make lint`\n")
    monkeypatch.setattr(check_docs, "ROOT", tmp_path)
    assert main() == 0
    assert capsys.readouterr().out == ""


def test_what_a_copy_command_creates_is_not_a_missing_file(tmp_path: Path) -> None:
    """The docs tell you to `cp -r inventory/example inventory/production`."""
    _git_tree(tmp_path)
    files = {"infra/ansible/inventory/example/hosts.yml"}
    code = ["cp -r infra/ansible/inventory/example infra/ansible/inventory/production"]
    assert check_paths(tmp_path, "README.md", code, files) == []
    gone = ["cp -r infra/ansible/inventory/gone infra/ansible/inventory/production"]
    assert check_paths(tmp_path, "README.md", gone, files) == [
        "README.md: infra/ansible/inventory/gone is not in the tree"
    ]
