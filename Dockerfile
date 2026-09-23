# Minimal image for the GStreamer webrtcbin camera demo (WebRTC rig only).
#
# This image contains ONLY the WebRTC services (signalling server, Python
# sendrecv camera app, static JS client). It deliberately does NOT contain
# memfaultd: memfaultd is a host-level device daemon and must run natively on
# the device (see README.md). On a device, run this image with
# --network=host so the app can reach the host's memfaultd StatsD socket.
FROM debian:trixie-slim

# Minimal runtime dependency set, verified in a clean container.
# Deliberately omitted vs. the full apt list:
#   - v4l-utils        : camera *debugging* CLI, not used at runtime
#   - gstreamer1.0-tools: gst-inspect/gst-launch, not used at runtime
#   - libnice10        : pulled in transitively by gstreamer1.0-nice
# No virtualenv: the distro's python3-websockets (15.x) is used directly.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3-gi \
      python3-gst-1.0 \
      python3-websockets \
      gir1.2-gstreamer-1.0 \
      gir1.2-gst-plugins-base-1.0 \
      gir1.2-gst-plugins-bad-1.0 \
      gstreamer1.0-plugins-base \
      gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad \
      gstreamer1.0-nice \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY signalling/ ./signalling/
COPY sendrecv/ ./sendrecv/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# 8443 = signalling (ws://), 8080 = static web client (http)
EXPOSE 8443 8080

# Unbuffered stdout so `docker logs` shows the sender's output in real time.
ENV PYTHONUNBUFFERED=1

# All knobs are overridable with -e. SOURCE=--camera streams the USB camera;
# set SOURCE= (empty) to use the built-in test pattern (for machines with no camera).
ENV OUR_ID=camera1 \
    VIDEO_ENCODING=vp8 \
    SOURCE=--camera \
    SIGNALLING_PORT=8443 \
    HTTP_PORT=8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
