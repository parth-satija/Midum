# =============================================================================
# GEMINI LIVE (VOICE CONTROL) BACKEND
# =============================================================================
# Real-time, bidirectional speech-to-speech control of Midum through Google's
# official Gemini Live API (google-genai SDK, `client.aio.live.connect`).
# This is a SEPARATE transport from every other provider in providers/ --
# those are all single-shot request/response over HTTP (OpenAI-compatible
# /chat/completions). This one holds a persistent WebSocket session, streams
# microphone audio to Gemini continuously, and plays back Gemini's spoken
# audio replies as they arrive, while ALSO getting full native tool calling
# with Midum's entire tool catalogue -- every tool call the model requests
# is executed through the exact same dispatcher the manual tool sandbox and
# GUI chat pane already use (gui/dispatch.py:_dispatch_midum_tool), gated by
# the exact same Permissions-tab enforcement text chat uses
# (permissions.py:enforce_tool_permission, called right before dispatch --
# see _recv_task below), so voice control has 100% tool parity with text
# chat: files, terminal, UI automation, browser, MCP servers, permissions,
# everything.
#
# SETUP:
#   1. Get a free key at https://aistudio.google.com/app/apikey
#   2. Add it to the shared secrets file as GEMINI_API_KEY (same key used by
#      MODEL_PROVIDER="gemini_api" -- if you've already set that up, voice
#      control works with zero extra configuration).
#   3. pip install google-genai sounddevice numpy
#
# ARCHITECTURE:
#   VoiceSession runs its own asyncio event loop on a dedicated background
#   thread (start() spawns it, stop() tears it down), so it never blocks or
#   competes with the GUI's Qt event loop or the main text-chat tool loop.
#   Three concurrent async tasks once connected:
#     - _mic_drain_task : sounddevice input stream (started immediately in
#                          _main, decoupled from connection state) -> queue
#                          -> session.send_realtime_input
#     - _recv_task  : session.receive() -> speaker playback + transcripts +
#                     tool_call handling -> session.send_tool_response
#   All UI-facing events (status changes, transcripts, tool calls/results,
#   errors) go through a single `on_event(kind, payload)` callback supplied
#   by the caller (the GUI's Api class wires this straight to _push_event,
#   the same async bridge used for every other GUI event).
# =============================================================================

import asyncio
import json
import os
import queue
import threading

import config
from config import SECRETS_FILE
from screen_capture import capture_screen_frame_jpeg_bytes
from system_prompt import get_system_prompt
from tools_schema import tools as _OPENAI_TOOLS

try:
    from google import genai
    from google.genai import types as genai_types
    _GENAI_SDK_AVAILABLE = True
except ImportError:
    genai = None
    genai_types = None
    _GENAI_SDK_AVAILABLE = False

try:
    import sounddevice as sd
    _SOUNDDEVICE_AVAILABLE = True
except ImportError:
    sd = None
    _SOUNDDEVICE_AVAILABLE = False


# ── API key loading (shared secrets file, same key as gemini_api_backend) ───
_GEMINI_LIVE_KEY = None
_GEMINI_LIVE_AVAILABLE = False
_gemini_live_load_msg = "not loaded yet"


def _load_gemini_live_key():
    global _GEMINI_LIVE_KEY, _GEMINI_LIVE_AVAILABLE, _gemini_live_load_msg
    try:
        secrets_path = os.path.abspath(SECRETS_FILE)
        if not os.path.exists(secrets_path):
            _gemini_live_load_msg = f"Secrets file not found: {secrets_path}"
            return
        with open(secrets_path, "r", encoding="utf-8") as f:
            secrets = json.load(f)
        key = secrets.get("GEMINI_API_KEY", "").strip()
        if not key:
            _gemini_live_load_msg = "GEMINI_API_KEY is empty in secrets file."
            return
        _GEMINI_LIVE_KEY = key
        _GEMINI_LIVE_AVAILABLE = True
        _gemini_live_load_msg = "OK"
    except Exception as e:
        _gemini_live_load_msg = str(e)


_load_gemini_live_key()


def reload_gemini_live_key():
    """Re-read GEMINI_API_KEY from the secrets file (e.g. after the user
    saves a new key from the GUI). Returns (available: bool, message: str)."""
    _load_gemini_live_key()
    return _GEMINI_LIVE_AVAILABLE, _gemini_live_load_msg


