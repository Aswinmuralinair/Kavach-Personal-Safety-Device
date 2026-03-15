"""
alerts.py  —  Project Kavach
All three alert pipelines in one place.

  sos_sequence()      → calls police, SMS guardian, GPS loop, evidence upload
  medical_sequence()  → calls ambulance/medical contact, SMS "MEDICAL EMERGENCY" + GPS
  safe_sequence()     → SMS "I AM SAFE" to guardian, cancels any active SOS loop

Each function is designed to be run in its own daemon thread so it never
blocks the button polling loop.
"""

import time
import threading
import os
import json
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Alert, Base
from hardware.comms import SIM7600

try:
    from hardware.power import INA219Simple, voltage_to_percentage
    I2C_AVAILABLE = True
except (ImportError, FileNotFoundError):
    I2C_AVAILABLE = False

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Global state — shared across all alert threads
# ─────────────────────────────────────────────────────────────────────────────

_alert_lock         = threading.Lock()
_active_alert_type  = None        # "sos" | "medical" | None
_stop_alert_event   = threading.Event()   # set this to cancel the update loop


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    with open('config.json', 'r') as f:
        return json.load(f)


def _get_db_session():
    engine = create_engine('sqlite:///alerts.db')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _ist_timestamp() -> str:
    """Current time formatted for SMS in IST."""
    ist = timezone(timedelta(hours=5, minutes=30), name='IST')
    return datetime.now(ist).strftime("%d-%m-%Y %I:%M:%S %p")


def _build_maps_link(location: str) -> str:
    """
    Turns a raw GPS string (lat,lon or NMEA) into a Google Maps link.
    Falls back gracefully if the format is unexpected.
    """
    try:
        parts = location.replace(' ', '').split(',')
        lat, lon = float(parts[0]), float(parts[1])
        return f"https://maps.google.com/?q={lat},{lon}"
    except Exception:
        return location   # return raw string if parsing fails


def _init_power_monitor():
    """Returns an INA219 instance or None if hardware unavailable."""
    if not I2C_AVAILABLE:
        return None
    try:
        mon = INA219Simple()
        logger.info("[Power] INA219 initialised.")
        return mon
    except IOError:
        logger.warning("[Power] INA219 not found — battery monitoring disabled.")
        return None


def _read_battery(power_monitor) -> str:
    """Returns battery percentage as a string, or 'N/A' / 'Error'."""
    if power_monitor is None:
        return "N/A"
    try:
        v   = power_monitor.get_voltage_V()
        pct = voltage_to_percentage(v)
        return f"{pct}%"
    except IOError:
        return "Error"


