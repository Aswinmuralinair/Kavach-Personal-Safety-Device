"""
alerts.py — Project Kavach

All three alert pipelines in one place.

  sos_sequence()     → calls police, SMS guardian, GPS loop, evidence upload
  medical_sequence() → calls ambulance/medical contact, SMS "MEDICAL EMERGENCY" + GPS
  safe_sequence()    → SMS "I AM SAFE" to guardian, cancels any active SOS loop

FIXES APPLIED:

① Serial port conflict (original fix kept):
    Every function accepts `sim: SIM7600` as its first argument.
    A single shared instance is created once at boot in main.py and passed in.

② GPS double-URL bug:
    comms.py:get_gps_location() now returns raw "lat,lon" coordinates, not a
    full Google Maps URL.  The _build_maps_link() helper here correctly turns
    those coordinates into a Maps URL.  Previously this caused the URL to be
    re-processed as if it were a coordinate string, and the fallback silently
    returned the raw URL — so the bug was invisible but data was wrong.

③ Duplicate state removed:
    The duplicate _alert_lock / _active_alert_type state has been removed.
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
# Base directory — all relative paths resolve from here, not the CWD.
# This ensures alerts.py works when launched via systemd or from another dir.
# ─────────────────────────────────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────────────────────────────────────────
# Single module-level stop event — used so safe_sequence() can wake the update
# loop across threads.  State ownership stays in KavachStateMachine (main.py).
# ─────────────────────────────────────────────────────────────────────────────
_stop_alert_event = threading.Event()

# ─────────────────────────────────────────────────────────────────────────────
# Shared SQLAlchemy engine — created ONCE, not per call
# ─────────────────────────────────────────────────────────────────────────────
_db_path = os.path.join(_BASE_DIR, 'alerts.db')
_ENGINE = create_engine(f'sqlite:///{_db_path}')
Base.metadata.create_all(_ENGINE)
_SessionFactory = sessionmaker(bind=_ENGINE)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    config_path = os.path.join(_BASE_DIR, 'config.json')
    with open(config_path, 'r') as f:
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
    Turns a raw GPS string "lat,lon" into a Google Maps link.

    FIX ②: comms.py now returns raw "lat,lon" so this function works correctly.
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
    uploaded_files: set,      # tracks already-uploaded filenames (prevents re-upload)
) -> None:
    """
    Runs every 60 seconds until _stop_alert_event is set (safe button pressed).
    Sends: GPS location SMS, battery SMS, uploads any NEW evidence files.
    """
    logger.info("[%s] Update loop started.", alert_label)

    while not _stop_alert_event.is_set():
        logger.info("[%s] --- 60-second cycle ---", alert_label)

        # 1. Battery
        battery_str = _read_battery(power_monitor)
        alert_row.battery_percentage = battery_str
        sim.send_sms(config['guardian_number'], f"[Kavach {alert_label}] Battery: {battery_str}")

        # 2. GPS — comms.py returns raw "lat,lon"; build the Maps link here
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
        evidence_dir = os.path.join(_BASE_DIR, config.get('evidence_dir', 'evidence'))
        try:
            all_files = [
                f for f in os.listdir(evidence_dir)
                if os.path.isfile(os.path.join(evidence_dir, f))
                and not f.endswith('.txt')   # skip placeholder text files
            ]
            new_files = [f for f in all_files if f not in uploaded_files]

            for file_name in new_files:
                file_path = os.path.join(evidence_dir, file_name)
                success, uploaded_filename = sim.upload_alert(
                    config['server_url'], alert_row, file_path
                )
                if success:
                    uploaded_files.add(file_name)   # mark as done — never re-send
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
# 1. SOS SEQUENCE
# ─────────────────────────────────────────────────────────────────────────────

def sos_sequence(sim: SIM7600, trigger_source: str = "button", camera=None) -> None:
    """
    Full SOS pipeline:
      1. Start camera recording (if camera hardware available)
      2. Calls police (config['police_number'])
      3. SMS guardian: "SOS ALERT" + Google Maps link
      4. Enters 60-second GPS + evidence upload loop until safe_sequence() fires

    `sim` is the shared SIM7600 instance from main.py — do NOT construct a new one here.
    `camera` is the shared CameraManager from main.py (may be None).
    """
    _stop_alert_event.clear()

    # Start evidence recording immediately
    if camera:
        camera.start_recording()
    logger.info("[SOS] ACTIVATED — trigger: %s", trigger_source)

    config        = _load_config()
    power_monitor = _init_power_monitor()
    session       = _get_db_session()

    alert_row = Alert(
        device_id          = config['device_id'],
        timestamp          = datetime.now(timezone.utc),
        trigger_source     = trigger_source,
        alert_type         = "SOS",
        battery_percentage = "N/A" if not I2C_AVAILABLE else None,
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
        "SOS ALERT - Emergency triggered on Kavach device. Location SMS to follow."
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
            f"[SOS] Last known location: {maps_link}"
        )
        alert_row.location_sms_status = True
    else:
        sim.send_sms(
            config['guardian_number'],
            "[SOS] GPS fix unavailable. Tracking started."
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
        uploaded_files=set(),   # fresh set per alert session
    )

    session.close()
    logger.info("[SOS] Sequence ended.")


# ─────────────────────────────────────────────────────────────────────────────
# 2. MEDICAL ALERT SEQUENCE
# ─────────────────────────────────────────────────────────────────────────────

def medical_sequence(sim: SIM7600, camera=None) -> None:
    """
    Medical emergency pipeline:
      1. Start camera recording (if camera hardware available)
      2. Calls ambulance / medical contact (config['medical_number'])
      3. SMS guardian AND medical contact: "MEDICAL EMERGENCY" + Google Maps link
      4. Enters the same 60-second GPS + evidence upload loop, tagged "MEDICAL"

    `sim` is the shared SIM7600 instance from main.py — do NOT construct a new one here.
    `camera` is the shared CameraManager from main.py (may be None).
    """
    _stop_alert_event.clear()

    # Start evidence recording immediately
    if camera:
        camera.start_recording()
    logger.info("[MEDICAL] ACTIVATED — double press.")

    config        = _load_config()
    power_monitor = _init_power_monitor()
    session       = _get_db_session()

    alert_row = Alert(
        device_id          = config['device_id'],
        timestamp          = datetime.now(timezone.utc),
        trigger_source     = "double_press",
        alert_type         = "MEDICAL",
        battery_percentage = "N/A" if not I2C_AVAILABLE else None,
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

    # ── Step 3: SMS guardian ──────────────────────────────────────────────────
    logger.info("[MEDICAL] Step 3 — Sending MEDICAL EMERGENCY SMS to guardian.")
    guardian_msg = (
        f"MEDICAL EMERGENCY\n"
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
            f"MEDICAL EMERGENCY — KAVACH DEVICE ALERT\n"
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

    session.close()
    logger.info("[MEDICAL] Sequence ended.")


# ─────────────────────────────────────────────────────────────────────────────
# 3. SAFE SEQUENCE
# ─────────────────────────────────────────────────────────────────────────────

def safe_sequence(sim: SIM7600, was_active_type: str = None, camera=None) -> None:
    """
    'I am safe' pipeline:
      - Stops camera recording (if running)
      - Stops the running SOS/MEDICAL loop immediately via _stop_alert_event
      - SMS guardian: "I AM SAFE" confirmation
      - SMS police cancellation if SOS was active (to prevent false response)

    `sim` is the shared SIM7600 instance from main.py — do NOT construct a new one here.
    `was_active_type` is passed from KavachStateMachine so we know what to cancel.
    `camera` is the shared CameraManager from main.py (may be None).
    """
    logger.info("[SAFE] Long press detected — sending SAFE alert.")
    config = _load_config()

    # ── Stop camera recording ─────────────────────────────────────────────────
    if camera:
        camera.stop_recording()

    # ── Cancel any running alert loop ─────────────────────────────────────────
    if was_active_type:
        logger.info("[SAFE] Cancelling active %s alert.", was_active_type.upper())
        _stop_alert_event.set()   # wake + exit the update loop immediately
        time.sleep(2)             # give the loop up to 2 s to exit cleanly

    # ── Build the SMS ─────────────────────────────────────────────────────────
    ts = _ist_timestamp()
    if was_active_type:
        message = (
            f"SAFE CONFIRMATION\n"
            f"The previous {was_active_type.upper()} alert has been CANCELLED.\n"
            f"The user has confirmed they are safe.\n"
            f"Time: {ts}"
        )
    else:
        message = (
            f"SAFE CHECK-IN\n"
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