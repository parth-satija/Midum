"""
Discord voice-channel calling for Midum, bridged to GEMINI LIVE.
=============================================================================
Midum's bot account joins a DISCORD VOICE CHANNEL and holds a REAL real-time
spoken conversation -- not a record-utterance -> transcribe -> single-prompt
-> speak loop, but a live, continuous, interruptible audio conversation,
using the exact same Gemini Live API (google-genai SDK,
`client.aio.live.connect`) that powers Midum's desktop voice mode
(providers/gemini_live_backend.py). Gemini does its own speech-to-text,
turn detection (VAD), and text-to-speech natively over the live connection;
this module's job is just to plumb PCM audio between Discord and Gemini:

    Discord mic audio (48kHz stereo, per speaker)
        -> resampled to 16kHz mono
        -> streamed continuously into the Gemini Live session

    Gemini's spoken reply (24kHz mono, streamed as it's generated)
        -> resampled to 48kHz stereo
        -> fed into a custom discord.AudioSource playing into the channel

AUTO-JOIN: Midum also joins DISCORD_DEFAULT_VOICE_CHANNEL_ID automatically
the moment any real (non-bot) member enters that channel -- no tool call
needed. It automatically leaves again once the channel is empty of humans.
call_discord() still exists for manually starting/joining on demand (e.g. a
different channel, or a custom opening line/objective), but for the default
channel you generally don't need to call it at all.

Like the desktop voice mode, this bridge also gives Gemini full access to
Midum's tool catalogue (files, terminal, browser, MCP, etc.) via the same
dispatcher (gui/dispatch.py:_dispatch_midum_tool) -- so a spoken request in
a Discord voice channel can actually trigger real actions, not just talk.
There's no live screen-share here (no video feed to see), unlike desktop
voice mode. Text chat (DMs / server messages, see below) also gets Midum's
full tool-using pipeline, via a separate code path.

SETUP (one-time)
-----------------
  1. pip install discord.py[voice] discord-ext-voice-recv PyNaCl google-genai
  2. Install ffmpeg and make sure it's on PATH -- needed by discord.py's
     voice pipeline even though this module doesn't shell out to it
     directly for TTS anymore.
  3. Create a bot at https://discord.com/developers/applications:
       - New Application -> Bot -> Reset Token -> copy it.
       - Under "Privileged Gateway Intents", enable "MESSAGE CONTENT INTENT"
         (needed for DM/server chat text), "SERVER MEMBERS INTENT" if you
         want the auto-join/auto-leave voice-state tracking to be reliable,
         and "PRESENCE INTENT" (needed so Midum can tell how many humans
         are currently online in the server -- see TEXT CHAT below).
       - OAuth2 -> URL Generator: scope "bot", permissions "Connect",
         "Speak", "Use Voice Activity", "Send Messages", "Read Message
         History" -> open the generated URL and add the bot to your server.
  4. Add to the SAME secrets file used everywhere else in Midum:
        { "DISCORD_BOT_TOKEN": "...",
          "DISCORD_DEFAULT_VOICE_CHANNEL_ID": "123456789012345678" }
     GEMINI_API_KEY must ALSO already be set in that same secrets file
     (same key used by MODEL_PROVIDER="gemini_api" / desktop voice mode --
     if you've set that up already, this needs zero extra configuration).

TEXT CHAT
---------
Separately from voice, start_discord_bot() (or just letting the bot start
via auto-join/call_discord()) turns on full text chat: DM the bot directly,
or message any text channel in the same server as
DISCORD_DEFAULT_VOICE_CHANNEL_ID, and Midum replies using its full
reasoning/tool-use pipeline (orchestration.process_chat_turn) -- the same
brain as the terminal. Each DM or channel keeps its own conversation history.

DMs always get a reply, no mention needed. In a SERVER channel, though,
Midum only replies when explicitly pinged with @Midum -- EXCEPT when
exactly one human is online in the server at all, in which case any
message clearly has nowhere else to go and Midum replies without needing
the mention. This needs the PRESENCE privileged intent (see SETUP above)
to see accurate online/idle/dnd statuses; without it every member looks
offline and Midum just always requires the @Midum mention in servers.
"""

import asyncio
import audioop
import json
import os
import random
import re
import threading
import time
import uuid

import config
from config import SECRETS_FILE

# ── Optional dependencies — loaded lazily so the rest of Midum works fine
# on machines that never install the voice-calling extras. ──────────────────
try:
    import discord
    from discord.ext import commands
    _DISCORD_AVAILABLE = True
except ImportError:
    discord = None
    commands = None
    _DISCORD_AVAILABLE = False

try:
    from discord.ext import voice_recv as _voice_recv
    _VOICE_RECV_AVAILABLE = True
except ImportError:
    _voice_recv = None
    _VOICE_RECV_AVAILABLE = False

# PyNaCl is what discord.py actually uses under the hood to encrypt/decrypt
# voice-channel audio -- channel.connect() raises a plain RuntimeError at
# runtime if it's missing ("PyNaCl library needed in order to use voice"),
# which previously went completely unhandled/unlogged (see the done-callback
# fix on _start_call_from_bot_loop below). Checking it explicitly up front
# means _check_voice_extras_available() can report it clearly instead of
# the join just silently never happening.
try:
    import nacl  # noqa: F401
    _PYNACL_AVAILABLE = True
except ImportError:
    _PYNACL_AVAILABLE = False

try:
    from google import genai as _genai
    from google.genai import types as _genai_types
    _GENAI_SDK_AVAILABLE = True
except ImportError:
    _genai = None
    _genai_types = None
    _GENAI_SDK_AVAILABLE = False


# =============================================================================
# CREDENTIALS
# =============================================================================
_DISCORD_TOKEN = None
_DEFAULT_CHANNEL_ID = None
_CREDS_AVAILABLE = False


