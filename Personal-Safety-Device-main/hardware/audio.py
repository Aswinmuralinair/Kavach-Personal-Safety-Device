"""
hardware/audio.py  —  Project Kavach
Sound event detection using Google's YAMNet model (521 AudioSet classes).

Detects:    Screaming · Gunshot · Explosion · Smash/Crash · Glass breaking
            Crying/Sobbing · Shout · and more — fully configurable.

NOT keyword spotting — this listens for ENVIRONMENTAL DANGER SOUNDS.

Works on:
    Windows (ROG G14 / any laptop)  — for development & testing
    Raspberry Pi 4                  — for production
    Same code, zero changes needed between the two.

Plug-and-play pattern (same as sensors.py):
    AudioManager tries YAMNet + real microphone first.
    Falls back to FakeAudioDetector if model or mic is unavailable.

─────────────────────────────────────────────────────────────────────────────
SETUP — run this ONCE before first use:
    python setup_audio.py
That script downloads yamnet.tflite and yamnet_class_map.csv into models/.
─────────────────────────────────────────────────────────────────────────────

Dependencies:
    pip install sounddevice numpy

On Pi (instead of full tensorflow):
    pip install tflite-runtime

On Windows/laptop (full TensorFlow):
    pip install tensorflow
"""

import os
import csv
import time
import threading
import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

MODEL_PATH     = os.path.join("models", "yamnet.tflite")
CLASS_MAP_PATH = os.path.join("models", "yamnet_class_map.csv")

# ─────────────────────────────────────────────────────────────────────────────
# Tunable constants
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_RATE        = 16000
WINDOW_SECONDS     = 1.0
WINDOW_SAMPLES     = int(SAMPLE_RATE * WINDOW_SECONDS)
OVERLAP_SECONDS    = 0.5
OVERLAP_SAMPLES    = int(SAMPLE_RATE * OVERLAP_SECONDS)
HOP_SAMPLES        = WINDOW_SAMPLES - OVERLAP_SAMPLES

DETECTION_THRESHOLD = 0.45
COOLDOWN_SECONDS    = 15

# ─────────────────────────────────────────────────────────────────────────────
# Danger sound catalogue — YAMNet class name → alert category
# ─────────────────────────────────────────────────────────────────────────────

DANGER_SOUNDS = {
    # Human distress
    "screaming":                    "distress",
    "shout":                        "distress",
    "crying, sobbing":              "distress",
    "whimper":                      "distress",
    "whoop":                        "distress",

    # Weapons / explosions
    "gunshot, gunfire":             "weapon",
    "machine gun":                  "weapon",
    "fusillade":                    "weapon",
    "artillery fire":               "weapon",
    "cap gun":                      "weapon",
    "explosion":                    "weapon",
    "burst, pop":                   "weapon",
    "bang":                         "impact",

    # Physical impact / accident
    "smash, crash":                 "impact",
    "glass":                        "impact",
    "crashing":                     "impact",
    "thud":                         "impact",
    "slam":                         "impact",
    "whack, thwack":                "impact",
    "slap, smack":                  "impact",

    # Fire / alarms
    "smoke detector, smoke alarm":  "fire",
    "fire alarm":                   "fire",
    "alarm":                        "fire",
    "siren":                        "fire",
    "civil defense siren":          "fire",
}

SOS_TRIGGER_CATEGORIES = {"distress", "weapon", "impact", "fire"}


# ─────────────────────────────────────────────────────────────────────────────
# Detection event
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DetectionEvent:
    sound_class:        str
    category:           str
    confidence:         float
    should_trigger_sos: bool


# ─────────────────────────────────────────────────────────────────────────────
# Abstract base
# ─────────────────────────────────────────────────────────────────────────────

class BaseAudioDetector(ABC):

    @abstractmethod
    def initialise(self) -> None: ...

    @abstractmethod
    def start_listening(self, on_detection: Callable[[DetectionEvent], None]) -> None: ...

    @abstractmethod
    def stop_listening(self) -> None: ...

    @abstractmethod
    def shutdown(self) -> None: ...


# ─────────────────────────────────────────────────────────────────────────────
# TFLite loader — works on Windows (tensorflow) and Pi (tflite-runtime)
# ─────────────────────────────────────────────────────────────────────────────

