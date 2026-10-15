import os
from pathlib import Path
import shutil
import subprocess

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "infra/ansible/roles/firewall/files/docker-user-rules.sh"
# Stands in for both restore tools: records the rule set instead of applying it.
FAKE_RESTORE = '#!/bin/sh\ncat >> "$RULES_OUT"\n'
WEB_PORTS_RULE = "-A DOCKER-USER -p tcp -m multiport --dports 80,443 -j RETURN"


def _rules(tmp_path: Path, environment: dict[str, str]) -> list[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("iptables-restore", "ip6tables-restore"):
        fake = bin_dir / name
        fake.write_text(FAKE_RESTORE, encoding="utf-8")
        fake.chmod(0o755)
    rules_out = tmp_path / "rules"
    bash = shutil.which("bash")
    assert bash is not None
    subprocess.run(
        [bash, str(SCRIPT)],
        env={
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "RULES_OUT": str(rules_out),
            **environment,
        },
        check=True,
    )
    lines = rules_out.read_text(encoding="utf-8").splitlines()
    # iptables and ip6tables get the same rule set; one copy is enough.
    assert lines[: len(lines) // 2] == lines[len(lines) // 2 :]
    return lines[: len(lines) // 2]


def test_polling_lets_no_published_port_through(tmp_path: Path) -> None:
    """A polling bot only calls out. A published port on its server is a mistake
    (a debug container, a forgotten -p), and DOCKER-USER is the only thing that
    filters it: ufw never sees traffic Docker forwards."""
    rules = _rules(tmp_path, {"PUBLIC_TCP_PORTS": "", "TRUSTED_INTERFACES": ""})

    assert not [rule for rule in rules if "--dports" in rule]
    assert rules[-3:] == [
        "-A DOCKER-USER -j DROP",
        "-A DOCKER-USER -j RETURN",
        "COMMIT",
    ]


def test_a_missing_port_list_lets_nothing_through(tmp_path: Path) -> None:
    """The settings file is written by Ansible; a server where it is missing or
    lost the line must fail closed, not fall back to opening web ports."""
    rules = _rules(tmp_path, {})

    assert not [rule for rule in rules if "--dports" in rule]
    assert "-A DOCKER-USER -j DROP" in rules


def test_webhook_lets_80_and_443_through(tmp_path: Path) -> None:
    """Telegram reaches the webhook through nginx on 443 and certbot's HTTP-01
    challenge on 80; a DOCKER-USER without them silently stops every update."""
    rules = _rules(tmp_path, {"PUBLIC_TCP_PORTS": "80,443", "TRUSTED_INTERFACES": ""})

    assert WEB_PORTS_RULE in rules
    assert rules.index(WEB_PORTS_RULE) < rules.index("-A DOCKER-USER -j DROP")