def _load_discord_creds():
    global _DISCORD_TOKEN, _DEFAULT_CHANNEL_ID, _CREDS_AVAILABLE

    # Only discord.py itself is required for text chat (DMs + server
    # messages). Voice-specific extras (voice_recv/google-genai + a Gemini
    # API key) are checked separately, only when voice is actually used.
    if not _DISCORD_AVAILABLE:
        return False, "discord.py not installed. Run: pip install discord.py[voice]"

    try:
        secrets_path = os.path.abspath(SECRETS_FILE)
        if not os.path.exists(secrets_path):
            return False, f"Secrets file not found: {secrets_path}"
        with open(secrets_path, "r", encoding="utf-8") as f:
            secrets = json.load(f)
        token = secrets.get("DISCORD_BOT_TOKEN", "").strip()
        if not token:
            return False, f"DISCORD_BOT_TOKEN is empty in {secrets_path}"
        _DISCORD_TOKEN = token
        _DEFAULT_CHANNEL_ID = secrets.get("DISCORD_DEFAULT_VOICE_CHANNEL_ID", "").strip()
        _CREDS_AVAILABLE = True
        return True, "OK"
    except Exception as e:
        return False, str(e)


def _get_gemini_live_creds():
    """Reuses providers/gemini_live_backend's already-loaded GEMINI_API_KEY
    (read from the SAME secrets file, same key as MODEL_PROVIDER=gemini_api
    / the desktop Voice tab) instead of duplicating key-loading logic here.
    Read as module attributes (not imported by value) so a key added after
    this module first loaded is still picked up correctly."""
    import providers.gemini_live_backend as _glb
    return _glb._GEMINI_LIVE_KEY, _glb._GEMINI_LIVE_AVAILABLE, _glb._gemini_live_load_msg


def _check_voice_extras_available():
    missing = []
    if not _VOICE_RECV_AVAILABLE:
        missing.append("discord-ext-voice-recv (pip install discord-ext-voice-recv)")
    if not _PYNACL_AVAILABLE:
        missing.append("PyNaCl (pip install PyNaCl)")
    if not _GENAI_SDK_AVAILABLE:
        missing.append("google-genai (pip install google-genai)")
    if missing:
        return False, "Missing packages: " + "; ".join(missing)
    _, gemini_ok, gemini_msg = _get_gemini_live_creds()
    if not gemini_ok:
        return False, f"Gemini Live not configured: {gemini_msg}"
    return True, "OK"


_discord_load_ok, _discord_load_msg = _load_discord_creds()

# call_id -> session dict. Kept for get_discord_call_status/transcript to
# still work the same way as before, even though the actual conversation is
# now driven live by Gemini rather than turn-by-turn by this module.
_CALL_SESSIONS: dict = {}
_CALL_SESSIONS_LOCK = threading.RLock()

# The single long-lived bot + its dedicated asyncio loop/thread.
_bot = None
_bot_loop: "asyncio.AbstractEventLoop | None" = None
_bot_thread: "threading.Thread | None" = None
_bot_ready_event = threading.Event()
_bot_lock = threading.RLock()

# channel_id (str) -> call_id currently occupying that channel, so
# auto-join/auto-leave and manual call_discord() calls never double-join.
_active_channel_calls: dict = {}
_voice_clients: dict = {}   # call_id -> discord.VoiceClient


def _new_session(channel_id: str, opening_message: str, objective: str) -> str:
    call_id = uuid.uuid4().hex[:16]
    with _CALL_SESSIONS_LOCK:
        _CALL_SESSIONS[call_id] = {
            "call_id": call_id,
            "channel_id": channel_id,
            "objective": objective or "Have a natural, friendly voice conversation.",
            "opening_message": opening_message,
            "transcript": [],
            "status": "connecting",
            "ended": False,
            "created_at": time.time(),
        }
    return call_id


def _get_session(call_id: str):
    with _CALL_SESSIONS_LOCK:
        return _CALL_SESSIONS.get(call_id)


def _append_turn(call_id: str, role: str, text: str):
    if not text:
        return
    with _CALL_SESSIONS_LOCK:
        sess = _CALL_SESSIONS.get(call_id)
        if sess is not None:
            sess["transcript"].append({"role": role, "text": text})


# =============================================================================
# AUDIO RESAMPLING — Discord uses 48kHz stereo 16-bit PCM; Gemini Live wants
# 16kHz mono in, and sends back 24kHz mono. audioop (stdlib) handles both
# the rate conversion and the channel-count conversion in one pass.
# =============================================================================
def _discord_pcm_to_gemini_pcm(pcm_48k_stereo: bytes) -> bytes:
    mono = audioop.tomono(pcm_48k_stereo, 2, 0.5, 0.5)
    resampled, _ = audioop.ratecv(mono, 2, 1, 48000, 16000, None)
    return resampled


def _gemini_pcm_to_discord_pcm(pcm_24k_mono: bytes) -> bytes:
    resampled, _ = audioop.ratecv(pcm_24k_mono, 2, 1, 24000, 48000, None)
    stereo = audioop.tostereo(resampled, 2, 1, 1)
    return stereo


# =============================================================================
# DISCORD-SIDE AUDIO PLUMBING
# =============================================================================
_DISCORD_FRAME_BYTES = 3840   # 20ms @ 48kHz stereo 16-bit = 48000*2*2*0.02


class _LivePlaybackSource(discord.AudioSource if _DISCORD_AVAILABLE else object):
    """Feeds Gemini's spoken audio (already resampled to 48kHz stereo) into
    Discord's voice-send pipeline. discord.py's internal player thread calls
    read() every 20ms; when there's nothing queued yet we hand back silence
    rather than ending the source, so playback just stays open and quiet."""

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()

    def feed(self, pcm_48k_stereo: bytes):
        with self._lock:
            self._buf.extend(pcm_48k_stereo)

    def read(self) -> bytes:
        with self._lock:
            if len(self._buf) >= _DISCORD_FRAME_BYTES:
                chunk = bytes(self._buf[:_DISCORD_FRAME_BYTES])
                del self._buf[:_DISCORD_FRAME_BYTES]
                return chunk
        return b"\x00" * _DISCORD_FRAME_BYTES

    def is_opus(self) -> bool:
        return False


