"""
Twilio-based outbound phone calling for Midum.
=============================================================================
Lets Midum place a real phone call to any number and carry on a two-way
SPOKEN conversation — Twilio handles the actual telephony (dialing, call
audio) and speech-to-text/text-to-speech at the edge; Midum supplies the
conversational brain via the official Gemini API (same key/chain already
used by consult_gemini_api), so no separate LLM wiring is needed.

ARCHITECTURE
------------
  1. Twilio needs a PUBLIC https URL it can POST to for call instructions
     (TwiML) and speech results. Since Midum normally runs on someone's
     desktop with no public IP, this module spins up a tiny local Flask
     server and tunnels it to the internet with pyngrok — zero manual
     port-forwarding/deployment needed. If you already have a stable public
     URL (a real deployment, a reserved ngrok domain, etc.) set
     PUBLIC_BASE_URL in the secrets file and the ngrok tunnel is skipped.
  2. call_phone() creates a call session (transcript + objective), starts
     the webhook server if it isn't already running, and places the call
     via Twilio's REST API pointed at that session's /voice/<call_id> URL.
  3. Twilio calls /voice/<call_id> the moment the person picks up → we
     reply with TwiML: <Say> the opening line, then <Gather input="speech">
     to listen for their reply.
  4. Twilio POSTs the transcribed speech to /gather/<call_id> → we hand the
     transcript + objective + running history to consult_gemini_api(), get
     back what Midum should say next, and reply with more TwiML (<Say> +
     another <Gather>, looping the conversation) until the model decides
     the call is done (see _HANGUP_TOKEN) or the person stops responding.
  5. The full transcript is kept in memory (_CALL_SESSIONS) and can be
     pulled with get_phone_call_transcript()/get_phone_call_status() at any
     point, including after the call ends.

SETUP (one-time)
-----------------
  1. pip install twilio flask pyngrok
  2. Create a free Twilio account: https://www.twilio.com/try-twilio
     - Buy/claim a phone number capable of voice (trial accounts get one
       free number and can only call verified numbers until upgraded).
     - Grab Account SID + Auth Token from the Twilio Console dashboard.
  3. Add to the SAME secrets file used everywhere else in Midum
     (see config.SECRETS_FILE):
        {
          "TWILIO_ACCOUNT_SID": "AC...",
          "TWILIO_AUTH_TOKEN":  "...",
          "TWILIO_FROM_NUMBER": "+1xxxxxxxxxx",
          "NGROK_AUTH_TOKEN":   "..."   (free, from https://dashboard.ngrok.com)
        }
     NGROK_AUTH_TOKEN is optional for short test calls (anonymous ngrok
     tunnels work but are rate-limited/short-lived) but strongly
     recommended for anything real.
  4. Optionally set "PUBLIC_BASE_URL" in the same secrets file (e.g.
     "https://midum.example.com") to skip ngrok entirely and use your own
     reachable server instead.

Nothing here touches the local machine's default audio devices or Midum's
own microphone pipeline — this is entirely server-side telephony through
Twilio's own infrastructure.
"""

import json
import os
import threading
import time
import uuid

import config
from config import SECRETS_FILE

# ── Optional dependencies — loaded lazily so the rest of Midum works fine
# on machines that never install the telephony extras. ──────────────────────
try:
    from twilio.rest import Client as _TwilioClient
    from twilio.twiml.voice_response import VoiceResponse as _VoiceResponse, Gather as _Gather
    _TWILIO_SDK_AVAILABLE = True
except ImportError:
    _TwilioClient = None
    _VoiceResponse = None
    _Gather = None
    _TWILIO_SDK_AVAILABLE = False

try:
    from flask import Flask as _Flask, request as _flask_request, Response as _FlaskResponse
    _FLASK_AVAILABLE = True
except ImportError:
    _Flask = None
    _flask_request = None
    _FlaskResponse = None
    _FLASK_AVAILABLE = False

try:
    from pyngrok import ngrok as _ngrok
    _NGROK_AVAILABLE = True
except ImportError:
    _ngrok = None
    _NGROK_AVAILABLE = False


# =============================================================================
# CREDENTIALS
# =============================================================================
_TWILIO_AVAILABLE   = False
_TWILIO_ACCOUNT_SID = None
_TWILIO_AUTH_TOKEN  = None
_TWILIO_FROM_NUMBER = None
_NGROK_AUTH_TOKEN   = None
_PUBLIC_BASE_URL    = None   # user-supplied, skips ngrok if set
_twilio_client      = None


