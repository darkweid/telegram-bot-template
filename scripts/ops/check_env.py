"""Checks a deployment's .env before anything starts from it: `make check-env`
and the first step of infra/deploy/deploy.sh. It catches what the bot's own
config cannot see (the compose profile, nginx's subnets, container limits, a
placeholder left in a secret) and what it would only report once the container
crash-loops.

Standard library only, and nothing from `src`: it runs on the box with the
system python3, where the bot's dependencies are not installed. The format
rules repeat the ones in src/main/config.py on purpose for that reason.

A message names the key, never its value: the file holds the bot token and
every password, and the output lands in CI logs.

    python3 scripts/ops/check_env.py [PATH]          check, exit 1 on an error
    python3 scripts/ops/check_env.py --get KEY [PATH] print one value
"""

import argparse
from dataclasses import dataclass, field
import ipaddress
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

# Every key the template reads from .env: the bot's settings, compose's own
# (profiles, image, host ports, limits) and those of nginx. A key missing here
# is reported as unknown, so a new setting is added here in the same commit as
# its line in .env.example (tests/unit/scripts/ops/test_check_env.py).
KNOWN_KEYS = frozenset(
    {
        # AppConfig
        "PROJECT_NAME",
        "VERSION",
        "DEBUG",
        "TESTING",
        "LOG_LEVEL",
        "LOG_JSON",
        # BotConfig
        "BOT_TOKEN",
        "BOT_MODE",
        "DEFAULT_LOCALE",
        "USER_TOUCH_INTERVAL_SECONDS",
        "USER_CACHE_TTL_SECONDS",
        "TELEGRAM_RETRY_ATTEMPTS",
        "SHUTDOWN_TIMEOUT_SECONDS",
        "ADMIN_IDS",
        "DEVELOPER_IDS",
        "DEV_ALERT_CHAT_ID",
        "ALERT_COOLDOWN_SECONDS",
        "NOTIFY_ADMINS_ON_NEW_USER",
        # HttpConfig
        "HTTP_USER_AGENT",
        # WebServerConfig, WebhookConfig
        "WEB_SERVER_HOST",
        "WEB_SERVER_PORT",
        "WEBHOOK_BASE_URL",
        "WEBHOOK_PATH",
        "WEBHOOK_SECRET",
        "WEBHOOK_MAX_CONNECTIONS",
        # PostgresConfig
        "DB_ECHO",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "DB_STATEMENT_TIMEOUT_SECONDS",
        "DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS",
        # RedisConfig
        "REDIS_HOST",
        "REDIS_PASSWORD",
        "REDIS_PORT",
        "REDIS_DATABASE",
        "REDIS_FSM_DATABASE",
        "REDIS_TASKS_DATABASE",
        # CacheConfig
        "CACHE_ENABLED",
        "CACHE_DEFAULT_TTL",
        "CACHE_VERSION_TTL",
        "CACHE_KEY_PREFIX",
        # SentryConfig
        "SENTRY_DSN",
        "SENTRY_ENV",
        "SENTRY_ENABLED",
        # ThrottlingConfig
        "THROTTLE_DEFAULT_LIMIT",
        "THROTTLE_DEFAULT_WINDOW_SECONDS",
        "THROTTLE_LLM_LIMIT",
        "THROTTLE_LLM_WINDOW_SECONDS",
        # BroadcastConfig
        "BROADCAST_RATE_PER_SECOND",
        "BROADCAST_CHECKPOINT_EVERY",
        # LLMConfig: the rule below reads it by name
        "LLM_LOG_CONTENT",
        # Build time: refused below, listed so the message says why
        "COMMIT_SHA",
        # compose and nginx
        "COMPOSE_PROFILES",
        "TELEGRAM_ALLOWED_SUBNETS",
        "BOT_IMAGE",
        "POSTGRES_HOST_PORT",
        "REDIS_HOST_PORT",
        *(
            f"{service}_{resource}"
            for service in (
                "POSTGRES",
                "MIGRATE",
                "BOT",
                "WORKER",
                "SCHEDULER",
                "REDIS",
                "NGINX",
                "CERTBOT",
            )
            for resource in ("CPUS", "MEMORY")
        ),
    }
)

