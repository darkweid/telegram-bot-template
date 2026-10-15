from collections.abc import Iterator
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NGINX_DIR = PROJECT_ROOT / "infra/nginx"
# A comment, a quoted string, a brace or semicolon, or a bare word; a word may
# carry a template's ${NAME}, whose braces are not blocks.
_TOKEN = re.compile(r'#[^\n]*|"[^"]*"|[{};]|(?:\$\{[A-Z_]+\}|[^\s{};"#])+')
# What render.sh hands envsubst; anything else in a template must reach
# nginx untouched ($host, $remote_addr, ...).
SUBSTITUTED = frozenset({"WEBHOOK_HOST", "WEBHOOK_PATH"})
DEFAULT_TELEGRAM_SUBNETS = "149.154.160.0/20,91.108.4.0/22"
HSTS = "Strict-Transport-Security"
# Stands in for getent: resolves only the names listed in KNOWN_HOSTS.
FAKE_GETENT = '#!/bin/sh\ncase " $KNOWN_HOSTS " in *" $2 "*) exit 0 ;; esac\nexit 2\n'


@dataclass
class Directive:
    name: str
    args: list[str]
    block: list["Directive"] | None = None

    def children(self, name: str) -> list["Directive"]:
        return [child for child in self.block or [] if child.name == name]


def _parse_tokens(tokens: Iterator[str]) -> list[Directive]:
    directives: list[Directive] = []
    words: list[str] = []
    for token in tokens:
        if token == ";":
            directives.append(Directive(words[0], words[1:]))
            words = []
        elif token == "{":
            directives.append(Directive(words[0], words[1:], _parse_tokens(tokens)))
            words = []
        elif token == "}":
            return directives
        else:
            words.append(token.strip('"'))
    return directives


def _parse(file_name: str) -> list[Directive]:
    text = (NGINX_DIR / file_name).read_text(encoding="utf-8")
    tokens = (token for token in _TOKEN.findall(text) if not token.startswith("#"))
    return _parse_tokens(tokens)


def _servers(file_name: str) -> list[Directive]:
    return [directive for directive in _parse(file_name) if directive.name == "server"]


def _server(file_name: str, *, default: bool) -> Directive:
    matching = [
        server
        for server in _servers(file_name)
        if ("default_server" in server.children("listen")[0].args) is default
    ]
    assert len(matching) == 1, file_name
    return matching[0]


def _drops(location: Directive) -> bool:
    return [(child.name, child.args) for child in location.block or []] == [
        ("return", ["444"])
    ]


def _drops_every_request(server: Directive) -> bool:
    """A `return 444` at server level answers no request whatever its path."""
    return [child.args for child in server.children("return")] == [["444"]]


def _guard(directive: Directive) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """An `if` as (condition words, body directives)."""
    assert directive.name == "if"
    body = [(child.name, child.args) for child in directive.block or []]
    return directive.args, body


def _geo(variable: str) -> Directive:
    geos = [
        directive
        for directive in _parse("main.conf")
        if directive.name == "geo" and directive.args == [variable]
    ]
    assert len(geos) == 1, variable
    return geos[0]


def _ci() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (PROJECT_ROOT / ".github/workflows/_ci.yml").read_text(encoding="utf-8")
    )
    return loaded


def test_the_webhook_is_the_only_route_open_to_telegram() -> None:
    """Everything the bot serves besides the webhook (health, metrics a bot
    adds later) must not become reachable from the internet by accident."""
    server = _server("tls.conf.template", default=False)
    assert server.children("server_name")[0].args == ["${WEBHOOK_HOST}"]
    locations = {
        tuple(location.args): location for location in server.children("location")
    }
    assert set(locations) == {
        ("=", "${WEBHOOK_PATH}"),
        ("~", "^/(live|ready|health)$"),
        ("/",),
    }
    webhook = locations[("=", "${WEBHOOK_PATH}")]
    assert webhook.block is not None
    assert [_guard(child) for child in webhook.block[:2]] == [
        (["($from_telegram", "=", "0)"], [("return", ["444"])]),
        (["($request_method", "!=", "POST)"], [("return", ["444"])]),
    ]
    assert webhook.block[2].name == "proxy_pass"
    assert _drops(locations[("/",)])


