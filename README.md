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
upstream — it works as-is once `python3-gst-1.0` is installed (see Prerequisites).

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

Then, from a browser on another LAN machine, open **one URL** — the fragment pre-fills the
peer id and ticks "remote offerer" (so the Pi sends the offer with its camera):

```
http://<pi-hostname-or-ip>:8080/#peer-id=camera1,remote-offerer=1
```

Click **Connect** and the camera feed should appear. (Manual equivalent: open
`http://<pi>:8080/`, type `camera1` in "Enter peer id", tick **Remote offerer**, click
**Connect**.)

Starting the stream is always an explicit click. An earlier revision accepted
`connect=1` in the fragment and clicked for you on a 2s timer, which could fire before
the signalling socket was open; that was removed.

## Keep-alive mode

By default the sender builds its GStreamer pipeline when a viewer asks for a stream and
tears the whole thing down when they leave — and the sender process itself exits between
viewers, so the next one pays full startup again. Passing `--keep-alive` to `start.sh`
sets `KEEP_PIPELINE_ALIVE=1` in the container and changes that:

```bash
./start.sh --keep-alive
```

Concretely, keep-alive does four things:

1. **The camera stays open.** `v4l2src` holds `/dev/video0` from container start, so the
   sensor never re-initialises between viewers.
2. **The encoder stays running.** The whole source pipeline
   (`v4l2src → jpegdec → videoconvert → vp8enc → rtpvp8pay → tee`) stays in `PLAYING`
   permanently, ending in a `tee` with `allow-not-linked=true` so it keeps running with no
   viewer attached. Only `webrtcbin` and one queue are added and removed per session.
3. **The sender process persists.** It loops internally instead of exiting, so one-time
   per-process costs — GStreamer registry and plugin load, and whatever `webrtcbin`
   initialises on its first use — are paid once at startup rather than per viewer.
4. **A keyframe is forced when a viewer attaches.** Because the encoder has been running,
   the next frame is almost certainly a delta frame the new viewer cannot decode, so
   attaching sends an upstream `GstForceKeyUnit`. Without this a viewer could wait up to
   `keyframe-max-dist` frames for a decodable picture.

One mechanism detail worth knowing if you read the code: `on-negotiation-needed` does not
fire reliably for a `webrtcbin` added to an already-running pipeline, so the keep-alive
path emits `create-offer` explicitly once the element reaches `PLAYING`.

**When not to use it.** Keep-alive trades power for latency. The sensor and encoder run
continuously whether or not anyone is watching, which is reasonable on a mains-powered
camera and usually wrong on a battery-powered one. It also holds the camera open
permanently, so on hardware with a activity LED tied to the sensor, that indicator stays
lit. The first viewer after a restart does not benefit — the savings begin with the second.

## Security

"Plaintext" here means the **signalling** (SDP + ICE over `ws://`) and the **page delivery**
(`http://`) are unencrypted and unauthenticated. It does **not** mean the video is
unencrypted: WebRTC media is always DTLS-SRTP encrypted end-to-end — that is mandatory in the
protocol and cannot be turned off. What's exposed is the signalling channel: on a trusted LAN
with these disclaimers that's fine; in production you would run `wss://` + auth on signalling.

## License

Inherited from upstream gst-examples — see `LICENSE`.

## Memfault Session Metrics (optional)

The camera sender includes optional Memfault instrumentation that records per-viewing
session metrics: a segmented time-to-first-frame (TTFF) breakdown and ongoing streaming
quality stats. If memfaultd is not installed, the sender works identically — no metrics
are recorded.

### Setup

