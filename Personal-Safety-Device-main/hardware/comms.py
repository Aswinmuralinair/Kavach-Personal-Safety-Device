"""
hardware/comms.py — Project Kavach

SIM7600G-H driver. Handles AT commands, SMS, voice calls, GPS, and HTTP uploads.

FIXES APPLIED:
  ① upload_alert() now encrypts the telemetry payload with ChaCha20-Poly1305
      before sending.  The server's POST /api/alerts handler requires an
      `encrypted_payload` form field — sending plaintext resulted in HTTP 400
      on every upload attempt.

  ② upload_alert() now reads `saved_files[0]` from the server response.
      The server returns { "saved_files": [...] }, not { "filename": ... }.
      The old code always got None, so the guardian SMS link was always wrong.

  ③ get_gps_location() now returns a raw "lat,lon" string instead of a full
      Google Maps URL.  alerts.py calls _build_maps_link() on whatever this
      function returns, so returning a URL caused double-processing and broken
      coordinate parsing.  alerts.py already builds the Maps link itself.

  ④ Added close() method so the shared instance can cleanly release the serial
      port on shutdown (called from main.py's finally block).

  ⑤ Replaced bare print() calls with proper logging.
"""

import serial
import time
import json
import base64
import os
import logging
import threading
import requests

logger = logging.getLogger(__name__)


