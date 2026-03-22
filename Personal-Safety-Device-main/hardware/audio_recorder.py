"""
hardware/audio_recorder.py — Project Kavach

Records 42-second .wav audio clips during active alerts for evidence.

Architecture (same Real/Fake pattern as camera.py):
  - RealAudioRecorder  — captures mic input via sounddevice, writes .wav files
  - DisabledAudioRecorder — no-op when no REAL microphone is detected
  - AudioRecorderManager — auto-detects hardware, exposes start/stop API

Hardware detection:
  - Rejects virtual/monitor audio devices (PulseAudio "pulse", HDMI outputs,
    monitor sinks) that appear as input devices but are not real microphones.
  - Only activates when a physical USB or I2S microphone is detected.
  - If no real mic is found, audio recording is DISABLED (no simulation).

Recording behaviour:
  - When an alert fires (SOS / MEDICAL), start_recording() is called.
  - Records 42-second .wav clips into the evidence/ folder.
  - The existing 60-second upload loop in alerts.py picks up new files.
  - When safe_sequence() fires (long press), stop_recording() is called.

NOTE: This is separate from audio.py (YAMNet detection). Both can use the
microphone simultaneously — sounddevice/ALSA handle shared access.
"""

import os
import wave
import threading
import logging
import numpy as np
from datetime import datetime
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

# Recording parameters
SAMPLE_RATE = 16000   # 16 kHz — good balance of quality vs file size
CHANNELS    = 1       # mono
DTYPE       = 'int16' # 16-bit PCM

# Virtual/monitor audio device names that are NOT real microphones.
# PulseAudio, ALSA, and HDMI adapters often expose these as input devices
# even though they can't capture real audio from a physical microphone.
_VIRTUAL_DEVICE_KEYWORDS = [
    'pulse',          # PulseAudio default virtual device
    'monitor',        # PulseAudio monitor sinks (e.g. "Monitor of Built-in Audio")
    'hdmi',           # HDMI audio output exposed as input
    'spdif',          # S/PDIF digital audio
    'loopback',       # ALSA loopback devices
]


class BaseAudioRecorder(ABC):
    @abstractmethod
    def start_recording(self) -> None:
        """Begin recording 42-second wav clips to evidence/ folder."""

    @abstractmethod
    def stop_recording(self) -> None:
        """Stop the recording loop."""

    @abstractmethod
    def shutdown(self) -> None:
        """Release all resources."""


