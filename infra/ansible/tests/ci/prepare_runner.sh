#!/usr/bin/env bash
# Makes a GitHub-hosted runner look like the fresh Ubuntu server site.yml previews:
# sshd and ufw installed as a stock server image ships them, and keys for ops and
# deploy. The preview itself changes nothing (tests/ci/check_mode.sh).
set -euo pipefail

if [ "${RUNNER_ENVIRONMENT:-}" != "github-hosted" ]; then
    echo "Refusing to prepare anything but a GitHub-hosted runner" >&2
    exit 1
fi

keys="${RUNNER_TEMP:?}/ansible-ci"
mkdir -p "$keys"

sudo apt-get update -q
# python3-apt: the apt module runs under the system interpreter.
sudo apt-get install -yq openssh-server ufw python3-apt

# The sshd -t check of the sshd role needs it; ssh.socket creates it only on the
# first connection, which a runner may never have had.
sudo install -d -m 0755 /run/sshd

for name in ops deploy; do
    ssh-keygen -q -t ed25519 -N '' -C "ci-$name" -f "$keys/$name"
done
