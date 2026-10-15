"""Checks that the tracked Markdown still matches the tree: relative links and
their anchors resolve, every `make <target>` names a Makefile target, and every
repository path in backticks exists.

A moved file or a renamed target breaks the documentation silently otherwise.
It runs as a pre-commit hook, so CI's lint job runs it on every change, a
documentation-only one included (the unit tests are skipped then).

Paths are checked only under the top-level directories in REPOSITORY_PATH:
a root file named in backticks (`Makefile`, `.env.example`) is not checked.
git runs with a fixed argument list and no shell, hence the `nosec` marks."""

from collections.abc import Iterable
from pathlib import Path
import posixpath
import re
import shlex
import subprocess  # nosec B404

ROOT = Path(__file__).resolve().parents[2]
# `](target)` rather than `[text](target)`: a badge nests one link in another,
# and a title after the target is allowed.
LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
INLINE_CODE = re.compile(r"`([^`\n]+)`")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
# A call starts a line or follows a shell operator; "make targets" inside a
# sentence of a layout tree is not one.
MAKE_CALL = re.compile(
    r"(?:^\s*|\$\s+|&&\s*|;\s*|\|\s*|\(\s*)make\s+([^;&|#`\n]+)", re.MULTILINE
)
PLACEHOLDER = re.compile(r"<[^>]*>")
TARGET = re.compile(r"[a-z][a-z0-9-]*")
MAKEFILE_RULE = re.compile(r"^([A-Za-z0-9_-]+):(?!=)", re.MULTILINE)
REPOSITORY_PATH = re.compile(
    r"(?:src|tests|infra|scripts|loggers|models|migrations|locales|docs|\.github)"
    r"/[A-Za-z0-9_./-]*"
)
EXTERNAL = ("http://", "https://", "mailto:")


def repository_files(root: Path) -> set[str]:
    """Tracked files plus new ones not yet added; ignored files are left out,
    since CI never sees them."""
    listing = subprocess.run(  # nosec B603 B607
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {path for path in listing.split("\0") if path}


def _in_tree(path: str, files: set[str]) -> bool:
    path = path.rstrip("/")
    return path in files or any(file.startswith(path + "/") for file in files)


def split_code(text: str) -> tuple[str, list[str]]:
    """Prose with code removed, and the code: inline spans and fenced blocks."""
    prose_lines: list[str] = []
    code: list[str] = []
    block: list[str] | None = None
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(("```", "~~~")):
            if block is None:
                block = []
            else:
                code.append("".join(block))
                block = None
            continue
        if block is not None:
            block.append(line)
        else:
            prose_lines.append(line)
    prose = "".join(prose_lines)
    spans = INLINE_CODE.findall(prose)
    return INLINE_CODE.sub("", prose), spans + code


def _slug(heading: str) -> str:
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _outside_fences(text: str) -> list[str]:
    lines: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(line)
    return lines


def heading_anchors(text: str) -> set[str]:
    """GitHub's anchors: lowercase, punctuation dropped, spaces to dashes, and
    `-1`, `-2` for repeated headings. Inline code stays in a heading's text
    (a heading `make run` in backticks is #make-run), so only fenced blocks
    are skipped."""
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    for line in _outside_fences(text):
        match = HEADING.match(line)
        if not match:
            continue
        base = _slug(match.group(1))
        count = seen.get(base, 0)
        seen[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


def check_links(root: Path, path: str, prose: str, files: set[str]) -> list[str]:
    problems = []
    for target in LINK.findall(prose):
        if target.startswith(EXTERNAL):
            continue
        file_part, _, anchor = target.partition("#")
        if file_part:
            relative = _normalise(posixpath.join(posixpath.dirname(path), file_part))
        else:
            relative = path
        if relative is None or not _in_tree(relative, files):
            problems.append(f"{path}: link to {target}, which is not in the tree")
            continue
        if anchor and relative.endswith(".md"):
            text = (root / relative).read_text(encoding="utf-8")
            if anchor not in heading_anchors(text):
                problems.append(f"{path}: link to {target}, which has no such heading")
    return problems


def _normalise(path: str) -> str | None:
    """`docs/../README.md` to `README.md`; None for a path that leaves the
    repository."""
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def check_make_calls(path: str, code: Iterable[str], targets: set[str]) -> list[str]:
    problems = []
    for snippet in code:
        for match in MAKE_CALL.finditer(snippet):
            arguments = PLACEHOLDER.sub("", match.group(1))
            try:
                words = shlex.split(arguments)
            except ValueError:
                words = arguments.split()
            for word in words:
                if "=" in word or not TARGET.fullmatch(word):
                    continue
                if word not in targets:
                    problems.append(f"{path}: make {word} names no Makefile target")
    return problems


def _ignored(root: Path, path: str) -> bool:
    """A path git ignores is something the docs tell you to create (a
    virtualenv, downloaded collections), not a file that went missing."""
    return (
        subprocess.run(  # nosec B603 B607
            ["git", "check-ignore", "-q", "--no-index", "--", path],
            cwd=root,
            check=False,
        ).returncode
        == 0
    )


def check_paths(
    root: Path, path: str, code: Iterable[str], files: set[str]
) -> list[str]:
    problems = []
    for snippet in code:
        candidate = snippet.strip()
        if not REPOSITORY_PATH.fullmatch(candidate):
            continue
        beside = _normalise(posixpath.join(posixpath.dirname(path), candidate))
        if _in_tree(candidate, files) or (beside and _in_tree(beside, files)):
            continue
        if not _ignored(root, candidate):
            problems.append(f"{path}: {candidate} is not in the tree")
    return problems


def makefile_targets(root: Path) -> set[str]:
    text = (root / "Makefile").read_text(encoding="utf-8")
    return {name for name in MAKEFILE_RULE.findall(text) if not name.startswith(".")}


def check(root: Path) -> list[str]:
    files = repository_files(root)
    targets = makefile_targets(root)
    problems: list[str] = []
    for path in sorted(file for file in files if file.endswith(".md")):
        if not (root / path).is_file():
            continue
        prose, code = split_code((root / path).read_text(encoding="utf-8"))
        problems += check_links(root, path, prose, files)
        problems += check_make_calls(path, code, targets)
        problems += check_paths(root, path, code, files)
    return problems


def main() -> int:
    problems = check(ROOT)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
