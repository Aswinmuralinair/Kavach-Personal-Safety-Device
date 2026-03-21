"""
hardware/comms.py — Project Kavach

SIM7600G-H driver. Handles AT commands, SMS, voice calls, GPS (with
cell tower fallback via Unwired Labs), and encrypted evidence uploads
to the Kavach server. Thread-safe — all public methods acquire
self._lock before accessing the serial port.
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

    # ── close() — called from main.py's finally block on shutdown ────────────
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
        try:
            # Flush stale data before sending new command
            self.ser.reset_input_buffer()
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
        except Exception as e:
            logger.error("[SIM7600] _send_command('%s') error: %s", command[:20], e)
            return False, str(e)

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
            try:
                success, _ = self._send_command(f'ATD{number};', 'OK', 10)
                return success
            except Exception as e:
                logger.error("[SIM7600] place_call error: %s", e)
                return False

    def hang_up_call(self):
        if not self.ser:
            return False
        with self._lock:
            try:
                return self._send_command('AT+CHUP', 'OK', 5)[0]
            except Exception as e:
                logger.error("[SIM7600] hang_up error: %s", e)
                return False

    # ── GPS (primary) + Cell Tower fallback ──────────────────────────────────
    def get_gps_location(self, api_token: str = None):
        """
        Returns a raw coordinate string "lat,lon" (e.g. "12.9716,77.5946"),
        or None if no location could be obtained.

        Location strategy (two-tier):
          1. GPS via AT+CGPS — accurate to ~3 m (needs sky view, may take 30+ s)
          2. Cell tower via AT+CPSI? + Unwired Labs API — accurate to ~200-500 m
             (works indoors, needs internet + api_token from config.json)

        alerts.py builds the Google Maps URL from raw coordinates via _build_maps_link().
        """
        if not self.ser:
            return None

        with self._lock:
            # ── Tier 1: GPS satellite fix ─────────────────────────────────────
            self._send_command('AT+CGPS=1,1', 'OK', 1)
            logger.info("[SIM7600] Tier 1 — Acquiring GPS fix...")

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

            self._send_command('AT+CGPS=0', 'OK', 1)

            if coordinates:
                return coordinates

            # ── Tier 2: Cell tower fallback via Unwired Labs ──────────────────
            logger.warning("[SIM7600] GPS failed after 15 attempts — trying cell tower fallback.")
            coordinates = self._cell_tower_locate(api_token)

        return coordinates

    def _cell_tower_locate(self, api_token: str = None) -> str:
        """
        Reads the serving cell tower info via AT+CPSI? and queries the
        Unwired Labs Geolocation API to get an approximate lat/lon.

        AT+CPSI? response format (LTE example):
          +CPSI: LTE,Online,404-30,0x1234,12345678,100,EUTRAN-BAND40,38950,...

        Fields: mode, status, MCC-MNC, LAC_hex, CellID, ...

        Returns "lat,lon" string or None on failure.
        """
        if not api_token or api_token.startswith('YOUR_'):
            logger.warning("[SIM7600] No api_token configured — cell tower fallback skipped.")
            return None

        # Read serving cell info from SIM7600
        success, response = self._send_command('AT+CPSI?', '+CPSI:', 3)
        if not success:
            logger.warning("[SIM7600] AT+CPSI? failed — no cell tower info.")
            return None

        try:
            # Parse: +CPSI: LTE,Online,404-30,0x1234,12345678,...
            cpsi_line = response.split('+CPSI:')[1].strip().split('\r')[0]
            fields    = [f.strip() for f in cpsi_line.split(',')]

            if len(fields) < 5:
                logger.warning("[SIM7600] AT+CPSI? response too short: %s", cpsi_line)
                return None

            # fields[0] = mode (LTE/WCDMA/GSM)
            # fields[2] = MCC-MNC (e.g. "404-30")
            # fields[3] = LAC/TAC hex (e.g. "0x1234")
            # fields[4] = CellID (decimal or hex)
            mcc_mnc = fields[2]
            mcc, mnc = mcc_mnc.split('-')

            # LAC/TAC — strip 0x prefix if present
            lac_str = fields[3]
            lac = int(lac_str, 16) if lac_str.startswith('0x') else int(lac_str)

            # CellID — may be decimal or hex
            cid_str = fields[4]
            cid = int(cid_str, 16) if cid_str.startswith('0x') else int(cid_str)

            logger.info(
                "[SIM7600] Cell tower: MCC=%s MNC=%s LAC=%d CID=%d",
                mcc, mnc, lac, cid
            )
        except (ValueError, IndexError) as exc:
            logger.error("[SIM7600] Failed to parse AT+CPSI? response: %s", exc)
            return None

        # Query Unwired Labs Geolocation API
        try:
            payload = {
                "token": api_token,
                "radio": fields[0].lower(),   # "lte", "gsm", "wcdma"
                "mcc":   int(mcc),
                "mnc":   int(mnc),
                "cells": [{
                    "lac": lac,
                    "cid": cid,
                }],
            }

            r = requests.post(
                "https://us1.unwiredlabs.com/v2/process.php",
                json=payload,
                timeout=10,
            )

            result = r.json()
            if result.get("status") == "ok":
                lat = result["lat"]
                lon = result["lon"]
                accuracy = result.get("accuracy", "unknown")
                coordinates = f"{lat},{lon}"
                logger.info(
                    "[SIM7600] Cell tower location: %s (accuracy ~%s m)",
                    coordinates, accuracy
                )
                return coordinates
            else:
                logger.warning(
                    "[SIM7600] Unwired Labs API error: %s",
                    result.get("message", "unknown error")
                )
                return None

        except Exception as exc:
            logger.error("[SIM7600] Cell tower API request failed: %s", exc)
            return None

    # ── Evidence upload ───────────────────────────────────────────────────────
    def upload_alert(self, server_url, alert_object, file_path):
        """
        Encrypts the alert telemetry AND evidence file with ChaCha20-Poly1305,
        computes a SHA-256 hash of the original file for server-side integrity
        verification, and uploads everything to the Kavach server.

        Upload fields sent:
          encrypted_payload  — base64-encoded ChaCha20 encrypted JSON telemetry
          file               — ChaCha20 encrypted evidence file bytes
          file_encrypted     — "true" (tells server to decrypt the file)
          file_sha256        — SHA-256 hex digest of the ORIGINAL file (pre-encryption)

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

            # Encrypt telemetry JSON
            from crypto_utils import chacha_encrypt_text, chacha_encrypt_bytes
            import hashlib

            payload_json  = json.dumps(payload_dict)
            encrypted     = chacha_encrypt_text(payload_json)
            encrypted_b64 = base64.b64encode(encrypted).decode()

            logger.info(
                "[SIM7600] Uploading — device=%s location=%s battery=%s",
                payload_dict['device_id'],
                payload_dict['gps_location'],
                payload_dict['battery_percentage'],
            )

            # Read, hash, and encrypt the evidence file
            with open(file_path, 'rb') as f:
                raw_file_bytes = f.read()

            # SHA-256 of the ORIGINAL file (before encryption) for server verification
            file_hash = hashlib.sha256(raw_file_bytes).hexdigest()

            # Encrypt the evidence file with ChaCha20-Poly1305
            encrypted_file_bytes = chacha_encrypt_bytes(raw_file_bytes)

            logger.info(
                "[SIM7600] Evidence encrypted: %s (%d → %d bytes, hash=%s...)",
                os.path.basename(file_path),
                len(raw_file_bytes),
                len(encrypted_file_bytes),
                file_hash[:16],
            )

            import io
            encrypted_file_obj = io.BytesIO(encrypted_file_bytes)
            files = {'file': (os.path.basename(file_path), encrypted_file_obj)}
            r = requests.post(
                server_url,
                files=files,
                data={
                    'encrypted_payload': encrypted_b64,
                    'file_encrypted':    'true',
                    'file_sha256':       file_hash,
                },
                timeout=30,
            )

            if r.status_code == 201:
                logger.info("[SIM7600] Uploaded %s.", os.path.basename(file_path))
                try:
                    # Server returns saved_files list
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