def _load_tflite_interpreter(model_path: str):
    try:
        import tflite_runtime.interpreter as tflite
        logger.info("[Audio] Using tflite_runtime (Pi mode).")
        return tflite.Interpreter(model_path=model_path)
    except ImportError:
        pass
    try:
        import tensorflow as tf
        logger.info("[Audio] Using tensorflow.lite (laptop mode).")
        return tf.lite.Interpreter(model_path=model_path)
    except ImportError:
        raise ImportError(
            "Neither tflite_runtime nor tensorflow is installed.\n"
            "  On laptop:  pip install tensorflow\n"
            "  On Pi:      pip install tflite-runtime"
        )


# ─────────────────────────────────────────────────────────────────────────────
# REAL detector — YAMNet + sounddevice
# ─────────────────────────────────────────────────────────────────────────────

class YAMNetDetector(BaseAudioDetector):
    """
    Continuously listens to the microphone, runs YAMNet every 0.5 seconds,
    fires DetectionEvent when a danger sound exceeds DETECTION_THRESHOLD.

    Ring buffer approach: keeps last 1 second of audio, slides by 0.5 s.
    This gives 2 inference passes per second without heavy CPU load.
    """

    def __init__(self):
        self._interpreter          = None
        self._input_index          = None
        self._output_scores_index  = None
        self._class_names: list[str] = []
        self._danger_index: dict[int, str] = {}

        self._ring_buffer = np.zeros(WINDOW_SAMPLES, dtype=np.float32)
        self._buffer_lock = threading.Lock()

        self._thread: Optional[threading.Thread] = None
        self._running  = False
        self._callback: Optional[Callable[[DetectionEvent], None]] = None
        self._last_trigger = 0.0

    def initialise(self) -> None:
        self._load_model()
        self._load_class_map()
        self._build_danger_index()
        self._check_microphone()
        logger.info(
            "[YAMNet] Ready. Danger classes: %d  |  Threshold: %.0f%%",
            len(self._danger_index), DETECTION_THRESHOLD * 100
        )

    def _load_model(self) -> None:
        if not os.path.exists(MODEL_PATH):
            raise FileNotFoundError(
                f"YAMNet model not found at '{MODEL_PATH}'.\n"
                "Run:  python setup_audio.py"
            )
        interp = _load_tflite_interpreter(MODEL_PATH)
        interp.allocate_tensors()
        self._interpreter = interp
        self._input_index         = interp.get_input_details()[0]['index']
        self._output_scores_index = interp.get_output_details()[0]['index']
        logger.info("[YAMNet] Model loaded. Input: %s",
                    interp.get_input_details()[0]['shape'])

    def _load_class_map(self) -> None:
        if not os.path.exists(CLASS_MAP_PATH):
            raise FileNotFoundError(
                f"Class map not found at '{CLASS_MAP_PATH}'.\n"
                "Run:  python setup_audio.py"
            )
        with open(CLASS_MAP_PATH, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            rows = sorted(reader, key=lambda r: int(r['index']))
            self._class_names = [row['display_name'].lower() for row in rows]
        logger.info("[YAMNet] %d classes loaded.", len(self._class_names))

    def _build_danger_index(self) -> None:
        for idx, name in enumerate(self._class_names):
            if name in DANGER_SOUNDS:
                self._danger_index[idx] = DANGER_SOUNDS[name]
        if not self._danger_index:
            logger.warning("[YAMNet] No danger classes matched in the class map! "
                           "Check DANGER_SOUNDS keys match yamnet_class_map.csv names.")
        else:
            logger.info("[YAMNet] Matched danger sounds: %s",
                        {self._class_names[i]: c for i, c in self._danger_index.items()})

    def _check_microphone(self) -> None:
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            inputs  = [d for d in devices if d['max_input_channels'] > 0]
            if not inputs:
                raise RuntimeError("No microphone input device found.")
            logger.info("[YAMNet] Mic: '%s'", inputs[0]['name'])
        except ImportError:
            raise ImportError("sounddevice not installed. Run: pip install sounddevice")

    def _run_inference(self, waveform: np.ndarray) -> list[tuple[str, str, float]]:
        audio_in = waveform.astype(np.float32)
        self._interpreter.set_tensor(self._input_index, audio_in)
        self._interpreter.invoke()
        scores = self._interpreter.get_tensor(self._output_scores_index)
        mean_scores = scores.mean(axis=0) if scores.ndim == 2 else scores

        results = []
        for idx, cat in self._danger_index.items():
            if idx < len(mean_scores):
                conf = float(mean_scores[idx])
                if conf >= DETECTION_THRESHOLD:
                    results.append((self._class_names[idx], cat, conf))
        return sorted(results, key=lambda x: x[2], reverse=True)

    def _audio_thread(self) -> None:
        import sounddevice as sd

        hop_buf = np.zeros(HOP_SAMPLES, dtype=np.float32)
        hop_pos = 0

        def callback(indata, frames, time_info, status):
            nonlocal hop_pos
            mono = indata[:, 0].astype(np.float32)
            if mono.max() > 1.0:
                mono /= 32768.0

            src = 0
            remaining = frames
            while remaining > 0:
                space = HOP_SAMPLES - hop_pos
                take  = min(space, remaining)
                hop_buf[hop_pos:hop_pos + take] = mono[src:src + take]
                hop_pos   += take
                src       += take
                remaining -= take

                if hop_pos == HOP_SAMPLES:
                    with self._buffer_lock:
                        self._ring_buffer = np.roll(self._ring_buffer, -HOP_SAMPLES)
                        self._ring_buffer[-HOP_SAMPLES:] = hop_buf.copy()
                        snap = self._ring_buffer.copy()

                    threading.Thread(
                        target=self._infer_and_fire,
                        args=(snap,),
                        daemon=True
                    ).start()
                    hop_pos = 0

        logger.info("[YAMNet] Opening mic stream at %d Hz...", SAMPLE_RATE)
        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype='float32',
                blocksize=HOP_SAMPLES,
                callback=callback
            ):
                logger.info("[YAMNet] Listening. Categories: %s",
                            sorted(SOS_TRIGGER_CATEGORIES))
                while self._running:
                    time.sleep(0.1)
        except Exception as exc:
            logger.error("[YAMNet] Stream error: %s", exc)
        logger.info("[YAMNet] Audio thread stopped.")

    def _infer_and_fire(self, waveform: np.ndarray) -> None:
        try:
            detections = self._run_inference(waveform)
        except Exception as exc:
            logger.error("[YAMNet] Inference error: %s", exc)
            return

        if not detections:
            return

        top_class, top_cat, top_conf = detections[0]

        for cls, cat, conf in detections:
            logger.info("[YAMNet] %-30s  %-10s  %.1f%%", cls, cat, conf * 100)

        now = time.monotonic()
        if (now - self._last_trigger) < COOLDOWN_SECONDS:
            return

        should_sos = top_cat in SOS_TRIGGER_CATEGORIES
        event = DetectionEvent(top_class, top_cat, top_conf, should_sos)

        if should_sos:
            self._last_trigger = now
            logger.warning("[YAMNet] DANGER: '%s' (%s %.0f%%)",
                           top_class, top_cat, top_conf * 100)

        if self._callback:
            try:
                self._callback(event)
            except Exception as exc:
                logger.error("[YAMNet] Callback error: %s", exc)

    def start_listening(self, on_detection: Callable[[DetectionEvent], None]) -> None:
        self._callback = on_detection
        self._running  = True
        self._thread   = threading.Thread(target=self._audio_thread,
                                          name="YAMNet", daemon=True)
        self._thread.start()

    def stop_listening(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)

    def shutdown(self) -> None:
        self.stop_listening()
        logger.info("[YAMNet] Shutdown.")


