"""
hardware/audio_recorder.py — Project Kavach

Records 60-second .wav audio clips during active alerts for evidence.

Architecture (same Real/Fake pattern as camera.py):
  - RealAudioRecorder  — captures mic input via sounddevice, writes .wav files
  - FakeAudioRecorder  — no-op fallback when no microphone is available
  - AudioRecorderManager — auto-detects hardware, exposes start/stop API

Recording behaviour:
  - When an alert fires (SOS / MEDICAL), start_recording() is called.
  - Records 60-second .wav clips into the evidence/ folder.
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


class BaseAudioRecorder(ABC):
    @abstractmethod
    def start_recording(self) -> None:
        """Begin recording 60-second wav clips to evidence/ folder."""

    @abstractmethod
    def stop_recording(self) -> None:
        """Stop the recording loop."""

    @abstractmethod
    def shutdown(self) -> None:
        """Release all resources."""


class RealAudioRecorder(BaseAudioRecorder):
    """
    Records 60-second .wav clips using sounddevice.
    All recording happens on a dedicated daemon thread.
    """

    def __init__(self, evidence_dir: str, clip_duration: int = 60):
        self._evidence_dir  = evidence_dir
        self._clip_duration = clip_duration
        self._recording     = False
        self._stop_event    = threading.Event()
        self._record_thread = None

        # Verify sounddevice is importable and a mic is available
        import sounddevice as sd
        devices = sd.query_devices()
        default_input = sd.default.device[0]
        if default_input is None or default_input < 0:
            raise RuntimeError("No default input device found.")
        logger.info("[AudioRecorder] sounddevice found. Default input: %s", devices[default_input]['name'])

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


class FakeAudioRecorder(BaseAudioRecorder):
    """No-op fallback when no microphone is available."""

    def __init__(self):
        logger.warning(
            "[FakeAudioRecorder] No microphone detected — SIMULATION mode. "
            "No audio evidence will be recorded."
        )

    def start_recording(self) -> None:
        logger.info("[FakeAudioRecorder] start_recording() called — no mic, skipping.")

    def stop_recording(self) -> None:
        logger.info("[FakeAudioRecorder] stop_recording() called — nothing to stop.")

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

    def __init__(self, evidence_dir: str, clip_duration: int = 60):
        self._recorder = self._detect(evidence_dir, clip_duration)

    @staticmethod
    def _detect(evidence_dir: str, clip_duration: int) -> BaseAudioRecorder:
        try:
            recorder = RealAudioRecorder(evidence_dir, clip_duration)
            logger.info("[AudioRecorderManager] Microphone detected — REAL mode.")
            return recorder
        except ImportError:
            logger.warning(
                "[AudioRecorderManager] sounddevice not installed. "
                "Falling back to FakeAudioRecorder."
            )
        except (RuntimeError, OSError) as e:
            logger.warning(
                "[AudioRecorderManager] Microphone error (%s). "
                "Falling back to FakeAudioRecorder.", e
            )
        except Exception as e:
            logger.warning(
                "[AudioRecorderManager] Unexpected error (%s). "
                "Falling back to FakeAudioRecorder.", e
            )
        return FakeAudioRecorder()

    def start_recording(self) -> None:
        self._recorder.start_recording()

    def stop_recording(self) -> None:
        self._recorder.stop_recording()

    def shutdown(self) -> None:
        self._recorder.shutdown()

    def status_string(self) -> str:
        mode = "REAL (Microphone)" if isinstance(self._recorder, RealAudioRecorder) else "FAKE"
        return f"AudioRecorder={mode}"
