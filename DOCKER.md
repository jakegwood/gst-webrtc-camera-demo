# Running the demo with Docker

This image contains **only the WebRTC rig** (signalling server + Python camera
sender + static JS client). It does **not** contain `memfaultd` — that's a
host-level device daemon and stays native on the device (see Tier 2).

```bash
docker build -t webrtc-cam .
```

The image is ~740 MB (the GStreamer `-bad` plugins, which provide `webrtcbin`,
are the bulk). Build it for the architecture you'll run on (arm64 for a Pi).

---

## Tier 1 — "just see WebRTC work" (any machine with Docker)

No camera, no Memfault. The container streams a **test pattern**; browse to it
on the same machine.

```bash
docker run --rm -p 8080:8080 -p 8443:8443 -e SOURCE= webrtc-cam
```

Then open (same machine):

```
http://localhost:8080/#peer-id=camera1,remote-offerer=1,connect=1
```

> **Docker Desktop (macOS/Windows) caveat:** everything on one machine can still
> be fiddly, because the container's ICE *host candidate* is an address inside
> Docker's Linux VM that a browser on the host may not be able to reach. The
> reliable portable target is Linux with `--network=host`. If the media doesn't
> connect on a Mac, that's why — it is not a bug in the demo.

## Tier 2 — "watch Memfault localize a WebRTC fault" (a real Linux device)

**This is the point of the whitepaper, and it requires a device** (e.g. a
Raspberry Pi). You cannot get the observability story on a Mac: `memfaultd`
reports real device telemetry (reboots, coredumps, CPU/mem, device identity),
which is meaningless for a laptop or a throwaway Docker VM.

Architecture:
- **`memfaultd` runs natively on the device host** (not in this image).
- **This container runs with `--network=host`** and the real camera:

```bash
docker run --rm --network=host --device /dev/video0 webrtc-cam
```

`--network=host` does double duty here: it's required for WebRTC/ICE to work
(a bridged container advertises an unreachable private candidate IP), **and** it
puts the container on the host's loopback so the app can emit metrics to the
host's `memfaultd` StatsD socket on `127.0.0.1:8125`. The (forthcoming)
TTFF instrumentation sends there; on this device the metrics land and flow to
Memfault, while the exact same image run elsewhere simply drops them harmlessly.

Then, from a browser on another LAN machine:

```
http://<device-lan-ip>:8080/#peer-id=camera1,remote-offerer=1,connect=1
```

---

## Configuration (env vars)

| Var | Default | Meaning |
|---|---|---|
| `OUR_ID` | `camera1` | id the sender registers under (the browser calls this) |
| `VIDEO_ENCODING` | `vp8` | `vp8`, `h264` (software x264enc), or `av1` |
| `SOURCE` | `--camera` | `--camera` = USB cam via `autovideosrc`; empty = test pattern |
| `SIGNALLING_PORT` | `8443` | signalling ws:// port |
| `HTTP_PORT` | `8080` | static client http port |

Example (H.264 from the camera):
```bash
docker run --rm --network=host --device /dev/video0 \
  -e VIDEO_ENCODING=h264 webrtc-cam
```
