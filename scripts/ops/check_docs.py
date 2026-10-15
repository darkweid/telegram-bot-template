"""Checks that the tracked Markdown still matches the tree: relative links and
their anchors resolve, every `make <target>` names a Makefile target, and every
repository path in backticks exists. Every key in .env.example has a row in
docs/configuration.md, and every row there names a key the example has.

A moved file or a renamed target breaks the documentation silently otherwise.
It runs as a pre-commit hook, so CI's lint job runs it on every change, a
documentation-only one included (the unit tests are skipped then).

Paths are looked for in inline code and in the lines of fenced blocks, as
tokens that start with one of the top-level directories in REPOSITORY_PATH and
are not glued to a longer word, a URL, a host or another absolute path
(`ghcr.io/org/src/app`, `/etc/src/x`, `host:8000/docs/x` are not repository
paths). A `::test` or `:12` suffix is not part of the path; a token with a
placeholder or glob (`<name>`, `*`, `{a,b}`) is skipped, and so are lines that
draw a tree. A root file named in backticks (`Makefile`, `.env.example`) is not
checked. What `cp`, `mv`, `mkdir`, `touch` or `ln` creates (the last word of
its command) is not looked for. Mermaid blocks are diagrams, not shell: no make
call or path in them is checked.
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
LINK = re.compile(r"\]\(([^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\)")
REFERENCE = re.compile(r"^ {0,3}\[[^\]]+\]:[ \t]*<?([^\s>]+)", re.MULTILINE)
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
SETEXT = re.compile(r"^ {0,3}(?:=+|-+)\s*$")
CREATES = re.compile(r"^\s*(?:sudo\s+)?(?:cp|mv|mkdir|touch|ln)\s.*\s(\S+)\s*$")
COMMAND_SEPARATOR = re.compile(r"&&|\|\||;|\|")
TREE_DRAWING = re.compile(r"[\u251c\u2514\u2502]")
INLINE_CODE = re.compile(r"`([^`\n]+)`")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
# A call starts a line or follows a shell operator; "make targets" inside a
# sentence of a layout tree is not one.
MAKE_CALL = re.compile(
    r"(?:^|[$;&|(]|\brun:)\s*(?:[A-Za-z_]\w*=\S*\s+)*(?:(?:sudo|time|then|do|else|exec)\s+)*"
    r"make\s+([^;&|#`\n]+)",
    re.MULTILINE,
)
# make options that take the next word as their value, which is no target.
MAKE_VALUE_OPTIONS = {
    "-C", "-f", "-I", "-o", "-W",
    "--directory", "--file", "--makefile", "--include-dir", "--old-file",
    "--what-if",
}  # fmt: skip
PLACEHOLDER = re.compile(r"<[^>]*>")
TARGET = re.compile(r"[a-z][a-z0-9-]*")
MAKEFILE_RULE = re.compile(r"^([A-Za-z0-9_-]+):(?!=)", re.MULTILINE)
PATH_TOKEN = re.compile(
    r"(?<![\w:@~$%-])(?<![\w-]/)"
    r"(?:src|tests|infra|scripts|loggers|models|migrations|locales|docs|\.github)"
    r"/[\w./<>*{}-]*"
)
EXTERNAL = ("http://", "https://", "mailto:")
CONFIGURATION_DOC = "docs/configuration.md"
# `KEY=` and `# KEY=` at the start of a line: a setting, set or shown commented
# out. A prose comment can start that way too (`# BOT_MODE=webhook: ...`); in
# .env.example every such comment names a real key, so the set is still exact.
EXAMPLE_KEY = re.compile(r"^#?\s?([A-Z][A-Z0-9_]*)=", re.MULTILINE)
TABLE_KEY = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|", re.MULTILINE)


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


def _parse(text: str) -> tuple[list[str], list[tuple[str, str]], bool]:
    """Prose lines, fenced blocks as (info string, body), and whether the last
    block was never closed. A fence closes only on the same character, at least
    as long as the one that opened it."""
    prose: list[str] = []
    blocks: list[tuple[str, str]] = []
    opened: tuple[str, int, str, list[str]] | None = None
    for line in text.splitlines(keepends=True):
        match = FENCE.match(line.rstrip("\r\n"))
        if opened is None:
            if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
                opened = (match.group(1)[0], len(match.group(1)), match.group(2), [])
            else:
                prose.append(line)
            continue
        char, length, info, body = opened
        if (
            match
            and match.group(1)[0] == char
            and len(match.group(1)) >= length
            and not match.group(2).strip()
        ):
            blocks.append((info.strip(), "".join(body)))
            opened = None
        else:
            body.append(line)
    if opened is not None:
        blocks.append((opened[2].strip(), "".join(opened[3])))
    return prose, blocks, opened is not None


def has_unterminated_fence(text: str) -> bool:
    return _parse(text)[2]


def split_code(text: str) -> tuple[str, list[str]]:
    """Prose with code removed, and the code: inline spans and fenced blocks,
    mermaid diagrams excepted."""
    prose_lines, blocks, _ = _parse(text)
    prose = "".join(prose_lines)
    spans = INLINE_CODE.findall(prose)
    shell = [body for info, body in blocks if info.split(maxsplit=1)[:1] != ["mermaid"]]
    return INLINE_CODE.sub("", prose), spans + shell


def _slug(heading: str) -> str:
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _is_paragraph_line(line: str) -> bool:
    stripped = line.strip()
    return (
        bool(stripped)
        and not HEADING.match(line)
        and not SETEXT.match(line)
        and not stripped.startswith(("-", "*", "+", ">", "|", "<"))
    )


def heading_anchors(text: str) -> set[str]:
    """GitHub's anchors: lowercase, punctuation dropped, spaces to dashes, and
    `-1`, `-2` for repeated headings. Inline code stays in a heading's text
    (a heading `make run` in backticks is #make-run), so only fenced blocks
    are skipped. A line of `===` or `---` under a paragraph line makes that
    line a heading too."""
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    lines = [line.rstrip("\r\n") for line in _parse(text)[0]]
    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if match:
            title = match.group(1)
        elif index and SETEXT.match(line) and _is_paragraph_line(lines[index - 1]):
            first = index - 1
            while first and _is_paragraph_line(lines[first - 1]):
                first -= 1
            title = " ".join(part.strip() for part in lines[first:index])
        else:
            continue
        base = _slug(title)
        count = seen.get(base, 0)
        seen[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


def check_links(root: Path, path: str, prose: str, files: set[str]) -> list[str]:
    problems = []
    for target in LINK.findall(prose) + REFERENCE.findall(prose):
        if target.startswith(EXTERNAL):
            continue
        file_part, _, anchor = target.partition("#")
        if file_part:
            relative = _normalise(posixpath.join(posixpath.dirname(path), file_part))
        else:
            relative = path
        if relative == "":
            continue  # the repository root
        if relative is None or not _in_tree(relative, files):
            problems.append(f"{path}: link to {target}, which is not in the tree")
            continue
        if anchor and relative.endswith(".md"):
            try:
                text = (root / relative).read_text(encoding="utf-8")
            except FileNotFoundError:  # listed by git, deleted from the disk
                problems.append(f"{path}: link to {target}, which is not in the tree")
                continue
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
            skip_next = False
            for word in words:
                if skip_next:
                    skip_next = False
                    continue
                if word in MAKE_VALUE_OPTIONS:
                    skip_next = True
                    continue
                if "=" in word or not TARGET.fullmatch(word):
                    continue
                if word not in targets:
                    problems.append(f"{path}: make {word} names no Makefile target")
    return problems


def _ignored(root: Path, path: str) -> bool:
    """A path git ignores is something the docs tell you to create (a
    virtualenv, downloaded collections), not a file that went missing."""
    # A pattern such as `.venv/` matches a directory only: a path that does
    # not exist is tried both ways.
    return any(
        subprocess.run(  # nosec B603 B607
            ["git", "check-ignore", "-q", "--no-index", "--", variant],
            cwd=root,
            check=False,
        ).returncode
        == 0
        for variant in (path, path.rstrip("/") + "/")
    )


def _missing_paths(root: Path, path: str, command: str, files: set[str]) -> list[str]:
    created = CREATES.match(command)
    problems = []
    for match in PATH_TOKEN.finditer(command):
        candidate = match.group(0).rstrip(".")
        if created and created.group(1).rstrip(".") == candidate:
            continue
        if any(mark in candidate for mark in "<>*{}"):
            continue
        beside = _normalise(posixpath.join(posixpath.dirname(path), candidate))
        if _in_tree(candidate, files) or (beside and _in_tree(beside, files)):
            continue
        if not _ignored(root, candidate):
            problems.append(f"{path}: {candidate} is not in the tree")
    return problems


def check_paths(
    root: Path, path: str, code: Iterable[str], files: set[str]
) -> list[str]:
    problems = []
    for snippet in code:
        for line in snippet.splitlines():
            if TREE_DRAWING.search(line):
                continue
            for command in COMMAND_SEPARATOR.split(line):
                problems += _missing_paths(root, path, command, files)
    return problems


def makefile_targets(root: Path) -> set[str]:
    text = (root / "Makefile").read_text(encoding="utf-8")
    return {name for name in MAKEFILE_RULE.findall(text) if not name.startswith(".")}


def check_configuration(root: Path, files: set[str]) -> list[str]:
    """Every key in .env.example has a row in the configuration doc, and every
    row names a key the example has."""
    if CONFIGURATION_DOC not in files:
        return [f"{CONFIGURATION_DOC} is missing"]
    example = set(
        EXAMPLE_KEY.findall((root / ".env.example").read_text(encoding="utf-8"))
    )
    documented = set(
        TABLE_KEY.findall((root / CONFIGURATION_DOC).read_text(encoding="utf-8"))
    )
    return [
        f"{CONFIGURATION_DOC}: {key} from .env.example is not documented"
        for key in sorted(example - documented)
    ] + [
        f"{CONFIGURATION_DOC}: {key} is not in .env.example"
        for key in sorted(documented - example)
    ]


def check(root: Path) -> list[str]:
    files = repository_files(root)
    targets = makefile_targets(root)
    problems: list[str] = []
    for path in sorted(file for file in files if file.endswith(".md")):
        if not (root / path).is_file():
            continue
        text = (root / path).read_text(encoding="utf-8")
        prose, code = split_code(text)
        problems += check_links(root, path, prose, files)
        problems += check_make_calls(path, code, targets)
        problems += check_paths(root, path, code, files)
        if has_unterminated_fence(text):
            problems.append(f"{path}: a fenced code block is never closed")
    problems += check_configuration(root, files)
    return problems


def main() -> int:
    problems = check(ROOT)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
