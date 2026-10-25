"""Experimental browser media + server-owned Realtime sideband control.

Only the server calls OpenAI with a key. A short-lived capability binds SDP
creation to the current phone socket. No client-selected model/tools/instructions.
"""
import asyncio
import json
import re
import secrets
import time
from urllib.parse import urlsplit

import httpx
import websockets

from realtime_provider import RealtimeConfig, local_values


def openai_config():
    import os
    local = local_values()
    value = lambda name, default: os.environ.get(name) or local.get(name) or default
    return RealtimeConfig("openai", value("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1"),
        value("OPENAI_REALTIME_TRANSCRIPTION_MODEL", "gpt-transcribe"), value("OPENAI_REALTIME_VOICE", "marin"))


def call_id_from_location(location):
    parsed = urlsplit(location or "")
    if parsed.netloc and (parsed.scheme != "https" or parsed.netloc != "api.openai.com"):
        raise ValueError("Unexpected Realtime call location")
    match = re.fullmatch(r"/v1/realtime/calls/(rtc_[A-Za-z0-9_-]{1,160})", parsed.path)
    if not match or parsed.query or parsed.fragment:
        raise ValueError("Missing Realtime call identity")
    return match[1]


async def create_call(config, session, sdp):
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as http:
        response = await http.post("https://api.openai.com/v1/realtime/calls",
            headers={"Authorization": "Bearer " + config.openai_key()}, files={
                "sdp": (None, sdp, "application/sdp"),
                "session": (None, json.dumps({**session, "model": config.model}), "application/json")})
        if response.status_code != 201:
            # Do not expose provider response bodies, SDP, headers or credentials.
            raise RuntimeError("Realtime call creation failed (HTTP " + str(response.status_code) + ")")
        return call_id_from_location(response.headers.get("location")), response.text


async def hangup_call(config, call_id):
    if not call_id:
        return
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as http:
            await http.post("https://api.openai.com/v1/realtime/calls/" + call_id + "/hangup",
                            headers={"Authorization": "Bearer " + config.openai_key()})
    except (httpx.HTTPError, RuntimeError):
        pass  # Closing the browser peer also terminates media. Cleanup is best effort.