class _LiveInputSink(_voice_recv.AudioSink if _VOICE_RECV_AVAILABLE else object):
    """Forwards every incoming Discord audio packet, from whoever is
    talking, straight to a callback -- no buffering/silence-detection here,
    since Gemini's own server-side VAD segments turns from a continuous
    stream. Multiple simultaneous speakers each arrive as separate write()
    calls; both get forwarded, which Gemini generally handles gracefully
    as overlapping speech (same as a real group call would sound to it)."""

    def __init__(self, on_audio):
        super().__init__()
        self._on_audio = on_audio

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data):
        if user is None or user.bot:
            return
        self._on_audio(bytes(data.pcm))

    def cleanup(self):
        pass


# =============================================================================
# GEMINI LIVE BRIDGE — one live session per active voice call
# =============================================================================
def _live_system_prompt(sess: dict) -> str:
    return (
        "You are Midum, an AI assistant, currently talking with someone live "
        "over voice in a Discord voice channel. This is a real-time spoken "
        "conversation -- keep replies natural, conversational, and fairly "
        "short, the way a person talks out loud, not the way you'd write "
        "text. You may be interrupted mid-sentence; if so, stop and listen.\n\n"
        f"OBJECTIVE FOR THIS CONVERSATION: {sess['objective']}\n\n"
        f"Start by saying, in your own natural words: \"{sess['opening_message']}\""
    )


async def _run_live_conversation(call_id: str, channel_id: int):
    sess = _get_session(call_id)
    channel = _bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await _bot.fetch_channel(channel_id)
        except Exception as e:
            sess["status"] = "failed"
            sess["ended"] = True
            _active_channel_calls.pop(str(channel_id), None)
            print(f"⚠️ [Discord voice] could not find channel {channel_id}: {e}")
            return

    # channel.connect() previously had NO try/except at all, and this whole
    # coroutine is scheduled via asyncio.ensure_future()/run_coroutine_threadsafe()
    # with no result ever awaited or exception handler attached (see the
    # done-callback added to _start_call_from_bot_loop below) -- so ANY
    # failure here (missing PyNaCl, opus not loaded, a voice-gateway
    # timeout, missing Connect/Speak permission on this specific channel,
    # a 4006/4014 websocket close code, etc.) previously just vanished:
    # no console output, no session status change, the bot simply never
    # appeared in the channel and nothing told you why.
    try:
        voice_client = await channel.connect(cls=_voice_recv.VoiceRecvClient)
    except Exception as e:
        sess["status"] = "failed"
        sess["ended"] = True
        _active_channel_calls.pop(str(channel_id), None)
        print(f"⚠️ [Discord voice] failed to connect to voice channel {channel_id}: {e}")
        return
    _voice_clients[call_id] = voice_client
    sess["status"] = "in-progress"

    playback = _LivePlaybackSource()
    voice_client.play(playback)

    gemini_key, _, _ = _get_gemini_live_creds()
    client = _genai.Client(api_key=gemini_key, http_options={"api_version": "v1alpha"})
    model = getattr(config, "GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")
    voice_name = getattr(config, "GEMINI_LIVE_VOICE", "Puck")

    # Same tool catalogue + same FunctionDeclaration shaping as the desktop
    # voice backend (providers/gemini_live_backend.py:_build_live_tools) --
    # previously this bridge never passed `tools=` at all, so a Discord
    # voice call could never actually DO anything (no files, no terminal,
    # no MCP, nothing), unlike text chat and desktop voice which both get
    # Midum's full tool-using pipeline. Reusing the existing builder keeps
    # the tool schema in exactly one place.
    from providers.gemini_live_backend import _build_live_tools

    live_cfg = _genai_types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=_genai_types.SpeechConfig(
            voice_config=_genai_types.VoiceConfig(
                prebuilt_voice_config=_genai_types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        ),
        system_instruction=_genai_types.Content(parts=[_genai_types.Part(text=_live_system_prompt(sess))]),
        tools=_build_live_tools(),
        input_audio_transcription={},
        output_audio_transcription={},
        # Automatic (server-side) VAD stays ENABLED here -- this is the
        # "always listening" behavior of a normal Gemini Live app: Midum
        # just streams continuous audio in, and Gemini's own server-side
        # VAD figures out turn boundaries entirely on its own, no
        # push-to-talk or manual activity_start/end needed on our end.
        #
        # IMPORTANT: don't ever send an explicit send_client_content() turn
        # on a session configured like this (e.g. to force a proactive
        # greeting) -- it was tried, and it silently corrupts the server's
        # turn-tracking state: that one explicit turn completes fine, but
        # automatic VAD then never recognizes a new turn boundary again,
        # so the bot answers once and goes permanently silent afterward.
        # Automatic VAD needs to be the ONLY thing driving turns for the
        # whole session. Practical effect: Midum won't speak until someone
        # else does first -- same as the real Gemini app.
    )

    try:
        cm = client.aio.live.connect(model=model, config=live_cfg)
        async with cm as live_session:
            await _bridge_loop(call_id, live_session, voice_client, playback)
    except Exception as e:
        if "gemini-3.1-flash-live-preview" in model:
            print(f"⚠️ [Discord voice] {model} unavailable ({e}); retrying on gemini-live-2.5-flash-preview")
            fallback_cfg = _genai_types.LiveConnectConfig(
                response_modalities=live_cfg.response_modalities,
                speech_config=live_cfg.speech_config,
                system_instruction=live_cfg.system_instruction,
                tools=live_cfg.tools,
                input_audio_transcription={},
                output_audio_transcription={},
            )
            try:
                async with client.aio.live.connect(model="gemini-live-2.5-flash-preview", config=fallback_cfg) as live_session:
                    await _bridge_loop(call_id, live_session, voice_client, playback)
            except Exception as e2:
                sess["status"] = "failed"
                _append_turn(call_id, "assistant", f"(voice session error: {e2})")
        else:
            sess["status"] = "failed"
            _append_turn(call_id, "assistant", f"(voice session error: {e})")

    if voice_client.is_connected():
        await voice_client.disconnect()
    _voice_clients.pop(call_id, None)
    _active_channel_calls.pop(str(channel_id), None)
    sess["status"] = sess.get("status") if sess.get("status") == "failed" else "completed"
    sess["ended"] = True


