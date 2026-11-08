"""System-audio (loopback) capture for Live Notes: "what the computer is playing", not the microphone.

Delivers 16 kHz mono int16 PCM chunks to a callback.  Backends are tried in order (config.LIVENOTES_AUDIO_BACKEND
= "auto" | "soundcard" | "pyaudiowpatch" | "sounddevice"):

  soundcard      pip install soundcard         Windows (WASAPI loopback) and Linux (PulseAudio monitor)
  pyaudiowpatch  pip install pyaudiowpatch     Windows only (WASAPI loopback)
  sounddevice    already installed             any input device named like "Stereo Mix" / "Monitor" /
                                               "BlackHole" / "Loopback" (macOS needs BlackHole or similar)

Everything is imported lazily, so a missing backend never breaks Midum -- start() just returns an error string.
"""
import queue
import threading
import time

try:
    import numpy as np
except ImportError:          # numpy is in requirements.txt; guard anyway
    np = None

TARGET_RATE = 16000
CHUNK_SECONDS = 0.1
_LOOPBACK_WORDS = ("stereo mix", "monitor", "blackhole", "soundflower", "loopback", "what u hear", "wave out")


def _gcd(a, b):
    while b:
        a, b = b, a % b
    return a


def to_mono_16k(block, src_rate, dst_rate=TARGET_RATE):
    """float32/int16 samples (n,) or (n, ch) at src_rate -> little-endian int16 bytes, mono, dst_rate."""
    x = np.asarray(block)
    if x.dtype == np.int16:
        x = x.astype(np.float32) / 32768.0
    else:
        x = x.astype(np.float32)
    if x.ndim == 2:
        x = x.mean(axis=1)
    if src_rate != dst_rate and len(x):
        try:
            from scipy.signal import resample_poly
            g = _gcd(int(src_rate), int(dst_rate))
            x = resample_poly(x, int(dst_rate) // g, int(src_rate) // g).astype(np.float32)
        except ImportError:
            if src_rate % dst_rate == 0:                      # integer decimation: average (cheap low-pass)
                k = int(src_rate // dst_rate)
                x = x[: len(x) // k * k].reshape(-1, k).mean(axis=1)
            else:
                n_out = int(len(x) * dst_rate / src_rate)
                x = np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x).astype(np.float32)
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


def rms_int16(pcm):
    if not pcm or np is None:
        return 0.0
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if len(a) else 0.0


def available_backends():
    """Which loopback backends can be imported here (for diagnostics)."""
    out = {}
    for name in ("soundcard", "pyaudiowpatch", "sounddevice"):
        try:
            __import__(name)
            out[name] = True
        except Exception:
            out[name] = False
    return out


class SystemAudioCapture:
    def __init__(self, backend="auto"):
        self.backend_pref = (backend or "auto").lower()
        self.backend = ""
        self._thread = None
        self._stop = threading.Event()
        self._cleanup = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def start(self, on_chunk):
        """Start capturing. Returns "" on success, otherwise a human-readable reason it could not start."""
        if np is None:
            return "numpy is not installed (pip install numpy)"
        if self.running:
            return ""
        self._stop.clear()
        order = ["soundcard", "pyaudiowpatch", "sounddevice"] if self.backend_pref == "auto" else [self.backend_pref]
        problems = []
        for name in order:
            try:
                starter = getattr(self, "_start_" + name, None)
                if starter is None:
                    problems.append(f"unknown backend '{name}'")
                    continue
                err = starter(on_chunk)
                if not err:
                    self.backend = name
                    return ""
                problems.append(f"{name}: {err}")
            except Exception as e:               # never let a driver problem escape
                problems.append(f"{name}: {e}")
        hint = ("Install one of: 'pip install soundcard' (Windows/Linux) or 'pip install pyaudiowpatch' (Windows). "
                "On macOS install BlackHole and route the output through it.")
        return "no system-audio backend worked (" + "; ".join(problems) + "). " + hint

    def stop(self):
        self._stop.set()
        cb, self._cleanup = self._cleanup, None
        if cb:
            try:
                cb()
            except Exception:
                pass

    # ── soundcard ────────────────────────────────────────────────────────────────────────
    def _start_soundcard(self, on_chunk):
        import soundcard as sc
        speaker = sc.default_speaker()
        mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        box = {}
        ready = threading.Event()

        def run():
            for rate, ch in ((TARGET_RATE, 1), (48000, None)):
                try:
                    with mic.recorder(samplerate=rate, channels=ch) as rec:
                        box["ok"] = True
                        ready.set()
                        frames = int(rate * CHUNK_SECONDS)
                        while not self._stop.is_set():
                            data = rec.record(numframes=frames)     # may block while nothing is playing
                            if self._stop.is_set():
                                return
                            pcm = to_mono_16k(data, rate)
                            if pcm:
                                on_chunk(pcm)
                    return
                except Exception as e:
                    box["err"] = str(e)
                    if box.get("ok"):
                        return
            ready.set()

        self._thread = threading.Thread(target=run, daemon=True, name="livenotes-audio")
        self._thread.start()
        ready.wait(5.0)
        if not box.get("ok"):
            self._stop.set()
            return box.get("err") or f"could not open loopback of '{speaker.name}'"
        return ""

    # ── pyaudiowpatch (callback mode: silence never blocks us) ───────────────────────────
    def _start_pyaudiowpatch(self, on_chunk):
        import pyaudiowpatch as pa
        p = pa.PyAudio()
        try:
            wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
            dev = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
            if not dev.get("isLoopbackDevice"):
                for lb in p.get_loopback_device_info_generator():
                    if dev["name"] in lb["name"]:
                        dev = lb
                        break
                else:
                    raise RuntimeError("no loopback device for the default output")
            rate, ch = int(dev["defaultSampleRate"]), int(dev["maxInputChannels"])

            def cb(in_data, frame_count, time_info, status):
                try:
                    a = np.frombuffer(in_data, dtype=np.int16).reshape(-1, ch)
                    on_chunk(to_mono_16k(a, rate))
                except Exception:
                    pass
                return (None, pa.paContinue)

            stream = p.open(format=pa.paInt16, channels=ch, rate=rate, input=True,
                            frames_per_buffer=int(rate * CHUNK_SECONDS), input_device_index=dev["index"],
                            stream_callback=cb)
        except Exception:
            p.terminate()
            raise

        def cleanup():
            try:
                stream.stop_stream()
                stream.close()
            finally:
                p.terminate()

        self._cleanup = cleanup
        self._thread = threading.Thread(target=self._idle_until_stop, daemon=True, name="livenotes-audio")
        self._thread.start()
        return ""

    # ── sounddevice (monitor / Stereo Mix / BlackHole style devices) ─────────────────────
    def _start_sounddevice(self, on_chunk):
        import sounddevice as sd
        pick = None
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0 and any(w in d["name"].lower() for w in _LOOPBACK_WORDS):
                pick = (i, d)
                break
        if pick is None:
            return "no loopback-style input device found (e.g. 'Stereo Mix', 'Monitor of ...', 'BlackHole')"
        idx, d = pick
        rate, ch = int(d["default_samplerate"]), max(1, min(2, int(d["max_input_channels"])))

        def cb(indata, frames, t, status):
            try:
                on_chunk(to_mono_16k(indata, rate))
            except Exception:
                pass

        stream = sd.InputStream(device=idx, samplerate=rate, channels=ch, dtype="int16",
                                blocksize=int(rate * CHUNK_SECONDS), callback=cb)
        stream.start()

        def cleanup():
            stream.stop()
            stream.close()

        self._cleanup = cleanup
        self._thread = threading.Thread(target=self._idle_until_stop, daemon=True, name="livenotes-audio")
        self._thread.start()
        return ""

    def _idle_until_stop(self):
        while not self._stop.is_set():
            time.sleep(0.25)
