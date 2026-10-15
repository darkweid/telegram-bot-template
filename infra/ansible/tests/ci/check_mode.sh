#!/usr/bin/env bash
# Previews site.yml on the runner in both bot modes and checks what follows the
# mode: the ports DOCKER-USER lets through, and the refusal of a bot_mode that
# differs from BOT_MODE in .env. Every run is --check, so the runner stays as it
# was (tests/unit/test_ansible_workflow.py).
set -euo pipefail

cd "$(dirname "$0")/../.."
export ANSIBLE_CONFIG="$PWD/ansible.cfg"
# bot_mode_env_file in tests/ci/inventory/group_vars/all.yml.
env_file="${RUNNER_TEMP:?}/ansible-ci/bot.env"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# Prints the run's output on stdout and its recap on stderr.
preview() {
    local mode="$1" output
    echo "== a preview of a fresh server, bot_mode=$mode" >&2
    printf 'BOT_MODE=%s\n' "$mode" > "$env_file"
    output="$(.venv/bin/ansible-playbook -i tests/ci/inventory playbooks/site.yml --check --diff -e "bot_mode=$mode" 2>&1)" || {
        printf '%s\n' "$output" >&2
        fail "the $mode preview failed"
    }
    printf '%s\n' "$output" | sed -n '/^PLAY RECAP/,$p' >&2
    printf '%s\n' "$output"
}

polling="$(preview polling)"
grep -qx '+PUBLIC_TCP_PORTS=' <<<"$polling" || fail "the polling preview opens web ports"

webhook="$(preview webhook)"
grep -qx '+PUBLIC_TCP_PORTS=80,443' <<<"$webhook" || fail "the webhook preview does not open 80 and 443"

echo "== a preview whose bot_mode differs from BOT_MODE in .env" >&2
# Last line wins; leading blanks, export, quotes and a comment must all be read.
printf 'BOT_MODE=polling\n  export BOT_MODE="webhook"  # comment\n' > "$env_file"
if mismatch="$(.venv/bin/ansible-playbook -i tests/ci/inventory playbooks/site.yml --check -e bot_mode=polling 2>&1)"; then
    fail "a bot_mode that differs from .env passed"
fi
grep -q 'bot_mode/BOT_MODE mismatch' <<<"$mismatch" || {
    printf '%s\n' "$mismatch" >&2
    fail "the mismatched preview failed for another reason"
}

echo "OK"
