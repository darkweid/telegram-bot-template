import ast
from pathlib import Path
import re
import sys

import pytest

from scripts.ops.check_env import KNOWN_KEYS, EnvFileError, check, main, parse_env

PROJECT_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = PROJECT_ROOT / "scripts/ops/check_env.py"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
# Shaped like real ones, issued by nobody.
TOKEN = "123456789:" + "A" * 35
SECRET = "s" * 48
VALID = {
    "BOT_TOKEN": TOKEN,
    "BOT_MODE": "polling",
    "COMPOSE_PROFILES": "",
    "POSTGRES_PASSWORD": "a-password",
    "REDIS_PASSWORD": "another-password",
}
WEBHOOK = {
    **VALID,
    "BOT_MODE": "webhook",
    "COMPOSE_PROFILES": "webhook",
    "WEBHOOK_BASE_URL": "https://bot.example.com",
    "WEBHOOK_SECRET": SECRET,
}


def _errors(**changes: str) -> list[str]:
    return check({**VALID, **changes}).errors


def _webhook_errors(**changes: str) -> list[str]:
    return check({**WEBHOOK, **changes}).errors


def _filled_env_example() -> str:
    """.env.example as a developer fills it in: a token, and a real value in
    place of every placeholder."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    text = re.sub(r"^BOT_TOKEN=.*$", f"BOT_TOKEN={TOKEN}", text, flags=re.MULTILINE)
    return re.sub(
        r"^([A-Z0-9_]+)=.*not-real.*$", r"\1=filled-in", text, flags=re.MULTILINE
    )


def test_a_valid_polling_env_passes() -> None:
    assert check(VALID).errors == []
    assert check(VALID).warnings == []


def test_a_valid_webhook_env_passes() -> None:
    assert check(WEBHOOK).errors == []


def test_the_filled_in_example_passes_without_a_warning() -> None:
    """The example is the starting point of every deployment; a rule it trips
    on from the start would be ignored, and a warning on it would teach
    everyone to read past warnings."""
    report = check(parse_env(_filled_env_example()))
    assert report.errors == []
    assert report.warnings == []


def test_the_example_as_shipped_is_refused() -> None:
    """A deploy from an unedited copy would start with a password everyone
    can read in the repository."""
    report = check(parse_env(ENV_EXAMPLE.read_text(encoding="utf-8")))
    assert "BOT_TOKEN is not a token @BotFather issued" in report.errors
    assert any(error.startswith("POSTGRES_PASSWORD ") for error in report.errors)
    assert any(error.startswith("REDIS_PASSWORD ") for error in report.errors)


def test_every_key_in_the_example_is_known() -> None:
    """A key added to .env.example and not here would warn on every
    deployment; a new setting goes into KNOWN_KEYS in the same commit as its
    line in the example."""
    names = re.findall(
        r"^(?:# )?([A-Z][A-Z0-9_]*)=",
        ENV_EXAMPLE.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    assert names
    assert sorted(set(names) - KNOWN_KEYS) == []


@pytest.mark.parametrize(
    "token",
    ["", "replace-with-the-token-from-BotFather", "1234:" + "A" * 35, TOKEN + "!"],
)
def test_a_malformed_token_is_refused(token: str) -> None:
    assert _errors(BOT_TOKEN=token) == ["BOT_TOKEN is not a token @BotFather issued"]


def test_an_unknown_mode_is_refused() -> None:
    assert "BOT_MODE must be polling or webhook" in _errors(BOT_MODE="Webhook")


def test_an_empty_mode_is_refused() -> None:
    """BotConfig rejects an empty BOT_MODE; only a missing key means polling."""
    assert "BOT_MODE must be polling or webhook" in _errors(BOT_MODE="")
    env = {key: value for key, value in VALID.items() if key != "BOT_MODE"}
    assert check(env).errors == []


@pytest.mark.parametrize(
    ("mode", "profiles"),
    [("polling", "webhook"), ("webhook", ""), ("webhook", "monitoring")],
)
def test_the_compose_profile_follows_the_mode(mode: str, profiles: str) -> None:
    """A polling bot with nginx up publishes ports nobody serves; a webhook bot
    without it never receives an update."""
    env = WEBHOOK if mode == "webhook" else VALID
    errors = check({**env, "COMPOSE_PROFILES": profiles}).errors
    assert len(errors) == 1
    assert errors[0].startswith("COMPOSE_PROFILES must contain webhook")


def test_other_profiles_may_run_beside_the_webhook() -> None:
    assert _webhook_errors(COMPOSE_PROFILES="monitoring, webhook") == []


@pytest.mark.parametrize(
    "base_url",
    ["", "http://bot.example.com", "https://", "bot.example.com"],
)
def test_webhook_mode_needs_an_https_origin(base_url: str) -> None:
    assert _webhook_errors(WEBHOOK_BASE_URL=base_url) == [
        "WEBHOOK_BASE_URL must be an https:// URL in webhook mode"
    ]


@pytest.mark.parametrize(
    "base_url",
    ["https://bot.example.com/bot", "https://bot.example.com/?a=1"],
)
def test_a_base_url_with_a_path_is_refused(base_url: str) -> None:
    """nginx serves WEBHOOK_PATH at the root of the host: Telegram would call
    /bot/webhook/telegram and get no answer, and the bot would sit silent."""
    errors = _webhook_errors(WEBHOOK_BASE_URL=base_url)
    assert len(errors) == 1
    assert errors[0].startswith("WEBHOOK_BASE_URL must be the origin only")


def test_a_trailing_slash_and_the_https_port_are_an_origin() -> None:
    assert _webhook_errors(WEBHOOK_BASE_URL="https://bot.example.com:443/") == []


@pytest.mark.parametrize(
    "url",
    [
        "https://bot.example.com:8443",
        "https://bot.example.com:8443/",
        "https://bot.example.com:port",
        "https://bot.example.com:99999",
    ],
)
def test_a_port_other_than_443_is_refused(url: str) -> None:
    """nginx publishes 443 only and the firewall opens nothing else; Telegram
    would call the named port, nothing would answer, and every other check
    would still pass."""
    errors = _webhook_errors(WEBHOOK_BASE_URL=url)
    assert len(errors) == 1
    assert errors[0].startswith("WEBHOOK_BASE_URL must not name a port")


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "127.1.2.3", "localhost", "::1", "[::1]"]
)
def test_a_loopback_web_server_is_refused_in_webhook_mode(host: str) -> None:
    """The bot's own healthcheck passes on loopback, but nginx runs in another
    container and could never reach it: every update would get a 502."""
    errors = _webhook_errors(WEB_SERVER_HOST=host)
    assert len(errors) == 1
    assert errors[0].startswith("WEB_SERVER_HOST must not be a loopback address")


@pytest.mark.parametrize("host", ["", "0.0.0.0", "::", "bot"])
def test_a_reachable_web_server_passes_in_webhook_mode(host: str) -> None:
    assert _webhook_errors(WEB_SERVER_HOST=host) == []


def test_a_loopback_web_server_passes_in_polling_mode() -> None:
    """Polling serves only the health app, which the bot's own healthcheck
    probes from inside its container."""
    assert _errors(WEB_SERVER_HOST="127.0.0.1") == []


@pytest.mark.parametrize(
    "path", ["webhook", "/web hook", "/-", "/a//b", "/" + "a" * 256]
)
def test_a_malformed_webhook_path_is_refused(path: str) -> None:
    errors = _webhook_errors(WEBHOOK_PATH=path)
    assert len(errors) == 1
    assert errors[0].startswith("WEBHOOK_PATH must start with /")


@pytest.mark.parametrize("secret", ["", "short", "s" * 31 + "!", "s" * 257])
def test_webhook_mode_needs_a_secret(secret: str) -> None:
    errors = _webhook_errors(WEBHOOK_SECRET=secret)
    assert len(errors) == 1
    assert errors[0].startswith("WEBHOOK_SECRET must be")


def test_webhook_settings_are_not_checked_in_polling_mode() -> None:
    assert _errors(WEBHOOK_BASE_URL="http://x", WEBHOOK_SECRET="short") == []


@pytest.mark.parametrize(
    "subnets", ["149.154.160.1/20", "149.154.160.0/20,nonsense", "1.2.3.4/33"]
)
def test_the_telegram_subnets_must_be_networks(subnets: str) -> None:
    """nginx would refuse a host address with a mask, or worse, allow a range
    nobody meant."""
    errors = _errors(TELEGRAM_ALLOWED_SUBNETS=subnets)
    assert len(errors) == 1
    assert errors[0].startswith("TELEGRAM_ALLOWED_SUBNETS must be")


def test_empty_subnets_fall_back_to_the_default() -> None:
    """compose substitutes its default for an empty value, so nothing is
    wrong with the file."""
    assert _errors(TELEGRAM_ALLOWED_SUBNETS="") == []


def test_the_default_subnets_pass() -> None:
    assert _errors(TELEGRAM_ALLOWED_SUBNETS="149.154.160.0/20, 91.108.4.0/22") == []


@pytest.mark.parametrize("key", ["ADMIN_IDS", "DEVELOPER_IDS"])
@pytest.mark.parametrize("value", ["-100123", "12,abc", "0", "1\u00b2"])
def test_role_ids_are_positive_user_ids(key: str, value: str) -> None:
    assert _errors(**{key: value}) == [
        f"{key} must be comma-separated positive Telegram user ids"
    ]


def test_role_ids_may_be_spaced_or_empty() -> None:
    assert _errors(ADMIN_IDS="1, 2", DEVELOPER_IDS="") == []


@pytest.mark.parametrize("value", ["0", "chat", "-"])
def test_the_alert_chat_is_a_non_zero_id(value: str) -> None:
    assert _errors(DEV_ALERT_CHAT_ID=value) == [
        "DEV_ALERT_CHAT_ID must be a non-zero chat id or empty"
    ]


def test_the_alert_chat_may_be_a_group() -> None:
    assert _errors(DEV_ALERT_CHAT_ID="-1001234567890") == []


@pytest.mark.parametrize("value", ["true", "1", "on", "yes"])
def test_logging_model_content_is_refused(value: str) -> None:
    """It writes what users send to the model into the logs; useful while
    developing, never in a deployment."""
    errors = _errors(LLM_LOG_CONTENT=value)
    assert len(errors) == 1
    assert errors[0].startswith("LLM_LOG_CONTENT must be off")


@pytest.mark.parametrize("value", ["", "false", "0", "off", "No"])
def test_model_content_logging_off_passes(value: str) -> None:
    assert _errors(LLM_LOG_CONTENT=value) == []


def test_a_placeholder_is_refused() -> None:
    errors = _errors(POSTGRES_PASSWORD="example-postgres-password-not-real")
    assert errors == ["POSTGRES_PASSWORD still holds the placeholder from .env.example"]


def test_a_placeholder_of_a_disabled_feature_passes() -> None:
    """An optional feature ships switched off with its placeholders in place; a
    bot that uses none of them needs to touch none of them."""
    assert (
        _errors(
            SENTRY_ENABLED="false",
            SENTRY_DSN="https://example-key-not-real@sentry.example/1",
            S3_ENABLED="false",
            S3_SECRET_ACCESS_KEY="example-not-real",
            EMAIL_ENABLED="false",
            EMAIL_PASSWORD="example-not-real",
            LLM_PROVIDER="",
            LLM_FALLBACK_PROVIDER="",
            ANTHROPIC_API_KEY="example-not-real",
        )
        == []
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"SENTRY_ENABLED": "true", "SENTRY_DSN": "https://not-real@sentry.example/1"},
        {"S3_ENABLED": "True", "S3_SECRET_ACCESS_KEY": "example-not-real"},
        {"EMAIL_ENABLED": "1", "EMAIL_PASSWORD": "example-not-real"},
        {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "example-not-real"},
        {
            "LLM_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "real",
            "LLM_FALLBACK_PROVIDER": "openrouter",
            "OPENROUTER_API_KEY": "example-not-real",
        },
    ],
)
def test_a_placeholder_of_an_enabled_feature_is_refused(
    changes: dict[str, str],
) -> None:
    errors = check({**VALID, **changes}).errors
    assert len(errors) == 1
    assert errors[0].endswith("still holds the placeholder from .env.example")


def test_the_commit_is_not_set_in_env() -> None:
    """env_file puts it into every container over the image's own ENV, so
    /health and /status would report the .env commit whatever image runs."""
    errors = _errors(COMMIT_SHA="abc")
    assert len(errors) == 1
    assert errors[0].startswith("COMMIT_SHA is stamped into the image")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("BOT_CPUS", "0"),
        ("BOT_CPUS", "half"),
        ("BOT_MEMORY", "512 MB"),
        ("REDIS_MEMORY", "-1G"),
        ("POSTGRES_HOST_PORT", "65536"),
        ("REDIS_HOST_PORT", "port"),
        ("REDIS_HOST_PORT", "1\u00b2"),
        ("BOT_CPUS", "1\u00b2"),
    ],
)
def test_limits_and_ports_must_parse(key: str, value: str) -> None:
    """compose fails on them only when it starts the service, halfway through
    a deploy."""
    errors = _errors(**{key: value})
    assert len(errors) == 1
    assert errors[0].startswith(key)


def test_valid_limits_pass() -> None:
    assert _errors(BOT_CPUS="0.5", BOT_MEMORY="512M", REDIS_MEMORY="1gb") == []


def test_an_unknown_key_is_a_warning() -> None:
    """A misspelt key leaves the default in place, which may well be what was
    meant to change; a key of the developer's own is fine, so it is not an
    error."""
    report = check({**VALID, "BOT_MOD": "webhook"})
    assert report.errors == []
    assert report.warnings == [
        "BOT_MOD is not a setting the template reads; a misspelt key leaves the "
        "default in place"
    ]


def test_messages_never_echo_a_value() -> None:
    """The file holds the token and every password, and the output lands in CI
    logs and terminals."""
    secret_values = {
        "BOT_TOKEN": "leaked-token-value",
        "WEBHOOK_BASE_URL": "https://leaked-host.example/leaked-path",
        "WEBHOOK_SECRET": "leaked-secret",
        "POSTGRES_PASSWORD": "leaked-password-not-real",
        "ADMIN_IDS": "leaked-admin",
        "BOT_CPUS": "leaked-cpus",
        "UNKNOWN_LEAKED": "leaked-unknown",
    }
    report = check({**WEBHOOK, **secret_values})
    assert len(report.errors) >= 6
    for message in report.errors + report.warnings:
        assert "leaked" not in message


def test_parse_reads_the_mode_line_as_the_ansible_guard_does() -> None:
    """The provisioning guard (infra/ansible/playbooks/tasks/bot_mode_guard.yml)
    compares bot_mode with BOT_MODE read by its own regex;
    both must see the same value in every spelling the guard documents."""
    text = "  export BOT_MODE = 'polling'\nBOT_MODE =\"webhook\" # why\n"
    assert parse_env(text)["BOT_MODE"] == "webhook"


def test_parse_reads_env_files_as_compose_does() -> None:
    text = (
        "# comment\n"
        "\n"
        "A=1\n"
        "export B=two\n"
        "C='quoted # not a comment'\n"
        'D="double"\n'
        "E=value # a comment\n"
        "F=\n"
        "A=later\n"
    )
    assert parse_env(text) == {
        "A": "later",
        "B": "two",
        "C": "quoted # not a comment",
        "D": "double",
        "E": "value",
        "F": "",
    }


@pytest.mark.parametrize("line", ["leaked without an equals sign", "A='leaked"])
def test_a_malformed_line_names_its_number_only(line: str) -> None:
    """The line may be a secret pasted without its key."""
    with pytest.raises(EnvFileError) as raised:
        parse_env(f"A=1\n{line}\n")
    assert str(raised.value).startswith("line 2 ")
    assert "leaked" not in str(raised.value)


def test_main_exits_1_and_reports_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("BOT_TOKEN=nope\nCOMPOSE_PROFILES=\n", encoding="utf-8")
    assert main([str(env_file)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"{env_file}: error: BOT_TOKEN is not a token" in captured.err


def test_main_passes_with_warnings_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    lines = [f"{key}={value}" for key, value in {**VALID, "MY_OWN": "1"}.items()]
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert main([str(env_file)]) == 0
    assert f"{env_file}: warning: MY_OWN" in capsys.readouterr().err


def test_main_refuses_a_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([str(tmp_path / ".env")]) == 1
    assert f"{tmp_path / '.env'}: error: no such file" in capsys.readouterr().err


def test_get_prints_one_value_for_the_deploy_script(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """deploy.sh reads BOT_MODE and BOT_IMAGE through it, so the shell never
    sources .env and never runs what a value contains."""
    env_file = tmp_path / ".env"
    env_file.write_text("BOT_MODE=webhook\nBOT_TOKEN=x\n", encoding="utf-8")
    assert main(["--get", "BOT_MODE", str(env_file)]) == 0
    assert main(["--get", "BOT_IMAGE", str(env_file)]) == 0
    assert capsys.readouterr().out == "webhook\n\n"


def test_the_script_needs_nothing_but_the_standard_library() -> None:
    """It runs on the box with the system python3, where neither the bot's
    dependencies nor the src package can be imported."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module is not None
            imported.add(node.module.split(".")[0])
    assert imported
    assert sorted(imported - sys.stdlib_module_names) == []