def _load_twilio_creds():
    """Load Twilio + ngrok credentials from the shared secrets file."""
    global _TWILIO_AVAILABLE, _TWILIO_ACCOUNT_SID, _TWILIO_AUTH_TOKEN
    global _TWILIO_FROM_NUMBER, _NGROK_AUTH_TOKEN, _PUBLIC_BASE_URL, _twilio_client

    if not _TWILIO_SDK_AVAILABLE:
        return False, "twilio package not installed. Run: pip install twilio"
    if not _FLASK_AVAILABLE:
        return False, "flask package not installed. Run: pip install flask"

    try:
        secrets_path = os.path.abspath(SECRETS_FILE)
        if not os.path.exists(secrets_path):
            return False, f"Secrets file not found: {secrets_path}"
        with open(secrets_path, "r", encoding="utf-8") as f:
            secrets = json.load(f)

        sid   = secrets.get("TWILIO_ACCOUNT_SID", "").strip()
        token = secrets.get("TWILIO_AUTH_TOKEN", "").strip()
        from_number = secrets.get("TWILIO_FROM_NUMBER", "").strip()
        if not (sid and token and from_number):
            return False, (
                "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER "
                f"must all be set in {secrets_path}"
            )

        _TWILIO_ACCOUNT_SID = sid
        _TWILIO_AUTH_TOKEN  = token
        _TWILIO_FROM_NUMBER = from_number
        _NGROK_AUTH_TOKEN   = secrets.get("NGROK_AUTH_TOKEN", "").strip()
        _PUBLIC_BASE_URL    = secrets.get("PUBLIC_BASE_URL", "").strip().rstrip("/")

        _twilio_client = _TwilioClient(sid, token)
        _TWILIO_AVAILABLE = True
        return True, "OK"
    except Exception as e:
        return False, str(e)


_twilio_load_ok, _twilio_load_msg = _load_twilio_creds()


# =============================================================================
# CALL SESSION STATE
# =============================================================================
# call_id (our own short id, embedded in the webhook URL) -> session dict.
# Keyed separately from Twilio's own CallSid because we need the id BEFORE
# Twilio hands us a CallSid (the webhook URL has to be ready at call-create
# time). Once Twilio calls back we also index by call_sid for convenience.
_CALL_SESSIONS: dict = {}
_CALL_SESSIONS_LOCK = threading.RLock()

_HANGUP_TOKEN = "[[HANGUP]]"   # the model appends this when the call should end

_WEBHOOK_PORT   = 8731
_flask_app      = None
_flask_thread   = None
_ngrok_tunnel   = None
_server_lock    = threading.RLock()


def _new_session(phone_number: str, opening_message: str, objective: str, voice: str) -> str:
    call_id = uuid.uuid4().hex[:16]
    with _CALL_SESSIONS_LOCK:
        _CALL_SESSIONS[call_id] = {
            "call_id": call_id,
            "call_sid": None,
            "phone_number": phone_number,
            "objective": objective or "Have a brief, polite conversation and relay any information you're asked to relay.",
            "opening_message": opening_message,
            "voice": voice or "Polly.Matthew",
            "transcript": [],          # list of {"role": "assistant"|"caller", "text": ...}
            "status": "initiating",    # initiating -> ringing -> in-progress -> completed/failed
            "ended": False,
            "no_input_strikes": 0,
            "created_at": time.time(),
        }
    return call_id


def _get_session(call_id: str):
    with _CALL_SESSIONS_LOCK:
        return _CALL_SESSIONS.get(call_id)


def _append_turn(call_id: str, role: str, text: str):
    with _CALL_SESSIONS_LOCK:
        sess = _CALL_SESSIONS.get(call_id)
        if sess is not None:
            sess["transcript"].append({"role": role, "text": text})