1. Install memfaultd on the Pi host (see [Memfault Linux docs](https://docs.memfault.com/docs/linux/introduction)).

2. Add the `live-view` session to `/etc/memfaultd.conf`. This block is the only
   demo-specific addition — `project_key`, `software_version`, `software_type` and
   `enable_data_collection` are baseline memfaultd configuration covered by the docs
   above, and memfaultd will not report without them. The metric names here must match
   what the sender emits; a metric the sender sends that is not declared here is
   silently dropped.

```json
{
  "sessions": [
    {
      "name": "live-view",
      "captured_metrics": [
        "negotiation_setup_ms",
        "signaling_rtt_ms",
        "ice_ms",
        "dtls_ms",
        "media_start_ms",
        "ttff_total_ms",
        "video_bitrate_kbps",
        "rtt_ms"
      ]
    }
  ]
}
```

3. Restart memfaultd:

```bash
sudo systemctl restart memfaultd
```

4. Run `./start.sh` — it will detect memfaultd and mount the CLI into the container
   automatically.

5. **Force an upload before checking the dashboard.** memfaultd batches by default —
   `upload_interval_seconds` is commonly 3600 or 7200 — so a session you just recorded
   will sit on the device for up to that long. Nothing is wrong; it simply has not
   shipped yet. After a session:

```bash
memfaultctl sync
```

   Without this step the usual first experience is an empty dashboard and the conclusion
   that the instrumentation is broken.

   While developing, `"enable_dev_mode": true` in `/etc/memfaultd.conf` is worth setting:
   it relaxes rate limiting so rapid back-to-back sessions are not dropped. Turn it off
   for anything resembling a fleet.

### Verifying it works

Session metrics are written by `memfaultctl` and the periodic gauges go out over StatsD
to `127.0.0.1:8125`, so neither appears in `docker logs`. To confirm end to end:

```bash
# 1. The sender logs each segment as it computes them
sudo docker logs $(sudo docker ps -q) 2>&1 | grep "TTFF"

# 2. Push to the cloud, then look for a live-view session on the device timeline
memfaultctl sync
```

A successful session prints all six TTFF segments. If `memfaultctl` is missing from the
container the sender still streams normally and logs nothing — that is the intended
no-op behaviour, not a failure.

### Metrics recorded

**TTFF segments** (one-time per session, written at session end):

| Metric | Measures |
|---|---|
| `negotiation_setup_ms` | Pipeline startup + offer creation (t0 to offer created) |
| `signaling_rtt_ms` | Signalling path round-trip (offer sent to answer received) |
| `ice_ms` | NAT traversal (answer received to ICE connected) |
| `dtls_ms` | DTLS-SRTP handshake (ICE connected to peer connected) |
| `media_start_ms` | First outbound RTP observed (peer connected to first RTP packet) |
| `ttff_total_ms` | Device-side total (t0 to first RTP packet sent) |

`t0` is set when the sender receives `OFFER_REQUEST` from the signalling server —
the moment the camera is asked to begin a viewing session. Everything before that
(page load, websocket connect, `SESSION` handshake) is outside these metrics, as is
everything after the sender emits its first RTP packet (network transit, the
viewer's jitter buffer, decode and render).

**Streaming quality** (sampled every 2s via StatsD, aggregated by Memfault over the session):

| Metric | Source |
|---|---|
| `video_bitrate_kbps` | Outbound video throughput |
| `rtt_ms` | Network round-trip time from RTCP receiver reports |

The sender deliberately emits only these two. Earlier revisions also sent
`video_framerate`, `video_packets_sent` and `video_nack_count`; they were dropped
because cumulative packet counts are MTU-dependent and not actionable on their own,
and because `frames-encoded` / `nack-count` are optional fields this `webrtcbin`
does not populate — they were silently never recorded.

## Network Requirements

This demo is designed for **same-LAN** use: the browser and the Pi must be on the same local
network. WebRTC media (video/audio) is always peer-to-peer — the signalling server only
brokers the initial connection setup.

On a LAN, the Pi's host ICE candidates (e.g. `192.168.x.x`) are directly reachable from the
browser, so connections succeed without any relay infrastructure.

**Remote access** (browser and Pi on different networks) requires a
[TURN](https://en.wikipedia.org/wiki/Traversal_Using_Relays_around_NAT) relay server to
forward media through NAT. You can run [coturn](https://github.com/coturn/coturn) on a
publicly reachable host and configure it in `webrtc_sendrecv.py`:

```python
WEBRTCBIN = 'webrtcbin name=sendrecv latency=0 \
 stun-server=stun://stun.l.google.com:19302 \
 turn-server=turn://user:pass@your-turn-server.example.com:3478'
```

Without a TURN server, remote connections will fail unless both NATs happen to allow
direct srflx (STUN) connectivity, which is unreliable.