def test_health_answers_private_networks_only() -> None:
    """/ready and /health query Postgres and Redis on every call; open to the
    internet they are a free way to load both."""
    server = _server("tls.conf.template", default=False)
    health = next(
        location
        for location in server.children("location")
        if location.args == ["~", "^/(live|ready|health)$"]
    )
    assert health.block is not None
    assert _guard(health.block[0]) == (
        ["($from_private_network", "=", "0)"],
        [("return", ["444"])],
    )


def test_every_proxied_location_checks_the_caller_first() -> None:
    """A proxy_pass without a guard in front of it is an open route; the guard
    must come first because nginx runs `if ... return` in order."""
    server = _server("tls.conf.template", default=False)
    for location in server.children("location"):
        if not location.children("proxy_pass"):
            continue
        assert location.block is not None
        first = location.block[0]
        condition, body = _guard(first)
        assert condition[0] in ("($from_telegram", "($from_private_network")
        assert body == [("return", ["444"])]


def test_unknown_hosts_get_no_answer() -> None:
    """A scanner that reaches the address by IP or with a foreign Host learns
    nothing, not even the certificate's name."""
    http_default = _server("http.conf.template", default=True)
    assert [tuple(location.args) for location in http_default.children("location")] == [
        ("/",)
    ]
    assert _drops(http_default.children("location")[0])
    tls_default = _server("tls.conf.template", default=True)
    assert tls_default.children("ssl_reject_handshake")[0].args == ["on"]
    # The webhook host's SNI with another Host header passes the handshake and
    # lands here; without the return nginx would answer it with a 404 page.
    assert _drops_every_request(tls_default)


def test_port_80_serves_the_acme_challenge_and_nothing_else() -> None:
    """Telegram calls HTTPS only; plain HTTP exists for certbot's webroot
    challenge, and nothing on it reaches the bot."""
    server = _server("http.conf.template", default=False)
    assert server.children("server_name")[0].args == ["${WEBHOOK_HOST}"]
    locations = {
        tuple(location.args): location for location in server.children("location")
    }
    assert set(locations) == {("/.well-known/acme-challenge/",), ("/",)}
    challenge = locations[("/.well-known/acme-challenge/",)]
    assert challenge.children("root")[0].args == ["/var/www/certbot"]
    assert _drops(locations[("/",)])
    assert "proxy_pass" not in (NGINX_DIR / "http.conf.template").read_text(
        encoding="utf-8"
    )


def _sends_hsts(server: Directive) -> bool:
    return any(header.args[0] == HSTS for header in server.children("add_header"))


def test_every_answering_server_sends_the_security_headers() -> None:
    """A server added later without the include would answer without them,
    and nothing else would notice."""
    for file_name in ("http.conf.template", "tls.conf.template"):
        for server in _servers(file_name):
            if server.children("ssl_reject_handshake"):
                continue  # refuses the handshake; no response is ever sent
            includes = [include.args for include in server.children("include")]
            assert ["/etc/nginx/generated/security_headers.inc"] in includes, file_name


def _includes(directives: list[Directive]) -> Iterator[str]:
    for directive in directives:
        if directive.name == "include":
            yield directive.args[0]
        yield from _includes(directive.block or [])


def test_nothing_nginx_loads_is_read_from_the_checkout() -> None:
    """A reload would read a file from the mounted checkout as soon as `git pull`
    changed it, untested, and a configuration the running nginx refuses could
    not be put back: everything nginx includes is rendered or copied first."""
    for path in sorted(NGINX_DIR.glob("*.template")) + [NGINX_DIR / "main.conf"]:
        for include in _includes(_parse(path.name)):
            assert not include.startswith("/etc/nginx/bot/"), (path.name, include)
    render = (NGINX_DIR / "render.sh").read_text(encoding="utf-8")
    assert (
        'cp "$source_dir/security_headers.inc" "$generated/security_headers.inc"'
        in render
    )


def test_hsts_is_sent_over_tls_only() -> None:
    """Browsers ignore Strict-Transport-Security over plain HTTP (RFC 6797), so
    on port 80 it only looks like a promise; the webhook host's TLS server is
    where it counts."""
    include = (NGINX_DIR / "security_headers.inc").read_text(encoding="utf-8")
    assert HSTS not in include
    assert not any(_sends_hsts(server) for server in _servers("http.conf.template"))
    assert _sends_hsts(_server("tls.conf.template", default=False))


def test_the_http_context_hides_the_version_and_caps_the_body() -> None:
    """Telegram's updates are small JSON bodies; a megabyte is far above any of
    them and far below what would let a caller tie up the bot."""
    main = {directive.name: directive.args for directive in _parse("main.conf")}
    assert main["server_tokens"] == ["off"]
    assert main["client_max_body_size"] == ["1m"]