# =============================================================================
# CONVERSATION BRAIN — reuses the existing Gemini API consult path
# =============================================================================
def _generate_next_line(sess: dict, caller_said: str) -> str:
    """
    Ask the Gemini API for Midum's next spoken line, given the call's
    objective and the transcript so far. The model is told to append
    _HANGUP_TOKEN once the objective is satisfied / it's time to end the
    call — that token is stripped before speaking and used to trigger a
    real <Hangup/>.
    """
    from providers.gemini_api_backend import consult_gemini_api

    history_lines = []
    for turn in sess["transcript"]:
        speaker = "Midum" if turn["role"] == "assistant" else "Caller"
        history_lines.append(f"{speaker}: {turn['text']}")
    history_text = "\n".join(history_lines) if history_lines else "(call just connected)"

    prompt = (
        "You are Midum, an AI assistant currently on a live PHONE CALL on behalf "
        "of your user. You are speaking out loud through text-to-speech — keep "
        "every reply short (1-3 sentences), natural, and conversational, the way "
        "a real person talks on the phone. Never use markdown, bullet points, or "
        "any formatting — plain spoken sentences only.\n\n"
        f"OBJECTIVE FOR THIS CALL: {sess['objective']}\n\n"
        f"CALL TRANSCRIPT SO FAR:\n{history_text}\n\n"
        f"The other person on the line just said: \"{caller_said}\"\n\n"
        "Reply with EXACTLY what you should say next, nothing else. "
        f"If the objective is now complete, or the person wants to end the "
        f"call, say a brief natural goodbye and append the literal token "
        f"{_HANGUP_TOKEN} at the very end of your reply (on the same line)."
    )

    try:
        reply = consult_gemini_api(prompt)
        # consult_gemini_api prefixes replies with "[Gemini-API/<model>]\n"
        if reply.startswith("["):
            reply = reply.split("\n", 1)[-1].strip()
        return reply.strip() or f"Sorry, could you say that again? {_HANGUP_TOKEN}"
    except Exception as e:
        return f"Sorry, I'm having trouble right now. Goodbye. {_HANGUP_TOKEN}"


# =============================================================================
# FLASK WEBHOOK SERVER
# =============================================================================
def _build_flask_app():
    app = _Flask("midum_twilio_voice")

    @app.route("/voice/<call_id>", methods=["POST", "GET"])
    def _voice(call_id):
        sess = _get_session(call_id)
        vr = _VoiceResponse()
        if sess is None:
            vr.say("Sorry, this call session is no longer valid. Goodbye.")
            vr.hangup()
            return _FlaskResponse(str(vr), mimetype="text/xml")

        sess["status"] = "in-progress"
        opening = sess["opening_message"]
        _append_turn(call_id, "assistant", opening)

        gather = _Gather(
            input="speech",
            action=f"/gather/{call_id}",
            method="POST",
            speech_timeout="auto",
            timeout=6,
        )
        gather.say(opening, voice=sess["voice"])
        vr.append(gather)
        # Fires if the Gather times out with no speech at all
        vr.redirect(f"/no_input/{call_id}", method="POST")
        return _FlaskResponse(str(vr), mimetype="text/xml")

    @app.route("/gather/<call_id>", methods=["POST"])
    def _gather(call_id):
        sess = _get_session(call_id)
        vr = _VoiceResponse()
        if sess is None:
            vr.hangup()
            return _FlaskResponse(str(vr), mimetype="text/xml")

        if sess.get("call_sid") is None:
            sess["call_sid"] = _flask_request.form.get("CallSid")

        caller_said = (_flask_request.form.get("SpeechResult") or "").strip()
        if not caller_said:
            return _no_input_response(call_id, vr)

        sess["no_input_strikes"] = 0
        _append_turn(call_id, "caller", caller_said)

        reply = _generate_next_line(sess, caller_said)
        hang_up = _HANGUP_TOKEN in reply
        spoken = reply.replace(_HANGUP_TOKEN, "").strip()
        _append_turn(call_id, "assistant", spoken)

        if hang_up:
            vr.say(spoken, voice=sess["voice"])
            vr.hangup()
            sess["status"] = "completed"
            sess["ended"] = True
        else:
            gather = _Gather(
                input="speech",
                action=f"/gather/{call_id}",
                method="POST",
                speech_timeout="auto",
                timeout=6,
            )
            gather.say(spoken, voice=sess["voice"])
            vr.append(gather)
            vr.redirect(f"/no_input/{call_id}", method="POST")
        return _FlaskResponse(str(vr), mimetype="text/xml")

    def _no_input_response(call_id, vr):
        sess = _get_session(call_id)
        sess["no_input_strikes"] = sess.get("no_input_strikes", 0) + 1
        if sess["no_input_strikes"] >= 2:
            vr.say("I didn't catch a response, so I'll let you go. Goodbye.", voice=sess["voice"])
            vr.hangup()
            sess["status"] = "completed"
            sess["ended"] = True
            return _FlaskResponse(str(vr), mimetype="text/xml")
        gather = _Gather(
            input="speech",
            action=f"/gather/{call_id}",
            method="POST",
            speech_timeout="auto",
            timeout=6,
        )
        gather.say("Sorry, I didn't catch that — could you repeat it?", voice=sess["voice"])
        vr.append(gather)
        vr.redirect(f"/no_input/{call_id}", method="POST")
        return _FlaskResponse(str(vr), mimetype="text/xml")

    @app.route("/no_input/<call_id>", methods=["POST"])
    def _no_input(call_id):
        vr = _VoiceResponse()
        return _no_input_response(call_id, vr)

    @app.route("/status/<call_id>", methods=["POST"])
    def _status(call_id):
        sess = _get_session(call_id)
        if sess is not None:
            call_status = _flask_request.form.get("CallStatus", "")
            if call_status:
                sess["status"] = call_status
                if call_status in ("completed", "busy", "failed", "no-answer", "canceled"):
                    sess["ended"] = True
        return ("", 204)

    return app