# ─────────────────────────────────────────────────────────────────────────────
# FAKE detector
# ─────────────────────────────────────────────────────────────────────────────

class FakeAudioDetector(BaseAudioDetector):
    """
    Cycles through simulated danger sounds every ~60 s for testing.
    Call audio_manager.simulate_detection("screaming") to trigger immediately.
    """

    AUTO_INTERVAL = 60

    _SOUNDS = [
        ("screaming",            "distress", 0.88),
        ("gunshot, gunfire",     "weapon",   0.76),
        ("smash, crash",         "impact",   0.61),
        ("glass",                "impact",   0.54),
        ("explosion",            "weapon",   0.82),
        ("crying, sobbing",      "distress", 0.71),
    ]

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._running  = False
        self._callback: Optional[Callable[[DetectionEvent], None]] = None
        self._wake     = threading.Event()
        self._pending: Optional[DetectionEvent] = None
        self._idx      = 0

    def initialise(self) -> None:
        logger.warning("[FakeAudio] SIMULATION mode — danger sounds fire every ~%ds.",
                       self.AUTO_INTERVAL)

    def _loop(self) -> None:
        while self._running:
            self._wake.wait(timeout=self.AUTO_INTERVAL)
            if not self._running:
                break
            self._wake.clear()

            if self._pending:
                event = self._pending
                self._pending = None
                logger.info("[FakeAudio] Manual: '%s'", event.sound_class)
            else:
                cls, cat, conf = self._SOUNDS[self._idx % len(self._SOUNDS)]
                conf = round(max(0.45, min(0.99, conf + random.uniform(-0.05, 0.05))), 2)
                event = DetectionEvent(cls, cat, conf, cat in SOS_TRIGGER_CATEGORIES)
                self._idx += 1
                logger.info("[FakeAudio] AUTO DETECTION: '%s' (%s %.0f%%)",
                            cls, cat, conf * 100)

            if self._callback:
                try:
                    self._callback(event)
                except Exception as exc:
                    logger.error("[FakeAudio] Callback error: %s", exc)

    def start_listening(self, on_detection: Callable[[DetectionEvent], None]) -> None:
        self._callback = on_detection
        self._running  = True
        self._thread   = threading.Thread(target=self._loop, name="FakeAudio", daemon=True)
        self._thread.start()

    def stop_listening(self) -> None:
        self._running = False
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def shutdown(self) -> None:
        self.stop_listening()

    def trigger_now(self, sound_class: str = "screaming",
                    category: str = "distress", confidence: float = 0.88) -> None:
        self._pending = DetectionEvent(
            sound_class, category, confidence, category in SOS_TRIGGER_CATEGORIES
        )
        self._wake.set()


