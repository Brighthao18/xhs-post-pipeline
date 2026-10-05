#!/bin/bash
set -e
cd /app
source /app/environment.sh
platform_name="$(uname -m)"
source "/app/env_${platform_name}/bin/activate"
export DISPLAY=:99
Xvfb :99 -screen 0 1920x1080x24 -ac &
exec python3 /app/codex-bootstrap.py
