"""
alerts.py — Project Kavach

All three alert pipelines in one place.

    sos_sequence()     → calls police, SMS guardian, GPS loop, evidence upload
    medical_sequence() → calls ambulance/medical contact, SMS "MEDICAL EMERGENCY" + GPS
    safe_sequence()    → SMS "I AM SAFE" to guardian, cancels any active SOS loop

FIX applied (serial port conflict):
    Previously each function created its own SIM7600(port=...) instance, which
    caused an OSError: [Errno 16] Device or resource busy when safe_sequence()
    tried to open the same /dev/ttyUSB2 that sos_sequence() already held.

    Now every function accepts `sim: SIM7600` as its FIRST argument.
    A single shared instance is created once at boot in main.py and passed in.
    This means the serial port is opened exactly once for the lifetime of the
    device, and all sequences share it safely.

    The duplicate _alert_lock / _active_alert_type state has also been removed —
    KavachStateMachine in main.py is now the single source of truth for state.
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
# Single module-level stop event — still needed so safe_sequence() can wake
# the update loop across threads.  State ownership stays in KavachStateMachine.
# ─────────────────────────────────────────────────────────────────────────────

_stop_alert_event = threading.Event()

# ─────────────────────────────────────────────────────────────────────────────
# Shared SQLAlchemy engine — created ONCE, not per call
# ─────────────────────────────────────────────────────────────────────────────

_ENGINE = create_engine('sqlite:///alerts.db')
Base.metadata.create_all(_ENGINE)
_SessionFactory = sessionmaker(bind=_ENGINE)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    with open('config.json', 'r') as f:
        return json.load(f)


def _get_db_session():
    """Return a new Session backed by the shared engine."""
    return _SessionFactory()


def _ist_timestamp() -> str:
    """Current time formatted for SMS in IST (UTC+5:30)."""
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
        return location  # return raw string if parsing fails


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
        v = power_monitor.get_voltage_V()
        pct = voltage_to_percentage(v)
        return f"{pct}%"
    except IOError:
        return "Error"


# ─────────────────────────────────────────────────────────────────────────────
# Shared 60-second update loop (used by both SOS and medical sequences)
# ─────────────────────────────────────────────────────────────────────────────

def _run_update_loop(
    sim: SIM7600,
    config: dict,
    alert_row: Alert,
    session,
    power_monitor,
    alert_label: str,
    uploaded_files: set,          # ← tracks already-uploaded filenames (fix #2)
) -> None:
    """
    Runs every 60 seconds until _stop_alert_event is set (safe button pressed).
    Sends: GPS location SMS, battery SMS, uploads any NEW evidence files.

    The `uploaded_files` set prevents the same file being re-sent every cycle.
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
        ts = _ist_timestamp()
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

        # 4. Evidence upload — only files NOT already uploaded this session
        evidence_dir = config.get('evidence_dir', 'evidence')
        try:
            all_files = [
                f for f in os.listdir(evidence_dir)
                if os.path.isfile(os.path.join(evidence_dir, f))
                and not f.endswith('.txt')           # skip placeholder text files
            ]
            new_files = [f for f in all_files if f not in uploaded_files]

            for file_name in new_files:
                file_path = os.path.join(evidence_dir, file_name)
                success, uploaded_filename = sim.upload_alert(
                    config['server_url'], alert_row, file_path
                )
                if success:
                    uploaded_files.add(file_name)    # mark as done — never re-send
                    file_link = config['server_public_url'] + uploaded_filename
                    sim.send_sms(
                        config['guardian_number'],
                        f"[Kavach {alert_label}] Evidence: {file_link}"
                    )
                    alert_row.uploaded_files = (
                        (alert_row.uploaded_files or "") + uploaded_filename + ","
                    )
                    session.commit()
                    logger.info("[%s] Uploaded: %s", alert_label, file_name)

        except Exception as exc:
            logger.error("[%s] Evidence upload error: %s", alert_label, exc)

        # Wait 60 s — wakes immediately if safe button is pressed
        _stop_alert_event.wait(timeout=60)

    logger.info("[%s] Update loop stopped (safe event received).", alert_label)


# ─────────────────────────────────────────────────────────────────────────────
# 1. SOS SEQUENCE (single button press / fall / heartrate / audio trigger)
# ─────────────────────────────────────────────────────────────────────────────