def voice_dependencies_status() -> str:
    """Human-readable readiness report for the Voice tab to show before
    letting the user hit Start."""
    problems = []
    if not _GENAI_SDK_AVAILABLE:
        problems.append("google-genai SDK not installed (pip install google-genai)")
    if not _SOUNDDEVICE_AVAILABLE:
        problems.append("sounddevice not installed (pip install sounddevice numpy)")
    if not _GEMINI_LIVE_AVAILABLE:
        problems.append(f"GEMINI_API_KEY not configured ({_gemini_live_load_msg})")
    return "OK" if not problems else "; ".join(problems)


# ── Tool schema conversion: Midum's OpenAI-shaped `tools` -> Gemini's
#    FunctionDeclaration/Tool shape. Gemini's Live API accepts standard
#    JSON-schema `parameters` dicts directly, so this is mostly a re-shape,
#    not a re-write. Also runs the schema through filter_tools_schema()
#    first -- same as every text-mode provider backend does -- so a tool
#    set to "Don't Allow" in the Permissions tab isn't even offered to the
#    model here, matching text chat instead of just relying on the
#    dispatch-time block in _recv_task to reject it after the fact. ───────
def _build_live_tools():
    from permissions import filter_tools_schema
    declarations = []
    for t in filter_tools_schema(_OPENAI_TOOLS):
        fn = t.get("function", {})
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {"type": "object", "properties": {}}
        # Gemini rejects empty {"properties": {}} objects on some tools less
        # strictly than OpenAI, but it does require "type": "object" to be
        # present -- guaranteed already by tools_schema.py's shape.
        declarations.append({
            "name": name,
            "description": (fn.get("description") or "")[:1000],
            "parameters": params,
        })
    return [genai_types.Tool(function_declarations=declarations)]


