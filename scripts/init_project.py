"""Turn a fresh copy of the template into a bot of its own: rename the project,
fill .env with generated secrets, set the default locale and replace the README.

It runs once, before the virtualenv exists, on whatever `python3` the machine
has, so it uses the standard library only and nothing newer than Python 3.9.
pyupgrade may rewrite its annotations, which `from __future__ import
annotations` keeps as strings. git runs with a fixed argument list and no
shell, hence the `nosec` marks.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import re
import secrets
import subprocess  # nosec B404
import sys

TEMPLATE_SLUG = "telegram-bot-template"
# Links to the template itself ("started from") keep pointing at it.
SLUG_PATTERN = re.compile(r"(?<!darkweid/)telegram-bot-template")
# The name becomes the compose project and the prefix of every image, volume
# and network, which take lowercase letters, digits and dashes.
NAME_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
MAX_NAME_LENGTH = 40

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = "infra/docker-compose.yml"
ENV_EXAMPLE = ".env.example"
ENV_FILE = ".env"
LOCALES_FILE = "src/core/locales.py"
MAKEFILE = "Makefile"
README = "README.md"
SELF_FILES = ("scripts/init_project.py", "tests/unit/scripts/test_init_project.py")

# Shared placeholders would otherwise start the dev stack; `make check-env`
# refuses them before a deploy anyway.
GENERATED_SECRETS = ("POSTGRES_PASSWORD", "REDIS_PASSWORD", "WEBHOOK_SECRET")
PLACEHOLDER_MARK = "not-real"

MAKEFILE_BLOCK = (
    "##@ Project\n"
    "\n"
    ".PHONY: init-project\n"
    "init-project: ## Turn the template into your bot: "
    "make init-project NAME=my-bot [LOCALE=ru]\n"
    '\t@test -n "$(NAME)" || '
    '{ echo "NAME is required: make init-project NAME=my-bot"; exit 1; }\n'
    '\tpython3 scripts/init_project.py "$(NAME)" '
    '$(if $(LOCALE),--locale "$(LOCALE)")\n'
    "\n"
)

README_TEMPLATE = """# {name}