def sos_sequence(sim: SIM7600, trigger_source: str = "button") -> None:
    """
    Full SOS pipeline:
      1. Calls police (config['police_number'])
      2. SMS guardian: "SOS ALERT" + Google Maps link
      3. Enters 60-second GPS + evidence upload loop until safe_sequence() fires

    `sim` is the shared SIM7600 instance from main.py — do NOT construct a new one here.
    """
    _stop_alert_event.clear()
    logger.info("[SOS] ACTIVATED — trigger: %s", trigger_source)

    config = _load_config()
    power_monitor = _init_power_monitor()
    session = _get_db_session()

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
        sim.send_sms(
            config['guardian_number'],
            "🚨 [SOS] GPS fix unavailable. Tracking started."
        )
    session.commit()

    # ── Step 4: Enter 60-second update loop ───────────────────────────────────
    _run_update_loop(
        sim=sim,
        config=config,
        alert_row=alert_row,
        session=session,
        power_monitor=power_monitor,
        alert_label="SOS",
        uploaded_files=set(),     # fresh set per alert session
    )

    # Loop exited — clean up
    session.close()
    logger.info("[SOS] Sequence ended.")


# ─────────────────────────────────────────────────────────────────────────────
# 2. MEDICAL ALERT SEQUENCE (double button press)
# ─────────────────────────────────────────────────────────────────────────────

def medical_sequence(sim: SIM7600) -> None:
    """
    Medical emergency pipeline:
      1. Calls ambulance / medical contact (config['medical_number'])
      2. SMS guardian AND medical contact: "MEDICAL EMERGENCY" + Google Maps link
      3. Enters the same 60-second GPS + evidence upload loop, tagged "MEDICAL"

    `sim` is the shared SIM7600 instance from main.py — do NOT construct a new one here.
    """
    _stop_alert_event.clear()
    logger.info("[MEDICAL] ACTIVATED — double press.")

    config = _load_config()
    power_monitor = _init_power_monitor()
    session = _get_db_session()

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
    location = sim.get_gps_location()
    maps_link = _build_maps_link(location) if location else "GPS unavailable"
    if location:
        alert_row.gps_location = location
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

    # ── Step 5: Enter 60-second update loop ───────────────────────────────────
    _run_update_loop(
        sim=sim,
        config=config,
        alert_row=alert_row,
        session=session,
        power_monitor=power_monitor,
        alert_label="MEDICAL",
        uploaded_files=set(),
    )

    # Loop exited — clean up
    session.close()
    logger.info("[MEDICAL] Sequence ended.")


# ─────────────────────────────────────────────────────────────────────────────
# 3. SAFE SEQUENCE (long press ≥ 5 seconds)
# ─────────────────────────────────────────────────────────────────────────────

def safe_sequence(sim: SIM7600, was_active_type: str = None) -> None:
    """
    'I am safe' pipeline:
      - Stops the running SOS/MEDICAL loop immediately via _stop_alert_event
      - SMS guardian: "I AM SAFE" confirmation
      - SMS police cancellation if SOS was active (to prevent false response)

    `sim` is the shared SIM7600 instance from main.py — do NOT construct a new one here.
    `was_active_type` is passed from KavachStateMachine so we know what to cancel.
    """
    logger.info("[SAFE] Long press detected — sending SAFE alert.")

    config = _load_config()

    # ── Cancel any running alert loop ─────────────────────────────────────────
    if was_active_type:
        logger.info("[SAFE] Cancelling active %s alert.", was_active_type.upper())
        _stop_alert_event.set()   # wake + exit the update loop immediately
        time.sleep(2)             # give the loop up to 2 s to exit cleanly

    # ── Build the SMS ─────────────────────────────────────────────────────────
    ts = _ist_timestamp()
    if was_active_type:
        message = (
            f"✅ SAFE CONFIRMATION ✅\n"
            f"The previous {was_active_type.upper()} alert has been CANCELLED.\n"
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

    if was_active_type == "sos":
        cancel_msg = (
            f"KAVACH DEVICE — FALSE ALARM CANCEL\n"
            f"The SOS alert from this device at {ts} has been cancelled by the user. "
            f"No further response required."
        )
        sim.send_sms(config['police_number'], cancel_msg)
        logger.info("[SAFE] Cancellation SMS also sent to police number.")

    logger.info("[SAFE] Device reset to IDLE — ready for next trigger.")