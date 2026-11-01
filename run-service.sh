#!/usr/bin/env bash
set -euo pipefail
image=$(python3 /usr/local/lib/valhalla-node/node.py image)
exec /usr/bin/docker run --rm --name spoke-valhalla \
  --log-driver local --log-opt max-size=10m --log-opt max-file=3 \
  --publish 127.0.0.1:8002:8002 \
  --env-file /usr/local/lib/valhalla-node/valhalla.env \
  --mount type=bind,src=/opt/valhalla-node/current/custom_files,dst=/custom_files \
  "$image" build_tiles