async def _bridge_loop(call_id: str, live_session, voice_client, playback: "_LivePlaybackSource"):
    sess = _get_session(call_id)

    # "Always listening" -- just forward every incoming Discord audio
    # packet straight into the Gemini Live session as a continuous stream.
    # No manual turn-boundary signaling here at all: automatic (server-side)
    # VAD, enabled in live_cfg/fallback_cfg above, is entirely responsible
    # for detecting when someone starts/stops talking and generating a
    # reply, the same as the real Gemini app.
    def _on_discord_audio(pcm_48k_stereo: bytes):
        if sess.get("ended"):
            return
        try:
            gemini_pcm = _discord_pcm_to_gemini_pcm(pcm_48k_stereo)
        except Exception:
            return
        asyncio.run_coroutine_threadsafe(
            live_session.send_realtime_input(
                audio=_genai_types.Blob(data=gemini_pcm, mime_type="audio/pcm;rate=16000")
            ),
            _bot_loop,
        )

    sink = _LiveInputSink(_on_discord_audio)
    voice_client.listen(sink)

    async def _recv_loop():
        while not sess.get("ended"):
            turn = live_session.receive()
            async for response in turn:
                if sess.get("ended"):
                    return
                sc = getattr(response, "server_content", None)
                if sc is None:
                    continue
                in_tx = getattr(sc, "input_transcription", None)
                if in_tx and getattr(in_tx, "text", None):
                    _append_turn(call_id, "caller", in_tx.text)
                out_tx = getattr(sc, "output_transcription", None)
                if out_tx and getattr(out_tx, "text", None):
                    _append_turn(call_id, "assistant", out_tx.text)
                model_turn = getattr(sc, "model_turn", None)
                if model_turn is not None:
                    for part in (model_turn.parts or []):
                        inline = getattr(part, "inline_data", None)
                        if inline is not None and inline.data:
                            try:
                                playback.feed(_gemini_pcm_to_discord_pcm(inline.data))
                            except Exception:
                                pass

                # Tool calls -- previously dropped entirely (this loop never
                # even looked at response.tool_call), which is the "half
                # implemented" part: Gemini could request a tool, but nothing
                # ever executed it or sent a FunctionResponse back, so the
                # model would just stall waiting on a call that never
                # resolves. Mirrors gemini_live_backend.VoiceSession._recv_task's
                # handling, minus screen-share (no video feed in a Discord
                # voice channel), using the same dispatcher so voice calls
                # over Discord get the same tool access as everything else
                # (files, terminal, browser, MCP, etc).
                tool_call = getattr(response, "tool_call", None)
                if tool_call is not None:
                    fn_responses = []
                    for fc in (tool_call.function_calls or []):
                        args = dict(fc.args or {})
                        if fc.name == "say":
                            _append_turn(call_id, "assistant", str(args.get("message", "")))
                            fn_responses.append(
                                _genai_types.FunctionResponse(
                                    id=fc.id, name=fc.name,
                                    response={"result": "Message displayed to user."}
                                )
                            )
                            continue
                        try:
                            from gui.dispatch import _dispatch_midum_tool
                            result = _dispatch_midum_tool(fc.name, args)
                        except Exception as e:
                            result = f"Error executing '{fc.name}': {e}"
                        result_str = str(result)
                        _append_turn(call_id, "assistant", f"(used tool {fc.name}: {result_str[:200]})")
                        fn_responses.append(
                            _genai_types.FunctionResponse(
                                id=fc.id, name=fc.name, response={"result": result_str}
                            )
                        )
                    if fn_responses:
                        await live_session.send_tool_response(function_responses=fn_responses)

    while voice_client.is_connected() and not sess.get("ended"):
        try:
            await asyncio.wait_for(_recv_loop(), timeout=None)
        except asyncio.CancelledError:
            break
        except Exception as e:
            if sess.get("ended"):
                break
            print(f"⚠️ [Discord voice] recv loop error: {e}")
            await asyncio.sleep(1.0)
        if not voice_client.is_connected():
            break

    sink.cleanup()


# =============================================================================
# TEXT CHAT (DMs + server messages) — routes into Midum's REAL reasoning
# pipeline (orchestration.process_chat_turn), the same one the terminal/GUI
# uses. Entirely separate from the voice bridge above.
#
# CHAT SESSIONS — every conversation (a DM thread, or a text channel) is
# split into named SESSIONS rather than one endless running history:
#   - The first message in a DM/channel starts session #1 automatically.
#   - Sending nothing for over an hour, then sending a new message, starts
#     a fresh session automatically -- a gap that long usually means a new
#     topic, and dragging hours/days-old context into an unrelated reply
#     tends to confuse the model more than help it.
#   - Typing "new session" (anywhere in the message) always force-starts a
#     fresh session immediately, regardless of the 1-hour timer.
#   - Typing "session <id>" jumps back into a specific PAST session by its
#     id (works even if that session belongs to a different channel/DM --
#     ids are unique across the whole bot, not just within one channel) and
#     continues it from wherever it left off.
# Whenever a new session starts (either way), Midum's reply is prefixed
# with "**Session: <id>**" so the id is always visible right when it's
# needed to come back to that conversation later. Sessions live in memory
# only for now (same as everything else in this module) -- they don't
# survive an app restart.
# =============================================================================
_DISCORD_SESSIONS: dict = {}         # session_id (str) -> {history, key, created_at, last_active}
_DISCORD_SESSIONS_LOCK = threading.RLock()
# key ("dm:<id>" / "channel:<id>") -> the session_id currently active there
_ACTIVE_SESSION_PER_KEY: dict = {}
_CHAT_ALLOWED_GUILD_ID = None
_DISCORD_MSG_LIMIT = 2000
_SESSION_IDLE_TIMEOUT = 3600   # seconds -- 1 hour of silence starts a new session

# Matched against the START of a message (case-insensitive), so "new
# session" or "new session: what's the weather" both work -- anything
# after the match is treated as the actual message content for that turn.
_NEW_SESSION_RE = re.compile(r"^\s*new\s+session\b[:,\-]?\s*", re.IGNORECASE)
# "session 1234 ..." -- the id is a single non-whitespace token (matches
# the numeric ids _generate_session_id() hands out, but doesn't hard-code
# the format in case that ever changes).
_RESUME_SESSION_RE = re.compile(r"^\s*session\s+(\S+)\b[:,\-]?\s*", re.IGNORECASE)


