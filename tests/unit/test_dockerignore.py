from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

RUNTIME_PATHS = {
    "!src/",
    "!loggers/",
    "!models/",
    "!migrations/",
    "!locales/",
    "!alembic.ini",
    "!infra/requirements/base.txt",
    "!infra/requirements/prod.txt",
    "!infra/requirements/dev.txt",
    "!infra/postgres/postgresql.conf",
}


def _patterns() -> list[str]:
    lines = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def test_the_build_context_denies_everything_but_a_short_allowlist() -> None:
    """The Dockerfile copies the whole context, so a deny list let any file
    nobody thought of - an env file under another spelling, a backup, a key -
    ship in a layer of an image pushed to the registry."""
    patterns = _patterns()

    assert patterns[0] == "*"
    assert {p for p in patterns if p.startswith("!")} == RUNTIME_PATHS


def test_compiled_catalogs_and_bytecode_stay_out_of_the_image() -> None:
    patterns = _patterns()

    assert patterns.index("**/*.mo") > patterns.index("!locales/")
    assert patterns.index("**/__pycache__") > patterns.index("!src/")
