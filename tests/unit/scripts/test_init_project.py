import ast
from collections.abc import Iterator
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from scripts import init_project
from scripts.init_project import (
    GENERATED_SECRETS,
    MAKEFILE_BLOCK,
    MAX_NAME_LENGTH,
    SELF_FILES,
    InitError,
    main,
    run,
    validate_name,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}")
GIT = (
    "git",
    "-c",
    "user.name=test",
    "-c",
    "user.email=test@example.com",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        [*GIT, *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture(scope="session")
def pristine(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A committed copy of the tracked tree, as a fresh clone would have it."""
    root = tmp_path_factory.mktemp("pristine")
    tracked = _git(REPO_ROOT, "ls-files", "-z").split("\0")
    for path in {p for p in tracked if p} | set(SELF_FILES):
        source = REPO_ROOT / path
        if source.is_symlink() or not source.is_file():
            continue
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "template")
    return root


@pytest.fixture
def copy(pristine: Path, tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "bot"
    shutil.copytree(pristine, root, symlinks=True)
    yield root


def _env(root: Path) -> dict[str, str]:
    pairs = {}
    for line in (root / ".env").read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.startswith("#"):
            pairs[key] = value
    return pairs


@pytest.mark.parametrize(
    "name",
    [
        "",
        "My-Bot",
        "1bot",
        "bot_",
        "-bot",
        "bot-",
        "my--bot",
        "a" * 41,
        "telegram-bot-template",
    ],
)
def test_a_name_compose_cannot_carry_is_refused(name: str) -> None:
    """The name becomes the compose project and every volume's prefix; a bad one
    would fail later inside docker with a far less helpful message."""
    with pytest.raises(InitError):
        validate_name(name)


def test_an_unknown_locale_is_refused_untouched(copy: Path) -> None:
    """DEFAULT_LOCALE outside SUPPORTED_LOCALES stops the bot at startup."""
    with pytest.raises(InitError, match="de"):
        run(copy, "demo", "de")
    assert _git(copy, "status", "--porcelain") == ""
    assert not (copy / ".env").exists()


def test_the_slug_is_replaced_everywhere_but_links_to_the_template(copy: Path) -> None:
    """A leftover slug shares volumes and images with every other bot started
    from the template on the same machine."""
    run(copy, "demo", None)
    found = subprocess.run(
        ["git", "grep", "-n", "telegram-bot-template", "--", "."],
        cwd=copy,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    leftovers = [line for line in found if "darkweid/telegram-bot-template" not in line]
    assert leftovers == []
    compose = (copy / "infra/docker-compose.yml").read_text(encoding="utf-8")
    assert "\nname: demo\n" in compose
    assert "name: demo-postgres-data" in compose
    assert "LOCAL_BOT_IMAGE = demo-bot:latest" in (copy / "Makefile").read_text(
        encoding="utf-8"
    )
    catalog = (copy / "locales/ru/LC_MESSAGES/messages.po").read_text(encoding="utf-8")
    assert '"Project-Id-Version: demo\\n"' in catalog


def test_the_env_gets_generated_secrets(copy: Path) -> None:
    """`make run-dev` must start on the first try, and with secrets nobody else
    has, not the placeholders every copy of the template shares."""
    run(copy, "demo", "ru")
    env = _env(copy)
    secrets = [env[key] for key in GENERATED_SECRETS]
    assert all(SECRET_PATTERN.fullmatch(value) for value in secrets)
    assert len(set(secrets)) == len(secrets)
    assert env["PROJECT_NAME"] == '"demo"'
    assert env["DEFAULT_LOCALE"] == "ru"
    assert "not-real" in env["BOT_TOKEN"]
    assert (copy / ".env").stat().st_mode & 0o777 == 0o600
    example = (copy / ".env.example").read_text(encoding="utf-8")
    assert 'PROJECT_NAME="demo"' in example
    assert "DEFAULT_LOCALE=ru" in example


def test_an_existing_env_keeps_its_values(copy: Path) -> None:
    """A developer who ran the stack before init keeps the token and the
    passwords their volumes were created with."""
    (copy / ".env").write_text(
        "BOT_TOKEN=123:kept\nPOSTGRES_PASSWORD=mine\n"
        "REDIS_PASSWORD=example-redis-password-not-real\nWEBHOOK_SECRET=\n",
        encoding="utf-8",
    )
    run(copy, "demo", None)
    env = _env(copy)
    assert env["BOT_TOKEN"] == "123:kept"
    assert env["POSTGRES_PASSWORD"] == "mine"
    assert SECRET_PATTERN.fullmatch(env["REDIS_PASSWORD"])
    assert SECRET_PATTERN.fullmatch(env["WEBHOOK_SECRET"])
    assert env["PROJECT_NAME"] == '"demo"'


def test_secrets_never_reach_the_output(
    copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The output lands in terminal scrollback and CI logs."""
    assert main(["demo"], root=copy) == 0
    printed = capsys.readouterr()
    env = _env(copy)
    for key in GENERATED_SECRETS:
        assert key in printed.out
        assert env[key] not in printed.out + printed.err
    assert "SECURITY.md" in printed.out


def test_the_readme_is_replaced_and_the_script_removes_itself(copy: Path) -> None:
    """The template's README describes the template; a second run has nothing
    left to rename, so the tool goes with the job."""
    run(copy, "demo", None)
    readme = (copy / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# demo\n")
    assert "https://github.com/darkweid/telegram-bot-template" in readme
    for path in SELF_FILES:
        assert not (copy / path).exists()
    makefile = (copy / "Makefile").read_text(encoding="utf-8")
    assert "init-project" not in makefile
    assert "##@ Stack" in makefile


def test_a_dirty_tree_is_refused_untouched(copy: Path) -> None:
    """The rename has to be one diff the developer can review or drop."""
    (copy / "src/main/config.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(InitError, match="commit or stash"):
        run(copy, "demo", None)
    compose = (copy / "infra/docker-compose.yml").read_text(encoding="utf-8")
    assert "name: telegram-bot-template" in compose


def test_a_second_run_is_refused(copy: Path) -> None:
    run(copy, "demo", None)
    _git(copy, "add", "-A")
    _git(copy, "commit", "-qm", "init")
    with pytest.raises(InitError, match="already initialised"):
        run(copy, "other", None)


def test_a_refusal_exits_1_with_one_line(
    copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["Bad_Name"], root=copy) == 1
    err = capsys.readouterr().err
    assert err.startswith("init-project: ")
    assert err.count("\n") == 1


def test_the_makefile_carries_the_exact_target_block() -> None:
    """The script deletes the target by its exact text; a drifted Makefile would
    leave a target pointing at a deleted script."""
    assert MAKEFILE_BLOCK in (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


def test_the_script_runs_on_python_3_9() -> None:
    """It runs on the machine's python3 before any virtualenv exists, which on
    macOS is 3.9."""
    source = Path(init_project.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 9))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported <= sys.stdlib_module_names | {"__future__"}


def test_the_rename_changes_only_the_project_constants_in_python(copy: Path) -> None:
    """A renamed copy has to pass `make lint` untouched: black wraps a line by its
    length, so a Python line that carries the name is reflowed for some names and
    not for others. The name lives in one single-line constant per module, which
    no name up to the longest one can make black rewrap."""
    run(copy, "a" * MAX_NAME_LENGTH, None)
    diff = _git(
        copy, "diff", "-U0", "--", "*.py", *(f":!{path}" for path in SELF_FILES)
    ).splitlines()
    changed = [
        line[1:]
        for line in diff
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    assert changed
    assert all(re.fullmatch(r'PROJECT = "[a-z0-9-]+"', line) for line in changed)