A Telegram bot on aiogram 3, started from
[telegram-bot-template](https://github.com/darkweid/telegram-bot-template).

## Run it

You need Docker with Compose v2 and Python 3.13 for the tooling.

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r infra/requirements/dev.txt
make run-dev                  # put the token from @BotFather into BOT_TOKEN in .env first
make logs s=bot
```

`make` lists every target. How the code is organised, how to deploy it and how
to remove the modules this bot does not need: [docs/](docs/).
"""


class InitError(Exception):
    """A refusal, raised before anything is written."""


def validate_name(name: str) -> None:
    if name == TEMPLATE_SLUG:
        raise InitError(f"NAME must differ from {TEMPLATE_SLUG}")
    if len(name) > MAX_NAME_LENGTH or not NAME_PATTERN.fullmatch(name):
        raise InitError(
            "NAME must be lowercase letters and digits in dash-separated words, "
            f"start with a letter and be at most {MAX_NAME_LENGTH} characters: my-bot"
        )


def supported_locales(root: Path) -> tuple[str, ...]:
    tree = ast.parse((root / LOCALES_FILE).read_text(encoding="utf-8"))
    for node in tree.body:
        target: ast.expr
        value: ast.expr | None
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id == "SUPPORTED_LOCALES" and value:
            return tuple(ast.literal_eval(value))
    raise InitError(f"SUPPORTED_LOCALES is not defined in {LOCALES_FILE}")


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(  # nosec B603 B607
            ["git", *args], cwd=root, capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        raise InitError("git is not installed") from None
    if result.returncode != 0:
        raise InitError("run this in a git clone of the repository")
    return result.stdout


def _check_can_run(root: Path, name: str, locale: str | None) -> list[str]:
    """Every refusal happens here, before the first write."""
    validate_name(name)
    locales = supported_locales(root)
    if locale is not None and locale not in locales:
        raise InitError(f"LOCALE {locale} is not one of {', '.join(locales)}")
    if f"\nname: {TEMPLATE_SLUG}\n" not in (root / COMPOSE_FILE).read_text(
        encoding="utf-8"
    ):
        raise InitError("this copy is already initialised")
    if _git(root, "status", "--porcelain"):
        raise InitError(
            "commit or stash your changes first: the rename is meant to be one diff"
        )
    if MAKEFILE_BLOCK not in (root / MAKEFILE).read_text(encoding="utf-8"):
        raise InitError(f"the init-project target in {MAKEFILE} was changed")
    return [path for path in _git(root, "ls-files", "-z").split("\0") if path]


def rename(root: Path, files: list[str], name: str) -> int:
    changed = 0
    for path in files:
        if path in SELF_FILES or path == README:
            continue
        file = root / path
        if file.is_symlink() or not file.is_file():
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        renamed = SLUG_PATTERN.sub(name, text)
        if renamed != text:
            file.write_text(renamed, encoding="utf-8")
            changed += 1
    return changed


def _read_key(text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}=(.*)$", text, re.MULTILINE)
    return match.group(1) if match else None


def _set_key(text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda _: line, text, count=1)
    return text.rstrip("\n") + "\n" + line + "\n"


def _with_identity(text: str, name: str, locale: str | None) -> str:
    text = _set_key(text, "PROJECT_NAME", f'"{name}"')
    if locale is not None:
        text = _set_key(text, "DEFAULT_LOCALE", locale)
    return text


def configure_env(root: Path, name: str, locale: str | None) -> list[str]:
    """Returns the keys that got a generated value; never the values."""
    example = root / ENV_EXAMPLE
    example_text = _with_identity(example.read_text(encoding="utf-8"), name, locale)
    example.write_text(example_text, encoding="utf-8")
    env = root / ENV_FILE
    text = env.read_text(encoding="utf-8") if env.exists() else example_text
    text = _with_identity(text, name, locale)
    generated = []
    for key in GENERATED_SECRETS:
        current = _read_key(text, key)
        if not current or PLACEHOLDER_MARK in current:
            text = _set_key(text, key, secrets.token_urlsafe(48))
            generated.append(key)
    env.touch(mode=0o600, exist_ok=True)
    env.chmod(0o600)
    env.write_text(text, encoding="utf-8")
    return generated


def run(root: Path, name: str, locale: str | None) -> tuple[int, list[str]]:
    files = _check_can_run(root, name, locale)
    changed = rename(root, files, name)
    generated = configure_env(root, name, locale)
    (root / README).write_text(README_TEMPLATE.format(name=name), encoding="utf-8")
    makefile = root / MAKEFILE
    makefile.write_text(
        makefile.read_text(encoding="utf-8").replace(MAKEFILE_BLOCK, ""),
        encoding="utf-8",
    )
    for path in SELF_FILES:
        (root / path).unlink(missing_ok=True)
    return changed, generated


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", help="the bot's name, such as my-bot")
    parser.add_argument("--locale", help="the default locale, such as ru")
    args = parser.parse_args(argv)
    try:
        changed, generated = run(root, args.name, args.locale)
    except InitError as error:
        print(f"init-project: {error}", file=sys.stderr)
        return 1
    print(f"Renamed {TEMPLATE_SLUG} to {args.name} in {changed} files.")
    if generated:
        print(f"{ENV_FILE}: generated {', '.join(generated)}.")
    print(
        "Next: put the token from @BotFather into BOT_TOKEN in .env and run "
        "make run-dev. Review the change with git status and commit it."
    )
    print(
        "LICENSE, SECURITY.md and .github/ISSUE_TEMPLATE still describe the "
        "template: rewrite them for your bot, or delete them together with the "
        "links to them; make lint names each link."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
