"""60db (60db.ai) Text-to-Speech plugin for LiveKit Agents.

This module provides a thin LiveKit ``tts.TTS`` adapter around 60db's realtime
WebSocket TTS API (``ws/tts``). It is designed to drop straight into a
``VoicePipelineAgent`` alongside the existing Deepgram STT / OpenAI LLM, so the
agent keeps *listening* with Deepgram and starts *speaking* with 60db.

Protocol reference: https://docs.60db.ai/websocket-api/tts

Why a custom plugin?
--------------------
60db is not (yet) shipped as a first-party ``livekit-plugins-*`` package, so we
implement the small ``tts.TTS`` / ``tts.SynthesizeStream`` surface the pipeline
needs. The pipeline drives audio in 16-bit PCM internally and the SIP layer does
the final telephony encoding toward the carrier, so we request ``LINEAR16`` at
8 kHz on the wire (no transcoding into LiveKit). ``MULAW`` is supported too via a
local decoder so the encoding is a one-line switch.

Notes
-----
The exact ``livekit.agents.tts`` base-class signatures vary slightly across
0.12.x releases; this targets ``livekit-agents>=0.12.11``. Smoke-test in your
environment after install (see ``agent.py`` healthcheck).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Literal, Optional
from urllib.parse import urlencode

import aiohttp
from livekit import rtc
from livekit.agents import tts, utils

logger = logging.getLogger("sixtydb-tts")

# ---------------------------------------------------------------------------
# Defaults — override via constructor args or environment variables.
# ---------------------------------------------------------------------------
DEFAULT_WS_URL = os.getenv("SIXTYDB_WS_URL", "wss://api.60db.ai/ws/tts")
# LiveKit works in PCM16 internally; telephony encoding happens at the SIP layer.
# LINEAR16 @ 8 kHz => bytes are already PCM16 LE mono, fed directly to AudioFrame.
DEFAULT_ENCODING: "Encoding" = os.getenv("SIXTYDB_TTS_ENCODING", "LINEAR16")  # type: ignore[assignment]
DEFAULT_SAMPLE_RATE = int(os.getenv("SIXTYDB_TTS_SAMPLE_RATE", "8000"))
NUM_CHANNELS = 1

Encoding = Literal["LINEAR16", "PCM", "MULAW", "OGG_OPUS"]


@dataclass
class _TTSOptions:
    voice_id: str
    encoding: Encoding
    sample_rate: int
    speed: float
    stability: int
    similarity: int
    enhance: bool


class TTS(tts.TTS):
    """LiveKit TTS plugin backed by the 60db WebSocket API."""

    def __init__(
        self,
        *,
        voice_id: Optional[str] = None,
        api_key: Optional[str] = None,
        ws_url: str = DEFAULT_WS_URL,
        encoding: Encoding = DEFAULT_ENCODING,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        speed: float = 1.0,
        stability: int = 50,
        similarity: int = 75,
        enhance: bool = True,
        http_session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=sample_rate,
            num_channels=NUM_CHANNELS,
        )

        api_key = api_key or os.getenv("SIXTYDB_API_KEY")
        if not api_key:
            raise ValueError(
                "60db API key required: pass api_key= or set SIXTYDB_API_KEY"
            )
        voice_id = voice_id or os.getenv("SIXTYDB_VOICE_ID")
        if not voice_id:
            raise ValueError(
                "60db voice id required: pass voice_id= or set SIXTYDB_VOICE_ID"
            )

        self._api_key = api_key
        self._ws_url = ws_url
        self._session = http_session
        self._owns_session = http_session is None
        self._opts = _TTSOptions(
            voice_id=voice_id,
            encoding=encoding,
            sample_rate=sample_rate,
            speed=speed,
            stability=stability,
            similarity=similarity,
            enhance=enhance,
        )

    # -- session management -------------------------------------------------
    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    def _connect_url(self) -> str:
        return f"{self._ws_url}?{urlencode({'apiKey': self._api_key})}"

    async def aclose(self) -> None:  # noqa: D401 - LiveKit lifecycle hook
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    # -- LiveKit entry points ----------------------------------------------
    def synthesize(self, text: str) -> "ChunkedStream":
        return ChunkedStream(tts=self, input_text=text, opts=self._opts)

    def stream(self) -> "SynthesizeStream":
        return SynthesizeStream(tts=self, opts=self._opts)


class SynthesizeStream(tts.SynthesizeStream):
    """Streaming synthesis: incremental ``send_text`` from the LLM -> audio."""

    def __init__(self, *, tts: TTS, opts: _TTSOptions) -> None:
        super().__init__(tts=tts)
        self._tts: TTS = tts
        self._opts = opts

    async def _run(self) -> None:
        request_id = utils.shortuuid()
        context_id = utils.shortuuid()
        session = self._tts._ensure_session()

        async with session.ws_connect(self._tts._connect_url()) as ws:
            await _await_connection_established(ws)
            await ws.send_str(json.dumps(_create_context_msg(context_id, self._opts)))
            await _await_message(ws, "context_created")

            send = asyncio.create_task(self._send_loop(ws, context_id))
            recv = asyncio.create_task(self._recv_loop(ws, request_id))
            try:
                await asyncio.gather(send, recv)
            finally:
                for t in (send, recv):
                    if not t.done():
                        t.cancel()

    async def _send_loop(self, ws: aiohttp.ClientWebSocketResponse, ctx: str) -> None:
        async for data in self._input_ch:
            if isinstance(data, self._FlushSentinel):
                await ws.send_str(json.dumps({"flush_context": {"context_id": ctx}}))
            else:
                await ws.send_str(
                    json.dumps({"send_text": {"context_id": ctx, "text": data}})
                )
        # input ended -> flush remaining text and close the context
        await ws.send_str(json.dumps({"close_context": {"context_id": ctx}}))

    async def _recv_loop(
        self, ws: aiohttp.ClientWebSocketResponse, request_id: str
    ) -> None:
        bstream = utils.audio.AudioByteStream(
            sample_rate=self._opts.sample_rate, num_channels=NUM_CHANNELS
        )
        async for raw in _iter_json(ws):
            mtype, payload = _classify(raw)
            if mtype == "audio_chunk":
                pcm = _decode_audio(payload, self._opts.encoding)
                for frame in bstream.write(pcm):
                    self._emit(request_id, frame, is_final=False)
            elif mtype == "error":
                raise RuntimeError(f"60db TTS error: {payload}")
            elif mtype in ("context_closed",):
                break
            # flush_completed / other control frames: nothing to do
        for frame in bstream.flush():
            self._emit(request_id, frame, is_final=False)
        # signal end of this synthesis stream
        self._emit_final(request_id)

    def _emit(self, request_id: str, frame: rtc.AudioFrame, *, is_final: bool) -> None:
        self._event_ch.send_nowait(
            tts.SynthesizedAudio(request_id=request_id, frame=frame, is_final=is_final)
        )

    def _emit_final(self, request_id: str) -> None:
        # Emit a zero-length final marker if the lib expects an explicit is_final.
        empty = rtc.AudioFrame(
            data=b"",
            sample_rate=self._opts.sample_rate,
            num_channels=NUM_CHANNELS,
            samples_per_channel=0,
        )
        self._event_ch.send_nowait(
            tts.SynthesizedAudio(request_id=request_id, frame=empty, is_final=True)
        )


class ChunkedStream(tts.ChunkedStream):
    """One-shot synthesis of a full string (non-streaming fallback path)."""

    def __init__(self, *, tts: TTS, input_text: str, opts: _TTSOptions) -> None:
        super().__init__(tts=tts, input_text=input_text)
        self._tts: TTS = tts
        self._opts = opts

    async def _run(self) -> None:
        request_id = utils.shortuuid()
        context_id = utils.shortuuid()
        session = self._tts._ensure_session()
        bstream = utils.audio.AudioByteStream(
            sample_rate=self._opts.sample_rate, num_channels=NUM_CHANNELS
        )

        async with session.ws_connect(self._tts._connect_url()) as ws:
            await _await_connection_established(ws)
            await ws.send_str(json.dumps(_create_context_msg(context_id, self._opts)))
            await _await_message(ws, "context_created")
            await ws.send_str(
                json.dumps(
                    {"send_text": {"context_id": context_id, "text": self._input_text}}
                )
            )
            await ws.send_str(json.dumps({"close_context": {"context_id": context_id}}))

            async for raw in _iter_json(ws):
                mtype, payload = _classify(raw)
                if mtype == "audio_chunk":
                    pcm = _decode_audio(payload, self._opts.encoding)
                    for frame in bstream.write(pcm):
                        self._event_ch.send_nowait(
                            tts.SynthesizedAudio(request_id=request_id, frame=frame)
                        )
                elif mtype == "error":
                    raise RuntimeError(f"60db TTS error: {payload}")
                elif mtype == "context_closed":
                    break

        for frame in bstream.flush():
            self._event_ch.send_nowait(
                tts.SynthesizedAudio(request_id=request_id, frame=frame)
            )


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------
def _create_context_msg(context_id: str, opts: _TTSOptions) -> dict:
    return {
        "create_context": {
            "context_id": context_id,
            "voice_id": opts.voice_id,
            "audio_config": {
                "audio_encoding": opts.encoding,
                "sample_rate_hertz": opts.sample_rate,
            },
            "speed": opts.speed,
            "stability": opts.stability,
            "similarity": opts.similarity,
            "enhance": opts.enhance,
        }
    }


# Recognised server message keys (used when messages are key-tagged rather than
# carrying an explicit {"type": ...} field).
_SERVER_KEYS = (
    "audio_chunk",
    "context_created",
    "flush_completed",
    "context_closed",
    "connection_established",
    "connecting",
    "error",
)


def _classify(msg: dict) -> tuple[str, dict]:
    """Return (message_type, payload) handling both message encodings.

    60db docs show two shapes across endpoints:
      * key-tagged:  {"audio_chunk": {"audioContent": "..."}}
      * type-tagged: {"type": "chunk", "result": {"audioContent": "..."}}
    """
    mtype = msg.get("type")
    if mtype is not None:
        # normalise the streaming-REST naming to the websocket vocabulary
        if mtype == "chunk":
            return "audio_chunk", msg.get("result", msg)
        if mtype == "complete":
            return "context_closed", {}
        return mtype, msg.get("result", msg)
    for key in _SERVER_KEYS:
        if key in msg:
            payload = msg[key]
            return key, payload if isinstance(payload, dict) else {"value": payload}
    return "unknown", msg


def _decode_audio(payload: dict, encoding: Encoding) -> bytes:
    b64 = payload.get("audioContent") or payload.get("audio_base64") or ""
    raw = base64.b64decode(b64) if b64 else b""
    if encoding == "MULAW":
        return _ulaw_to_pcm16(raw)
    # LINEAR16 / PCM are already 16-bit signed LE mono.
    return raw


async def _await_connection_established(ws: aiohttp.ClientWebSocketResponse) -> None:
    async for raw in _iter_json(ws):
        mtype, payload = _classify(raw)
        if mtype == "connection_established":
            return
        if mtype == "error":
            raise RuntimeError(f"60db TTS connection error: {payload}")
        # ignore "connecting" and any other pre-auth chatter
    raise RuntimeError("60db TTS: websocket closed before connection_established")


async def _await_message(
    ws: aiohttp.ClientWebSocketResponse, expected: str
) -> dict:
    async for raw in _iter_json(ws):
        mtype, payload = _classify(raw)
        if mtype == expected:
            return payload
        if mtype == "error":
            raise RuntimeError(f"60db TTS error while awaiting {expected}: {payload}")
    raise RuntimeError(f"60db TTS: websocket closed before {expected}")


async def _iter_json(ws: aiohttp.ClientWebSocketResponse):
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            yield json.loads(msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            # some servers may send raw audio frames as binary; wrap them
            yield {"audio_chunk": {"audioContent": base64.b64encode(msg.data).decode()}}
        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
            break
        elif msg.type == aiohttp.WSMsgType.ERROR:
            raise RuntimeError(f"60db TTS websocket error: {ws.exception()}")


# ---------------------------------------------------------------------------
# G.711 μ-law -> PCM16 (used only when encoding == "MULAW").
# Prefer stdlib audioop (Python <= 3.12); fall back to a pure-Python table for
# 3.13+ where audioop was removed.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - availability depends on Python version
    import audioop  # type: ignore

    def _ulaw_to_pcm16(data: bytes) -> bytes:
        return audioop.ulaw2lin(data, 2)

except ImportError:  # pragma: no cover - Python 3.13+
    import struct

    def _build_ulaw_table() -> list[int]:
        table = []
        for i in range(256):
            u = ~i & 0xFF
            sign = u & 0x80
            exponent = (u >> 4) & 0x07
            mantissa = u & 0x0F
            sample = ((mantissa << 3) + 0x84) << exponent
            sample -= 0x84
            table.append(-sample if sign else sample)
        return table

    _ULAW_TABLE = _build_ulaw_table()

    def _ulaw_to_pcm16(data: bytes) -> bytes:
        return struct.pack("<%dh" % len(data), *(_ULAW_TABLE[b] for b in data))
