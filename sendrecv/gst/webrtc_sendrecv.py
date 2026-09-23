#!/usr/bin/env python3
#
# Copyright (C) 2018 Matthew Waters <matthew@centricular.com>
#               2022 Nirbheek Chauhan <nirbheek@centricular.com>
#
# Demo gstreamer app for negotiating and streaming a sendrecv webrtc stream
# with a browser JS app, implemented in Python.

from websockets.version import version as wsv
import random
import ssl
import websockets
import asyncio
import logging
import os
import sys
import json
import argparse
import time

import threading
from memfault_metrics import MemfaultSession

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst  # NOQA
gi.require_version('GstWebRTC', '1.0')
from gi.repository import GstWebRTC  # NOQA
gi.require_version('GstSdp', '1.0')
from gi.repository import GstSdp  # NOQA

# Ensure that gst-python is installed
try:
    from gi.overrides import Gst as _
except ImportError:
    print('gstreamer-python binding overrides aren\'t available, please install them')
    raise

KEEP_ALIVE = os.environ.get('KEEP_PIPELINE_ALIVE', '') == '1'

# These properties all mirror the ones in webrtc-sendrecv.c, see there for explanations
WEBRTCBIN = 'webrtcbin name=sendrecv latency=0 \
 stun-server=stun://stun.l.google.com:19302'
# Video only, in both modes. The viewer page is receive-only and the camera has
# no real mic (audio is audiotestsrc silence), so an audio m-line carries no
# content. It is also a liability on reconnect: a browser that declines the
# unwanted audio m-line answers "m=audio 0 / a=inactive", which carries no
# ice-ufrag, and webrtcbin discards the whole answer over it ("media 1 is
# missing ... 'ice-ufrag'"), leaving libnice with no remote credentials so every
# connectivity check is cancelled. One m-line also means one ICE transport
# instead of two, which is why keep-alive and default stay comparable.
PIPELINE_DESC_VP8 = WEBRTCBIN + '''
 {vsrc} ! videoconvert ! queue !
  vp8enc deadline=1 keyframe-max-dist=2000 ! rtpvp8pay picture-id-mode=15-bit !
  queue ! application/x-rtp,media=video,encoding-name=VP8,payload={video_pt} ! sendrecv.
'''
PIPELINE_DESC_H264 = WEBRTCBIN + '''
 {vsrc} ! videoconvert ! queue !
  x264enc tune=zerolatency speed-preset=ultrafast key-int-max=30 intra-refresh=true !
  rtph264pay aggregate-mode=zero-latency config-interval=-1 !
  queue ! application/x-rtp,media=video,encoding-name=H264,payload={video_pt} ! sendrecv.
'''
# Force I420 because dav1d bundled with Chrome doesn't support 10-bit choma/luma (I420_10LE)
PIPELINE_DESC_AV1 = WEBRTCBIN + '''
 {vsrc} ! videoconvert ! queue !
  video/x-raw,format=I420 ! svtav1enc preset=13 ! av1parse ! rtpav1pay !
  queue ! application/x-rtp,media=video,encoding-name=AV1,payload={video_pt} ! sendrecv.
'''
PIPELINE_DESC = {
    'AV1': PIPELINE_DESC_AV1,
    'H264': PIPELINE_DESC_H264,
    'VP8': PIPELINE_DESC_VP8,
}

