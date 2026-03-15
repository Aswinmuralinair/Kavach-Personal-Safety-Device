"""
main.py  —  Project Kavach
Complete entry point with button + sensors + audio keyword detection.
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
from hardware.audio   import AudioManager          # ← ADD
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
# Sensor monitor threads (unchanged from before)
# ─────────────────────────────────────────────────────────────────────────────

def _imu_monitor(sensor_manager: SensorManager) -> None:
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
# Button callbacks (unchanged from before)
# ─────────────────────────────────────────────────────────────────────────────

def _on_sos():
    logger.info("[Main] Button: SINGLE PRESS → SOS")
    threading.Thread(target=sos_sequence, kwargs={"trigger_source": "button_single"}, daemon=True).start()

def _on_medical():
    logger.info("[Main] Button: DOUBLE PRESS → MEDICAL ALERT")
    threading.Thread(target=medical_sequence, daemon=True).start()

def _on_safe():
    logger.info("[Main] Button: LONG PRESS → SAFE ALERT")
    threading.Thread(target=safe_sequence, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# Audio keyword callback                                        ← ADD THIS
# ─────────────────────────────────────────────────────────────────────────────

def _on_keyword_detected(keyword: str, confidence: float) -> None:
    """Called by AudioManager when a trigger keyword is heard."""
    logger.warning(
        "[Main] Audio keyword '%s' detected (%.0f%%) → SOS",
        keyword, confidence * 100
    )
    threading.Thread(
        target=sos_sequence,
        kwargs={"trigger_source": f"audio_{keyword}"},
        daemon=True
    ).start()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("=== Kavach device starting ===")

    # 1. Database
    engine = create_engine('sqlite:///alerts.db')
    Base.metadata.create_all(engine)
    logger.info("[DB] alerts.db ready.")

    config = load_config()

    # 2. Sensors
    sensor_manager = SensorManager()
    sensor_manager.start()
    logger.info("[Sensors] %s", sensor_manager.status_string())

    # 3. Button
    button = ButtonHandler(
        pin=config['sos_button_pin'],
        on_sos_press=_on_sos,
        on_medical_press=_on_medical,
        on_safe_press=_on_safe,
    )
    button.start()

    # 4. Audio keyword detection                                ← ADD
    audio_manager = AudioManager()
    audio_manager.start(on_keyword_detected=_on_keyword_detected)
    logger.info("[Audio] %s", audio_manager.status_string())

    # 5. Sensor monitor threads
    threading.Thread(target=_imu_monitor,        args=(sensor_manager,), daemon=True).start()
    threading.Thread(target=_heart_rate_monitor, args=(sensor_manager,), daemon=True).start()

    logger.info("=== Kavach ARMED — waiting for trigger ===")
    logger.info("    Single press   → SOS")
    logger.info("    Double press   → MEDICAL ALERT")
    logger.info("    Long press 5s  → SAFE")
    logger.info("    Say 'help'     → SOS (audio trigger)")

    try:
        signal.pause()
    except KeyboardInterrupt:
        logger.info("\nShutdown requested.")
    finally:
        button.stop()
        sensor_manager.stop()
        audio_manager.stop()
        logger.info("=== Kavach shutdown complete ===")