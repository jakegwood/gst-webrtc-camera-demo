#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# ---------------------------------------------------------------------------
# Parse flags
# ---------------------------------------------------------------------------
KEEP_ALIVE=""
DOCKER_ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--keep-alive" ]; then
        KEEP_ALIVE=1
    else
        DOCKER_ARGS+=("$arg")
    fi
done

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

    # Build the environment block
    ENV_BLOCK="      - PYTHONUNBUFFERED=1"
    if [ -n "$KEEP_ALIVE" ]; then
        ENV_BLOCK="$ENV_BLOCK
      - KEEP_PIPELINE_ALIVE=1"
        echo "[start] Keep-alive mode enabled — pipeline persists across sessions."
    fi

    cat > docker-compose.override.yml << OVERRIDE
services:
  webrtc-cam:
    environment:
${ENV_BLOCK}
    volumes:
      - /usr/bin/memfaultctl:/usr/bin/memfaultctl:ro
      - /etc/memfaultd.conf:/etc/memfaultd.conf:ro
      - ./sendrecv:/app/sendrecv:ro
OVERRIDE
else
    # Even without Memfault, support keep-alive
    if [ -n "$KEEP_ALIVE" ]; then
        cat > docker-compose.override.yml << 'OVERRIDE'
services:
  webrtc-cam:
    environment:
      - PYTHONUNBUFFERED=1
      - KEEP_PIPELINE_ALIVE=1
    volumes:
      - ./sendrecv:/app/sendrecv:ro
OVERRIDE
        echo "[start] Keep-alive mode enabled (no Memfault)."
    else
        rm -f docker-compose.override.yml
    fi

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

sudo docker compose up -d "${DOCKER_ARGS[@]}"
