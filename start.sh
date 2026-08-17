#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
sudo docker-compose up -d
echo "View stream at: http://$(hostname).local:8080/#peer-id=camera1,remote-offerer=1,connect=1"