# src/main/config.py: _BOT_TOKEN_PATTERN, _WEBHOOK_SECRET_PATTERN and
# WebhookConfig.validate_path.
BOT_TOKEN_PATTERN = re.compile(r"[0-9]{5,}:[A-Za-z0-9_-]{30,}")
WEBHOOK_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}")
WEBHOOK_PATH_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
WEBHOOK_PATH_MAX_LENGTH = 256
DEFAULT_WEBHOOK_PATH = "/webhook/telegram"
BOT_MODES = ("polling", "webhook")
WEBHOOK_PROFILE = "webhook"
# What pydantic reads as true for a bool setting.
TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})
FALSE_VALUES = frozenset({"", "0", "false", "f", "no", "n", "off"})
# Marks every secret placeholder in .env.example.
PLACEHOLDER_MARK = "not-real"
# A key with one of these prefixes belongs to a feature its switch turns off;
# a placeholder there is harmless until the switch is on.
FEATURE_SWITCHES = {
    "SENTRY_": "SENTRY_ENABLED",
    "S3_": "S3_ENABLED",
    "EMAIL_": "EMAIL_ENABLED",
}
# ANTHROPIC_API_KEY and the like: needed only when the provider it is named
# after is LLM_PROVIDER or LLM_FALLBACK_PROVIDER.
PROVIDER_KEY = re.compile(r"([A-Z][A-Z0-9]*)_API_KEY")
LLM_PROVIDER_KEYS = ("LLM_PROVIDER", "LLM_FALLBACK_PROVIDER")
ASCII_DIGITS = re.compile(r"[0-9]+")
CPUS_PATTERN = re.compile(r"[0-9]+(\.[0-9]+)?")
# compose's byte units: 512M, 1g, 1.5GB, 1048576.
MEMORY_PATTERN = re.compile(r"[0-9]+(\.[0-9]+)?([bkmgBKMG][bB]?)?")
HOST_PORT_KEYS = ("POSTGRES_HOST_PORT", "REDIS_HOST_PORT")
_LINE = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)")


class EnvFileError(Exception):
    """A line that is not KEY=VALUE; the message carries the line number only."""


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _unquote(raw: str) -> str:
    if raw[:1] in ("'", '"'):
        closing = raw.find(raw[0], 1)
        if closing == -1:
            raise ValueError
        if raw[0] == '"' and "\\" in raw[1:closing]:
            # compose unescapes inside double quotes; this parser would not.
            raise ValueError
        return raw[1:closing]
    # Unquoted: a " #" starts a comment, as in compose's env files.
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def parse_env(text: str) -> dict[str, str]:
    """The subset of compose's env-file syntax the template's files use:
    KEY=VALUE lines, an optional `export`, '...' and "..." quotes without
    escapes, a ` #` comment after an unquoted value; a later line wins.
    It does not interpolate `$VAR`, which compose does in unquoted and
    double-quoted values (single quotes keep a literal `$`), and it refuses a
    backslash inside double quotes, which compose would unescape."""
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _LINE.fullmatch(stripped)
        if match is None:
            raise EnvFileError(f"line {number} is not KEY=VALUE")
        try:
            values[match.group(1)] = _unquote(match.group(2).strip())
        except ValueError:
            raise EnvFileError(
                f"line {number} has an unclosed quote or a backslash in double quotes"
            ) from None
    return values


def _is_true(value: str) -> bool:
    return value.strip().lower() in TRUE_VALUES


def _check_ids(env: dict[str, str], key: str, report: Report) -> None:
    for raw_id in env.get(key, "").split(","):
        user_id = raw_id.strip()
        if user_id and not (ASCII_DIGITS.fullmatch(user_id) and int(user_id) > 0):
            report.errors.append(
                f"{key} must be comma-separated positive Telegram user ids"
            )
            return


def _check_webhook(env: dict[str, str], report: Report) -> None:
    base_url = urlsplit(env.get("WEBHOOK_BASE_URL", ""))
    if base_url.scheme != "https" or not base_url.hostname:
        report.errors.append("WEBHOOK_BASE_URL must be an https:// URL in webhook mode")
    elif base_url.path not in ("", "/") or base_url.query or base_url.fragment:
        # nginx serves WEBHOOK_PATH at the root of the host; Telegram would
        # call <path>/WEBHOOK_PATH and get no answer.
        report.errors.append(
            "WEBHOOK_BASE_URL must be the origin only (no path, query or "
            "fragment); the path goes into WEBHOOK_PATH"
        )
    path = env.get("WEBHOOK_PATH") or DEFAULT_WEBHOOK_PATH
    segments = path[1:].split("/")
    if (
        not path.startswith("/")
        or len(path) > WEBHOOK_PATH_MAX_LENGTH
        or not all(
            WEBHOOK_PATH_SEGMENT.fullmatch(segment) and segment.strip("-")
            for segment in segments
        )
    ):
        report.errors.append(
            "WEBHOOK_PATH must start with / and hold segments of "
            "A-Z a-z 0-9 _ -, at most 256 characters"
        )
    if not WEBHOOK_SECRET_PATTERN.fullmatch(env.get("WEBHOOK_SECRET", "")):
        report.errors.append(
            "WEBHOOK_SECRET must be 32 to 256 characters of A-Z a-z 0-9 _ - "
            "in webhook mode"
        )