def test_a_backslash_in_double_quotes_is_refused() -> None:
    """compose unescapes it and this parser would not, so the two would read
    different values; the error names the line, never the value."""
    with pytest.raises(EnvFileError) as raised:
        parse_env('A=1\nB="leaked\\nvalue"\n')
    assert str(raised.value).startswith("line 2 ")
    assert "leaked" not in str(raised.value)


def test_a_backslash_in_single_quotes_is_literal() -> None:
    assert parse_env("A='a\\nb'\n") == {"A": "a\\nb"}


def test_main_refuses_a_file_it_cannot_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A directory, a permission error or a non-UTF-8 file must end in one
    line, not a traceback that could quote the content."""
    binary = tmp_path / "binary.env"
    binary.write_bytes(b"A=\xff\xfe leaked\n")
    for path in (tmp_path, binary):
        assert main([str(path)]) == 1
        err = capsys.readouterr().err
        assert err == f"{path}: error: cannot read the file as UTF-8 text\n"


def test_main_refuses_a_file_the_user_may_not_read(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permission error ends in the same one line; the OS message may name
    paths and is left out."""

    def refuse(self: Path, encoding: str | None = None) -> str:
        raise PermissionError(13, "denied for leaked-reason", str(self))

    monkeypatch.setattr(Path, "read_text", refuse)
    env_file = tmp_path / ".env"
    assert main([str(env_file)]) == 1
    err = capsys.readouterr().err
    assert err == f"{env_file}: error: cannot read the file as UTF-8 text\n"