class VoiceSession:
    """One long-lived Gemini Live voice-control session. Reused across
    start()/stop() calls -- create ONE instance per app run (see
    get_voice_session() below) rather than a new one per session."""

    def __init__(self, on_event):
        self.on_event = on_event      # callback(kind: str, payload: dict)
        self._thread = None
        self._stop_evt = threading.Event()
        self._running = False
        self._muted = False           # mic muted but session stays open
        # Live screen-share: set/cleared by start_screen_share()/stop_screen_share()
        # (called via the start_screen_share/stop_screen_share tools, dispatched
        # like any other tool call). The background _screen_share_task polls this
        # flag rather than being started/stopped itself, so toggling it is a plain
        # thread-safe Event set/clear -- no cross-thread asyncio scheduling needed.
        self._screen_share_evt = threading.Event()

    # ── Public control surface (safe to call from the GUI/Qt thread) ──────
    def start(self, model: str = "", voice: str = "") -> str:
        if self._running:
            return "Voice session is already running."
        status = voice_dependencies_status()
        if status != "OK":
            return f"Cannot start voice session: {status}"

        self._stop_evt.clear()
        self._muted = False
        self._screen_share_evt.clear()
        self._thread = threading.Thread(
            target=self._thread_main, args=(model.strip(), voice.strip()), daemon=True
        )
        self._running = True
        self._thread.start()
        return "Voice session starting..."

    def stop(self) -> str:
        if not self._running:
            return "Voice session is not running."
        self._stop_evt.set()
        return "Stopping voice session..."

    def set_muted(self, muted: bool) -> str:
        self._muted = bool(muted)
        return "Muted" if self._muted else "Unmuted"

    def is_running(self) -> bool:
        return self._running

    # ── Live screen-share control (safe to call from the GUI/Qt thread, or
    # from the model's own tool-call dispatch, which also happens off the
    # session's event-loop thread) ─────────────────────────────────────────
    def start_screen_share(self) -> str:
        if not self._running:
            return "Cannot start screen share: voice session is not running."
        if self._screen_share_evt.is_set():
            return "Screen share is already live."
        self._screen_share_evt.set()
        return "Live screen viewing started -- streaming screenshots into the conversation until stop_screen_share is called."

    def stop_screen_share(self) -> str:
        if not self._screen_share_evt.is_set():
            return "Screen share is not currently active."
        self._screen_share_evt.clear()
        return "Live screen viewing stopped."

    def is_screen_sharing(self) -> bool:
        return self._screen_share_evt.is_set()

    # ── Background thread entry point ──────────────────────────────────────
    def _thread_main(self, model: str, voice: str):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main(model, voice))
        except Exception as e:
            self.on_event("voice_error", {"message": str(e)})
        finally:
            self._running = False
            self._screen_share_evt.clear()
            self.on_event("voice_status", {"status": "stopped"})
            try:
                loop.close()
            except Exception:
                pass

    async def _main(self, model: str, voice: str):
        client = genai.Client(api_key=_GEMINI_LIVE_KEY, http_options={"api_version": "v1alpha"})
        use_model = model or getattr(config, "GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")
        use_voice = voice or getattr(config, "GEMINI_LIVE_VOICE", "Puck")

        try:
            sys_prompt = get_system_prompt(effective_provider="gemini_live", effective_model=use_model)
        except Exception:
            sys_prompt = (
                "You are Midum, an autonomous desktop assistant, now operating in a "
                "real-time VOICE conversation. Keep spoken replies short and natural -- "
                "you are talking, not writing. Use tools freely and narrate briefly "
                "what you're doing before acting."
            )
        sys_prompt += (
            "\n\n\u2500\u2500\u2500 VOICE MODE \u2500\u2500\u2500\n"
            "You are speaking with the user live, out loud. Keep replies conversational "
            "and brief -- a sentence or two before or after acting, not a written report. "
            "Call tools directly and immediately when the user asks for an action; don't "
            "narrate at length first. If you're interrupted mid-sentence, stop and listen."
        )

        live_cfg = genai_types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(voice_name=use_voice)
                )
            ),
            system_instruction=genai_types.Content(parts=[genai_types.Part(text=sys_prompt)]),
            tools=_build_live_tools(),
            input_audio_transcription={},
            output_audio_transcription={},
            # Turn boundaries are driven MANUALLY (see _mic_drain_task's
            # activity_start/activity_end calls) instead of Gemini's own
            # server-side VAD. With automatic detection on, Gemini decides
            # a turn is "done" after it detects a bit of silence -- which is
            # what made push-to-talk feel like it only sent the prompt on
            # the *next* button press instead of immediately on release.
            # Disabling it and sending explicit start/end markers makes
            # release-to-send actually release-to-send.
            realtime_input_config=genai_types.RealtimeInputConfig(
                automatic_activity_detection=genai_types.AutomaticActivityDetection(disabled=True)
            ),
        )

        # ── Start capturing the microphone IMMEDIATELY, in parallel with the
        # WebSocket handshake below -- not after it finishes. Every captured
        # chunk is tagged with the mute state AT THE MOMENT IT WAS CAPTURED
        # and dropped into mic_queue, which is drained by _mic_drain_task
        # once (and only once) a session exists.
        #
        # This is the fix for the "push-to-talk only submits on the second
        # press" bug: a fast press+release (the whole PTT gesture) could
        # previously complete *before* client.aio.live.connect() returned.
        # The old code only opened the mic stream inside _mic_task(session),
        # which didn't start running until the session existed -- so that
        # entire first utterance was captured by nothing and silently lost.
        # The user heard no reply, pressed again, and *that* utterance went
        # out over an already-open connection and got a response -- hence
        # "only works on the second click". Buffering from the moment the
        # thread starts, decoupled from connection state, means the first
        # press is never lost: its audio (and its correct activity_start /
        # activity_end boundary, recorded per-chunk) just waits in the
        # queue until the drain task can flush it the instant we connect.
        send_rate = getattr(config, "GEMINI_LIVE_SEND_RATE", 16000)
        chunk = getattr(config, "GEMINI_LIVE_CHUNK_SIZE", 1024)
        mic_queue: "queue.Queue[tuple[bool, bytes]]" = queue.Queue()

        def _mic_callback(indata, frames, time_info, status):
            mic_queue.put((self._muted, bytes(indata)))

        mic_stream = sd.RawInputStream(
            samplerate=send_rate, blocksize=chunk, dtype="int16", channels=1, callback=_mic_callback
        )

        self.on_event("voice_status", {"status": "connecting", "model": use_model})
        try:
            with mic_stream:
                async with client.aio.live.connect(model=use_model, config=live_cfg) as session:
                    self.on_event("voice_status", {"status": "connected", "model": use_model})
                    await asyncio.gather(
                        self._mic_drain_task(session, mic_queue, send_rate),
                        self._recv_task(session),
                        self._screen_share_task(session),
                    )
        except Exception as e:
            # Retry once against the older-generation model name if the
            # preview id above isn't enabled on this API key yet.
            if model == "" and "gemini-3.1-flash-live-preview" in use_model:
                self.on_event("voice_status", {
                    "status": "retrying",
                    "message": f"{use_model} unavailable ({e}); retrying on gemini-live-2.5-flash-preview"
                })
                await self._main("gemini-live-2.5-flash-preview", voice)
                return
            raise

    async def _mic_drain_task(self, session, mic_queue, send_rate):
        """Drain mic_queue (fed continuously by the mic_callback in _main,
        already running before this task starts) and stream it to Gemini.

        Since live_cfg disables Gemini's automatic VAD, turn boundaries have
        to be signalled explicitly. Each queued item carries the mute state
        that was active at the exact moment it was *captured* -- not
        whatever self._muted happens to be right now -- so a push-to-talk
        press/release that both happened while we were still connecting is
        replayed here in the correct order the instant the session opens:
        activity_start, then its audio, then activity_end, immediately
        triggering Gemini's reply instead of waiting for a second press.
        """
        loop = asyncio.get_event_loop()
        was_muted = True   # forces an activity_start the first time we go live
        while not self._stop_evt.is_set():
            try:
                muted_at_capture, data = await loop.run_in_executor(None, mic_queue.get, True, 0.5)
            except queue.Empty:
                continue
            if muted_at_capture:
                if not was_muted:
                    was_muted = True
                    try:
                        await session.send_realtime_input(activity_end=genai_types.ActivityEnd())
                    except Exception:
                        if self._stop_evt.is_set():
                            break
                continue
            if was_muted:
                was_muted = False
                try:
                    await session.send_realtime_input(activity_start=genai_types.ActivityStart())
                except Exception:
                    if self._stop_evt.is_set():
                        break
            try:
                await session.send_realtime_input(
                    audio=genai_types.Blob(data=data, mime_type=f"audio/pcm;rate={send_rate}")
                )
            except Exception:
                # Session likely closing -- let the outer loop's stop
                # check handle shutdown cleanly on the next iteration.
                if self._stop_evt.is_set():
                    break
        # Shutting down mid-turn (e.g. Stop clicked while still talking
        # or a PTT key still held) -- close the turn out so Gemini
        # doesn't process a hanging "still listening" state.
        if not was_muted:
            try:
                await session.send_realtime_input(activity_end=genai_types.ActivityEnd())
            except Exception:
                pass

    async def _screen_share_task(self, session):
        """While self._screen_share_evt is set (toggled by start_screen_share() /
        stop_screen_share()), grab a downscaled JPEG screenshot roughly every
        1/GEMINI_LIVE_SCREEN_FPS seconds and push it into the live session as
        a realtime video frame -- Gemini's Live API is happy to treat a slow
        still-frame stream as "video", which is enough for the model to
        actually see the desktop live while talking. Screen capture is a
        blocking call (ImageGrab/scrot under the hood), so it's run in the
        default executor to avoid blocking the event loop that also drives
        mic streaming and audio playback.
        """
        loop = asyncio.get_event_loop()
        fps = max(0.1, float(getattr(config, "GEMINI_LIVE_SCREEN_FPS", 1.0)))
        interval = 1.0 / fps
        max_w = int(getattr(config, "GEMINI_LIVE_SCREEN_MAX_W", 1024))
        quality = int(getattr(config, "GEMINI_LIVE_SCREEN_JPEG_QUALITY", 70))

        while not self._stop_evt.is_set():
            if not self._screen_share_evt.is_set():
                # Not sharing right now -- poll cheaply instead of busy-looping,
                # so start_screen_share() takes effect within ~0.3s of being called.
                await asyncio.sleep(0.3)
                continue
            try:
                jpeg_bytes = await loop.run_in_executor(
                    None, capture_screen_frame_jpeg_bytes, max_w, quality
                )
                await session.send_realtime_input(
                    video=genai_types.Blob(data=jpeg_bytes, mime_type="image/jpeg")
                )
            except Exception as e:
                if self._stop_evt.is_set():
                    break
                self.on_event("voice_error", {"message": f"Screen share frame failed: {e}"})
                # Back off briefly rather than hammering a failing capture/send path.
                await asyncio.sleep(1.0)
                continue
            await asyncio.sleep(interval)

    async def _recv_task(self, session):
        """Receive audio/text/tool-call events from Gemini and act on them."""
        recv_rate = getattr(config, "GEMINI_LIVE_RECV_RATE", 24000)
        out_stream = sd.RawOutputStream(samplerate=recv_rate, dtype="int16", channels=1)
        out_stream.start()
        try:
            while not self._stop_evt.is_set():
                turn = session.receive()
                async for response in turn:
                    if self._stop_evt.is_set():
                        break

                    sc = getattr(response, "server_content", None)
                    if sc is not None:
                        in_tx = getattr(sc, "input_transcription", None)
                        if in_tx and getattr(in_tx, "text", None):
                            self.on_event("voice_transcript", {"role": "user", "text": in_tx.text})

                        out_tx = getattr(sc, "output_transcription", None)
                        if out_tx and getattr(out_tx, "text", None):
                            self.on_event("voice_transcript", {"role": "assistant", "text": out_tx.text})

                        model_turn = getattr(sc, "model_turn", None)
                        if model_turn is not None:
                            for part in (model_turn.parts or []):
                                inline = getattr(part, "inline_data", None)
                                if inline is not None and inline.data:
                                    out_stream.write(inline.data)

                        if getattr(sc, "interrupted", False):
                            self.on_event("voice_interrupted", {})

                        if getattr(sc, "turn_complete", False):
                            self.on_event("voice_turn_complete", {})

                    tool_call = getattr(response, "tool_call", None)
                    if tool_call is not None:
                        responses = []
                        for fc in (tool_call.function_calls or []):
                            args = dict(fc.args or {})
                            self.on_event("voice_tool_call", {"name": fc.name, "args": args})

                            # ── Permission enforcement -- mirrors orchestration.py's
                            # process_chat_turn dispatch loop exactly, so a tool set to
                            # "Ask" or "Don't Allow" in the Permissions tab is gated the
                            # same way here as it is in text chat. MCP tools are keyed
                            # per-server-per-tool, same convention as text mode.
                            from permissions import enforce_tool_permission, mcp_permission_key
                            perm_key = fc.name
                            if fc.name == "call_mcp_tool":
                                perm_key = mcp_permission_key(
                                    str(args.get("server", "")), str(args.get("tool_name", ""))
                                )
                            # enforce_tool_permission's "ask" level pops a blocking
                            # Approve/Decline dialog (the same ask_user_approval tool
                            # text mode uses) and waits for the user's answer. Running
                            # that synchronously here would stall this coroutine's event
                            # loop -- which _mic_drain_task and audio playback also run
                            # on -- so it's offloaded to a worker thread via
                            # asyncio.to_thread instead of awaited inline.
                            perm_decision, perm_blocked_msg = await asyncio.to_thread(
                                enforce_tool_permission, perm_key, fc.name, args
                            )

                            if perm_decision == "blocked":
                                result = perm_blocked_msg
                            else:
                                try:
                                    from gui.dispatch import _dispatch_midum_tool
                                    result = _dispatch_midum_tool(fc.name, args)
                                except Exception as e:
                                    result = f"Error executing '{fc.name}': {e}"
                            result_str = str(result)
                            self.on_event("voice_tool_result", {
                                "name": fc.name,
                                "result": result_str[:2000],
                            })
                            responses.append(
                                genai_types.FunctionResponse(
                                    id=fc.id, name=fc.name, response={"result": result_str}
                                )
                            )
                        if responses:
                            await session.send_tool_response(function_responses=responses)
        finally:
            try:
                out_stream.stop()
                out_stream.close()
            except Exception:
                pass


# ── Module-level singleton so app.py's Api class doesn't juggle its own
#    background-thread lifecycle -- one VoiceSession per process. ───────────
_voice_session_instance = None


def get_voice_session(on_event) -> VoiceSession:
    global _voice_session_instance
    if _voice_session_instance is None:
        _voice_session_instance = VoiceSession(on_event)
    return _voice_session_instance


# ── Tool-facing entry points ────────────────────────────────────────────────
# These are what the start_screen_share/stop_screen_share tool schemas in
# tools_schema.py actually call, via _dispatch_midum_tool's generic fallback
# (hasattr(midum, tool_name) -> getattr(midum, tool_name)(**args)). They act
# on the single running VoiceSession singleton rather than needing the model
# to hold a session handle -- there's only ever one live voice session per
# process, exactly like start()/stop() on the Voice tab itself.
def start_screen_share() -> str:
    if _voice_session_instance is None:
        return "Cannot start screen share: no voice session has been started yet."
    return _voice_session_instance.start_screen_share()


def stop_screen_share() -> str:
    if _voice_session_instance is None:
        return "Cannot stop screen share: no voice session has been started yet."
    return _voice_session_instance.stop_screen_share()
