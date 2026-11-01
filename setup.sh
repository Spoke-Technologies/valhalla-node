#!/usr/bin/env bash
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo 'Run with sudo: sudo bash setup.sh' >&2; exit 1; }
[[ -f /etc/os-release ]] || exit 1
. /etc/os-release
[[ $ID == ubuntu && ( $VERSION_ID == 22.04 || $VERSION_ID == 24.04 ) ]] || {
  echo 'Supported: Ubuntu 22.04 or 24.04 LTS.' >&2; exit 1;
}
[[ $(uname -m) == x86_64 ]] || { echo 'Use an AMD64/x86_64 VPS.' >&2; exit 1; }
awk '/MemTotal/ {exit ($2 < 1800000)}' /proc/meminfo || {
  echo 'At least 2 GB RAM is required.' >&2; exit 1;
}
install -d -m 755 /opt/valhalla-node
exec 9>/opt/valhalla-node/update.lock
flock -n 9 || {
  echo 'An install, update or rollback is running. Wait before re-running setup.' >&2
  exit 1
}
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
apt-get update
apt-get install -y ca-certificates curl python3
if ! command -v docker >/dev/null; then apt-get install -y docker.io; fi
systemctl enable --now docker
[[ -x /usr/bin/docker ]] || { echo 'Expected Docker at /usr/bin/docker.' >&2; exit 1; }
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
install -d -m 755 /usr/local/lib/valhalla-node /opt/valhalla-node/releases
for file in node.py valhalla.json valhalla.env run-service.sh cloudflare-setup.sh; do
  install -m 644 "$source_dir/$file" "/usr/local/lib/valhalla-node/$file.new"
  mv "/usr/local/lib/valhalla-node/$file.new" "/usr/local/lib/valhalla-node/$file"
done
install -m 755 "$source_dir/valhalla-node" /usr/local/bin/valhalla-node
install -m 644 "$source_dir/valhalla-node.service" /etc/systemd/system/valhalla-node.service
systemctl daemon-reload
flock -u 9
exec 9>&-
valhalla-node update