class RealAudioRecorder(BaseAudioRecorder):
    """
    Records 42-second .wav clips using sounddevice.
    All recording happens on a dedicated daemon thread.
    """

    def __init__(self, evidence_dir: str, clip_duration: int = 42):
        self._evidence_dir  = evidence_dir
        self._clip_duration = clip_duration
        self._recording     = False
        self._stop_event    = threading.Event()
        self._record_thread = None

        # Verify sounddevice is importable and a REAL mic is available
        import sounddevice as sd
        devices = sd.query_devices()
        default_input = sd.default.device[0]
        if default_input is None or default_input < 0:
            raise RuntimeError("No default input device found.")

        device_name = devices[default_input]['name'].strip()

        # Reject virtual/monitor devices that aren't real microphones
        name_lower = device_name.lower()
        for keyword in _VIRTUAL_DEVICE_KEYWORDS:
            if keyword in name_lower:
                raise RuntimeError(
                    f"Default input '{device_name}' is a virtual device "
                    f"(matched '{keyword}') — not a real microphone."
                )

        # Also reject "default" if it's the only name (no actual hardware name)
        if name_lower == 'default':
            raise RuntimeError(
                "Default input device is named 'default' — likely a virtual "
                "PulseAudio/ALSA device, not a real microphone."
            )

        logger.info("[AudioRecorder] Real microphone found: %s", device_name)

    def start_recording(self) -> None:
        if self._recording:
            logger.warning("[AudioRecorder] Already recording — ignoring start_recording().")
            return

        os.makedirs(self._evidence_dir, exist_ok=True)
        self._recording = True
        self._stop_event.clear()

        self._record_thread = threading.Thread(
            target=self._record_loop,
            name="AudioRecorder",
            daemon=True,
        )
        self._record_thread.start()
        logger.info("[AudioRecorder] Recording started (clip=%ds, %dHz mono).",
                     self._clip_duration, SAMPLE_RATE)

    def _record_loop(self) -> None:
        """Runs on a dedicated thread. Records clip_duration-second wav clips."""
        import sounddevice as sd

        try:
            while self._recording:
                ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"audio_{ts}.wav"
                filepath = os.path.join(self._evidence_dir, filename)

                # Calculate total samples for this clip
                total_samples = SAMPLE_RATE * self._clip_duration
                frames_per_block = 1024
                recorded_frames = []
                samples_so_far = 0

                # Record in blocks, checking stop_event between blocks
                with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                                    dtype=DTYPE, blocksize=frames_per_block) as stream:
                    while samples_so_far < total_samples and self._recording:
                        if self._stop_event.is_set():
                            break
                        data, overflowed = stream.read(frames_per_block)
                        if overflowed:
                            logger.debug("[AudioRecorder] Input overflow (dropped frames).")
                        recorded_frames.append(data.copy())
                        samples_so_far += len(data)

                # Write whatever we captured to a wav file
                if recorded_frames:
                    audio_data = np.concatenate(recorded_frames, axis=0)
                    self._write_wav(filepath, audio_data)
                    logger.info("[AudioRecorder] Clip saved: %s (%d samples)",
                                filename, len(audio_data))

        except Exception as exc:
            logger.error("[AudioRecorder] Recording error: %s", exc, exc_info=True)
        finally:
            logger.info("[AudioRecorder] Record loop exited.")

    @staticmethod
    def _write_wav(filepath: str, audio_data: np.ndarray) -> None:
        """Write numpy int16 audio data to a .wav file."""
        with wave.open(filepath, 'wb') as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)  # 16-bit = 2 bytes
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_data.tobytes())

    def stop_recording(self) -> None:
        if not self._recording:
            return
        logger.info("[AudioRecorder] Stopping recording...")
        self._recording = False
        self._stop_event.set()
        if self._record_thread and self._record_thread.is_alive():
            self._record_thread.join(timeout=10)
        logger.info("[AudioRecorder] Recording stopped.")

    def shutdown(self) -> None:
        self.stop_recording()


class DisabledAudioRecorder(BaseAudioRecorder):
    """No-op — audio recording disabled when no real microphone is detected."""

    def __init__(self, reason: str = "no microphone"):
        logger.info(
            "[AudioRecorder] DISABLED (%s) — no audio evidence will be recorded.", reason
        )

    def start_recording(self) -> None:
        pass  # silently skip — no log spam

    def stop_recording(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


class AudioRecorderManager:
    """
    Auto-detects microphone hardware at construction time.
    Falls back to FakeAudioRecorder if sounddevice is not installed
    or no mic is detected.

    Usage:
        rec = AudioRecorderManager(evidence_dir="/home/pi/kavach/evidence")
        rec.start_recording()   # alert started
        rec.stop_recording()    # safe pressed
        rec.shutdown()          # device shutdown
    """

    def __init__(self, evidence_dir: str, clip_duration: int = 42):
        self._recorder = self._detect(evidence_dir, clip_duration)

    @staticmethod
    def _detect(evidence_dir: str, clip_duration: int) -> BaseAudioRecorder:
        try:
            recorder = RealAudioRecorder(evidence_dir, clip_duration)
            logger.info("[AudioRecorderManager] Real microphone detected — REAL mode.")
            return recorder
        except ImportError:
            return DisabledAudioRecorder("sounddevice not installed")
        except RuntimeError as e:
            return DisabledAudioRecorder(str(e))
        except (OSError, Exception) as e:
            return DisabledAudioRecorder(str(e))

    def start_recording(self) -> None:
        self._recorder.start_recording()

    def stop_recording(self) -> None:
        self._recorder.stop_recording()

    def shutdown(self) -> None:
        self._recorder.shutdown()

    def status_string(self) -> str:
        if isinstance(self._recorder, RealAudioRecorder):
            return "AudioRecorder=REAL (Microphone)"
        else:
            return "AudioRecorder=disabled (no real mic)"
