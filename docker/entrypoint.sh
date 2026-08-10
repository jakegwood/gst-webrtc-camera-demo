#!/usr/bin/env bash
# Launch the three WebRTC-rig processes in one container:
#   1. signalling server (ws://, --disable-ssl)
#   2. static web client (http)
#   3. camera sender (foreground, restarts after each session)
set -uo pipefail

echo "[entrypoint] signalling server on :${SIGNALLING_PORT} (ws://, --disable-ssl)"
python3 signalling/simple_server.py --disable-ssl --port "${SIGNALLING_PORT}" &

echo "[entrypoint] static web client on :${HTTP_PORT}"
python3 -m http.server "${HTTP_PORT}" -d sendrecv/js &

# Give the signalling server a moment to bind before the sender connects.
sleep 1

echo "[entrypoint] camera sender (id=${OUR_ID}, enc=${VIDEO_ENCODING}, source=${SOURCE:-test-pattern})"
# The sender exits after each session (upstream behavior), so loop to restart it.
# SOURCE is intentionally unquoted: "--camera" -> one flag, empty -> test pattern.
while true; do
    python3 sendrecv/gst/webrtc_sendrecv.py \
        --server "ws://127.0.0.1:${SIGNALLING_PORT}" \
        --our-id "${OUR_ID}" ${SOURCE} --video-encoding "${VIDEO_ENCODING}"
    echo "[entrypoint] sender exited, restarting in 2s..."
    sleep 2
done