def _ensure_webhook_server_running() -> tuple[bool, str]:
    """
    Idempotent: starts the Flask webhook server (in a daemon thread) and an
    ngrok tunnel to it the first time this is called; every subsequent call
    just returns the already-running public base URL. Returns (ok, base_url_or_error).
    """
    global _flask_app, _flask_thread, _ngrok_tunnel

    with _server_lock:
        if _PUBLIC_BASE_URL:
            return True, _PUBLIC_BASE_URL

        if _flask_thread is not None and _flask_thread.is_alive() and _ngrok_tunnel is not None:
            return True, _ngrok_tunnel.public_url

        if not _NGROK_AVAILABLE:
            return False, "pyngrok not installed and no PUBLIC_BASE_URL set. Run: pip install pyngrok"

        if _flask_app is None:
            _flask_app = _build_flask_app()

        if _flask_thread is None or not _flask_thread.is_alive():
            def _run():
                # use_reloader=False is required — Flask's reloader spawns a
                # second process, which would silently break the in-memory
                # _CALL_SESSIONS dict (the reloaded process starts empty).
                _flask_app.run(host="127.0.0.1", port=_WEBHOOK_PORT, debug=False, use_reloader=False)
            _flask_thread = threading.Thread(target=_run, daemon=True, name="midum-twilio-webhook")
            _flask_thread.start()
            time.sleep(1.0)   # give Flask a moment to bind before ngrok connects to it

        try:
            if _NGROK_AUTH_TOKEN:
                _ngrok.set_auth_token(_NGROK_AUTH_TOKEN)
            _ngrok_tunnel = _ngrok.connect(_WEBHOOK_PORT, "http")
            return True, _ngrok_tunnel.public_url
        except Exception as e:
            return False, f"ngrok tunnel failed to start: {e}"