class WebRTCSessionMixin:
    transport = "webrtc"
    MIN_BARGE_SECONDS = .18

    def __init__(self, session_id, realtime=None):
        super().__init__(session_id, realtime=realtime or openai_config())
        self.call_id = None
        self.call_lock = asyncio.Lock()
        self.sideband_task = None
        self.sideband_ready = None
        self.barge_task = None
        self.remote_response_id = None
        self.remote_playing = False

    @property
    def echo_guard(self):
        return self.mic is None or not getattr(self.mic, "duplex", False)

    def state_payload(self):
        state = super().state_payload()
        return {**state, "transport": "webrtc", "speaking": self.remote_playing or self.playback_pending()
                or time.monotonic() < self.play_until, "experimental_duplex": not self.echo_guard}

    def environment_contaminated(self):
        # Environmental capture is not claimed to be echo-free. Keep BirdNET's
        # existing playback mask even when voice input is full duplex.
        return self.remote_playing or self.playback_pending() or time.monotonic() < self.echo_deadline()

    def assistant_audio_pending(self):
        return self.remote_playing or super().assistant_audio_pending()

    def transcript_is_echo(self, text):
        # Full-duplex audio must be validated acoustically. Matching the model's
        # words would also erase a user deliberately repeating those words.
        return False if not self.echo_guard else super().transcript_is_echo(text)

    async def mic_attached(self, client):
        if client is not self.mic:
            return
        await self.end_call()
        if client is not self.mic:
            return
        client.rtc_token = secrets.token_urlsafe(32)
        client.rtc_token_created = time.monotonic()
        client.duplex = False
        await client.send_json({"type": "webrtc_ready", "token": client.rtc_token})

    async def mic_detached(self, client):
        client.rtc_token = ""
        await self.end_call()

    async def offer(self, token, sdp):
        owner = self.mic
        if (owner is None or not getattr(owner, "rtc_token", "") or
                not secrets.compare_digest(str(token), owner.rtc_token) or
                time.monotonic() - owner.rtc_token_created > 90):
            raise PermissionError("Reconnect the phone before starting WebRTC")
        async with self.call_lock:
            if self.mic is not owner or not secrets.compare_digest(str(token), getattr(owner, "rtc_token", "")):
                raise PermissionError("Phone capability expired")
            if self.call_id:
                raise ValueError("A voice call is already active")
            owner.rtc_token = ""  # Single use, including a failed attempt.
            call_id, answer = await create_call(self.realtime, self.session(), sdp)
            if self.closed or self.mic is not owner:
                await hangup_call(self.realtime, call_id)
                raise PermissionError("Phone connection changed")
            self.call_id = call_id
            self.sideband_ready = asyncio.get_running_loop().create_future()
            self.sideband_task = asyncio.create_task(self.sideband_loop(call_id))
            self.tasks.append(self.sideband_task)
            try:
                await asyncio.wait_for(asyncio.shield(self.sideband_ready), 22)
                if self.mic is not owner:
                    raise PermissionError("Phone connection changed")
                return answer
            except BaseException:
                await self.end_call()
                raise

    async def sideband_loop(self, call_id):
        try:
            async with websockets.connect("wss://api.openai.com/v1/realtime?call_id=" + call_id,
                    additional_headers={"Authorization": "Bearer " + self.realtime.openai_key()},
                    max_size=None, open_timeout=20, ping_interval=20, ping_timeout=20) as ws:
                self.rt = ws
                self.conversation_ids.clear()
                self.deleted_item_ids.clear()
                self.item_owners.clear()
                self.response_owners.clear()
                config = self.session()
                await self.rt_send({"type": "session.update", "session": config})
                async with asyncio.timeout(20):
                    while True:
                        event = json.loads(await ws.recv())
                        if event.get("type") == "error":
                            raise RuntimeError("Realtime sideband setup rejected")
                        if self.realtime.accepts_initial(event, config):
                            break
                self.connected = True
                self.log("realtime_connected", provider="openai", model=self.realtime.model, transport="webrtc")
                summary = "; ".join(f"{r['count']} {r['en']}" for r in self.items) or "empty"
                await self.inject("Current confirmed checklist (context only; do not speak): " + summary[:3000])
                await self.state()
                await self.handle_rt(event)
                await self.flush_pending()
                if not self.sideband_ready.done():
                    self.sideband_ready.set_result(True)
                async for raw in ws:
                    await self.handle_rt(json.loads(raw))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log("webrtc_sideband_failed", error_type=type(exc).__name__)
            if self.sideband_ready and not self.sideband_ready.done():
                self.sideband_ready.set_exception(ConnectionError("WebRTC control connection failed"))
            await self.error("Voice connection closed. End the session and start again.")
        finally:
            self.connected = False
            self.rt = None
            self.remote_playing = False
            self.user_speaking = self.awaiting_transcript = False
            for waiter in (self.update_waiter, self.response_waiter):
                if waiter is not None and not waiter.done():
                    waiter.set_exception(ConnectionError("WebRTC control connection closed"))
            await self.flush_audio()
            await self.state()
            if self.call_id == call_id:
                self.call_id = None
                await hangup_call(self.realtime, call_id)

    async def handle_rt(self, event):
        ty, rid = event.get("type"), event.get("response_id")
        if ty == "response.created" and self.cancel_barrier is not None and self.cancel_barrier["unknown"]:
            # This sideband owns response.create and automatic responses are off.
            # A cancel can beat response.created; bind its ordered acknowledgement
            # before the base class discards the cancelled generation.
            rid = event["response"]["id"]
            self.cancel_barrier.update(response_id=rid, unknown=False)
            self.cancelled_ids.append(rid)
        if ty in ("response.output_audio.delta", "response.audio.delta"):
            return  # Media arrives over WebRTC; never play a second PCM copy.
        if ty == "output_audio_buffer.started":
            if rid in self.cancelled_ids:
                await self.rt_send({"type": "output_audio_buffer.clear"})
                return
            self.remote_response_id, self.remote_playing = rid, True
            await self.begin_audio(rid)
            self.log("rtc_output_started", response_id=rid)
            await self.state()
            return
        if ty in ("output_audio_buffer.stopped", "output_audio_buffer.cleared"):
            if rid == self.remote_response_id:
                self.remote_playing = False
                if self.audio_playback_id == rid:
                    await self.end_audio()
                self.log("rtc_output_stopped", response_id=rid, cleared=ty.endswith("cleared"))
                await self.state()
                await self.flush_pending()
            return
        await super().handle_rt(event)

    async def wait_cancel_boundary(self, token):
        barrier = self.cancel_barrier
        if barrier is None:
            return
        await asyncio.wait([barrier["future"], token["cancel"]], timeout=3,
                           return_when=asyncio.FIRST_COMPLETED)
        if token["cancelled"]:
            return
        if not barrier["future"].done():
            # A WebRTC call cannot be recovered by replacing just its control
            # socket. Fail explicitly instead of waiting for a nonexistent peer.
            await self.end_call()
            raise ConnectionError("Voice cancellation was not acknowledged; restart the session")
        if self.cancel_barrier is barrier:
            self.cancel_barrier = None

    async def voice_started(self, record):
        if self.echo_guard:
            return await super().voice_started(record)
        if self.barge_task is not None:
            self.barge_task.cancel()
        async def confirm():
            await asyncio.sleep(self.MIN_BARGE_SECONDS)
            if (not self.closed and self.current_input_audio_turn is record and self.user_speaking
                    and self.assistant_audio_pending() and not self.echo_guard):
                self.log("barge_in", confirmation="sustained_vad", hold_ms=180)
                await self.cancel_speech()
        self.log("barge_in_candidate", confirmation="sustained_vad")
        self.barge_task = asyncio.create_task(confirm())

    async def cancel_speech(self):
        rid = self.remote_response_id
        if self.remote_playing and self.rt is not None:
            if rid:
                self.cancelled_ids.append(rid)
            try:
                # In WebRTC this clears the unplayed audio and synchronizes the
                # conversation truncation using the provider's playout buffer.
                await self.rt_send({"type": "output_audio_buffer.clear"})
            except (ConnectionError, websockets.ConnectionClosed):
                pass
        self.remote_playing = False
        await super().cancel_speech()

    async def handle_phone(self, event, client=None):
        if event.get("type") == "duplex" or (event.get("type") == "hello" and "duplex" in event):
            if client is not self.mic or not isinstance(event.get("duplex"), bool):
                raise ValueError("Only the current phone controls duplex mode")
            client.duplex = event["duplex"]
            self.log("duplex_mode", enabled=client.duplex)
            if event["type"] == "duplex":
                await self.state()
                return
        await super().handle_phone(event, client)

    def start(self):
        self.turn_task = asyncio.create_task(self.turn_worker())
        self.tasks = [self.turn_task, asyncio.create_task(self.focus_watch()), asyncio.create_task(self.suggestion_loop())]

    async def end_call(self):
        call_id, self.call_id = self.call_id, None
        task, self.sideband_task = self.sideband_task, None
        if self.sideband_ready is not None and not self.sideband_ready.done():
            self.sideband_ready.cancel()
        if self.barge_task:
            self.barge_task.cancel()
            await asyncio.gather(self.barge_task, return_exceptions=True)
            self.barge_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.remote_playing = False
        await hangup_call(self.realtime, call_id)

    async def close(self):
        await self.end_call()
        await super().close()


def make_session(base, session_id):
    return type("WebRTCSession", (WebRTCSessionMixin, base), {})(session_id)