# Keep-alive mode: persistent source/encoder pipelines ending in a tee.
# webrtcbin is attached/detached per session without restarting the encoder.
#
# Video only, deliberately. The viewer page is receive-only and the camera has
# no real mic (audio is audiotestsrc silence), so an audio m-line buys nothing.
# It actively breaks reconnects: browsers reject the unwanted audio m-line with
# "m=audio 0 / a=inactive", which carries no ice-ufrag, and webrtcbin then
# discards the whole answer ("media 1 is missing ... 'ice-ufrag'"), leaving
# libnice with no remote credentials so every connectivity check is cancelled.
KEEPALIVE_DESC_VP8 = '''
 {vsrc} ! videoconvert ! queue !
  vp8enc deadline=1 keyframe-max-dist=2000 ! rtpvp8pay picture-id-mode=15-bit !
  queue ! application/x-rtp,media=video,encoding-name=VP8,payload=97 !
  tee name=vtee allow-not-linked=true
'''
KEEPALIVE_DESC_H264 = '''
 {vsrc} ! videoconvert ! queue !
  x264enc tune=zerolatency speed-preset=ultrafast key-int-max=30 intra-refresh=true !
  rtph264pay aggregate-mode=zero-latency config-interval=-1 !
  queue ! application/x-rtp,media=video,encoding-name=H264,payload=97 !
  tee name=vtee allow-not-linked=true
'''
KEEPALIVE_DESC_AV1 = '''
 {vsrc} ! videoconvert ! queue !
  video/x-raw,format=I420 ! svtav1enc preset=13 ! av1parse ! rtpav1pay !
  queue ! application/x-rtp,media=video,encoding-name=AV1,payload=97 !
  tee name=vtee allow-not-linked=true
'''
KEEPALIVE_DESC = {
    'AV1': KEEPALIVE_DESC_AV1,
    'H264': KEEPALIVE_DESC_H264,
    'VP8': KEEPALIVE_DESC_VP8,
}

VSRC = {
    'test': 'videotestsrc is-live=true pattern=ball',
    'camera': 'v4l2src device=/dev/video0 ! image/jpeg,width=640,height=480,framerate=30/1 ! jpegdec',
}
ASRC = {
    'test': 'audiotestsrc is-live=true',
    'camera': 'audiotestsrc is-live=true wave=silence',
}


def print_status(msg, flush=False):
    print(f'--- {msg}', flush=flush)


def print_error(msg):
    print(f'!!! {msg}', file=sys.stderr)


def get_payload_types(sdpmsg, video_encoding, audio_encoding):
    '''
    Find the payload types for the specified video and audio encoding.

    Very simplistically finds the first payload type matching the encoding
    name. More complex applications will want to match caps on
    profile-level-id, packetization-mode, etc.
    '''
    video_pt = None
    audio_pt = None
    for i in range(0, sdpmsg.medias_len()):
        media = sdpmsg.get_media(i)
        for j in range(0, media.formats_len()):
            fmt = media.get_format(j)
            if fmt == 'webrtc-datachannel':
                continue
            pt = int(fmt)
            caps = media.get_caps_from_media(pt)
            s = caps.get_structure(0)
            encoding_name = s['encoding-name']
            if video_pt is None and encoding_name == video_encoding:
                video_pt = pt
            elif audio_pt is None and encoding_name == audio_encoding:
                audio_pt = pt
    return {video_encoding: video_pt, audio_encoding: audio_pt}