# =============================================================================
# PUBLIC TOOL FUNCTIONS
# =============================================================================
def call_phone(phone_number: str, opening_message: str, objective: str = "",
                voice: str = "Polly.Matthew") -> str:
    """
    Place an outbound phone call to `phone_number` and hold a real two-way
    SPOKEN conversation with whoever answers.

    phone_number     — E.164 format, e.g. "+15551234567" (include country code).
    opening_message  — the first thing Midum says once the call connects.
    objective        — what Midum is trying to accomplish on this call
                        (e.g. "Ask if tomorrow's 3pm dentist appointment can be
                        moved to Friday, and confirm whichever time works").
                        Midum keeps talking naturally, guided by this goal,
                        until it's satisfied or the other person ends the call.
    voice            — Twilio TTS voice name (default "Polly.Matthew"; see
                        https://www.twilio.com/docs/voice/twiml/say/text-speech
                        for the full list, e.g. "Polly.Joanna", "man", "woman").

    Returns a status string including the call_id to use with
    get_phone_call_status() / get_phone_call_transcript() / end_phone_call().
    The call happens asynchronously — this returns as soon as Twilio accepts
    the call request, not when the call finishes.
    """
    if not _TWILIO_AVAILABLE:
        return f"Error: Twilio is not configured — {_twilio_load_msg}"

    phone_number = (phone_number or "").strip()
    if not phone_number:
        return "Error: 'phone_number' is required (E.164 format, e.g. +15551234567)."
    if not phone_number.startswith("+"):
        return (
            f"Error: phone_number must be in E.164 format starting with '+' and "
            f"the country code (got '{phone_number}'). Example: +15551234567."
        )
    opening_message = (opening_message or "").strip()
    if not opening_message:
        return "Error: 'opening_message' is required — what Midum should say when the call connects."

    ok, base_url_or_err = _ensure_webhook_server_running()
    if not ok:
        return f"Error: could not start the call webhook server — {base_url_or_err}"
    base_url = base_url_or_err

    call_id = _new_session(phone_number, opening_message, objective, voice)

    try:
        call = _twilio_client.calls.create(
            to=phone_number,
            from_=_TWILIO_FROM_NUMBER,
            url=f"{base_url}/voice/{call_id}",
            status_callback=f"{base_url}/status/{call_id}",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
        )
    except Exception as e:
        with _CALL_SESSIONS_LOCK:
            _CALL_SESSIONS.pop(call_id, None)
        return f"Error: Twilio call failed to initiate: {e}"

    with _CALL_SESSIONS_LOCK:
        _CALL_SESSIONS[call_id]["call_sid"] = call.sid
        _CALL_SESSIONS[call_id]["status"] = "initiating"

    return (
        f"Success: calling {phone_number} now (call_id='{call_id}', "
        f"twilio_call_sid='{call.sid}'). The call is live/in-progress — use "
        f"get_phone_call_status('{call_id}') to check on it, or "
        f"get_phone_call_transcript('{call_id}') to read what's been said so far."
    )


def get_phone_call_status(call_id: str) -> str:
    """Check the current status and last few transcript lines of a call started with call_phone()."""
    sess = _get_session(call_id)
    if sess is None:
        return f"Error: no call session found for call_id='{call_id}'."
    last_lines = sess["transcript"][-4:]
    lines_text = "\n".join(
        f"  {'Midum' if t['role'] == 'assistant' else 'Caller'}: {t['text']}"
        for t in last_lines
    ) or "  (nothing said yet)"
    return (
        f"Call to {sess['phone_number']} — status: {sess['status']}"
        f"{' (ended)' if sess['ended'] else ''}\n"
        f"Recent lines:\n{lines_text}"
    )


def get_phone_call_transcript(call_id: str) -> str:
    """Return the FULL transcript so far of a call started with call_phone()."""
    sess = _get_session(call_id)
    if sess is None:
        return f"Error: no call session found for call_id='{call_id}'."
    if not sess["transcript"]:
        return f"No transcript yet for call to {sess['phone_number']} (status: {sess['status']})."
    lines = [
        f"{'Midum' if t['role'] == 'assistant' else 'Caller'}: {t['text']}"
        for t in sess["transcript"]
    ]
    return (
        f"Transcript — call to {sess['phone_number']} (status: {sess['status']}):\n\n"
        + "\n".join(lines)
    )


def end_phone_call(call_id: str) -> str:
    """Immediately hang up an in-progress call started with call_phone()."""
    if not _TWILIO_AVAILABLE:
        return f"Error: Twilio is not configured — {_twilio_load_msg}"
    sess = _get_session(call_id)
    if sess is None:
        return f"Error: no call session found for call_id='{call_id}'."
    if sess.get("ended"):
        return f"Call to {sess['phone_number']} has already ended (status: {sess['status']})."
    if not sess.get("call_sid"):
        return "Error: call has no Twilio CallSid yet (it may not have connected). Try again shortly."
    try:
        _twilio_client.calls(sess["call_sid"]).update(status="completed")
        sess["status"] = "completed"
        sess["ended"] = True
        return f"Success: ended the call to {sess['phone_number']}."
    except Exception as e:
        return f"Error hanging up: {e}"


def list_phone_calls() -> str:
    """List all phone call sessions from this run (active and finished), most recent first."""
    with _CALL_SESSIONS_LOCK:
        sessions = sorted(_CALL_SESSIONS.values(), key=lambda s: s["created_at"], reverse=True)
    if not sessions:
        return "No phone calls have been placed yet."
    lines = ["Phone calls this session:", ""]
    for s in sessions:
        lines.append(
            f"  call_id='{s['call_id']}'  {s['phone_number']}  "
            f"status={s['status']}{' (ended)' if s['ended'] else ''}  "
            f"objective={s['objective'][:60]!r}"
        )
    return "\n".join(lines)
