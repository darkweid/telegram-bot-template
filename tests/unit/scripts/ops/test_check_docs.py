from pathlib import Path
import subprocess

from scripts.ops.check_docs import (
    check_links,
    check_make_calls,
    check_paths,
    heading_anchors,
    makefile_targets,
    repository_files,
    split_code,
)

FILES = {"README.md", "docs/llm.md", "src/core/llm/client.py", "Makefile"}


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
    _git_tree(tmp_path, "infra/ansible/.venv\n")
    code = ["infra/ansible/.venv"]
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