class WebRTCClient:
    def __init__(self, loop, our_id, peer_id, server, remote_is_offerer, video_encoding, source_type):
        self.conn = None
        self.pipe = None
        self.webrtc = None
        self.event_loop = loop
        self.server = server
        # An optional user-specified ID we can use to register
        self.our_id = our_id
        # The actual ID we used to register
        self.id_ = None
        # An optional peer ID we should connect to
        self.peer_id = peer_id
        # Whether we will send the offer or the remote peer will
        self.remote_is_offerer = remote_is_offerer
        # Video encoding: VP8, H264, etc
        self.video_encoding = video_encoding.upper()
        # Audio and video source to use
        self.asrc = ASRC[source_type]
        self.vsrc = VSRC[source_type]
        # Memfault session instrumentation
        self.session = MemfaultSession()
        self._stats_task = None
        # Keep-alive state
        self.keep_alive = KEEP_ALIVE
        self._source_running = False
        self._vtee_pad = None
        self._atee_pad = None
        self._session_vq = None
        self._session_aq = None

    async def send(self, msg):
        assert self.conn
        print(f'>>> {msg}')
        await self.conn.send(msg)

    async def connect(self):
        self.conn = await websockets.connect(self.server)
        if self.our_id is None:
            self.id_ = str(random.randrange(10, 10000))
        else:
            self.id_ = self.our_id
        await self.send(f'HELLO {self.id_}')

    async def setup_call(self):
        assert self.peer_id
        await self.send(f'SESSION {self.peer_id}')

    def send_soon(self, msg):
        asyncio.run_coroutine_threadsafe(self.send(msg), self.event_loop)

    def on_bus_poll_cb(self, bus):
        def remove_bus_poll():
            self.event_loop.remove_reader(bus.get_pollfd().fd)
            self.event_loop.stop()
        while bus.peek():
            msg = bus.pop()
            if msg.type == Gst.MessageType.ERROR:
                err = msg.parse_error()
                print("ERROR:", err.gerror, err.debug)
                remove_bus_poll()
                break
            elif msg.type == Gst.MessageType.EOS:
                remove_bus_poll()
                break
            elif msg.type == Gst.MessageType.LATENCY:
                self.pipe.recalculate_latency()

    def send_sdp(self, offer):
        text = offer.sdp.as_text()
        if offer.type == GstWebRTC.WebRTCSDPType.OFFER:
            print_status('Sending offer:\n%s' % text)
            msg = json.dumps({'sdp': {'type': 'offer', 'sdp': text}})
            self.session.mark('offer_sent')
        elif offer.type == GstWebRTC.WebRTCSDPType.ANSWER:
            print_status('Sending answer:\n%s' % text)
            msg = json.dumps({'sdp': {'type': 'answer', 'sdp': text}})
        else:
            raise AssertionError(offer.type)
        self.send_soon(msg)

    def on_offer_created(self, promise, _, __):
        assert promise.wait() == Gst.PromiseResult.REPLIED
        reply = promise.get_reply()
        offer = reply['offer']
        promise = Gst.Promise.new()
        print_status('Offer created, setting local description')
        self.session.mark('offer_created')
        self.webrtc.emit('set-local-description', offer, promise)
        promise.interrupt()  # we don't care about the result, discard it
        self.send_sdp(offer)

    def on_negotiation_needed(self, _, create_offer):
        if create_offer:
            print_status('Call was connected: creating offer')
            promise = Gst.Promise.new_with_change_func(self.on_offer_created, None, None)
            self.webrtc.emit('create-offer', None, promise)

    def send_ice_candidate_message(self, _, mlineindex, candidate):
        icemsg = json.dumps({'ice': {'candidate': candidate, 'sdpMLineIndex': mlineindex}})
        self.send_soon(icemsg)

    def on_incoming_decodebin_stream(self, _, pad):
        if not pad.has_current_caps():
            print_error(pad, 'has no caps, ignoring')
            return

        caps = pad.get_current_caps()
        assert (len(caps))
        s = caps[0]
        name = s.get_name()
        if name.startswith('video'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('videoconvert')
            sink = Gst.ElementFactory.make('autovideosink')
            self.pipe.add(q, conv, sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(sink)
        elif name.startswith('audio'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('audioconvert')
            resample = Gst.ElementFactory.make('audioresample')
            sink = Gst.ElementFactory.make('autoaudiosink')
            self.pipe.add(q, conv, resample, sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(resample)
            resample.link(sink)

    def on_ice_connection_state_notify(self, pspec, _):
        state = self.webrtc.get_property('ice-connection-state')
        nick = state.value_nick
        print_status(f'ICE connection state: {nick}', flush=True)
        if nick in ('connected', 'completed'):
            self.session.mark('ice_connected')

    def on_connection_state_notify(self, pspec, _):
        state = self.webrtc.get_property('connection-state')
        nick = state.value_nick
        print_status(f'Peer connection state: {nick}', flush=True)
        if nick == 'connected':
            self.session.mark('dtls_connected')
            self._start_stats_poll()
        elif nick == 'failed':
            print_error('WebRTC connection failed, closing')
            self.session.end()
            # Close websocket to exit the main loop and trigger cleanup
            if self.conn:
                asyncio.run_coroutine_threadsafe(self.conn.close(), self.event_loop)
        elif nick in ('disconnected', 'closed'):
            self.session.end()

    def _start_stats_poll(self):
        """Begin polling webrtcbin get-stats via a background thread."""
        if self._stats_task is not None:
            return
        self._stats_stop = threading.Event()
        self._stats_task = threading.Thread(target=self._stats_poll_thread, daemon=True)
        self._stats_task.start()

    def _stats_poll_thread(self):
        """Background thread that polls get-stats."""
        first_rtp_detected = False
        while not self._stats_stop.is_set() and self.webrtc is not None:
            stats = self._get_webrtc_stats()
            if stats is not None:
                packets_sent = stats.get('packets-sent', 0)
                if not first_rtp_detected and packets_sent > 0:
                    self.session.mark('first_rtp')
                    self.session.write_ttff_segments()
                    first_rtp_detected = True
                self.session.report_periodic_stats(stats)
            # Poll fast until first RTP detected, then slow to 2s
            self._stats_stop.wait(0.01 if not first_rtp_detected else 2)

    def _get_webrtc_stats(self):
        """Synchronously request stats from webrtcbin and parse outbound-rtp."""
        if self.webrtc is None:
            return None
        try:
            promise = Gst.Promise.new()
            self.webrtc.emit('get-stats', None, promise)
            if promise.wait() != Gst.PromiseResult.REPLIED:
                return None
            reply = promise.get_reply()
            if reply is None:
                return None
        except Exception:
            return None
        result = {}
        for i in range(reply.n_fields()):
            name = reply.nth_field_name(i)
            val = reply.get_value(name)
            if val is None or not hasattr(val, 'n_fields'):
                continue
            # Match on field presence rather than 'type' string, which is
            # unreliable across GStreamer versions.
            if val.has_field('bytes-sent') and val.has_field('packets-sent'):
                # outbound-rtp stats — get_uint64 returns (success, value)
                for field in ('bytes-sent', 'packets-sent', 'nack-count'):
                    if val.has_field(field):
                        ok, v = val.get_uint64(field)
                        if ok:
                            result[field] = v
                if val.has_field('frames-encoded'):
                    ok, v = val.get_uint64('frames-encoded')
                    if ok:
                        result['frames-encoded'] = v
            elif val.has_field('round-trip-time'):
                # remote-inbound-rtp stats
                ok, v = val.get_double('round-trip-time')
                if ok:
                    result['round-trip-time'] = v
        return result if result else None

    def on_ice_gathering_state_notify(self, pspec, _):
        state = self.webrtc.get_property('ice-gathering-state')
        print_status(f'ICE gathering state changed to {state}')

    def on_incoming_stream(self, _, pad):
        if pad.direction != Gst.PadDirection.SRC:
            return

        decodebin = Gst.ElementFactory.make('decodebin')
        decodebin.connect('pad-added', self.on_incoming_decodebin_stream)
        self.pipe.add(decodebin)
        decodebin.sync_state_with_parent()
        pad.link(decodebin.get_static_pad('sink'))

    # ------------------------------------------------------------------
    # Keep-alive pipeline management
    # ------------------------------------------------------------------

    def start_source_pipeline(self):
        """Create the persistent source/encoder pipeline (keep-alive mode)."""
        desc = KEEPALIVE_DESC[self.video_encoding].format(vsrc=self.vsrc, asrc=self.asrc)
        self.pipe = Gst.parse_launch(desc)
        bus = self.pipe.get_bus()
        self.event_loop.add_reader(bus.get_pollfd().fd, self.on_bus_poll_cb, bus)
        self.pipe.set_state(Gst.State.PLAYING)
        self._source_running = True
        print_status('Source pipeline started (keep-alive mode)', flush=True)

    def _attach_webrtcbin(self, create_offer=True):
        """Create a webrtcbin and link it to the running source pipeline."""
        print_status(f'Attaching webrtcbin (keep-alive), create_offer: {create_offer}', flush=True)
        self.webrtc = Gst.ElementFactory.make('webrtcbin', 'sendrecv')
        self.webrtc.set_property('latency', 0)
        self.webrtc.set_property('stun-server', 'stun://stun.l.google.com:19302')

        self._session_vq = Gst.ElementFactory.make('queue', 'session_vq')

        self.pipe.add(self.webrtc, self._session_vq)

        self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed, False)
        self.webrtc.connect('on-ice-candidate', self.send_ice_candidate_message)
        self.webrtc.connect('notify::ice-gathering-state', self.on_ice_gathering_state_notify)
        self.webrtc.connect('notify::ice-connection-state', self.on_ice_connection_state_notify)
        self.webrtc.connect('notify::connection-state', self.on_connection_state_notify)
        self.webrtc.connect('pad-added', self.on_incoming_stream)

        vtee = self.pipe.get_by_name('vtee')
        self._vtee_pad = vtee.request_pad_simple('src_%u')
        self._vtee_pad.link(self._session_vq.get_static_pad('sink'))
        self._session_vq.get_static_pad('src').link(
            self.webrtc.request_pad_simple('sink_%u'))

        self._session_vq.sync_state_with_parent()
        self.webrtc.sync_state_with_parent()

        # The encoder has been running since startup, so the next frame is very
        # likely a delta frame the new viewer cannot decode. Ask for a keyframe
        # now rather than waiting for keyframe-max-dist or the viewer's PLI.
        self._vtee_pad.send_event(Gst.Event.new_custom(
            Gst.EventType.CUSTOM_UPSTREAM,
            Gst.Structure.new_from_string(
                'GstForceKeyUnit, all-headers=(boolean)true')))

        if create_offer:
            # on-negotiation-needed doesn't fire reliably on a dynamically
            # added webrtcbin — explicitly create the offer after state sync.
            self.webrtc.get_state(Gst.SECOND)
            print_status('Creating offer (keep-alive attach)')
            promise = Gst.Promise.new_with_change_func(self.on_offer_created, None, None)
            self.webrtc.emit('create-offer', None, promise)

    def _detach_webrtcbin(self):
        """Remove webrtcbin and per-session elements, keep source pipeline."""
        if self._stats_task is not None:
            self._stats_stop.set()
            self._stats_task.join(timeout=2)
            self._stats_task = None

        # Detach from the tee BEFORE touching element states, and do it from an
        # IDLE probe so we are not inside a gst_pad_push() when the pad goes away.
        #
        # Order matters more than it looks. Setting the queue to NULL while the
        # tee is still linked to it makes the tee's push return FLUSHING; that is
        # its only src pad, so FLUSHING propagates upstream and pauses the source
        # streaming task. The task does not restart when a later viewer adds a new
        # pad, so the next session negotiates and connects normally but never
        # receives a single RTP packet.
        vtee = self.pipe.get_by_name('vtee')
        tee_pad = self._vtee_pad
        self._vtee_pad = None

        if vtee is not None and tee_pad is not None:
            unlinked = threading.Event()

            def _unlink_on_idle(pad, _info):
                peer = pad.get_peer()
                if peer is not None:
                    pad.unlink(peer)
                vtee.release_request_pad(pad)
                unlinked.set()
                return Gst.PadProbeReturn.REMOVE

            tee_pad.add_probe(Gst.PadProbeType.IDLE, _unlink_on_idle)
            if not unlinked.wait(timeout=2):
                print_error('Timed out waiting for tee pad to go idle; '
                            'releasing anyway')
                vtee.release_request_pad(tee_pad)

        # Now that nothing upstream can push into them, stop and remove.
        for el in (self.webrtc, self._session_vq):
            if el:
                el.set_state(Gst.State.NULL)
                self.pipe.remove(el)
        self.webrtc = None
        self._session_vq = None

        print_status('Detached webrtcbin (source pipeline still running)', flush=True)

    # ------------------------------------------------------------------
    # Pipeline start / stop (branches on keep-alive)
    # ------------------------------------------------------------------

    def start_pipeline(self, create_offer=True, audio_pt=96, video_pt=97):
        if self.keep_alive and self._source_running:
            self._attach_webrtcbin(create_offer)
            return
        print_status(f'Creating pipeline, create_offer: {create_offer}')
        desc = PIPELINE_DESC[self.video_encoding].format(video_pt=video_pt,
                                                         audio_pt=audio_pt,
                                                         vsrc=self.vsrc,
                                                         asrc=self.asrc)
        self.pipe = Gst.parse_launch(desc)
        bus = self.pipe.get_bus()
        self.event_loop.add_reader(bus.get_pollfd().fd, self.on_bus_poll_cb, bus)
        self.webrtc = self.pipe.get_by_name('sendrecv')
        self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed, create_offer)
        self.webrtc.connect('on-ice-candidate', self.send_ice_candidate_message)
        self.webrtc.connect('notify::ice-gathering-state', self.on_ice_gathering_state_notify)
        self.webrtc.connect('notify::ice-connection-state', self.on_ice_connection_state_notify)
        self.webrtc.connect('notify::connection-state', self.on_connection_state_notify)
        self.webrtc.connect('pad-added', self.on_incoming_stream)
        self.pipe.set_state(Gst.State.PLAYING)

    def on_answer_created(self, promise, _, __):
        assert promise.wait() == Gst.PromiseResult.REPLIED
        reply = promise.get_reply()
        answer = reply['answer']
        promise = Gst.Promise.new()
        self.webrtc.emit('set-local-description', answer, promise)
        promise.interrupt()  # we don't care about the result, discard it
        self.send_sdp(answer)

    def on_offer_set(self, promise, _, __):
        assert promise.wait() == Gst.PromiseResult.REPLIED
        promise = Gst.Promise.new_with_change_func(self.on_answer_created, None, None)
        self.webrtc.emit('create-answer', None, promise)

    def handle_json(self, message):
        try:
            msg = json.loads(message)
        except json.decoder.JSONDecoderError:
            print_error('Failed to parse JSON message, this might be a bug')
            raise
        if 'sdp' in msg:
            sdp = msg['sdp']['sdp']
            if msg['sdp']['type'] == 'answer':
                print_status('Received answer:\n%s' % sdp)
                self.session.mark('answer_received')
                res, sdpmsg = GstSdp.SDPMessage.new_from_text(sdp)
                answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
                promise = Gst.Promise.new()
                self.webrtc.emit('set-remote-description', answer, promise)
                promise.interrupt()  # we don't care about the result, discard it
            else:
                print_status('Received offer:\n%s' % sdp)
                self.session.start()
                res, sdpmsg = GstSdp.SDPMessage.new_from_text(sdp)

                if not self.webrtc:
                    print_status('Incoming call: received an offer, creating pipeline')
                    pts = get_payload_types(sdpmsg, video_encoding=self.video_encoding, audio_encoding='OPUS')
                    assert self.video_encoding in pts
                    assert 'OPUS' in pts
                    self.start_pipeline(create_offer=False, video_pt=pts[self.video_encoding], audio_pt=pts['OPUS'])

                assert self.webrtc

                offer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.OFFER, sdpmsg)
                promise = Gst.Promise.new_with_change_func(self.on_offer_set, None, None)
                self.webrtc.emit('set-remote-description', offer, promise)
        elif 'ice' in msg:
            assert self.webrtc
            ice = msg['ice']
            candidate = ice['candidate']
            sdpmlineindex = ice['sdpMLineIndex']
            self.webrtc.emit('add-ice-candidate', sdpmlineindex, candidate)
        else:
            print_error('Unknown JSON message')

    def close_pipeline(self):
        self.session.end()
        if self.keep_alive and self._source_running:
            self._detach_webrtcbin()
            return
        if self._stats_task is not None:
            self._stats_stop.set()
            self._stats_task = None
        if self.pipe:
            self.pipe.set_state(Gst.State.NULL)
            self.pipe = None
        self.webrtc = None

    async def loop(self):
        assert self.conn
        async for message in self.conn:
            print(f'<<< {message}')
            if message == 'HELLO':
                assert self.id_
                # If a peer ID is specified, we want to connect to it. If not,
                # we wait for an incoming call.
                if not self.peer_id:
                    print_status(f'Waiting for incoming call: ID is {self.id_}')
                else:
                    if self.remote_is_offerer:
                        print_status('Have peer ID: initiating call (will request remote peer to create offer)')
                    else:
                        print_status('Have peer ID: initiating call (will create offer)')
                    await self.setup_call()
            elif message == 'SESSION_OK':
                if self.remote_is_offerer:
                    # We are initiating the call, but we want the remote peer to create the offer
                    print_status('Call was connected: requesting remote peer for offer')
                    await self.send('OFFER_REQUEST')
                else:
                    self.start_pipeline()
            elif message == 'OFFER_REQUEST':
                print_status('Incoming call: we have been asked to create the offer')
                self.session.start()
                self.start_pipeline()
            elif message.startswith('ERROR'):
                print_error(message)
                self.session.end()
                self.close_pipeline()
                return 1
            else:
                self.handle_json(message)
        self.close_pipeline()
        return 0

    async def stop(self):
        if self.conn:
            await self.conn.close()
        self.conn = None


def check_plugin_features(source_type, video_encoding):
    """ensure we have all the plugins/features we need"""
    needed = ['opusenc', 'nicesink', 'webrtcbin', 'dtlssrtpenc', 'srtpenc',
              'rtpbin', 'rtpopuspay']

    if source_type == 'camera':
        needed += ['autoaudiosrc', 'autovideosrc']
    else:
        needed += ['audiotestsrc', 'videotestsrc']

    if video_encoding == 'vp8':
        needed += ['vp8enc', 'vp8dec']
    elif video_encoding == 'h264':
        needed += ['x264enc', 'h264parse']
    elif video_encoding == 'av1':
        needed += ['svtav1enc', 'av1parse']

    missing = []
    reg = Gst.Registry.get()
    for fname in needed:
        feature = reg.find_feature(fname, Gst.ElementFactory.__gtype__)
        if not feature:
            missing.append(fname)
    if missing:
        print("Missing gstreamer elements:", *missing)
        return False
    return True


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(name)s: %(message)s')
    Gst.init(None)
    parser = argparse.ArgumentParser()
    parser.add_argument('--video-encoding', default='vp8', nargs='?', choices=['vp8', 'h264', 'av1'],
                        help='Video encoding to negotiate')
    parser.add_argument('--camera', default='test', const='camera', action='store_const',
                        dest='source_type',
                        help='Use an attached camera and mic instead of test sources')
    parser.add_argument('--peer-id', help='String ID of the peer to connect to')
    parser.add_argument('--our-id', help='String ID that the peer can use to connect to us')
    parser.add_argument('--server', default='wss://webrtc.gstreamer.net:8443',
                        help='Signalling server to connect to, eg "wss://127.0.0.1:8443"')
    parser.add_argument('--remote-offerer', default=False, action='store_true',
                        dest='remote_is_offerer',
                        help='Request that the peer generate the offer and we\'ll answer')
    args = parser.parse_args()
    if not check_plugin_features(args.source_type, args.video_encoding):
        sys.exit(1)
    if not args.peer_id and not args.our_id:
        print('You must pass either --peer-id or --our-id')
        sys.exit(1)
    loop = asyncio.new_event_loop()
    c = WebRTCClient(loop, args.our_id, args.peer_id, args.server, args.remote_is_offerer, args.video_encoding, args.source_type)

    if KEEP_ALIVE:
        print_status('Keep-alive mode enabled', flush=True)
        c.start_source_pipeline()
        while True:
            try:
                loop.run_until_complete(c.connect())
                res = loop.run_until_complete(c.loop())
            except Exception as e:
                print_error(f'Session error: {e}')
            print_status('Session ended, reconnecting in 2s... (keep-alive)', flush=True)
            time.sleep(2)
    else:
        loop.run_until_complete(c.connect())
        res = loop.run_until_complete(c.loop())
        sys.exit(res)