def _generate_session_id() -> str:
    """Short numeric id, easy to type back ("session 4821"), unique among
    currently-known sessions."""
    with _DISCORD_SESSIONS_LOCK:
        while True:
            sid = str(random.randint(1000, 999999))
            if sid not in _DISCORD_SESSIONS:
                return sid


def _create_chat_session(key: str) -> str:
    """Start a brand-new session, make it the active one for `key`, and
    return its id."""
    from system_prompt import get_system_prompt

    sid = _generate_session_id()
    now = time.time()
    with _DISCORD_SESSIONS_LOCK:
        _DISCORD_SESSIONS[sid] = {
            "history": [{"role": "system", "content": get_system_prompt()}],
            "key": key,
            "created_at": now,
            "last_active": now,
        }
        _ACTIVE_SESSION_PER_KEY[key] = sid
    return sid


def _get_chat_session(sid: str):
    with _DISCORD_SESSIONS_LOCK:
        return _DISCORD_SESSIONS.get(sid)


def _split_for_discord(text: str, limit: int = _DISCORD_MSG_LIMIT):
    text = text or "(no response)"
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks or ["(no response)"]


# Set (only) for the duration of _run_chat_turn_sync, on whichever executor
# thread is running that one call -- since that call runs synchronously
# start-to-finish on a single thread (including every tool call made during
# it), a plain threading.local is enough to let a tool like
# discord_upload_file() find "the channel/DM this turn is replying in"
# without process_chat_turn or the tool-dispatch chain needing to know
# anything about Discord at all.
_current_discord_channel = threading.local()


def _run_chat_turn_sync(sid: str, user_text: str, channel=None) -> str:
    from orchestration import process_chat_turn

    sess = _get_chat_session(sid)
    if sess is None:
        return "(error: session no longer exists)"

    with _DISCORD_SESSIONS_LOCK:
        history = sess["history"]
        history.append({"role": "user", "content": user_text})

    _current_discord_channel.channel = channel
    try:
        reply, _tool_outputs = process_chat_turn(history, user_request=user_text, gemini_plan="")
    except Exception as e:
        reply = f"(error while processing that: {e})"
    finally:
        _current_discord_channel.channel = None

    with _DISCORD_SESSIONS_LOCK:
        history.append({"role": "assistant", "content": reply})
        sess["last_active"] = time.time()

    return reply


def _resolve_chat_session(key: str, raw_text: str):
    """Figure out which session this incoming message belongs to, applying
    the "new session" / "session <id>" / 1-hour-timeout rules above.
    Returns (session_id, remaining_text, just_started: bool, unknown_id: str|None).
    """
    new_match = _NEW_SESSION_RE.match(raw_text)
    if new_match:
        sid = _create_chat_session(key)
        return sid, raw_text[new_match.end():].strip(), True, None

    resume_match = _RESUME_SESSION_RE.match(raw_text)
    if resume_match:
        requested_sid = resume_match.group(1)
        remaining = raw_text[resume_match.end():].strip()
        existing = _get_chat_session(requested_sid)
        if existing is None:
            # Unknown/typo'd id -- don't just go nowhere; start a fresh
            # session so the user still gets a working conversation, but
            # flag it so the reply can explain why a NEW id showed up
            # instead of the one they asked for.
            sid = _create_chat_session(key)
            return sid, remaining, True, requested_sid
        with _DISCORD_SESSIONS_LOCK:
            _ACTIVE_SESSION_PER_KEY[key] = requested_sid
        return requested_sid, remaining, False, None

    with _DISCORD_SESSIONS_LOCK:
        sid = _ACTIVE_SESSION_PER_KEY.get(key)
        sess = _DISCORD_SESSIONS.get(sid) if sid else None

    if sess is None or (time.time() - sess["last_active"]) > _SESSION_IDLE_TIMEOUT:
        sid = _create_chat_session(key)
        return sid, raw_text.strip(), True, None

    return sid, raw_text.strip(), False, None


def _mentions_bot(message) -> bool:
    """True if this message @-mentions Midum's bot user directly (a plain
    role mention or @everyone/@here doesn't count -- only an explicit
    @Midum ping does)."""
    if _bot is None or _bot.user is None:
        return False
    return any(u.id == _bot.user.id for u in message.mentions)


def _lone_human_online(guild) -> bool:
    """True if exactly one non-bot member of `guild` is currently online
    (online/idle/dnd -- anything but offline/invisible). This is the
    "only one person online besides Midum" exception: with just one human
    around there's no one else the message could be addressed to, so
    requiring an explicit @Midum mention just adds friction. Requires the
    SERVER MEMBERS + PRESENCE privileged intents to see accurate statuses;
    if presence data isn't available every member reads as "offline" and
    this simply returns False, falling back to requiring a mention."""
    if guild is None:
        return False
    online_humans = [
        m for m in guild.members
        if not m.bot and getattr(m, "status", discord.Status.offline) != discord.Status.offline
    ]
    return len(online_humans) == 1


# Strips a leading/trailing/embedded @Midum mention token (<@id> or <@!id>)
# out of the message text once we've already used it to decide whether to
# respond, so the mention itself doesn't pollute the actual request text
# (e.g. "@Midum new session hi" should still trigger _NEW_SESSION_RE).
_MENTION_TOKEN_RE = re.compile(r"<@!?\d+>")


