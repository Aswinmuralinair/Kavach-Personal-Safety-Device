"""
hardware/camera.py — Project Kavach

Pi Camera Module recording for evidence capture during alerts.

Architecture (follows the same Fake/Real pattern as sensors.py):
  - PiCameraRecorder  — real hardware via rpicam-vid CLI (CSI camera)
  - FakeCameraRecorder — no-op fallback when camera isn't connected
  - CameraManager      — auto-detects hardware, exposes start/stop API

Recording behaviour:
  - When an alert fires (SOS / MEDICAL), start_recording() is called.
  - The camera records 10-second MP4 clips into the evidence/ folder.
  - The existing 60-second upload loop in alerts.py picks up new files
    and uploads them to the server.
  - When safe_sequence() fires (long press), stop_recording() is called.
"""

import os
import shutil
import subprocess
import threading
import logging
from datetime import datetime
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Abstract base
# ─────────────────────────────────────────────────────────────────────────────
class BaseCameraRecorder(ABC):
    @abstractmethod
    def start_recording(self) -> None:
        """Begin recording clips to evidence/ folder."""

    @abstractmethod
    def stop_recording(self) -> None:
        """Stop the recording loop. May leave a partial final clip."""

    @abstractmethod
    def shutdown(self) -> None:
        """Release all hardware resources."""


# ─────────────────────────────────────────────────────────────────────────────
# Real implementation — Pi Camera via rpicam-vid CLI
# ─────────────────────────────────────────────────────────────────────────────
class PiCameraRecorder(BaseCameraRecorder):
    """
    Records MP4 clips of `clip_duration` seconds into `evidence_dir`
    using the rpicam-vid command-line tool (pre-installed on Raspberry Pi OS).
    All recording happens on a dedicated daemon thread.
    """

    def __init__(self, evidence_dir: str, clip_duration: int = 60):
        self._evidence_dir   = evidence_dir
        self._clip_duration  = clip_duration
        self._recording      = False
        self._stop_event     = threading.Event()
        self._record_thread  = None
        self._proc           = None        # current rpicam-vid process

        # Verify rpicam-vid is available
        if not shutil.which("rpicam-vid"):
            raise FileNotFoundError("rpicam-vid not found on PATH")
        logger.info("[Camera] rpicam-vid found.")

    def start_recording(self) -> None:
        if self._recording:
            logger.warning("[Camera] Already recording — ignoring start_recording().")
            return

        os.makedirs(self._evidence_dir, exist_ok=True)
        self._recording = True
        self._stop_event.clear()

        self._record_thread = threading.Thread(
            target=self._record_loop,
            name="CameraRecorder",
            daemon=True,
        )
        self._record_thread.start()
        logger.info("[Camera] Recording started (clip=%ds).", self._clip_duration)

    def _record_loop(self) -> None:
        """Runs on a dedicated thread. Records clip_duration-second MP4 clips."""
        import signal
        try:
            while self._recording and not self._stop_event.is_set():
                ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"video_{ts}.mp4"
                filepath = os.path.join(self._evidence_dir, filename)

                # rpicam-vid: 640x480, 15fps, libav codec for MP4, no preview
                cmd = [
                    "rpicam-vid",
                    "-t", str(self._clip_duration * 1000),  # duration in ms
                    "--width", "640",
                    "--height", "480",
                    "--framerate", "15",
                    "--codec", "libav",
                    "--libav-format", "mp4",
                    "--nopreview",
                    "-o", filepath,
                ]

                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

                # Wait for clip to finish OR stop_event to fire
                while self._proc.poll() is None:
                    if self._stop_event.wait(timeout=0.5):
                        # Graceful stop: SIGTERM lets rpicam-vid finalize the MP4
                        self._proc.send_signal(signal.SIGTERM)
                        self._proc.wait(timeout=5)
                        break

                self._proc = None

                if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    size = os.path.getsize(filepath)
                    logger.info("[Camera] Clip saved: %s (%s bytes)", filename, f"{size:,}")
                else:
                    logger.warning("[Camera] rpicam-vid produced no output for %s", filename)

        except Exception as exc:
            logger.error("[Camera] Recording error: %s", exc, exc_info=True)
        finally:
            logger.info("[Camera] Record loop exited.")

    def stop_recording(self) -> None:
        if not self._recording:
            return
        logger.info("[Camera] Stopping recording...")
        self._recording = False
        self._stop_event.set()
        if self._record_thread and self._record_thread.is_alive():
            self._record_thread.join(timeout=15)
        logger.info("[Camera] Recording stopped.")

    def shutdown(self) -> None:
        self.stop_recording()


# ─────────────────────────────────────────────────────────────────────────────
# Fake implementation — no camera hardware
# ─────────────────────────────────────────────────────────────────────────────
class FakeCameraRecorder(BaseCameraRecorder):
    """No-op fallback when the Pi Camera is not connected or rpicam-vid is not available."""

    def __init__(self):
        logger.warning(
            "[FakeCamera] No Pi Camera detected — running in SIMULATION mode. "
            "No video evidence will be recorded."
        )

    def start_recording(self) -> None:
        logger.info("[FakeCamera] start_recording() called — no camera, skipping.")

    def stop_recording(self) -> None:
        logger.info("[FakeCamera] stop_recording() called — nothing to stop.")

    def shutdown(self) -> None:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Manager — auto-detects hardware
# ─────────────────────────────────────────────────────────────────────────────
class CameraManager:
    """
    Auto-detects Pi Camera hardware at construction time.
    Falls back to FakeCameraRecorder if rpicam-vid is not available
    or no camera is detected.

    Usage:
        cam = CameraManager(evidence_dir="/home/pi/kavach/evidence")
        cam.start_recording()   # alert started
        cam.stop_recording()    # safe pressed
        cam.shutdown()          # device shutdown
    """

    def __init__(self, evidence_dir: str, clip_duration: int = 10):
        self._recorder = self._detect(evidence_dir, clip_duration)

    @staticmethod
    def _detect(evidence_dir: str, clip_duration: int) -> BaseCameraRecorder:
        try:
            recorder = PiCameraRecorder(evidence_dir, clip_duration)
            logger.info("[CameraManager] rpicam-vid detected — REAL mode.")
            return recorder
        except FileNotFoundError:
            logger.warning(
                "[CameraManager] rpicam-vid not found. "
                "Falling back to FakeCameraRecorder."
            )
        except Exception as e:
            logger.warning(
                "[CameraManager] Unexpected camera error (%s). "
                "Falling back to FakeCameraRecorder.", e
            )
        return FakeCameraRecorder()

    # ── Pass-through API ─────────────────────────────────────────────────────
    def start_recording(self) -> None:
        self._recorder.start_recording()

    def stop_recording(self) -> None:
        self._recorder.stop_recording()

    def shutdown(self) -> None:
        self._recorder.shutdown()

    def status_string(self) -> str:
        mode = "REAL (Pi Camera)" if isinstance(self._recorder, PiCameraRecorder) else "FAKE"
        return f"Camera={mode}"
