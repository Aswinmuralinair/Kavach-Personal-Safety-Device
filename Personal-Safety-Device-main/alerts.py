"""
alerts.py — Project Kavach

All three alert pipelines in one place. Each function receives the shared
SIM7600 instance from main.py (single serial port, thread-safe).

  sos_sequence()     → calls police, SMS + WhatsApp guardian, GPS loop, evidence upload to server
  medical_sequence() → calls ambulance, SMS + WhatsApp "MEDICAL EMERGENCY" + GPS + evidence upload
  safe_sequence()    → SMS + WhatsApp "I AM SAFE", cancels any active alert loop

State ownership lives in KavachStateMachine (main.py). This module only
owns the _stop_alert_event used to signal the update loop to exit.
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
from hardware.whatsapp import send_whatsapp

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

    comms.py returns raw "lat,lon" coordinates; this function builds the URL.
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
# WhatsApp helper — silently skips if not configured
# ─────────────────────────────────────────────────────────────────────────────

# Battery percentage below this threshold triggers a WhatsApp low-battery alert
_LOW_BATTERY_THRESHOLD = 15
# Track whether we already sent the low-battery WhatsApp this boot
# (once per boot, not per alert session — prevents spamming)
_low_battery_wa_sent = False

def _send_wa(config: dict, message: str) -> None:
    """
    Send a WhatsApp message via CallMeBot if configured.
    Silently skips if whatsapp_number or whatsapp_apikey are missing/placeholder.
    Never blocks or crashes the alert flow.
    """
    wa_number = config.get('whatsapp_number', '').strip()
    wa_apikey = config.get('whatsapp_apikey', '').strip()

    if (not wa_number or not wa_apikey
            or wa_number == '+91XXXXXXXXXX'
            or wa_apikey == 'YOUR_CALLMEBOT_APIKEY'):
        logger.debug("[WhatsApp] Not configured — skipping.")
        return

    try:
        send_whatsapp(wa_number, wa_apikey, message)
    except Exception as exc:
        logger.warning("[WhatsApp] Send failed (non-fatal): %s", exc)


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
    server_alert_id=None,     # if set, subsequent uploads UPDATE this server row
) -> None:
    """
    Runs every 60 seconds until _stop_alert_event is set (safe button pressed).
    Sends: GPS location SMS, battery SMS to guardian.
    Uploads any NEW evidence files to the server (accessible via app + dashboard).
    Evidence links are NOT sent via SMS — only viewable on server/app.
    """
    logger.info("[%s] Update loop started.", alert_label)

    while not _stop_alert_event.is_set():
        logger.info("[%s] --- 60-second cycle ---", alert_label)

        # 1. Battery
        battery_str = _read_battery(power_monitor)
        alert_row.battery_percentage = battery_str
        sim.send_sms(config['guardian_number'], f"[Kavach {alert_label}] Battery: {battery_str}")

        # 1b. WhatsApp low-battery alert (once per boot)
        global _low_battery_wa_sent
        if not _low_battery_wa_sent:
            try:
                pct_val = float(battery_str.replace('%', '').strip())
                if pct_val <= _LOW_BATTERY_THRESHOLD:
                    _send_wa(config, (
                        f"⚠️ KAVACH LOW BATTERY\n"
                        f"Device battery is at {battery_str}.\n"
                        f"Active alert: {alert_label}\n"
                        f"Time: {_ist_timestamp()}"
                    ))
                    _low_battery_wa_sent = True
                    logger.info("[%s] Low-battery WhatsApp alert sent.", alert_label)
            except (ValueError, AttributeError):
                pass   # battery_str is "N/A" or "Error" — skip

        # 2. GPS — comms.py returns (coords, source) tuple
        location, loc_source = sim.get_gps_location(api_token=config.get('api_token'))
        ts = _ist_timestamp()
        if location:
            logger.info("[%s] Location acquired via %s: %s", alert_label, loc_source, location)
            maps_link = _build_maps_link(location)
            alert_row.gps_location    = location
            alert_row.location_source = loc_source
            sim.send_sms(
                config['guardian_number'],
                f"[Kavach {alert_label}] Location ({loc_source}) at {ts}: {maps_link}"
            )
            # 2b. WhatsApp location update
            _send_wa(config, (
                f"📍 KAVACH {alert_label} — Location Update\n"
                f"Location: {maps_link}\n"
                f"Battery: {battery_str}\n"
                f"Time: {ts}"
            ))
        else:
            sim.send_sms(
                config['guardian_number'],
                f"[Kavach {alert_label}] Location at {ts}: Unable to get GPS fix."
            )

        # 3. Commit GPS + battery to DB
        session.commit()

        # 4. Evidence upload — only files NOT already uploaded this session
        evidence_dir = os.path.join(_BASE_DIR, config.get('evidence_dir', 'evidence'))
        os.makedirs(evidence_dir, exist_ok=True)
        try:
            all_files = [
                f for f in os.listdir(evidence_dir)
                if os.path.isfile(os.path.join(evidence_dir, f))
                and not f.endswith('.txt')   # skip placeholder text files
            ]
            new_files = [f for f in all_files if f not in uploaded_files]

            for file_name in new_files:
                file_path = os.path.join(evidence_dir, file_name)
                success, uploaded_filename, resp_alert_id = sim.upload_alert(
                    config['server_url'], alert_row, file_path,
                    server_alert_id=server_alert_id,
                )
                if resp_alert_id and server_alert_id is None:
                    server_alert_id = resp_alert_id  # capture on first successful upload
                if success:
                    uploaded_files.add(file_name)   # mark as done — never re-send
                    # Evidence is accessible via server dashboard and mobile app.
                    # No SMS with download links — avoids exposing URLs in plain text.
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

def sos_sequence(sim: SIM7600, trigger_source: str = "button",
                 cam=None, mic=None, **kwargs) -> None:
    """
    Full SOS pipeline:
      1. Starts camera + microphone evidence recording
      2. Calls police (config['police_number'])
      3. SMS guardian: "SOS ALERT" + Google Maps link
      4. Enters 60-second GPS + evidence upload loop until safe_sequence() fires

    `sim` is the shared SIM7600 instance from main.py — do NOT construct a new one here.
    `cam` is the CameraManager from main.py (None = no video recording).
    `mic` is the AudioRecorderManager from main.py (None = no audio recording).
    """
    _stop_alert_event.clear()
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
    if power_monitor:
        try:
            alert_row.battery_percentage = f"{power_monitor.battery_percent():.1f}%"
        except Exception:
            pass
    session.add(alert_row)
    session.commit()

    # ── Step 0a: Immediate server upload FIRST (lightweight, no evidence) ──
    # Do this BEFORE starting camera — crypto encryption + HTTP is RAM-heavy
    # and rpicam-vid subprocess competes for memory on the Pi.
    server_alert_id = None
    try:
        _ok, _fname, server_alert_id = sim.upload_alert(
            config['server_url'], alert_row, file_path=None
        )
        logger.info("[SOS] Immediate telemetry upload sent to server (server_alert_id=%s).", server_alert_id)
    except Exception as exc:
        logger.warning("[SOS] Immediate upload failed: %s — continuing.", exc)

    # ── Step 0b: Start evidence capture AFTER upload completes ─────────────
    # Camera subprocess (rpicam-vid) uses GPU RAM; starting it after the
    # upload + gc.collect() in _run_alert() prevents memory spike segfaults.
    if cam:
        cam.start_recording()
        logger.info("[SOS] Camera recording started.")
    if mic:
        time.sleep(3)  # stagger: let camera process stabilise before opening mic
        mic.start_recording()
        logger.info("[SOS] Audio recording started (3s after camera).")

    # ── Step 1: Call police ───────────────────────────────────────────────────
    try:
        logger.info("[SOS] Step 1 — Calling police: %s", config['police_number'])
        if sim.place_call(config['police_number']):
            logger.info("[SOS] Call connected. Waiting 15s...")
            time.sleep(15)
            sim.hang_up_call()
            alert_row.call_placed_status = True
            session.commit()
        else:
            logger.warning("[SOS] Call failed — modem did not respond.")
        time.sleep(2)  # let modem settle between operations
    except Exception as exc:
        logger.error("[SOS] Step 1 (call) failed: %s — continuing.", exc)

    # ── Step 2: SMS + WhatsApp guardian with initial SOS ─────────────────────
    try:
        logger.info("[SOS] Step 2 — Sending SOS SMS to guardian.")
        sent = sim.send_sms(
            config['guardian_number'],
            "SOS ALERT - Emergency triggered on Kavach device. Location SMS to follow."
        )
        alert_row.guardian_sms_status = sent
        session.commit()

        _send_wa(config, (
            "🚨 *KAVACH SOS ALERT*\n"
            "Emergency has been triggered on the Kavach device.\n"
            f"Trigger: {trigger_source}\n"
            f"Time: {_ist_timestamp()}\n"
            "Police have been called. Location updates to follow."
        ))
    except Exception as exc:
        logger.error("[SOS] Step 2 (SMS/WhatsApp) failed: %s — continuing.", exc)

    # ── Step 3: Immediate GPS fix + Maps link ─────────────────────────────────
    try:
        logger.info("[SOS] Step 3 — Acquiring location...")
        location, loc_source = sim.get_gps_location(api_token=config.get('api_token'))
        if location:
            logger.info("[SOS] Location via %s: %s", loc_source, location)
            maps_link = _build_maps_link(location)
            alert_row.gps_location    = location
            alert_row.location_source = loc_source
            sim.send_sms(
                config['guardian_number'],
                f"[SOS] Location ({loc_source}): {maps_link}"
            )
            alert_row.location_sms_status = True
        else:
            sim.send_sms(
                config['guardian_number'],
                "[SOS] GPS fix unavailable. Tracking started."
            )
        session.commit()
    except Exception as exc:
        logger.error("[SOS] Step 3 (GPS) failed: %s — continuing.", exc)

    # ── Step 4: Enter 60-second update loop ───────────────────────────────────
    _run_update_loop(
        sim=sim,
        config=config,
        alert_row=alert_row,
        session=session,
        power_monitor=power_monitor,
        alert_label="SOS",
        uploaded_files=set(),   # fresh set per alert session
        server_alert_id=server_alert_id,
    )

    session.close()
    logger.info("[SOS] Sequence ended.")


# ─────────────────────────────────────────────────────────────────────────────
# 2. MEDICAL ALERT SEQUENCE
# ─────────────────────────────────────────────────────────────────────────────

def medical_sequence(sim: SIM7600, cam=None, mic=None, **kwargs) -> None:
    """
    Medical emergency pipeline:
      0. Starts camera + microphone evidence recording
      1. Calls ambulance / medical contact (config['medical_number'])
      2. SMS guardian AND medical contact: "MEDICAL EMERGENCY" + Google Maps link
      3. Enters the same 60-second GPS + evidence upload loop, tagged "MEDICAL"

    `sim` is the shared SIM7600 instance from main.py — do NOT construct a new one here.
    `cam` is the CameraManager from main.py (None = no video recording).
    `mic` is the AudioRecorderManager from main.py (None = no audio recording).
    """
    _stop_alert_event.clear()
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
    if power_monitor:
        try:
            alert_row.battery_percentage = f"{power_monitor.battery_percent():.1f}%"
        except Exception:
            pass
    session.add(alert_row)
    session.commit()

    # ── Step 0a: Immediate server upload FIRST (lightweight, no evidence) ──
    server_alert_id = None
    try:
        _ok, _fname, server_alert_id = sim.upload_alert(
            config['server_url'], alert_row, file_path=None
        )
        logger.info("[MEDICAL] Immediate telemetry upload sent to server (server_alert_id=%s).", server_alert_id)
    except Exception as exc:
        logger.warning("[MEDICAL] Immediate upload failed: %s — continuing.", exc)

    # ── Step 0b: Start evidence capture AFTER upload completes ─────────────
    if cam:
        cam.start_recording()
        logger.info("[MEDICAL] Camera recording started.")
    if mic:
        time.sleep(3)  # stagger: let camera process stabilise before opening mic
        mic.start_recording()
        logger.info("[MEDICAL] Audio recording started (3s after camera).")

    # ── Step 1: Call ambulance / medical contact ──────────────────────────────
    try:
        medical_number = config.get('medical_number', config.get('police_number'))
        logger.info("[MEDICAL] Step 1 — Calling medical contact: %s", medical_number)
        if sim.place_call(medical_number):
            logger.info("[MEDICAL] Call connected. Waiting 15s...")
            time.sleep(15)
            sim.hang_up_call()
            alert_row.call_placed_status = True
            session.commit()
        else:
            logger.warning("[MEDICAL] Call failed — modem did not respond.")
        time.sleep(2)  # let modem settle
    except Exception as exc:
        logger.error("[MEDICAL] Step 1 (call) failed: %s — continuing.", exc)

    # ── Step 2: Immediate GPS fix ─────────────────────────────────────────────
    maps_link = "GPS unavailable"
    try:
        logger.info("[MEDICAL] Step 2 — Acquiring location...")
        location, loc_source = sim.get_gps_location(api_token=config.get('api_token'))
        maps_link = _build_maps_link(location) if location else "GPS unavailable"
        if location:
            logger.info("[MEDICAL] Location via %s: %s", loc_source, location)
            alert_row.gps_location        = location
            alert_row.location_source     = loc_source
            alert_row.location_sms_status = True
            session.commit()
    except Exception as exc:
        logger.error("[MEDICAL] Step 2 (GPS) failed: %s — continuing.", exc)

    # ── Step 3: SMS + WhatsApp guardian ──────────────────────────────────────
    try:
        logger.info("[MEDICAL] Step 3 — Sending MEDICAL EMERGENCY SMS + WhatsApp to guardian.")
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

        _send_wa(config, (
            "🏥 *KAVACH MEDICAL EMERGENCY*\n"
            "A medical emergency has been detected.\n"
            f"Location: {maps_link}\n"
            f"Time: {_ist_timestamp()}\n"
            "Ambulance has been called. Please respond immediately."
        ))
    except Exception as exc:
        logger.error("[MEDICAL] Step 3 (SMS/WhatsApp) failed: %s — continuing.", exc)

    # ── Step 4: SMS medical contact separately (if different from guardian) ───
    try:
        if config.get('medical_number') and config['medical_number'] != config['guardian_number']:
            logger.info("[MEDICAL] Step 4 — Notifying medical contact by SMS.")
            medical_msg = (
                f"MEDICAL EMERGENCY — KAVACH DEVICE ALERT\n"
                f"User requires immediate medical assistance.\n"
                f"Location: {maps_link}\n"
                f"Time: {_ist_timestamp()}"
            )
            sim.send_sms(config['medical_number'], medical_msg)
    except Exception as exc:
        logger.error("[MEDICAL] Step 4 (medical SMS) failed: %s — continuing.", exc)

    # ── Step 5: Enter 60-second update loop ───────────────────────────────────
    _run_update_loop(
        sim=sim,
        config=config,
        alert_row=alert_row,
        session=session,
        power_monitor=power_monitor,
        alert_label="MEDICAL",
        uploaded_files=set(),
        server_alert_id=server_alert_id,
    )

    session.close()
    logger.info("[MEDICAL] Sequence ended.")


# ─────────────────────────────────────────────────────────────────────────────
# 3. SAFE SEQUENCE
# ─────────────────────────────────────────────────────────────────────────────

def safe_sequence(sim: SIM7600, was_active_type: str = None,
                  cam=None, mic=None) -> None:
    """
    'I am safe' pipeline:
      - Stops camera + microphone evidence recording
      - Stops the running SOS/MEDICAL loop immediately via _stop_alert_event
      - SMS guardian: "I AM SAFE" confirmation
      - SMS police cancellation if SOS was active (to prevent false response)

    `sim` is the shared SIM7600 instance from main.py — do NOT construct a new one here.
    `was_active_type` is passed from KavachStateMachine so we know what to cancel.
    `cam` is the CameraManager from main.py (None = no video recording).
    `mic` is the AudioRecorderManager from main.py (None = no audio recording).
    """
    logger.info("[SAFE] Long press detected — sending SAFE alert.")
    config = _load_config()

    # ── Stop evidence recording ───────────────────────────────────────────────
    if cam:
        cam.stop_recording()
        logger.info("[SAFE] Camera recording stopped.")
    if mic:
        mic.stop_recording()
        logger.info("[SAFE] Audio recording stopped.")

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
    try:
        sim.send_sms(config['guardian_number'], message)
        logger.info("[SAFE] Safe SMS sent to guardian.")
    except Exception as exc:
        logger.error("[SAFE] Guardian SMS failed: %s", exc)

    # WhatsApp safe confirmation
    if was_active_type:
        _send_wa(config, (
            f"✅ *KAVACH — USER IS SAFE*\n"
            f"The {was_active_type.upper()} alert has been cancelled.\n"
            f"The user has confirmed they are safe.\n"
            f"Time: {ts}"
        ))
    else:
        _send_wa(config, (
            f"✅ *KAVACH — SAFE CHECK-IN*\n"
            f"The user has confirmed they are safe.\n"
            f"Time: {ts}"
        ))

    try:
        if was_active_type == "sos":
            cancel_msg = (
                f"KAVACH DEVICE — FALSE ALARM CANCEL\n"
                f"The SOS alert from this device at {ts} has been cancelled by the user. "
                f"No further response required."
            )
            sim.send_sms(config['police_number'], cancel_msg)
            logger.info("[SAFE] Cancellation SMS also sent to police number.")
    except Exception as exc:
        logger.error("[SAFE] Police cancel SMS failed: %s", exc)

    logger.info("[SAFE] Device reset to IDLE — ready for next trigger.")