async def _handle_chat_message(message):
    is_dm = message.guild is None
    if not is_dm:
        if _CHAT_ALLOWED_GUILD_ID is not None and message.guild.id != _CHAT_ALLOWED_GUILD_ID:
            return

        # In a server (not a DM), only respond when explicitly @-mentioned
        # -- UNLESS Midum is the only "other" presence, i.e. exactly one
        # human is online in the server, in which case any message in an
        # allowed channel is clearly meant for Midum anyway.
        if not _mentions_bot(message) and not _lone_human_online(message.guild):
            return

    key = f"dm:{message.author.id}" if is_dm else f"channel:{message.channel.id}"
    raw_text = _MENTION_TOKEN_RE.sub("", message.content or "").strip()

    sid, user_text, just_started, unknown_id = _resolve_chat_session(key, raw_text)

    session_note = (
        f"No session found with id `{unknown_id}` — starting a new one instead.\n\n"
        if unknown_id else ""
    )

    if not user_text:
        # "new session" / "session 1234" with nothing else in the message --
        # just confirm rather than running a full reasoning turn on nothing.
        verb = "Started" if just_started else "Resumed"
        await message.reply(f"{session_note}{verb} session.\n\n**Session: {sid}**", mention_author=False)
        return

    async with message.channel.typing():
        reply = await _bot_loop.run_in_executor(None, _run_chat_turn_sync, sid, user_text, message.channel)

    header = f"{session_note}**Session: {sid}**\n\n" if just_started else ""
    full_reply = header + reply

    # Use an actual Discord reply (the "replying to ..." thread link) rather
    # than a bare channel.send() -- otherwise in a busy channel like #general
    # it's invisible which message Midum is even responding to, and it can
    # LOOK like Midum never replied at all. Only the first chunk is a true
    # reply; any overflow chunks (long answers get split by _split_for_discord)
    # are follow-up sends so we don't spam a chain of reply-banners.
    chunks = _split_for_discord(full_reply)
    try:
        await message.reply(chunks[0], mention_author=False)
    except discord.HTTPException:
        # Original message may have been deleted, or we lack "Read Message
        # History" in this channel (reply requires it) -- fall back to a
        # plain send so the user still gets an answer either way.
        await message.channel.send(chunks[0])
    for chunk in chunks[1:]:
        await message.channel.send(chunk)


# =============================================================================
# AUTO-JOIN / AUTO-LEAVE — Midum joins the default voice channel the moment
# a real member enters it, and leaves again once it's empty of humans.
# =============================================================================
def _start_call_from_bot_loop(channel_id: int, opening_message: str, objective: str):
    """Schedules a new live-conversation session on the bot's own event
    loop. Safe to call from an async Discord event handler (already on that
    loop) since asyncio.ensure_future just schedules it."""
    channel_key = str(channel_id)
    if channel_key in _active_channel_calls:
        return  # already have a call running in this channel
    call_id = _new_session(channel_key, opening_message, objective)
    _active_channel_calls[channel_key] = call_id
    task = asyncio.ensure_future(_run_live_conversation(call_id, channel_id), loop=_bot_loop)

    # asyncio silently swallows an unhandled exception raised inside a task
    # scheduled this way -- it's only ever reported (via the default
    # exception handler, to stderr, and only sometimes) once the Task object
    # gets garbage collected, which can be long after the fact or never if
    # something still holds a reference. _run_live_conversation now catches
    # its own known failure points internally, but this callback is a
    # backstop for anything unanticipated, so a broken auto-join always
    # prints something instead of just quietly never happening.
    def _log_if_failed(finished_task):
        exc = finished_task.exception() if not finished_task.cancelled() else None
        if exc is not None:
            print(f"⚠️ [Discord voice] auto-join task for channel {channel_id} crashed: {exc}")
            sess = _get_session(call_id)
            if sess is not None:
                sess["status"] = "failed"
                sess["ended"] = True
            _active_channel_calls.pop(channel_key, None)

    task.add_done_callback(_log_if_failed)


async def _maybe_auto_leave(channel):
    """If Midum is connected to `channel` and no non-bot member remains in
    it, end that call."""
    if channel is None:
        return
    humans_left = any(not m.bot for m in channel.members)
    if humans_left:
        return
    channel_key = str(channel.id)
    call_id = _active_channel_calls.get(channel_key)
    if call_id is None:
        return
    sess = _get_session(call_id)
    if sess is not None:
        sess["ended"] = True   # the bridge loop notices this and tears itself down


# =============================================================================
# BOT LIFECYCLE — one persistent bot, own thread + asyncio loop
# =============================================================================
def _ensure_bot_running() -> tuple:
    global _bot, _bot_loop, _bot_thread, _CHAT_ALLOWED_GUILD_ID

    with _bot_lock:
        if _bot_thread is not None and _bot_thread.is_alive():
            _bot_ready_event.wait(timeout=15)
            return (True, "OK") if _bot_ready_event.is_set() else (False, "Bot did not become ready in time.")

        intents = discord.Intents.default()
        intents.voice_states = True
        intents.members = True             # more reliable channel.members for auto-leave
        intents.presences = True           # needed to see online/idle/dnd status for the
                                            # "only one human online" no-mention exception
        intents.message_content = True     # required to read DM/server message text
        _bot = commands.Bot(command_prefix="!midum-unused-", intents=intents)

        @_bot.event
        async def on_ready():
            global _CHAT_ALLOWED_GUILD_ID
            if _DEFAULT_CHANNEL_ID:
                try:
                    ch = _bot.get_channel(int(_DEFAULT_CHANNEL_ID)) or await _bot.fetch_channel(int(_DEFAULT_CHANNEL_ID))
                    if ch is not None and getattr(ch, "guild", None) is not None:
                        _CHAT_ALLOWED_GUILD_ID = ch.guild.id
                        # If humans are already sitting in the default channel
                        # when Midum starts up, join immediately rather than
                        # waiting for the NEXT join event.
                        voice_ok, _ = _check_voice_extras_available()
                        if voice_ok and any(not m.bot for m in ch.members):
                            _start_call_from_bot_loop(
                                ch.id,
                                "Hey, I'm here!",
                                "Have a natural, friendly voice conversation."
                            )
                except Exception:
                    pass
            _bot_ready_event.set()

        @_bot.event
        async def on_message(message):
            if message.author.bot or message.author.id == _bot.user.id:
                return
            if not (message.content or "").strip():
                return
            try:
                await _handle_chat_message(message)
            except Exception as e:
                try:
                    await message.channel.send(f"(error: {e})")
                except Exception:
                    pass

        @_bot.event
        async def on_voice_state_update(member, before, after):
            if member.bot:
                return
            default_id = None
            if _DEFAULT_CHANNEL_ID:
                try:
                    default_id = int(_DEFAULT_CHANNEL_ID)
                except ValueError:
                    default_id = None

            # Someone joined the default channel -- auto-join if not already there
            if (default_id is not None and after.channel is not None
                    and after.channel.id == default_id
                    and (before.channel is None or before.channel.id != default_id)):
                voice_ok, voice_msg = _check_voice_extras_available()
                if voice_ok:
                    _start_call_from_bot_loop(
                        default_id,
                        f"Hey {member.display_name}, I'm here!",
                        "Have a natural, friendly voice conversation."
                    )
                else:
                    print(f"⚠️ [Discord voice] {member.display_name} joined but voice calling isn't ready: {voice_msg}")

            # Someone left a channel Midum is in -- auto-leave if now empty of humans
            if before.channel is not None and str(before.channel.id) in _active_channel_calls:
                await _maybe_auto_leave(before.channel)

        def _run():
            global _bot_loop
            _bot_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_bot_loop)
            try:
                _bot_loop.run_until_complete(_bot.start(_DISCORD_TOKEN))
            except Exception as e:
                print(f"[Discord voice] bot crashed: {e}")

        _bot_thread = threading.Thread(target=_run, daemon=True, name="midum-discord-bot")
        _bot_thread.start()

        if not _bot_ready_event.wait(timeout=20):
            return False, "Discord bot did not log in within 20s — check DISCORD_BOT_TOKEN."
        return True, "OK"