def _feature_is_off(env: dict[str, str], key: str) -> bool:
    for prefix, switch in FEATURE_SWITCHES.items():
        if key.startswith(prefix) and key != switch:
            return not _is_true(env.get(switch, ""))
    provider = PROVIDER_KEY.fullmatch(key)
    if provider:
        in_use = {env.get(name, "").strip().lower() for name in LLM_PROVIDER_KEYS}
        return provider.group(1).lower() not in in_use
    return False


def _check_limits(env: dict[str, str], report: Report) -> None:
    for key, value in env.items():
        if key.endswith("_CPUS") and value:
            if not CPUS_PATTERN.fullmatch(value) or float(value) <= 0:
                report.errors.append(f"{key} must be a positive number of CPUs")
        elif key.endswith("_MEMORY") and value:
            if not MEMORY_PATTERN.fullmatch(value):
                report.errors.append(f"{key} must be a size such as 512M or 1G")


def check(env: dict[str, str]) -> Report:
    report = Report()

    if not BOT_TOKEN_PATTERN.fullmatch(env.get("BOT_TOKEN", "")):
        report.errors.append("BOT_TOKEN is not a token @BotFather issued")

    # Only an absent key means polling: BotConfig rejects an empty BOT_MODE.
    mode = env.get("BOT_MODE", "polling")
    if mode not in BOT_MODES:
        report.errors.append("BOT_MODE must be polling or webhook")
    elif mode == "webhook":
        _check_webhook(env, report)

    profiles = {
        profile.strip() for profile in env.get("COMPOSE_PROFILES", "").split(",")
    }
    if (WEBHOOK_PROFILE in profiles) != (mode == "webhook"):
        report.errors.append(
            "COMPOSE_PROFILES must contain webhook exactly when BOT_MODE=webhook: "
            "nginx and certbot run only in webhook mode"
        )

    # Empty falls back to compose's default, like an empty limit or port.
    subnets = env.get("TELEGRAM_ALLOWED_SUBNETS", "").strip()
    if subnets:
        try:
            parsed = [
                ipaddress.ip_network(item.strip(), strict=True)
                for item in subnets.split(",")
            ]
        except ValueError:
            parsed = []
        if not parsed:
            report.errors.append(
                "TELEGRAM_ALLOWED_SUBNETS must be comma-separated networks "
                "such as 149.154.160.0/20"
            )

    _check_ids(env, "ADMIN_IDS", report)
    _check_ids(env, "DEVELOPER_IDS", report)
    chat_id = env.get("DEV_ALERT_CHAT_ID", "").strip()
    if chat_id and (not re.fullmatch(r"-?[0-9]+", chat_id) or int(chat_id) == 0):
        report.errors.append("DEV_ALERT_CHAT_ID must be a non-zero chat id or empty")

    if env.get("LLM_LOG_CONTENT", "").strip().lower() not in FALSE_VALUES:
        report.errors.append(
            "LLM_LOG_CONTENT must be off in a deployment: it writes what users "
            "send to the model into the logs"
        )

    if "COMMIT_SHA" in env:
        report.errors.append(
            "COMMIT_SHA is stamped into the image at build time; remove it from "
            ".env: env_file would put it into every container over the image's "
            "own value, and /health and /status would report that commit "
            "whatever image runs"
        )

    for key in HOST_PORT_KEYS:
        port = env.get(key, "")
        if port and not (ASCII_DIGITS.fullmatch(port) and 0 < int(port) < 65536):
            report.errors.append(f"{key} must be a port number")

    _check_limits(env, report)

    for key, value in env.items():
        if PLACEHOLDER_MARK in value and not _feature_is_off(env, key):
            report.errors.append(f"{key} still holds the placeholder from .env.example")
        if key not in KNOWN_KEYS:
            report.warnings.append(
                f"{key} is not a setting the template reads; a misspelt key "
                "leaves the default in place"
            )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check a deployment's .env before anything starts from it."
    )
    parser.add_argument("--get", metavar="KEY", help="print the value of KEY")
    parser.add_argument("path", nargs="?", default=".env", type=Path)
    args = parser.parse_args(argv)

    try:
        env = parse_env(args.path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"{args.path}: error: no such file", file=sys.stderr)
        return 1
    except (OSError, UnicodeDecodeError):
        # The class only: the file's content and the OS message stay out.
        print(
            f"{args.path}: error: cannot read the file as UTF-8 text", file=sys.stderr
        )
        return 1
    except EnvFileError as malformed:
        print(f"{args.path}: error: {malformed}", file=sys.stderr)
        return 1

    if args.get:
        print(env.get(args.get, ""))
        return 0

    report = check(env)
    for warning in report.warnings:
        print(f"{args.path}: warning: {warning}", file=sys.stderr)
    for error in report.errors:
        print(f"{args.path}: error: {error}", file=sys.stderr)
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