class SIM7600:
    def __init__(self, port, baud=115200, timeout=1):
        self._lock = threading.Lock()
        try:
            self.ser = serial.Serial(port, baud, timeout=timeout)
            logger.info("[SIM7600] Initialized on %s @ %d baud.", port, baud)
        except Exception as e:
            self.ser = None
            logger.error("[SIM7600] Serial not available on %s — %s", port, e)

    # ── FIX ④: close() — call once on shutdown from main.py ──────────────────
    def close(self):
        """Cleanly release the serial port. Safe to call even if never opened."""
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
                logger.info("[SIM7600] Serial port closed.")
        except Exception as exc:
            logger.warning("[SIM7600] Error closing port: %s", exc)

    # ── Internal AT command sender ─────────────────────────────────────────────
    def _send_command(self, command, expected_response, timeout):
        """Send AT command. Caller must hold self._lock if thread-safety needed."""
        if not self.ser:
            return False, "Not connected"
        self.ser.write((command + '\r\n').encode())
        start_time = time.time()
        response   = ''
        while time.time() - start_time < timeout:
            response_line = self.ser.readline().decode('utf-8', errors='ignore')
            if response_line:
                response += response_line
                if expected_response in response:
                    return True, response
        return False, response

    # ── SMS ───────────────────────────────────────────────────────────────────
    def send_sms(self, number, text):
        if not self.ser:
            return False
        with self._lock:
            try:
                self._send_command('AT+CMGF=1', 'OK', 1)
                cmd = f'AT+CMGS="{number}"'
                success, _ = self._send_command(cmd, '>', 2)
                if success:
                    self.ser.write(text.encode() + b"\x1A")
                    sms_success, _ = self._send_command('', 'OK', 20)
                    if sms_success:
                        logger.info("[SIM7600] SMS sent to %s.", number)
                        return True
                logger.warning("[SIM7600] Failed to send SMS to %s.", number)
                return False
            except Exception as e:
                logger.error("[SIM7600] Error during send_sms: %s", e)
                return False

    # ── Voice call ────────────────────────────────────────────────────────────
    def place_call(self, number):
        if not self.ser:
            return False
        with self._lock:
            success, _ = self._send_command(f'ATD{number};', 'OK', 10)
            return success

    def hang_up_call(self):
        if not self.ser:
            return False
        with self._lock:
            return self._send_command('AT+CHUP', 'OK', 5)[0]

    # ── GPS ───────────────────────────────────────────────────────────────────
    def get_gps_location(self):
        """
        Returns a raw coordinate string "lat,lon" (e.g. "12.9716,77.5946"),
        or None if no fix could be obtained.

        FIX ③: Previously returned a full Google Maps URL.  alerts.py calls
        _build_maps_link() on this value, so returning a URL caused it to be
        double-processed and the coordinate parsing broke.  Return raw coords
        here and let alerts.py build the URL as intended.
        """
        if not self.ser:
            return None

        with self._lock:
            self._send_command('AT+CGPS=1,1', 'OK', 1)
            logger.info("[SIM7600] Acquiring GPS fix...")

            coordinates = None
            for _ in range(15):
                success, response = self._send_command('AT+CGPSINFO', '+CGPSINFO:', 2)
                if success and ',,,,,,' not in response:
                    try:
                        parts = response.split(': ')[1].split(',')
                        lat_raw, lat_dir, lon_raw, lon_dir = (
                            parts[0], parts[1], parts[2], parts[3]
                        )
                        lat_deg, lat_min = divmod(float(lat_raw), 100)
                        lon_deg, lon_min = divmod(float(lon_raw), 100)
                        latitude  = lat_deg + (lat_min / 60)
                        longitude = lon_deg + (lon_min / 60)
                        if lat_dir == 'S':
                            latitude  = -latitude
                        if lon_dir == 'W':
                            longitude = -longitude
                        coordinates = f"{latitude},{longitude}"
                        logger.info("[SIM7600] GPS fix: %s", coordinates)
                        break
                    except (ValueError, IndexError):
                        continue
                time.sleep(2)

            if not coordinates:
                logger.warning("[SIM7600] Failed to get GPS fix after 15 attempts.")

            self._send_command('AT+CGPS=0', 'OK', 1)
        return coordinates

    # ── Evidence upload ───────────────────────────────────────────────────────
    def upload_alert(self, server_url, alert_object, file_path):
        """
        Encrypts the alert telemetry and uploads it together with an evidence
        file to the Flask server.

        FIX ①: payload is now ChaCha20-Poly1305 encrypted and sent as
                `encrypted_payload` (base64-encoded) — matching the server's
                POST /api/alerts handler.
        FIX ②: reads `saved_files[0]` from the server JSON response instead of
                the nonexistent `filename` key.

        Returns (True, server_filename) on success, (False, None) on failure.
        """
        if not self.has_internet():
            logger.warning("[SIM7600] No internet — upload skipped.")
            return False, None

        try:
            # Build the JSON payload
            payload_dict = {
                'device_id':           alert_object.device_id,
                'timestamp':           alert_object.timestamp.isoformat(),
                'alert_type':          getattr(alert_object, 'alert_type', None),
                'trigger_source':      getattr(alert_object, 'trigger_source', None),
                'call_placed_status':  alert_object.call_placed_status,
                'guardian_sms_status': alert_object.guardian_sms_status,
                'location_sms_status': alert_object.location_sms_status,
                'gps_location':        alert_object.gps_location,
                'battery_percentage':  alert_object.battery_percentage,
            }

            # FIX ①: encrypt before sending
            from crypto_utils import chacha_encrypt_text
            payload_json  = json.dumps(payload_dict)
            encrypted     = chacha_encrypt_text(payload_json)
            encrypted_b64 = base64.b64encode(encrypted).decode()

            logger.info(
                "[SIM7600] Uploading — device=%s location=%s battery=%s",
                payload_dict['device_id'],
                payload_dict['gps_location'],
                payload_dict['battery_percentage'],
            )

            with open(file_path, 'rb') as f:
                files = {'file': (os.path.basename(file_path), f)}
                r = requests.post(
                    server_url,
                    files=files,
                    data={'encrypted_payload': encrypted_b64},
                    timeout=30,
                )

            if r.status_code == 201:
                logger.info("[SIM7600] Uploaded %s.", os.path.basename(file_path))
                try:
                    # FIX ②: server returns saved_files list, not filename
                    response_json   = r.json()
                    saved_files     = response_json.get('saved_files', [])
                    uploaded_filename = (
                        saved_files[0]
                        if saved_files
                        else os.path.basename(file_path)
                    )
                    return True, uploaded_filename
                except requests.exceptions.JSONDecodeError:
                    return True, os.path.basename(file_path)
            else:
                logger.warning(
                    "[SIM7600] Upload failed — server returned %d.", r.status_code
                )
                return False, None

        except Exception as e:
            logger.error("[SIM7600] Upload error: %s", e)
            return False, None

    # ── Connectivity check ────────────────────────────────────────────────────
    @staticmethod
    def has_internet():
        try:
            requests.head('https://www.google.com', timeout=3)
            return True
        except Exception:
            return False