# =============================================================================
# PUBLIC TOOL FUNCTIONS
# =============================================================================
def call_discord(opening_message: str, objective: str = "", channel_id: str = "") -> str:
    """
    Join a Discord voice channel and hold a live, real-time SPOKEN
    conversation (via Gemini Live, same tech as Midum's desktop voice mode)
    with whoever is in it -- the free alternative to a real phone call.

    NOTE: for DISCORD_DEFAULT_VOICE_CHANNEL_ID, Midum already auto-joins the
    moment anyone enters it -- you usually don't need to call this manually
    unless you want a custom opening line/objective, or to join a DIFFERENT
    channel than the configured default.

    opening_message — the first thing Midum says once it joins the channel.
    objective        — what Midum is trying to accomplish in this conversation.
    channel_id       — Discord voice channel ID to join. If omitted, uses
                        DISCORD_DEFAULT_VOICE_CHANNEL_ID from the secrets file.

    Returns a call_id to use with get_discord_call_status() /
    get_discord_call_transcript() / end_discord_call().
    """
    if not _CREDS_AVAILABLE:
        # Credentials are only ever read once, at import time (module load).
        # If the user fills in DISCORD_BOT_TOKEN in the secrets file AFTER
        # Midum has already started, _CREDS_AVAILABLE would otherwise stay
        # stuck False for the rest of the process's life -- looking exactly
        # like "Discord isn't configured" even once it is. Re-check the file
        # on demand before giving up.
        _load_discord_creds()
        if not _CREDS_AVAILABLE:
            return f"Error: Discord is not configured — {_discord_load_msg}"
    voice_ok, voice_msg = _check_voice_extras_available()
    if not voice_ok:
        return f"Error: Discord voice calling is missing dependencies — {voice_msg}"

    channel_id = (channel_id or _DEFAULT_CHANNEL_ID or "").strip()
    if not channel_id:
        return "Error: no channel_id given and DISCORD_DEFAULT_VOICE_CHANNEL_ID is not set in secrets."
    try:
        channel_id_int = int(channel_id)
    except ValueError:
        return f"Error: channel_id must be a numeric Discord channel ID, got '{channel_id}'."

    opening_message = (opening_message or "").strip()
    if not opening_message:
        return "Error: 'opening_message' is required."

    ok, msg = _ensure_bot_running()
    if not ok:
        return f"Error: could not start the Discord bot — {msg}"

    if str(channel_id_int) in _active_channel_calls:
        existing = _active_channel_calls[str(channel_id_int)]
        return f"Midum is already in that voice channel (call_id='{existing}')."

    call_id = _new_session(channel_id, opening_message, objective)
    _active_channel_calls[channel_id] = call_id
    future = asyncio.run_coroutine_threadsafe(_run_live_conversation(call_id, channel_id_int), _bot_loop)

    # Same silent-failure risk as the auto-join path in
    # _start_call_from_bot_loop -- run_coroutine_threadsafe's returned
    # concurrent.futures.Future is never awaited/checked here, so an
    # exception would otherwise just sit in the Future object unseen.
    def _log_if_failed(finished_future):
        exc = finished_future.exception() if not finished_future.cancelled() else None
        if exc is not None:
            print(f"⚠️ [Discord voice] call_discord() task for channel {channel_id_int} crashed: {exc}")

    future.add_done_callback(_log_if_failed)

    return (
        f"Success: joining Discord voice channel {channel_id} now "
        f"(call_id='{call_id}'). Use get_discord_call_status('{call_id}') to "
        f"check on it, or get_discord_call_transcript('{call_id}') to read "
        f"what's been said so far."
    )


def get_discord_call_status(call_id: str) -> str:
    """Check the current status and last few lines of a Discord call started with call_discord()."""
    sess = _get_session(call_id)
    if sess is None:
        return f"Error: no call session found for call_id='{call_id}'."
    last_lines = sess["transcript"][-4:]
    lines_text = "\n".join(
        f"  {'Midum' if t['role'] == 'assistant' else 'Other'}: {t['text']}"
        for t in last_lines
    ) or "  (nothing said yet)"
    return (
        f"Discord call in channel {sess['channel_id']} — status: {sess['status']}"
        f"{' (ended)' if sess['ended'] else ''}\n"
        f"Recent lines:\n{lines_text}"
    )


def get_discord_call_transcript(call_id: str) -> str:
    """Return the FULL transcript so far of a Discord call started with call_discord()."""
    sess = _get_session(call_id)
    if sess is None:
        return f"Error: no call session found for call_id='{call_id}'."
    if not sess["transcript"]:
        return f"No transcript yet for the call in channel {sess['channel_id']} (status: {sess['status']})."
    lines = [
        f"{'Midum' if t['role'] == 'assistant' else 'Other'}: {t['text']}"
        for t in sess["transcript"]
    ]
    return (
        f"Transcript — Discord call in channel {sess['channel_id']} (status: {sess['status']}):\n\n"
        + "\n".join(lines)
    )


