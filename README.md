# gst-webrtc-camera-demo

A minimal, **plaintext (no-TLS)**, **receive-only** WebRTC live-view demo: a Raspberry Pi
streams a USB camera to a browser on the same LAN, using GStreamer's `webrtcbin`.

This is a small fork of the WebRTC example from the **GStreamer monorepo**
(`gstreamer/gstreamer`, `subprojects/gst-examples/webrtc`), vendored at commit
`75fe368278d4`. Only the camera-demo pieces are included: the signalling server, the Python
sendrecv app, and the JS browser client.

> **Not a production architecture.** No TLS on signalling or page delivery, no authentication
> on the signalling server, everything on one device, single-LAN. These are deliberate
> simplifications for teaching. See "Security" below.

## What was changed vs. upstream (2 patches)

| Commit | Change | Why |
|---|---|---|
| `js: derive websocket scheme from page protocol` | Build the signalling URL as `ws://` for an http page, `wss://` for https, instead of hardcoding `wss://`. | Upstream hardcodes `wss://`, so a plaintext http deployment can't connect. |
| `js: support receive-only viewing over plain http` | Default constraints to `{video:false,audio:false}` and skip `getUserMedia` (returning a null stream, guarded in `createCall`). | An IP-camera view captures nothing in the browser. `navigator.mediaDevices` is `undefined` in an insecure (http) context, so the stock code crashes; receive-only sidesteps it and removes any need for TLS. |

Both are general improvements worth sending upstream. The Python sender is **unmodified** from
upstream — it works as-is once `python3-gst-1.0` is installed (see Prerequisites). See
`../REVIEW_FINDINGS.md` for the full analysis.

## Prerequisites (on the Pi)

The stack depends on a specific set of GStreamer + Python packages. On a clean Raspberry Pi OS
(Debian 13 / trixie, 64-bit) install:

```bash
sudo apt update
sudo apt install \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-nice \
  libnice10 \
  v4l-utils \
  python3-gi \
  python3-gst-1.0 \
  gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0 \
  gir1.2-gst-plugins-bad-1.0 \
  python3-websockets
```

Notes:
- **`python3-gst-1.0` is easy to miss and required** — the sender does `from gi.overrides
  import Gst` and will abort with "gstreamer-python binding overrides aren't available" without
  it. (It also provides the `Gst.Structure` subscripting the app uses.)
- **No virtualenv is needed.** The signalling server runs on the distro's `python3-websockets`
  (15.x) directly.
- These are the versions this was verified against: gstreamer 1.26.2, python3-gi 3.50.0,
  python3-websockets 15.0.1, on Python 3.13.

## Running it (all on the Pi)

```bash
# 1. Signalling server (plaintext ws://, port 8443)
python3 signalling/simple_server.py --disable-ssl

# 2. Static file server for the browser client (port 8080)
python3 -m http.server 8080 -d sendrecv/js

# 3. Camera sender: registers as "camera1" and waits for the browser to call it
python3 sendrecv/gst/webrtc_sendrecv.py \
    --server ws://127.0.0.1:8443 --our-id camera1 --camera --video-encoding vp8
```

Then, from a browser on another LAN machine, open **one URL** — the fragment auto-fills the
peer id, ticks "remote offerer" (so the Pi sends the offer with its camera), and connects:

```
http://<pi-hostname-or-ip>:8080/#peer-id=camera1,remote-offerer=1,connect=1
```

The camera feed should appear. (Manual equivalent: open `http://<pi>:8080/`, type `camera1`
in "Enter peer id", tick **Remote offerer**, click **Connect**.)

## Security

"Plaintext" here means the **signalling** (SDP + ICE over `ws://`) and the **page delivery**
(`http://`) are unencrypted and unauthenticated. It does **not** mean the video is
unencrypted: WebRTC media is always DTLS-SRTP encrypted end-to-end — that is mandatory in the
protocol and cannot be turned off. What's exposed is the signalling channel: on a trusted LAN
with these disclaimers that's fine; in production you would run `wss://` + auth on signalling.

## License

Inherited from upstream gst-examples — see `LICENSE`.
