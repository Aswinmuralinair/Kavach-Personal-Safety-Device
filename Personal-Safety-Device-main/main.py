"""
main.py  —  Project Kavach
Complete entry point with button + sensors + audio sound detection.
"""

import signal
import threading
import time
import logging
import json
from sqlalchemy import create_engine
from database import Alert, Base

from hardware.button  import ButtonHandler
from hardware.sensors import SensorManager
from hardware.audio   import AudioManager, DetectionEvent
from alerts import sos_sequence, medical_sequence, safe_sequence

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("kavach.main")


def load_config() -> dict:
    with open('config.json', 'r') as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────────────────
# Sensor monitor threads
# ─────────────────────────────────────────────────────────────────────────────

def _imu_monitor(sensor_manager: SensorManager) -> None:
    """Background thread: reads IMU at 10 Hz, fires SOS on fall detection."""
    logger.info("[IMU] Monitor thread started.")
    while True:
        try:
            reading = sensor_manager.imu.read()
            if reading.is_fall_detected:
                logger.warning("[IMU] FALL DETECTED — magnitude=%.2f m/s²", reading.accel_magnitude)
                threading.Thread(
                    target=sos_sequence,
                    kwargs={"trigger_source": "fall_detected"},
                    daemon=True
                ).start()
        except Exception as exc:
            logger.error("[IMU] Read error: %s", exc)
        time.sleep(0.1)


def _heart_rate_monitor(sensor_manager: SensorManager) -> None:
    """Background thread: reads heart rate every 5 s, fires SOS on distress."""
    logger.info("[HeartRate] Monitor thread started.")
    while True:
        try:
            reading = sensor_manager.heart_rate.read()
            if reading.is_valid:
                logger.info("[HeartRate] BPM=%.1f  SpO2=%.1f%%", reading.bpm, reading.spo2)
            if reading.is_distress_detected:
                logger.warning("[HeartRate] DISTRESS — BPM=%.1f", reading.bpm)
                threading.Thread(
                    target=sos_sequence,
                    kwargs={"trigger_source": "heartrate_spike"},
                    daemon=True
                ).start()
        except Exception as exc:
            logger.error("[HeartRate] Read error: %s", exc)
        time.sleep(5)


# ─────────────────────────────────────────────────────────────────────────────
# Button callbacks
# ─────────────────────────────────────────────────────────────────────────────

def _on_sos():
    """Single press → SOS."""
    logger.info("[Main] Button: SINGLE PRESS → SOS")
    threading.Thread(
        target=sos_sequence,
        kwargs={"trigger_source": "button_single"},
        daemon=True
    ).start()


def _on_medical():
    """Double press → Medical alert."""
    logger.info("[Main] Button: DOUBLE PRESS → MEDICAL ALERT")
    threading.Thread(target=medical_sequence, daemon=True).start()


def _on_safe():
    """Long press (5 s) → Safe / cancel alert."""
    logger.info("[Main] Button: LONG PRESS → SAFE ALERT")
    threading.Thread(target=safe_sequence, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# Audio detection callback
# ─────────────────────────────────────────────────────────────────────────────

def _on_audio_detection(event: DetectionEvent) -> None:
    """
    Called by AudioManager when a danger sound is detected.
    event.sound_class  = e.g. "Screaming", "Gunshot, gunfire", "Smash, crash"
    event.category     = "distress" | "weapon" | "impact" | "fire"
    event.confidence   = 0.0 – 1.0
    event.should_trigger_sos = True if category is in SOS_TRIGGER_CATEGORIES
    """
    if event.should_trigger_sos:
        logger.warning(
            "[Main] Audio danger detected: '%s' (%s, %.0f%%) → SOS",
            event.sound_class, event.category, event.confidence * 100
        )
        threading.Thread(
            target=sos_sequence,
            kwargs={"trigger_source": f"audio_{event.sound_class.replace(' ', '_').replace(',', '')}"},
            daemon=True
        ).start()
    else:
        # Detected but below trigger threshold — log only
        logger.info(
            "[Main] Audio event (no trigger): '%s' (%s, %.0f%%)",
            event.sound_class, event.category, event.confidence * 100
        )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("=== Kavach device starting ===")

    # 1. Database init
    engine = create_engine('sqlite:///alerts.db')
    Base.metadata.create_all(engine)
    logger.info("[DB] alerts.db ready.")

    config = load_config()

    # 2. Sensor manager (auto-detects real BNO055/MAX30102 vs fake)
    sensor_manager = SensorManager()
    sensor_manager.start()
    logger.info("[Sensors] %s", sensor_manager.status_string())

    # 3. Button handler (single / double / long press)
    button = ButtonHandler(
        pin=config['sos_button_pin'],
        on_sos_press=_on_sos,
        on_medical_press=_on_medical,
        on_safe_press=_on_safe,
    )
    button.start()

    # 4. Audio sound detection (YAMNet — real mic or fake simulation)
    audio_manager = AudioManager()
    audio_manager.start(on_detection=_on_audio_detection)
    logger.info("[Audio] %s", audio_manager.status_string())

    # 5. Sensor monitor threads
    threading.Thread(
        target=_imu_monitor,
        args=(sensor_manager,),
        daemon=True
    ).start()
    threading.Thread(
        target=_heart_rate_monitor,
        args=(sensor_manager,),
        daemon=True
    ).start()

    logger.info("=== Kavach ARMED — waiting for trigger ===")
    logger.info("    Single press      → SOS (calls police + SMS guardian)")
    logger.info("    Double press      → MEDICAL ALERT (calls ambulance)")
    logger.info("    Long press 5s     → SAFE (cancels alert, notifies guardian)")
    logger.info("    Scream/Gunshot    → SOS (audio detection)")
    logger.info("    Fall detected     → SOS (IMU sensor)")
    logger.info("    Heart rate spike  → SOS (pulse oximeter)")

    # 6. Wait forever — all work happens in daemon threads
    try:
        signal.pause()
    except KeyboardInterrupt:
        logger.info("\nShutdown requested.")
    finally:
        button.stop()
        sensor_manager.stop()
        audio_manager.stop()
        logger.info("=== Kavach shutdown complete ===")