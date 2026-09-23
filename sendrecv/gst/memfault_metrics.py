"""
Memfault session instrumentation for the WebRTC camera sender.

Captures per-viewing TTFF segments (one-time gauges) and ongoing session
averages (periodic StatsD UDP gauges) scoped to a ``live-view`` session.

No-op safe: if ``memfaultctl`` is not found on PATH every public method
logs a single warning and silently returns so the sender works identically
without Memfault installed.
"""

import logging
import shutil
import socket
import subprocess
import time

log = logging.getLogger(__name__)

STATSD_HOST = '127.0.0.1'
STATSD_PORT = 8125
SESSION_NAME = 'live-view'

# Ordered pairs used to compute TTFF segment deltas.
# Each tuple is (metric_name, start_mark, end_mark).
TTFF_SEGMENTS = [
    ('negotiation_setup_ms', 't0', 'offer_created'),
    ('signaling_rtt_ms', 'offer_sent', 'answer_received'),
    ('ice_ms', 'answer_received', 'ice_connected'),
    ('dtls_ms', 'ice_connected', 'dtls_connected'),
    ('media_start_ms', 'dtls_connected', 'first_rtp'),
    ('ttff_total_ms', 't0', 'first_rtp'),
]


class MemfaultSession:
    """Lifecycle wrapper around a single Memfault ``live-view`` session."""

    def __init__(self):
        self._memfaultctl = shutil.which('memfaultctl')
        if self._memfaultctl is None:
            log.warning('memfaultctl not found in PATH — metrics will be disabled')
        self._marks: dict[str, float] = {}
        self._active = False
        self._ttff_written = False
        self._sock: socket.socket | None = None
        # Previous values for computing deltas in periodic stats
        self._prev_bytes_sent: int | None = None
        self._prev_stats_time: float | None = None

    # ------------------------------------------------------------------
    # memfaultctl helpers
    # ------------------------------------------------------------------

    def _run(self, *args: str) -> bool:
        """Run memfaultctl with *args*. Returns True on success."""
        if self._memfaultctl is None:
            return False
        cmd = [self._memfaultctl, *args]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=5)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            log.warning('memfaultctl %s failed: %s', ' '.join(args), exc)
            return False

    # ------------------------------------------------------------------
    # StatsD UDP helpers
    # ------------------------------------------------------------------

    def _ensure_socket(self):
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _send_gauge(self, name: str, value: float):
        """Send a single StatsD gauge packet."""
        if self._memfaultctl is None:
            return
        self._ensure_socket()
        payload = f'{name}:{value:.2f}|g'.encode()
        try:
            self._sock.sendto(payload, (STATSD_HOST, STATSD_PORT))
        except OSError as exc:
            log.debug('StatsD send failed for %s: %s', name, exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Begin a new live-view session. Record t0."""
        self._marks.clear()
        self._ttff_written = False
        self._prev_bytes_sent = None
        self._prev_stats_time = None
        self._marks['t0'] = time.monotonic()
        self._active = self._run('start-session', SESSION_NAME)
        if self._active:
            log.info('Memfault session "%s" started', SESSION_NAME)

    def mark(self, name: str):
        """Record a monotonic timestamp for *name* (idempotent)."""
        if name not in self._marks:
            self._marks[name] = time.monotonic()
            log.info('mark(%s) at +%.1f ms',
                     name,
                     (self._marks[name] - self._marks.get('t0', self._marks[name])) * 1000)

    def write_ttff_segments(self):
        """Compute TTFF deltas and write them via memfaultctl."""
        if self._ttff_written or not self._active:
            return
        metrics: list[str] = []
        for metric, start, end in TTFF_SEGMENTS:
            t_start = self._marks.get(start)
            t_end = self._marks.get(end)
            if t_start is not None and t_end is not None:
                delta_ms = (t_end - t_start) * 1000
                metrics.append(f'{metric}={delta_ms:.1f}')
                log.info('TTFF  %s = %.1f ms', metric, delta_ms)
        if metrics:
            # memfaultctl write-metrics accepts key=value pairs
            self._run('write-metrics', *metrics)
            self._ttff_written = True

    def report_periodic_stats(self, stats: dict):
        """Send session-duration gauges via StatsD.

        *stats* is a dict with keys matching GstWebRTC outbound-rtp /
        remote-inbound-rtp stat fields.
        """
        if not self._active:
            return

        now = time.monotonic()
        bytes_sent = stats.get('bytes-sent')

        if self._prev_stats_time is not None and bytes_sent is not None:
            dt = now - self._prev_stats_time
            if dt > 0 and self._prev_bytes_sent is not None:
                bitrate_kbps = ((bytes_sent - self._prev_bytes_sent) * 8) / (dt * 1000)
                self._send_gauge('video_bitrate_kbps', bitrate_kbps)

        self._prev_bytes_sent = bytes_sent
        self._prev_stats_time = now

        rtt = stats.get('round-trip-time')
        if rtt is not None:
            self._send_gauge('rtt_ms', rtt * 1000)

    def end(self):
        """Finalize and end the session."""
        if not self._active:
            return
        self.write_ttff_segments()
        self._run('end-session', SESSION_NAME)
        log.info('Memfault session "%s" ended', SESSION_NAME)
        self._active = False
        if self._sock:
            self._sock.close()
            self._sock = None