def _create_dummy_evidence(path: str) -> None:
    """Creates a placeholder file so the upload loop has something to send."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write("Kavach evidence placeholder.")
    logger.info("[Evidence] Created placeholder: %s", os.path.basename(path))


# ─────────────────────────────────────────────────────────────────────────────
# Shared 60-second update loop (used by both SOS and medical sequences)
# ─────────────────────────────────────────────────────────────────────────────

def _run_update_loop(sim: SIM7600, config: dict, alert_row: Alert, session, power_monitor, alert_label: str) -> None:
    """
    Runs every 60 seconds until _stop_alert_event is set (safe button pressed).
    Sends: GPS location SMS, battery SMS, uploads any evidence files.

    alert_label: "SOS" or "MEDICAL" — used in SMS text.
    """
    logger.info("[%s] Update loop started.", alert_label)

    while not _stop_alert_event.is_set():
        logger.info("[%s] --- 60-second cycle ---", alert_label)

        # 1. Battery
        battery_str = _read_battery(power_monitor)
        alert_row.battery_percentage = battery_str
        sim.send_sms(config['guardian_number'], f"[Kavach {alert_label}] Battery: {battery_str}")

        # 2. GPS
        location = sim.get_gps_location()
        ts        = _ist_timestamp()
        if location:
            maps_link = _build_maps_link(location)
            alert_row.gps_location = location
            sim.send_sms(
                config['guardian_number'],
                f"[Kavach {alert_label}] Location at {ts}: {maps_link}"
            )
        else:
            sim.send_sms(
                config['guardian_number'],
                f"[Kavach {alert_label}] Location at {ts}: Unable to get GPS fix."
            )

        # 3. Commit GPS + battery to DB
        session.commit()

        # 4. Evidence upload
        evidence_dir = config.get('evidence_dir', 'evidence')
        try:
            files = [
                f for f in os.listdir(evidence_dir)
                if os.path.isfile(os.path.join(evidence_dir, f))
            ]
            for file_name in files:
                file_path = os.path.join(evidence_dir, file_name)
                success, uploaded_filename = sim.upload_alert(
                    config['server_url'], alert_row, file_path
                )
                if success:
                    file_link = config['server_public_url'] + uploaded_filename
                    sim.send_sms(
                        config['guardian_number'],
                        f"[Kavach {alert_label}] Evidence: {file_link}"
                    )
                    alert_row.uploaded_files = (alert_row.uploaded_files or "") + uploaded_filename + ","
                    session.commit()
                    logger.info("[%s] Uploaded: %s", alert_label, file_name)
        except Exception as exc:
            logger.error("[%s] Evidence upload error: %s", alert_label, exc)

        # Wait 60 s — but wake immediately if safe button is pressed
        _stop_alert_event.wait(timeout=60)

    logger.info("[%s] Update loop stopped (safe event received).", alert_label)


# ─────────────────────────────────────────────────────────────────────────────
# 1. SOS SEQUENCE  (single button press)
# ─────────────────────────────────────────────────────────────────────────────

def sos_sequence(trigger_source: str = "button") -> None:
    """
    Full SOS pipeline:
      1. Calls police (config['police_number'])
      2. SMS guardian: "SOS ALERT" + Google Maps link
      3. Enters 60-second GPS + evidence upload loop until safe_sequence() fires
    """
    global _active_alert_type

    with _alert_lock:
        if _active_alert_type is not None:
            logger.warning("[SOS] Ignored — %s alert already active.", _active_alert_type)
            return
        _active_alert_type = "sos"
        _stop_alert_event.clear()

    logger.info("[SOS] ACTIVATED — trigger: %s", trigger_source)
    config        = _load_config()
    sim           = SIM7600(port=config['serial_port'], baud=config['baud_rate'])
    power_monitor = _init_power_monitor()
    session       = _get_db_session()

    alert_row = Alert(
        device_id=config['device_id'],
        timestamp=datetime.now(timezone.utc),
        trigger_source=trigger_source,
        alert_type="SOS",
        battery_percentage="N/A" if not I2C_AVAILABLE else None,
    )
    session.add(alert_row)
    session.commit()

    # ── Step 1: Call police ───────────────────────────────────────────────────
    logger.info("[SOS] Step 1 — Calling police: %s", config['police_number'])
    if sim.place_call(config['police_number']):
        time.sleep(15)
        sim.hang_up_call()
        alert_row.call_placed_status = True
    session.commit()

    # ── Step 2: SMS guardian with initial SOS ─────────────────────────────────
    logger.info("[SOS] Step 2 — Sending initial SOS SMS to guardian.")
    sent = sim.send_sms(
        config['guardian_number'],
        "🚨 SOS ALERT 🚨 Emergency triggered on Kavach device. Location SMS to follow."
    )
    alert_row.guardian_sms_status = sent
    session.commit()

    # ── Step 3: Immediate GPS fix + Maps link ─────────────────────────────────
    logger.info("[SOS] Step 3 — Sending GPS location.")
    location = sim.get_gps_location()
    if location:
        maps_link = _build_maps_link(location)
        alert_row.gps_location = location
        sim.send_sms(
            config['guardian_number'],
            f"🚨 [SOS] Last known location: {maps_link}"
        )
        alert_row.location_sms_status = True
    else:
        sim.send_sms(config['guardian_number'], "🚨 [SOS] GPS fix unavailable. Tracking started.")
    session.commit()

    # ── Step 4: Create placeholder evidence + enter update loop ───────────────
    _create_dummy_evidence(os.path.join(config.get('evidence_dir', 'evidence'), 'sos_placeholder.txt'))
    _run_update_loop(sim, config, alert_row, session, power_monitor, alert_label="SOS")

    # Loop exited — clean up
    with _alert_lock:
        _active_alert_type = None
    session.close()
    logger.info("[SOS] Sequence ended.")


# ─────────────────────────────────────────────────────────────────────────────
# 2. MEDICAL ALERT SEQUENCE  (double button press)
# ─────────────────────────────────────────────────────────────────────────────

def medical_sequence() -> None:
    """
    Medical emergency pipeline:
      1. Calls ambulance / medical contact (config['medical_number'])
      2. SMS guardian AND medical contact: "MEDICAL EMERGENCY" + Google Maps link
      3. Enters the same 60-second GPS + evidence upload loop, tagged "MEDICAL"
    """
    global _active_alert_type

    with _alert_lock:
        if _active_alert_type is not None:
            logger.warning("[MEDICAL] Ignored — %s alert already active.", _active_alert_type)
            return
        _active_alert_type = "medical"
        _stop_alert_event.clear()

    logger.info("[MEDICAL] ACTIVATED — double press.")
    config        = _load_config()
    sim           = SIM7600(port=config['serial_port'], baud=config['baud_rate'])
    power_monitor = _init_power_monitor()
    session       = _get_db_session()

    alert_row = Alert(
        device_id=config['device_id'],
        timestamp=datetime.now(timezone.utc),
        trigger_source="double_press",
        alert_type="MEDICAL",
        battery_percentage="N/A" if not I2C_AVAILABLE else None,
    )
    session.add(alert_row)
    session.commit()

    # ── Step 1: Call ambulance / medical contact ──────────────────────────────
    medical_number = config.get('medical_number', config.get('police_number'))
    logger.info("[MEDICAL] Step 1 — Calling medical contact: %s", medical_number)
    if sim.place_call(medical_number):
        time.sleep(15)
        sim.hang_up_call()
        alert_row.call_placed_status = True
    session.commit()

    # ── Step 2: Immediate GPS fix ─────────────────────────────────────────────
    logger.info("[MEDICAL] Step 2 — Getting GPS fix.")
    location  = sim.get_gps_location()
    maps_link = _build_maps_link(location) if location else "GPS unavailable"
    if location:
        alert_row.gps_location        = location
        alert_row.location_sms_status = True
    session.commit()

    # ── Step 3: SMS guardian — MEDICAL EMERGENCY ──────────────────────────────
    logger.info("[MEDICAL] Step 3 — Sending MEDICAL EMERGENCY SMS to guardian.")
    guardian_msg = (
        f"🚑 MEDICAL EMERGENCY 🚑\n"
        f"Kavach device has detected a medical emergency.\n"
        f"Location: {maps_link}\n"
        f"Time: {_ist_timestamp()}\n"
        f"Ambulance has been called. Please respond immediately."
    )
    sent = sim.send_sms(config['guardian_number'], guardian_msg)
    alert_row.guardian_sms_status = sent
    session.commit()

    # ── Step 4: SMS medical contact separately (if different from guardian) ───
    if config.get('medical_number') and config['medical_number'] != config['guardian_number']:
        logger.info("[MEDICAL] Step 4 — Notifying medical contact by SMS.")
        medical_msg = (
            f"🚑 MEDICAL EMERGENCY — KAVACH DEVICE ALERT 🚑\n"
            f"User requires immediate medical assistance.\n"
            f"Location: {maps_link}\n"
            f"Time: {_ist_timestamp()}"
        )
        sim.send_sms(config['medical_number'], medical_msg)

    # ── Step 5: Create placeholder evidence + enter update loop ───────────────
    _create_dummy_evidence(
        os.path.join(config.get('evidence_dir', 'evidence'), 'medical_placeholder.txt')
    )
    _run_update_loop(sim, config, alert_row, session, power_monitor, alert_label="MEDICAL")

    # Loop exited — clean up
    with _alert_lock:
        _active_alert_type = None
    session.close()
    logger.info("[MEDICAL] Sequence ended.")


# ─────────────────────────────────────────────────────────────────────────────
# 3. SAFE SEQUENCE  (long press ≥ 5 seconds)
# ─────────────────────────────────────────────────────────────────────────────

def safe_sequence() -> None:
    """
    'I am safe' pipeline:
      - If an SOS or MEDICAL loop is running → stops it immediately
      - SMS guardian: "I AM SAFE" confirmation message
      - Resets device back to idle, waiting for next button press

    This is deliberately the ONLY way to cancel an active alert loop,
    requiring a deliberate 5-second hold to prevent accidental cancellation.
    """
    logger.info("[SAFE] Long press detected — sending SAFE alert.")
    config = _load_config()
    sim    = SIM7600(port=config['serial_port'], baud=config['baud_rate'])

    # ── Cancel any running alert loop ─────────────────────────────────────────
    with _alert_lock:
        was_active = _active_alert_type

    if was_active:
        logger.info("[SAFE] Cancelling active %s alert.", was_active.upper())
        _stop_alert_event.set()   # wake + exit the update loop
        # Give the loop up to 2 s to notice the event and exit cleanly
        time.sleep(2)

    # ── Build the SMS ─────────────────────────────────────────────────────────
    ts = _ist_timestamp()
    if was_active:
        message = (
            f"✅ SAFE CONFIRMATION ✅\n"
            f"The previous {was_active.upper()} alert has been CANCELLED.\n"
            f"The user has confirmed they are safe.\n"
            f"Time: {ts}"
        )
    else:
        message = (
            f"✅ SAFE CHECK-IN ✅\n"
            f"The Kavach user has confirmed they are safe.\n"
            f"Time: {ts}"
        )

    # ── Send to guardian (and police if SOS was active) ───────────────────────
    sim.send_sms(config['guardian_number'], message)
    logger.info("[SAFE] Safe SMS sent to guardian.")

    if was_active == "sos":
        # Also notify police that the emergency is cancelled, to avoid false response
        cancel_msg = (
            f"KAVACH DEVICE — FALSE ALARM CANCEL\n"
            f"The SOS alert from this device at {ts} has been cancelled by the user. "
            f"No further response required."
        )
        sim.send_sms(config['police_number'], cancel_msg)
        logger.info("[SAFE] Cancellation SMS also sent to police number.")

    logger.info("[SAFE] Device reset to IDLE — ready for next trigger.")