def test_the_bridge_gateway_is_carved_out_of_the_private_networks() -> None:
    """Docker forwards published-port connections it proxies (every IPv6 client
    among them) from the bridge gateway, a private address: allowing it would
    open the probes to the internet."""
    private = _geo("$from_private_network")
    entries = [(child.name, child.args) for child in private.block or []]
    assert entries[0] == ("default", ["0"])
    assert ("172.16.0.0/12", ["1"]) in entries
    assert ("include", ["/etc/nginx/generated/bridge_gateway.inc"]) in entries
    render = (NGINX_DIR / "render.sh").read_text(encoding="utf-8")
    assert "printf '%d.%d.%d.%d 0;\\n'" in render
    assert '>"$generated/bridge_gateway.inc"' in render


def test_only_telegram_subnets_reach_the_webhook() -> None:
    """The secret header is the second check, not the first: a request from
    anywhere else must not reach the bot to be refused there."""
    telegram = _geo("$from_telegram")
    assert [(child.name, child.args) for child in telegram.block or []] == [
        ("default", ["0"]),
        ("include", ["/etc/nginx/generated/telegram_subnets.inc"]),
    ]


def test_the_default_subnets_agree_everywhere() -> None:
    """compose's default is what runs, .env.example is what a developer reads."""
    services = yaml.safe_load(
        (PROJECT_ROOT / "infra/docker-compose.yml").read_text(encoding="utf-8")
    )["services"]
    assert services["nginx"]["environment"]["TELEGRAM_ALLOWED_SUBNETS"] == (
        f"${{TELEGRAM_ALLOWED_SUBNETS:-{DEFAULT_TELEGRAM_SUBNETS}}}"
    )
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert f"\n# TELEGRAM_ALLOWED_SUBNETS={DEFAULT_TELEGRAM_SUBNETS}\n" in env_example


def test_templates_use_only_the_substituted_variables() -> None:
    """envsubst is given an explicit list; a ${NAME} outside it would reach
    nginx literally and fail `nginx -t`, and an unrestricted envsubst would
    blank $host and every other nginx variable."""
    render = (NGINX_DIR / "render.sh").read_text(encoding="utf-8")
    listed = re.search(r"^variables='(.*)'$", render, re.MULTILINE)
    assert listed
    assert set(re.findall(r"\$\{([A-Z_]+)\}", listed.group(1))) == SUBSTITUTED
    for template in NGINX_DIR.glob("*.template"):
        used = set(re.findall(r"\$\{([A-Z_]+)\}", template.read_text(encoding="utf-8")))
        assert used <= SUBSTITUTED, template.name


def test_ci_gates_the_image_on_the_nginx_check() -> None:
    """A broken nginx configuration must stop the image from being pushed, or
    the next webhook deploy fails on the box."""
    jobs = _ci()["jobs"]
    assert "nginx" in jobs
    for job in ("record-pass", "build-and-push"):
        assert "nginx" in jobs[job]["needs"], job


def test_a_missing_certificate_names_the_command_that_issues_it() -> None:
    """A deploy that stops on a missing certificate shows this line as its
    reason, and an operator reads it as the next step to take."""
    render = (NGINX_DIR / "render.sh").read_text(encoding="utf-8")
    assert 'certificate="/etc/letsencrypt/live/$WEBHOOK_HOST/fullchain.pem"' in render
    assert (
        'echo "nginx: no certificate at $certificate; '
        'run make cert-issue DOMAIN=$WEBHOOK_HOST" >&2'
    ) in render


def test_a_failed_reload_does_not_end_the_reload_loop() -> None:
    """The loop runs under the entrypoint's `set -e`: one failed reload would
    end it, and no renewed certificate would be put in use again until the
    container restarts, by which time the old one may have expired."""
    entrypoint = (NGINX_DIR / "entrypoint.sh").read_text(encoding="utf-8")
    loop = re.search(
        r"^\s*\(while sleep 6h; do (.*); done\) &$", entrypoint, re.MULTILINE
    )
    assert loop
    assert re.fullmatch(r"nginx -s reload \|\| .+", loop.group(1))