def end_discord_call(call_id: str) -> str:
    """Leave the voice channel and end a Discord call started with call_discord()."""
    sess = _get_session(call_id)
    if sess is None:
        return f"Error: no call session found for call_id='{call_id}'."
    if sess.get("ended"):
        return f"Call in channel {sess['channel_id']} has already ended (status: {sess['status']})."
    sess["ended"] = True   # the bridge loop notices this and disconnects itself
    return f"Ending the call in channel {sess['channel_id']}."


def list_discord_calls() -> str:
    """List every Discord voice call session from this run, most recent first."""
    with _CALL_SESSIONS_LOCK:
        sessions = sorted(_CALL_SESSIONS.values(), key=lambda s: s["created_at"], reverse=True)
    if not sessions:
        return "No Discord calls have been placed yet."
    lines = ["Discord calls this session:", ""]
    for s in sessions:
        lines.append(
            f"  call_id='{s['call_id']}'  channel={s['channel_id']}  "
            f"status={s['status']}{' (ended)' if s['ended'] else ''}  "
            f"objective={s['objective'][:60]!r}"
        )
    return "\n".join(lines)


def list_discord_sessions() -> str:
    """
    List every Discord TEXT CHAT session (DM or channel conversation) known
    this run, most recently active first -- id, which DM/channel it belongs
    to, message count, and how long ago it was last active. Use this to
    find the id of an old conversation to resume with "session <id>" if it
    wasn't written down at the time. Separate from list_discord_calls(),
    which is for VOICE calls, not text sessions.
    """
    with _DISCORD_SESSIONS_LOCK:
        sessions = sorted(_DISCORD_SESSIONS.items(), key=lambda kv: kv[1]["last_active"], reverse=True)
    if not sessions:
        return "No Discord chat sessions yet."
    lines = ["Discord chat sessions this run:", ""]
    now = time.time()
    for sid, sess in sessions:
        age_min = int((now - sess["last_active"]) / 60)
        msg_count = max(0, len(sess["history"]) - 1)   # exclude the system prompt
        active_tag = " (active now)" if age_min < 1 else f" (last active {age_min}m ago)"
        lines.append(f"  session {sid}  —  {sess['key']}  —  {msg_count} message(s){active_tag}")
    lines.append("")
    lines.append('Say "session <id>" in that DM/channel to resume one.')
    return "\n".join(lines)


# Discord's own hard upload cap for a non-boosted server. A boosted server
# allows more (25/50/100MB tiers), but there's no cheap way to know a given
# channel's actual tier without an extra API round trip, and channel.send()
# already raises a clear discord.HTTPException (413/Payload Too Large) if a
# file exceeds whatever the real limit is -- so this is just a fast local
# pre-check to fail with a clear message instead of doing the upload attempt
# and round-tripping to Discord's servers for nothing.
_DISCORD_UPLOAD_MAX_BYTES = 8 * 1024 * 1024


def discord_upload_file(file_path: str) -> str:
    """
    Upload a local file to the Discord conversation (the DM or channel) this
    tool call is currently running inside of -- i.e. wherever the user's
    message that triggered this turn came from. Use this whenever the user
    asks you to send/upload/share/attach a file in the Discord chat.

    file_path -- ABSOLUTE path to a file that already exists on disk.

    Only works while handling a Discord TEXT message (a DM or channel
    message routed through Midum's normal Discord chat pipeline) -- calling
    this from the terminal, GUI, or a Discord VOICE call (which has no
    associated text channel context) returns a clear error instead of
    silently doing nothing.
    """
    channel = getattr(_current_discord_channel, "channel", None)
    if channel is None:
        return (
            "Error: no active Discord text conversation to upload to -- "
            "discord_upload_file only works while replying to a Discord DM "
            "or channel message."
        )

    if not file_path or not os.path.isabs(file_path):
        return f"Error: '{file_path}' is not an absolute file path."
    if not os.path.isfile(file_path):
        return f"Error: file not found at '{file_path}'."

    try:
        size = os.path.getsize(file_path)
    except OSError as e:
        return f"Error reading file size for '{file_path}': {e}"
    if size > _DISCORD_UPLOAD_MAX_BYTES:
        return (
            f"Error: '{os.path.basename(file_path)}' is {size / (1024 * 1024):.1f}MB, "
            f"over the {_DISCORD_UPLOAD_MAX_BYTES // (1024 * 1024)}MB upload limit for a "
            f"non-boosted Discord server (a boosted server may allow more)."
        )

    if _bot_loop is None:
        return "Error: the Discord bot is not currently running."

    async def _send():
        await channel.send(file=discord.File(file_path))

    future = asyncio.run_coroutine_threadsafe(_send(), _bot_loop)
    try:
        future.result(timeout=30)
    except Exception as e:
        return f"Error uploading '{os.path.basename(file_path)}' to Discord: {e}"

    return f"Success: uploaded '{os.path.basename(file_path)}' ({size} bytes) to Discord."


def start_discord_bot() -> str:
    """
    Start the Discord bot so it's listening for DMs, server messages, and
    voice-channel auto-join, without waiting for something else to trigger
    it first.

    Once running, the bot:
      - Answers any DM sent to it, from anyone.
      - Answers any message in a text channel of the SAME server as
        DISCORD_DEFAULT_VOICE_CHANNEL_ID (resolved automatically).
      - Auto-joins DISCORD_DEFAULT_VOICE_CHANNEL_ID the moment a real member
        enters it, and holds a live spoken conversation via Gemini Live.
        Auto-leaves again once the channel is empty of humans.
    """
    if not _CREDS_AVAILABLE:
        _load_discord_creds()   # see call_discord() -- picks up a key added after startup
        if not _CREDS_AVAILABLE:
            return f"Error: Discord is not configured — {_discord_load_msg}"
    ok, msg = _ensure_bot_running()
    if not ok:
        return f"Error: could not start the Discord bot — {msg}"
    return (
        "Success: Discord bot is running. It will reply to DMs from anyone, "
        "reply to messages in text channels of the configured server, and "
        "automatically join the default voice channel to talk the moment "
        "someone enters it."
    )
