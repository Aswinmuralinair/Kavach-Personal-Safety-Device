"""
hardware/comms.py — Project Kavach

SIM7600G-H driver. Handles AT commands, SMS, voice calls, GPS, and HTTP uploads.

CHANGE vs original:
  • Added close() method so the shared instance can cleanly release the serial
    port on shutdown (called from main.py's finally block).
  • Replaced bare print() calls with proper logging.
  • No other logic changes — all AT command behaviour is identical.
"""

import serial
import time
import requests
import os
import logging

logger = logging.getLogger(__name__)


class SIM7600:

    def __init__(self, port, baud=115200, timeout=1):
        try:
            self.ser = serial.Serial(port, baud, timeout=timeout)
            logger.info("[SIM7600] Initialized on %s @ %d baud.", port, baud)
        except Exception as e:
            self.ser = None
            logger.error("[SIM7600] Serial not available on %s — %s", port, e)

    # ─────────────────────────────────────────────────────────────────────────
    # NEW: close() — call this once on shutdown from main.py
    # ─────────────────────────────────────────────────────────────────────────

    def close(self):
        """Cleanly release the serial port. Safe to call even if never opened."""
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
                logger.info("[SIM7600] Serial port closed.")
        except Exception as exc:
            logger.warning("[SIM7600] Error closing port: %s", exc)

    # ─────────────────────────────────────────────────────────────────────────
    # Internal AT command sender
    # ─────────────────────────────────────────────────────────────────────────

    def _send_command(self, command, expected_response, timeout):
        if not self.ser:
            return False, "Not connected"
        self.ser.write((command + '\r\n').encode())
        start_time = time.time()
        response = ''
        while time.time() - start_time < timeout:
            response_line = self.ser.readline().decode('utf-8', errors='ignore')
            if response_line:
                response += response_line
                if expected_response in response:
                    return True, response
        return False, response

    # ─────────────────────────────────────────────────────────────────────────
    # SMS
    # ─────────────────────────────────────────────────────────────────────────

    def send_sms(self, number, text):
        if not self.ser:
            return False
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

    # ─────────────────────────────────────────────────────────────────────────
    # Voice call
    # ─────────────────────────────────────────────────────────────────────────

    def place_call(self, number):
        if not self.ser:
            return False
        success, _ = self._send_command(f'ATD{number};', 'OK', 10)
        return success

    def hang_up_call(self):
        if not self.ser:
            return False
        return self._send_command('AT+CHUP', 'OK', 5)[0]

    # ─────────────────────────────────────────────────────────────────────────
    # GPS
    # ─────────────────────────────────────────────────────────────────────────

    def get_gps_location(self):
        if not self.ser:
            return None
        self._send_command('AT+CGPS=1,1', 'OK', 1)
        logger.info("[SIM7600] Acquiring GPS fix...")
        maps_link = None
        for i in range(15):
            success, response = self._send_command('AT+CGPSINFO', '+CGPSINFO:', 2)
            if success and ',,,,,,' not in response:
                try:
                    parts = response.split(': ')[1].split(',')
                    lat_raw, lat_dir, lon_raw, lon_dir = (
                        parts[0], parts[1], parts[2], parts[3]
                    )
                    lat_deg, lat_min = divmod(float(lat_raw), 100)
                    lon_deg, lon_min = divmod(float(lon_raw), 100)
                    latitude = lat_deg + (lat_min / 60)
                    longitude = lon_deg + (lon_min / 60)
                    if lat_dir == 'S':
                        latitude = -latitude
                    if lon_dir == 'W':
                        longitude = -longitude
                    maps_link = f"https://maps.google.com/?q={latitude},{longitude}"
                    logger.info("[SIM7600] GPS fix: %s", maps_link)
                    break
                except (ValueError, IndexError):
                    continue
            time.sleep(2)
        if not maps_link:
            logger.warning("[SIM7600] Failed to get GPS fix after 15 attempts.")
        self._send_command('AT+CGPS=0', 'OK', 1)
        return maps_link

    # ─────────────────────────────────────────────────────────────────────────
    # Evidence upload
    # ─────────────────────────────────────────────────────────────────────────

    def upload_alert(self, server_url, alert_object, file_path):
        """
        Uploads a file and the full alert row data to the Flask server.
        Returns (True, filename) on success, (False, None) on failure.
        """
        if not self.has_internet():
            logger.warning("[SIM7600] No internet — upload skipped.")
            return False, None
        try:
            data = {
                'device_id':           alert_object.device_id,
                'timestamp':           alert_object.timestamp.isoformat(),
                'call_placed_status':  alert_object.call_placed_status,
                'guardian_sms_status': alert_object.guardian_sms_status,
                'location_sms_status': alert_object.location_sms_status,
                'location':            alert_object.gps_location,
                'battery_percentage':  alert_object.battery_percentage,
            }
            logger.info(
                "[SIM7600] Uploading — device=%s location=%s battery=%s",
                data['device_id'], data['location'], data['battery_percentage'],
            )
            with open(file_path, 'rb') as f:
                files = {'file': (os.path.basename(file_path), f)}
                r = requests.post(server_url, files=files, data=data, timeout=30)
            if r.status_code == 201:
                logger.info("[SIM7600] Uploaded %s.", os.path.basename(file_path))
                try:
                    uploaded_filename = r.json().get('filename')
                    if not uploaded_filename:
                        uploaded_filename = os.path.basename(file_path)
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

    # ─────────────────────────────────────────────────────────────────────────
    # Connectivity check
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def has_internet():
        try:
            requests.get('https://www.google.com', timeout=5)
            return True
        except Exception:
            return False