def _pin_is_live(tmp_path: Path, applied: str, known_hosts: str) -> bool:
    entrypoint = (NGINX_DIR / "entrypoint.sh").read_text(encoding="utf-8")
    function = re.search(
        r"^pin_is_live\(\) \{\n.*?^\}\n", entrypoint, re.DOTALL | re.MULTILINE
    )
    assert function
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    getent = bin_dir / "getent"
    getent.write_text(FAKE_GETENT, encoding="utf-8")
    getent.chmod(0o755)
    applied_file = tmp_path / "bot_upstream.applied"
    applied_file.write_text(applied, encoding="utf-8")
    sh = shutil.which("sh")
    assert sh is not None
    result = subprocess.run(
        [sh, "-c", f"set -eu\n{function.group(0)}pin_is_live"],
        env={
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "KNOWN_HOSTS": known_hosts,
            "applied": str(applied_file),
        },
        check=False,
    )
    return result.returncode == 0


def test_a_pin_to_running_containers_is_restored(tmp_path: Path) -> None:
    """nginx coming back in the middle of a roll must route where the roll left
    it, not to every bot container at once."""
    applied = "server bot-blue:8080 resolve;\nserver bot-green:8080 resolve;\n"
    assert _pin_is_live(tmp_path, applied, "bot-blue bot-green")


def test_a_pin_to_a_gone_container_is_dropped(tmp_path: Path) -> None:
    """A pin naming a container that no longer exists would leave nginx with
    nothing to route to; the service name is used instead."""
    applied = "server bot-blue:8080 resolve;\nserver bot-green:8080 resolve;\n"
    assert not _pin_is_live(tmp_path, applied, "bot-blue")


def test_a_pin_without_a_server_is_dropped(tmp_path: Path) -> None:
    """A record with no server line (cut short, written by hand) would leave
    the upstream empty and every update answered with 502."""
    assert not _pin_is_live(tmp_path, "# nothing pinned\n", "bot")


def _upstream_after(
    tmp_path: Path, command: str, inc: str | None, applied: str | None
) -> tuple[str | None, str | None]:
    """Runs the entrypoint's upstream step for `command` on a volume holding
    `inc` and `applied` (None: absent), and returns what the volume holds after."""
    entrypoint = (NGINX_DIR / "entrypoint.sh").read_text(encoding="utf-8")
    step = re.search(r"^upstream=.*?^fi\n", entrypoint, re.DOTALL | re.MULTILINE)
    assert step
    state = tmp_path / "upstream"
    state.mkdir()
    for name, content in (("bot_upstream.inc", inc), ("bot_upstream.applied", applied)):
        if content is not None:
            (state / name).write_text(content, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    getent = bin_dir / "getent"
    getent.write_text(FAKE_GETENT, encoding="utf-8")
    getent.chmod(0o755)
    sh = shutil.which("sh")
    assert sh is not None
    subprocess.run(
        [sh, "-c", f"set -eu\nset -- {command}\n{step.group(0)}"],
        env={
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "KNOWN_HOSTS": "bot bot-blue",
            "WEB_SERVER_PORT": "8080",
            "state": str(state),
        },
        check=True,
    )
    after = [
        path.read_text(encoding="utf-8") if path.exists() else None
        for path in (state / "bot_upstream.inc", state / "bot_upstream.applied")
    ]
    return after[0], after[1]


def test_a_configuration_test_leaves_the_running_pin_alone(tmp_path: Path) -> None:
    """deploy.sh tests the new configuration in a container of its own that
    shares the upstream volume with the running nginx; rewriting the pin there
    would make the running nginx route somewhere else on its next reload, and
    the record would no longer say where it routes."""
    pin = "server bot-blue:8080 resolve;\n"
    assert _upstream_after(tmp_path, "nginx -t", pin, pin) == (pin, pin)


def test_a_configuration_test_on_an_empty_volume_names_the_service(
    tmp_path: Path,
) -> None:
    """On a first deploy nothing has written the upstream yet, and the test's
    include must resolve; the record stays for the nginx that will serve."""
    assert _upstream_after(tmp_path, "nginx -t", None, None) == (
        "server bot:8080 resolve;\n",
        None,
    )


def test_a_starting_nginx_restores_the_pin(tmp_path: Path) -> None:
    """The test run's exemption must not reach the serving nginx, which routes
    where the record says after a restart in the middle of a roll."""
    pin = "server bot-blue:8080 resolve;\n"
    assert _upstream_after(tmp_path, "nginx -g 'daemon off;'", "stale", pin) == (
        pin,
        pin,
    )