# ─────────────────────────────────────────────────────────────────────────────
# AudioManager
# ─────────────────────────────────────────────────────────────────────────────

class AudioManager:
    """
    Usage in main.py:

        from hardware.audio import AudioManager

        def on_audio_detection(event):
            if event.should_trigger_sos:
                threading.Thread(
                    target=sos_sequence,
                    kwargs={"trigger_source": f"audio_{event.sound_class}"},
                    daemon=True
                ).start()

        audio = AudioManager()
        audio.start(on_audio_detection)
        ...
        audio.stop()
    """

    def __init__(self):
        self.detector: BaseAudioDetector = self._detect()

    @staticmethod
    def _detect() -> BaseAudioDetector:
        try:
            d = YAMNetDetector()
            d.initialise()
            return d
        except FileNotFoundError as e:
            logger.warning("[AudioManager] %s — FakeAudioDetector.", e)
        except ImportError as e:
            logger.warning("[AudioManager] Missing: %s — FakeAudioDetector.", e)
        except RuntimeError as e:
            logger.warning("[AudioManager] Hardware: %s — FakeAudioDetector.", e)
        except Exception as e:
            logger.warning("[AudioManager] Error: %s — FakeAudioDetector.", e)
        fake = FakeAudioDetector()
        fake.initialise()
        return fake

    def start(self, on_detection: Callable[[DetectionEvent], None]) -> None:
        self.detector.start_listening(on_detection)
        mode = "REAL" if isinstance(self.detector, YAMNetDetector) else "FAKE"
        logger.info("[AudioManager] Started (%s).", mode)

    def stop(self) -> None:
        self.detector.shutdown()

    def status_string(self) -> str:
        mode = "YAMNet/real" if isinstance(self.detector, YAMNetDetector) else "simulated"
        return f"Audio={mode} | Sounds={len(DANGER_SOUNDS)} | Threshold={DETECTION_THRESHOLD:.0%}"

    def simulate_detection(self, sound_class: str = "screaming") -> None:
        if isinstance(self.detector, FakeAudioDetector):
            cat = DANGER_SOUNDS.get(sound_class.lower(), "distress")
            self.detector.trigger_now(sound_class, cat, 0.88)
        else:
            logger.warning("[AudioManager] simulate_detection() only works in fake mode.")