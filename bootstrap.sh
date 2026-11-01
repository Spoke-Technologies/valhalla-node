#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo: sudo bash bootstrap.sh' >&2; exit 1; }
command -v curl >/dev/null || {
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl
}
directory=$(mktemp -d)
trap 'rm -rf -- "$directory"' EXIT
# shortcut: installs main; pin a commit archive for controlled fleet rollouts.
curl --fail --location --silent --show-error --proto '=https' --proto-redir '=https' \
  https://github.com/Spoke-Technologies/valhalla-node/archive/refs/heads/main.tar.gz \
  -o "$directory/source.tar.gz"
tar -xzf "$directory/source.tar.gz" -C "$directory"
bash "$directory/valhalla-node-main/setup.sh"
