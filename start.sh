#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# ---------------------------------------------------------------------------
# Memfault integration (optional but recommended)
# ---------------------------------------------------------------------------
# If memfaultd is installed and configured, mount its CLI and config into the
# container so the sender can record per-session TTFF and streaming metrics.
# Without it the demo still works — you just won't get metrics in Memfault.

MEMFAULT_OK=true

if [ ! -x /usr/bin/memfaultctl ]; then
    MEMFAULT_OK=false
fi

if [ ! -f /etc/memfaultd.conf ]; then
    MEMFAULT_OK=false
fi

if [ "$MEMFAULT_OK" = true ]; then
    # Check that the live-view session is configured
    if ! grep -q live-view /etc/memfaultd.conf 2>/dev/null; then
        MEMFAULT_OK=false
    fi
fi

if [ "$MEMFAULT_OK" = true ]; then
    echo "[start] Memfault detected — enabling session metrics."
    cat > docker-compose.override.yml << 'OVERRIDE'
services:
  webrtc-cam:
    volumes:
      - /usr/bin/memfaultctl:/usr/bin/memfaultctl:ro
      - /etc/memfaultd.conf:/etc/memfaultd.conf:ro
OVERRIDE
else
    rm -f docker-compose.override.yml
    echo ""
    echo "======================================================================"
    echo "  WARNING: Memfault is not configured — session metrics are DISABLED."
    echo ""
    echo "  The camera demo will work fine, but no TTFF or streaming metrics"
    echo "  will be recorded. To enable Memfault instrumentation:"
    echo ""
    echo "    1. Install memfaultd on the host"
    echo "    2. Add the live-view session to /etc/memfaultd.conf (see README)"
    echo "    3. sudo systemctl restart memfaultd"
    echo "    4. Re-run ./start.sh"
    echo "======================================================================"
    echo ""
fi

sudo docker compose up -d "$@"
