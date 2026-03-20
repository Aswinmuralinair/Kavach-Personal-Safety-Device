"""
app.py - Kavach Server

Flask API with admin dashboard, mobile app auth, and device telemetry.
All data routes are authenticated — no public access to alert data,
evidence files, or device configuration.

  GET  /                   - admin dashboard (session login required)
  POST /api/alerts         - receive encrypted telemetry + evidence (ChaCha20 = implicit auth)
  GET  /api/alerts         - list alerts (admin session or Bearer token)
  GET  /api/alerts/<id>    - alert detail + hash verification (admin session or Bearer token)
  GET  /api/health         - server + database health check (public)
  GET  /uploads/<file>     - serve evidence files (auth or signed download token)
  POST /api/auth/signup    - create mobile app account (user/guardian)
  POST /api/auth/login     - get auth token for mobile app
  GET  /api/user/alerts    - user's alerts (Bearer token, user role)
  GET  /api/guardian/alerts - guardian's alerts (Bearer token, guardian role)
  GET  /api/user/locations - location history (Bearer token, user role)
  GET  /api/user/config    - get device phone numbers (Bearer token, user role)
  PUT  /api/user/config    - update phone numbers from app (Bearer token, user role)
  GET  /api/device/config/<device_id> - Pi polls this (X-Device-Key header)
  GET  /api/guardian/evidence/<id> - evidence files (Bearer token, guardian role)
"""

from flask import Flask, request, jsonify, send_from_directory, render_template, session, redirect, url_for
from flask_cors import CORS
from functools import wraps
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import os
import sys
import json
import base64
import logging
import datetime
import uuid
import webbrowser
import threading
import subprocess

from database import DB, Alert
from utils import save_file_safe, compute_sha256, decrypt_file_in_place
from crypto_utils import chacha_decrypt_text

# ─────────────────────────────────────────────────────────────────────────────
# Logging - structured, goes to stdout (visible in server terminal)
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("kavach.server")

# ─────────────────────────────────────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

app.config['SQLALCHEMY_DATABASE_URI']        = 'sqlite:///kavach.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH']             = 64 * 1024 * 1024   # 64 MB max upload

DB.init_app(app)

_APP_DIR   = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(_APP_DIR, 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

CONFIG_DIR = os.path.join(_APP_DIR, 'device_configs')
os.makedirs(CONFIG_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# In-memory device status — updated every time the Pi polls /api/device/config
# ─────────────────────────────────────────────────────────────────────────────
_device_status = {}   # { device_id: { "battery": "85%", "last_seen": datetime } }
_DEVICE_ONLINE_TIMEOUT = 30   # seconds — device is "offline" if not seen in 30s


def _update_device_status(device_id: str, battery: str):
    """Record the latest heartbeat from a device."""
    _device_status[device_id] = {
        'battery':   battery,
        'last_seen': datetime.datetime.now(_UTC),
    }


def _get_device_status(device_id: str) -> dict:
    """Return device battery and online/offline status."""
    info = _device_status.get(device_id)
    if not info:
        return {'battery': None, 'online': False, 'last_seen': None}
    elapsed = (datetime.datetime.now(_UTC) - info['last_seen']).total_seconds()
    return {
        'battery':   info['battery'],
        'online':    elapsed <= _DEVICE_ONLINE_TIMEOUT,
        'last_seen': info['last_seen'].isoformat(),
    }


def _load_device_config(device_id: str) -> dict:
    """Load device config from JSON file."""
    path = os.path.join(CONFIG_DIR, f'{device_id}.json')
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return {}


def _save_device_config(device_id: str, config: dict):
    """Save device config to JSON file."""
    path = os.path.join(CONFIG_DIR, f'{device_id}.json')
    with open(path, 'w') as f:
        json.dump(config, f, indent=2)

# ─────────────────────────────────────────────────────────────────────────────
# Mobile app API auth (Kavach Flutter app)
# Uses itsdangerous (bundled with Flask) for signed tokens - no PyJWT needed.
# SECRET_KEY is persisted to .secret_key file so auth tokens survive
# server restarts.  Override via KAVACH_SECRET_KEY env var if desired.
# ─────────────────────────────────────────────────────────────────────────────
def _load_or_create_secret_key() -> str:
    """Load SECRET_KEY from env var or .secret_key file; create file if missing."""
    env_key = os.environ.get('KAVACH_SECRET_KEY')
    if env_key:
        return env_key
    base_dir = os.path.dirname(os.path.abspath(__file__))
    key_file = os.path.join(base_dir, '.secret_key')
    if os.path.exists(key_file):
        with open(key_file, 'r') as f:
            return f.read().strip()
    # First run - generate and persist
    new_key = os.urandom(32).hex()
    with open(key_file, 'w') as f:
        f.write(new_key)
    logger.info("[Auth] Generated new SECRET_KEY -- saved to .secret_key")
    return new_key

app.config['SECRET_KEY'] = _load_or_create_secret_key()
_token_serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])

# ─────────────────────────────────────────────────────────────────────────────
# UTC alias — datetime.timezone.utc works on Python 3.2+
# ─────────────────────────────────────────────────────────────────────────────
_UTC = datetime.timezone.utc

# Boot time - used in /api/health uptime calculation
_SERVER_START_TIME = datetime.datetime.now(_UTC)


# ─────────────────────────────────────────────────────────────────────────────
# Admin credentials — CHANGE DEFAULTS before production use!
# ─────────────────────────────────────────────────────────────────────────────
ADMIN_USERNAME = os.environ.get('KAVACH_ADMIN_USER', 'admin')
ADMIN_PASSWORD = os.environ.get('KAVACH_ADMIN_PASS', 'kavach2026')

# ─────────────────────────────────────────────────────────────────────────────
# Device API key — the Raspberry Pi sends this in the X-Device-Key header
# when polling /api/device/config.  Override via env var for production.
# ─────────────────────────────────────────────────────────────────────────────
KAVACH_DEVICE_KEY = os.environ.get('KAVACH_DEVICE_KEY', 'kavach-device-key-2026')

# ─────────────────────────────────────────────────────────────────────────────
# App user accounts (stored in app_users.json)
# ─────────────────────────────────────────────────────────────────────────────
from werkzeug.security import generate_password_hash, check_password_hash
import re as _re

APP_USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app_users.json')

# Regex for basic phone number validation
_PHONE_RE = _re.compile(r'^\+?[\d\s\-()]{3,20}$')


def _load_app_users() -> dict:
    """Load registered app users from JSON file."""
    if os.path.exists(APP_USERS_FILE):
        try:
            with open(APP_USERS_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _save_app_users(users: dict):
    """Save app users to JSON file."""
    with open(APP_USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)


def _hash_password(password: str) -> str:
    """Hash password with werkzeug (pbkdf2 with salt)."""
    return generate_password_hash(password)


def _check_password(stored_hash: str, password: str) -> bool:
    """Verify password against stored hash. Backward-compatible with old SHA-256 hashes."""
    # Backward compatibility: old accounts used bare SHA-256 before pbkdf2 migration
    if len(stored_hash) == 64 and not stored_hash.startswith('pbkdf2:'):
        import hashlib
        return stored_hash == hashlib.sha256(password.encode()).hexdigest()
    return check_password_hash(stored_hash, password)


def admin_required(f):
    """Decorator: redirect to login if not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────────────────────────────────────
# Auth helpers — used by API routes that need flexible authentication
# ─────────────────────────────────────────────────────────────────────────────

def _check_any_auth() -> bool:
    """
    Return True if the request carries valid authentication:
      1. Admin session cookie (from dashboard login), OR
      2. Valid Bearer token (from mobile app login).
    """
    if session.get('admin_logged_in'):
        return True
    try:
        _verify_token(request)
        return True
    except (ValueError, Exception):
        return False


def _check_device_key() -> bool:
    """Return True if the X-Device-Key header matches KAVACH_DEVICE_KEY."""
    return request.headers.get('X-Device-Key') == KAVACH_DEVICE_KEY


def _create_download_token(filename: str) -> str:
    """
    Create a short-lived signed token for downloading a specific evidence file.
    Used because the Flutter app opens files in an external browser which
    cannot send Authorization headers — so we embed auth in the URL.
    """
    return _token_serializer.dumps({'filename': filename, 'type': 'download'})


def _verify_download_token(filename: str) -> bool:
    """Verify a signed download token from the ?token= query parameter."""
    token = request.args.get('token', '')
    if not token:
        return False
    try:
        data = _token_serializer.loads(token, max_age=3600)  # 1-hour expiry
        return data.get('filename') == filename and data.get('type') == 'download'
    except (SignatureExpired, BadSignature, KeyError):
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Helper: generate a short request ID for tracing
# ─────────────────────────────────────────────────────────────────────────────
def _request_id() -> str:
    return uuid.uuid4().hex[:8].upper()


# ─────────────────────────────────────────────────────────────────────────────
# Admin Login / Logout
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            return redirect(url_for('dashboard'))
        error = 'Invalid username or password'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('login'))


# ─────────────────────────────────────────────────────────────────────────────
# GET / - Admin Dashboard (protected)
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/')
@admin_required
def dashboard():
    return render_template('dashboard.html')


# ─────────────────────────────────────────────────────────────────────────────
# App API - Auth + Role-based endpoints (Kavach Flutter app)
# ─────────────────────────────────────────────────────────────────────────────

def _create_token(device_id: str, role: str) -> str:
    """Create a signed token containing device_id and role."""
    return _token_serializer.dumps({'device_id': device_id, 'role': role})


def _verify_token(req) -> tuple:
    """
    Verify the Authorization: Bearer <token> header.
    Returns (device_id, role) on success.
    Raises ValueError with message on failure.
    """
    auth = req.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        raise ValueError('Missing or invalid Authorization header. Expected: Bearer <token>')
    token = auth[7:]
    try:
        data = _token_serializer.loads(token, max_age=86400)  # 24-hour expiry
        return data['device_id'], data['role']
    except SignatureExpired:
        raise ValueError('Token expired. Please login again.')
    except (BadSignature, KeyError):
        raise ValueError('Invalid token.')


@app.route('/api/auth/signup', methods=['POST'])
def auth_signup():
    """
    Register a new app account.
    Accepts: { "device_id": "KAVACH-001", "role": "user"|"guardian", "password": "..." }
    Each device_id can have one user account and one guardian account.
    """
    try:
        body = request.get_json()
        if not body:
            return jsonify({'status': 'error', 'message': 'JSON body required'}), 400

        device_id = body.get('device_id', '').strip()
        role      = body.get('role', '').strip()
        password  = body.get('password', '')

        if not device_id:
            return jsonify({'status': 'error', 'message': 'Device ID is required'}), 400
        if role not in ('user', 'guardian'):
            return jsonify({'status': 'error', 'message': 'Role must be "user" or "guardian"'}), 400
        if not password or len(password) < 4:
            return jsonify({'status': 'error', 'message': 'Password must be at least 4 characters'}), 400

        users = _load_app_users()
        account_key = f"{device_id}_{role}"

        if account_key in users:
            return jsonify({'status': 'error', 'message': f'A {role} account already exists for {device_id}. Please login instead.'}), 409

        users[account_key] = {
            'device_id':    device_id,
            'role':         role,
            'password_hash': _hash_password(password),
            'created_at':   datetime.datetime.now(_UTC).isoformat(),
        }
        _save_app_users(users)
        logger.info("[Auth] New %s account registered for device %s", role, device_id)

        # Auto-login after signup
        token = _create_token(device_id, role)
        return jsonify({
            'status':    'ok',
            'message':   'Account created successfully',
            'token':     token,
            'role':      role,
            'device_id': device_id,
        }), 201
    except Exception as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    """
    Login endpoint for the mobile app.
    Accepts: { "device_id": "KAVACH-001", "role": "user"|"guardian", "password": "..." }
    Returns: { "token": "...", "role": "...", "device_id": "..." }
    """
    try:
        body = request.get_json()
        if not body:
            return jsonify({'status': 'error', 'message': 'JSON body required'}), 400

        device_id = body.get('device_id', '').strip()
        role      = body.get('role', '').strip()
        password  = body.get('password', '')

        if not device_id:
            return jsonify({'status': 'error', 'message': 'Device ID is required'}), 400
        if role not in ('user', 'guardian'):
            return jsonify({'status': 'error', 'message': 'Role must be "user" or "guardian"'}), 400
        if not password:
            return jsonify({'status': 'error', 'message': 'Password is required'}), 400

        users = _load_app_users()
        account_key = f"{device_id}_{role}"

        if account_key not in users:
            return jsonify({'status': 'error', 'message': f'No {role} account found for {device_id}. Please sign up first.'}), 404

        stored = users[account_key]
        if not _check_password(stored['password_hash'], password):
            return jsonify({'status': 'error', 'message': 'Incorrect password'}), 401

        token = _create_token(device_id, role)
        return jsonify({
            'status':    'ok',
            'token':     token,
            'role':      role,
            'device_id': device_id,
        }), 200
    except Exception as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@app.route('/api/user/alerts', methods=['GET'])
def user_alerts():
    """All alerts for the authenticated user's device."""
    try:
        device_id, role = _verify_token(request)
        if role != 'user':
            return jsonify({'status': 'error', 'message': 'User role required'}), 403

        alerts = Alert.query.filter_by(device_id=device_id).order_by(Alert.id.desc()).all()
        return jsonify({
            'status': 'ok',
            'count':  len(alerts),
            'alerts': [_alert_to_dict(a) for a in alerts],
        }), 200
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 401
    except Exception as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@app.route('/api/guardian/alerts', methods=['GET'])
def guardian_alerts():
    """Only SOS/MEDICAL alerts for the authenticated guardian's device."""
    try:
        device_id, role = _verify_token(request)
        if role != 'guardian':
            return jsonify({'status': 'error', 'message': 'Guardian role required'}), 403

        alerts = (Alert.query
                  .filter_by(device_id=device_id)
                  .filter(Alert.alert_type.in_(['SOS', 'MEDICAL']))
                  .order_by(Alert.id.desc())
                  .all())
        return jsonify({
            'status': 'ok',
            'count':  len(alerts),
            'alerts': [_alert_to_dict(a) for a in alerts],
        }), 200
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 401
    except Exception as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@app.route('/api/user/locations', methods=['GET'])
def user_locations():
    """Location history for the authenticated user's device."""
    try:
        device_id, role = _verify_token(request)
        if role != 'user':
            return jsonify({'status': 'error', 'message': 'User role required'}), 403

        alerts = (Alert.query
                  .filter_by(device_id=device_id)
                  .filter(Alert.gps_location.isnot(None))
                  .order_by(Alert.id.desc())
                  .all())
        locations = [{
            'alert_id':     a.id,
            'timestamp':    a.timestamp.isoformat() if a.timestamp else None,
            'gps_location': a.gps_location,
            'alert_type':   a.alert_type,
        } for a in alerts]

        return jsonify({'status': 'ok', 'count': len(locations), 'locations': locations}), 200
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 401
    except Exception as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@app.route('/api/user/config', methods=['GET'])
def get_user_config():
    """Return the current device config (phone numbers) stored on the server."""
    try:
        device_id, role = _verify_token(request)
        if role != 'user':
            return jsonify({'status': 'error', 'message': 'User role required'}), 403

        config = _load_device_config(device_id)
        return jsonify({
            'status': 'ok',
            'config': {
                'police_number':   config.get('police_number', ''),
                'guardian_number':  config.get('guardian_number', ''),
                'medical_number':  config.get('medical_number', ''),
                'whatsapp_number': config.get('whatsapp_number', ''),
            },
        }), 200
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 401
    except Exception as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@app.route('/api/user/config', methods=['PUT'])
def update_user_config():
    """Update device phone numbers. The Pi polls this to sync config."""
    try:
        device_id, role = _verify_token(request)
        if role != 'user':
            return jsonify({'status': 'error', 'message': 'User role required'}), 403

        body = request.get_json()
        if not body:
            return jsonify({'status': 'error', 'message': 'JSON body required'}), 400

        allowed_keys = ['police_number', 'guardian_number', 'medical_number', 'whatsapp_number']
        config = _load_device_config(device_id)
        for key in allowed_keys:
            if key in body:
                val = str(body[key]).strip()
                if val and not _PHONE_RE.match(val):
                    return jsonify({'status': 'error', 'message': f'Invalid phone number for {key}'}), 400
                config[key] = val
        config['device_id'] = device_id
        config['updated_at'] = datetime.datetime.now(_UTC).isoformat()

        _save_device_config(device_id, config)
        logger.info("Config updated for device %s", device_id)

        return jsonify({'status': 'ok', 'message': 'Config saved'}), 200
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 401
    except Exception as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@app.route('/api/device/config/<device_id>', methods=['GET'])
def get_device_config(device_id: str):
    """Pi polls this endpoint every 60s to get latest config.
    Also serves as a heartbeat — captures X-Battery header for live status.
    Requires X-Device-Key header to prevent unauthenticated access."""
    if not _check_device_key():
        return jsonify({'status': 'error', 'message': 'Invalid or missing device key'}), 401

    # Capture device heartbeat (battery status)
    battery = request.headers.get('X-Battery', '')
    if battery:
        _update_device_status(device_id, battery)
        logger.debug("[Config] Heartbeat from %s — battery: %s", device_id, battery)

    config = _load_device_config(device_id)
    return jsonify({'status': 'ok', 'config': config}), 200


@app.route('/api/device/status/<device_id>', methods=['GET'])
def get_device_status(device_id: str):
    """
    Returns the live battery percentage and online/offline status for a device.
    The Pi reports battery every 60s via the config poll heartbeat.
    If no heartbeat received within 2 minutes, the device is considered offline.
    Requires admin session or Bearer token.
    """
    if not _check_any_auth():
        return jsonify({'status': 'error', 'message': 'Authentication required'}), 401
    info = _get_device_status(device_id)
    return jsonify({'status': 'ok', **info}), 200


@app.route('/api/guardian/evidence/<int:alert_id>', methods=['GET'])
def guardian_evidence(alert_id: int):
    """Evidence files for a specific alert (guardian role, SOS/MEDICAL only)."""
    try:
        device_id, role = _verify_token(request)
        if role != 'guardian':
            return jsonify({'status': 'error', 'message': 'Guardian role required'}), 403

        alert = DB.session.get(Alert, alert_id)
        if not alert:
            return jsonify({'status': 'error', 'message': 'Alert not found'}), 404
        if alert.device_id != device_id:
            return jsonify({'status': 'error', 'message': 'Access denied - wrong device'}), 403
        if alert.alert_type not in ('SOS', 'MEDICAL'):
            return jsonify({'status': 'error', 'message': 'Evidence only available for SOS/MEDICAL alerts'}), 403

        evidence = []
        if alert.uploaded_files:
            for fname in alert.uploaded_files.split(','):
                fname = fname.strip()
                if not fname:
                    continue
                fpath = os.path.join(UPLOAD_DIR, fname)
                dl_token = _create_download_token(fname)
                evidence.append({
                    'filename':        fname,
                    'url':             f'/uploads/{fname}?token={dl_token}',
                    'file_exists':     os.path.exists(fpath),
                    'file_size_bytes': os.path.getsize(fpath) if os.path.exists(fpath) else 0,
                })

        return jsonify({
            'status':     'ok',
            'alert_id':   alert_id,
            'alert_type': alert.alert_type,
            'evidence':   evidence,
        }), 200
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 401
    except Exception as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/alerts
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/api/alerts', methods=['POST'])
def receive_alert():
    rid = _request_id()
    logger.info("[%s] POST /api/alerts - new request", rid)

    try:
        # ── 1. Read and decode encrypted payload ─────────────────────────────
        encrypted_payload_b64 = request.form.get('encrypted_payload')
        if not encrypted_payload_b64:
            logger.warning("[%s] Missing encrypted_payload field.", rid)
            return jsonify({
                'status': 'error',
                'message': 'Missing encrypted payload',
                'request_id': rid,
            }), 400

        encrypted_payload = base64.b64decode(encrypted_payload_b64)
        logger.info(
            "[%s] Encrypted payload received: %d bytes first_10=%s",
            rid, len(encrypted_payload), encrypted_payload[:10].hex()
        )

        # ── 2. Decrypt with ChaCha20-Poly1305 ────────────────────────────────
        decrypted_json = chacha_decrypt_text(encrypted_payload)
        data = json.loads(decrypted_json)

        # ── 3. Log decrypted fields ───────────────────────────────────────────
        logger.info(
            "[%s] Decrypted alert - device=%s type=%s trigger=%s "
            "gps=%s battery=%s call=%s sms=%s",
            rid,
            data.get('device_id'),
            data.get('alert_type',   'N/A'),
            data.get('trigger_source', 'N/A'),
            data.get('gps_location') or data.get('location', 'N/A'),
            data.get('battery_percentage', 'N/A'),
            data.get('call_placed_status'),
            data.get('guardian_sms_status'),
        )

        # ── 4. Validate required fields ───────────────────────────────────────
        device_id = data.get('device_id')
        if not device_id:
            logger.error("[%s] device_id missing from payload.", rid)
            return jsonify({
                'status': 'error',
                'message': 'device_id is required',
                'request_id': rid,
            }), 400

        # ── 5. Save uploaded evidence files + verify SHA-256 hashes ──────────
        files = request.files
        saved_filenames = []
        hash_results = {}   # filename → {"expected": str, "computed": str, "verified": bool}

        # Check if the device sent encrypted evidence files
        evidence_encrypted = request.form.get('file_encrypted', '').lower() == 'true'

        for field_name in files:
            f    = files[field_name]
            path = save_file_safe(f, UPLOAD_DIR)
            if not path:
                logger.warning("[%s] Could not save file: %s", rid, field_name)
                continue

            # Decrypt evidence file if the device encrypted it
            if evidence_encrypted:
                if not decrypt_file_in_place(path):
                    logger.error("[%s] Failed to decrypt evidence file: %s - skipping.", rid, field_name)
                    continue

            fname = os.path.basename(path)
            saved_filenames.append(fname)
            logger.info("[%s] Saved evidence file: %s", rid, fname)

            computed_hash = compute_sha256(path)

            # Check if device sent a hash for this file
            # Convention: device sends hash as form field "<fieldname>_sha256"
            hash_field    = field_name + '_sha256'
            expected_hash = request.form.get(hash_field, '').strip().lower()

            if expected_hash:
                verified = (computed_hash == expected_hash)
                hash_results[fname] = {
                    "expected": expected_hash,
                    "computed": computed_hash,
                    "verified": verified,
                }
                if verified:
                    logger.info("[%s] Hash VERIFIED for %s: %s", rid, fname, computed_hash[:16] + "...")
                else:
                    logger.warning(
                        "[%s] Hash MISMATCH for %s! expected=%s computed=%s",
                        rid, fname, expected_hash[:16] + "...", computed_hash[:16] + "..."
                    )
            else:
                hash_results[fname] = {
                    "expected": None,
                    "computed": computed_hash,
                    "verified": None,
                }
                logger.info(
                    "[%s] No hash provided for %s - computed and stored: %s",
                    rid, fname, computed_hash[:16] + "..."
                )

        # ── 6. Build hash summary string for DB storage ───────────────────────
        hash_summary = ",".join(
            f"{fname}:{info['computed']}"
            for fname, info in hash_results.items()
        )

        # ── 7. Resolve GPS location field (device may use either key) ─────────
        location_data = data.get('gps_location') or data.get('location')

        # ── 8. Write to database ──────────────────────────────────────────────
        new_alert = Alert(
            device_id           = device_id,
            timestamp           = datetime.datetime.now(_UTC),
            alert_type          = data.get('alert_type'),
            trigger_source      = data.get('trigger_source'),
            call_placed_status  = str(data.get('call_placed_status',  'false')).lower() == 'true',
            guardian_sms_status = str(data.get('guardian_sms_status', 'false')).lower() == 'true',
            location_sms_status = str(data.get('location_sms_status', 'false')).lower() == 'true',
            gps_location        = location_data,
            battery_percentage  = data.get('battery_percentage'),
            uploaded_files      = ','.join(saved_filenames),
            file_hashes         = hash_summary or None,
        )
        DB.session.add(new_alert)
        DB.session.commit()
        logger.info("[%s] Alert saved to DB - id=%d", rid, new_alert.id)

        # ── 9. Build response ─────────────────────────────────────────────────
        response = {
            'status':       'ok',
            'request_id':   rid,
            'alert_id':     new_alert.id,
            'saved_files':  saved_filenames,
            'hash_results': hash_results,
        }
        return jsonify(response), 201

    except Exception as exc:
        logger.error("[%s] Unhandled exception: %s", rid, exc, exc_info=True)
        DB.session.rollback()
        return jsonify({
            'status':     'error',
            'message':    str(exc),
            'request_id': rid,
        }), 500


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/health
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/api/health', methods=['GET'])
def health():
    try:
        total_alerts  = Alert.query.count()
        latest_alert  = Alert.query.order_by(Alert.id.desc()).first()
        latest_id     = latest_alert.id        if latest_alert else None
        latest_device = latest_alert.device_id if latest_alert else None
        latest_time   = (
            latest_alert.timestamp.isoformat()
            if latest_alert and latest_alert.timestamp
            else None
        )
        uptime_seconds = int(
            (datetime.datetime.now(_UTC) - _SERVER_START_TIME).total_seconds()
        )
        return jsonify({
            'status':        'ok',
            'server':        'Kavach API',
            'version':       '3.3',
            'uptime_seconds': uptime_seconds,
            'uptime_human':  _format_uptime(uptime_seconds),
            'database': {
                'status':              'connected',
                'total_alerts':        total_alerts,
                'latest_alert_id':     latest_id,
                'latest_alert_device': latest_device,
                'latest_alert_time':   latest_time,
            },
            'upload_dir':       UPLOAD_DIR,
            'upload_dir_exists': os.path.isdir(UPLOAD_DIR),
        }), 200
    except Exception as exc:
        logger.error("Health check failed: %s", exc, exc_info=True)
        return jsonify({'status': 'error', 'message': str(exc)}), 500


def _format_uptime(seconds: int) -> str:
    """Convert seconds to human-readable string e.g. '2d 3h 15m 40s'."""
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s   = divmod(rem, 60)
    parts  = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return ' '.join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/alerts
# Optional: ?device_id=KAVACH-001   ?limit=20
# Clamp limit to [1, 200] to prevent abuse
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/api/alerts', methods=['GET'])
def list_alerts():
    if not _check_any_auth():
        return jsonify({'status': 'error', 'message': 'Authentication required'}), 401
    try:
        device_id = request.args.get('device_id')
        raw_limit = request.args.get('limit', '50')
        try:
            parsed_limit = int(raw_limit)
        except ValueError:
            return jsonify({
                'status': 'error',
                'message': f"Invalid limit '{raw_limit}'. Expected integer in [1, 200].",
            }), 400

        # Clamp to valid range
        limit = max(1, min(parsed_limit, 200))
        query = Alert.query.order_by(Alert.id.desc())
        if device_id:
            query = query.filter_by(device_id=device_id)
        alerts = query.limit(limit).all()

        return jsonify({
            'status': 'ok',
            'count':  len(alerts),
            'alerts': [_alert_to_dict(a) for a in alerts],
        }), 200
    except Exception as exc:
        logger.error("List alerts failed: %s", exc, exc_info=True)
        return jsonify({'status': 'error', 'message': str(exc)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/alerts/<int:alert_id>
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/api/alerts/<int:alert_id>', methods=['GET'])
def get_alert(alert_id: int):
    if not _check_any_auth():
        return jsonify({'status': 'error', 'message': 'Authentication required'}), 401
    try:
        # db.session.get() is the correct SQLAlchemy 2.x API
        alert = DB.session.get(Alert, alert_id)
        if not alert:
            return jsonify({'status': 'error', 'message': 'Alert not found'}), 404

        data = _alert_to_dict(alert)

        # Re-verify hashes live against files on disk
        evidence_verification = []
        if alert.uploaded_files:
            stored_hashes = {}
            if alert.file_hashes:
                for entry in alert.file_hashes.split(','):
                    if ':' in entry:
                        fname, fhash = entry.split(':', 1)
                        stored_hashes[fname.strip()] = fhash.strip()

            for fname in alert.uploaded_files.split(','):
                fname = fname.strip()
                if not fname:
                    continue
                fpath = os.path.join(UPLOAD_DIR, fname)
                if not os.path.exists(fpath):
                    evidence_verification.append({
                        'filename':   fname,
                        'file_exists': False,
                        'verified':   False,
                        'reason':     'File not found on disk',
                    })
                    continue

                current_hash = compute_sha256(fpath)
                stored_hash  = stored_hashes.get(fname)
                verified     = (current_hash == stored_hash) if stored_hash else None
                file_size    = os.path.getsize(fpath)
                dl_token     = _create_download_token(fname)
                public_url   = f"/uploads/{fname}?token={dl_token}"

                evidence_verification.append({
                    'filename':        fname,
                    'file_exists':     True,
                    'file_size_bytes': file_size,
                    'public_url':      public_url,
                    'stored_hash':     stored_hash,
                    'current_hash':    current_hash,
                    'verified':        verified,
                    'integrity': (
                        'verified'    if verified is True  else
                        'tampered'    if verified is False else
                        'not_checked'
                    ),
                })

        data['evidence'] = evidence_verification
        return jsonify({'status': 'ok', 'alert': data}), 200

    except Exception as exc:
        logger.error("Get alert %d failed: %s", alert_id, exc, exc_info=True)
        return jsonify({'status': 'error', 'message': str(exc)}), 500


def _alert_to_dict(alert: Alert) -> dict:
    """Serialise an Alert model instance to a plain dict."""
    return {
        'id':                  alert.id,
        'device_id':           alert.device_id,
        'timestamp':           alert.timestamp.isoformat() if alert.timestamp else None,
        'alert_type':          alert.alert_type,
        'trigger_source':      alert.trigger_source,
        'call_placed_status':  alert.call_placed_status,
        'guardian_sms_status': alert.guardian_sms_status,
        'location_sms_status': alert.location_sms_status,
        'gps_location':        alert.gps_location,
        'battery_percentage':  alert.battery_percentage,
        'uploaded_files':      alert.uploaded_files,
        'file_hashes':         alert.file_hashes,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /uploads/<filename>
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    """Serve evidence files. Requires one of:
      1. Admin session (dashboard), OR
      2. Valid Bearer token (mobile app), OR
      3. Signed ?token= query parameter (time-limited download link).
    """
    if not (_check_any_auth() or _verify_download_token(filename)):
        return jsonify({'status': 'error', 'message': 'Authentication required'}), 401
    return send_from_directory(UPLOAD_DIR, filename)


# ─────────────────────────────────────────────────────────────────────────────
# Ngrok tunnel helper
# ─────────────────────────────────────────────────────────────────────────────
NGROK_DOMAIN = "unpropitious-braelyn-blossomy.ngrok-free.dev"

def _find_ngrok() -> str:
    """Return the ngrok executable path, or None if not found."""
    # Check PATH first
    import shutil
    path = shutil.which("ngrok")
    if path:
        return path
    # Common WinGet install location
    winget_path = os.path.expanduser(
        r"~\AppData\Local\Microsoft\WinGet\Packages"
    )
    if os.path.isdir(winget_path):
        for dirpath, _, filenames in os.walk(winget_path):
            for f in filenames:
                if f.lower() == "ngrok.exe":
                    return os.path.join(dirpath, f)
    return None


def _start_ngrok(port: int):
    """Launch ngrok in a background thread if available."""
    ngrok_exe = _find_ngrok()
    if not ngrok_exe:
        logger.warning(
            "ngrok not found. The server will run locally on port %d only.\n"
            "  Install ngrok and add it to PATH for remote access.", port
        )
        return None

    def _run():
        cmd = [ngrok_exe, "http", "--url", NGROK_DOMAIN, str(port)]
        logger.info("Starting ngrok: %s", " ".join(cmd))
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("ngrok tunnel started: https://%s", NGROK_DOMAIN)
        except Exception as exc:
            logger.error("Failed to start ngrok: %s", exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# ─────────────────────────────────────────────────────────────────────────────
# Boot
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    with app.app_context():
        DB.create_all()

    logger.info("=" * 55)
    logger.info(" Kavach Server v3.3 starting")
    logger.info(" Database: kavach.db")
    logger.info(" Upload dir: %s", UPLOAD_DIR)
    logger.info(" Endpoints:")
    logger.info("   GET  /                  - admin dashboard (login required)")
    logger.info("   GET  /login             - admin login page")
    logger.info("   GET  /logout            - admin logout")
    logger.info("   POST /api/alerts          - receive telemetry")
    logger.info("   GET  /api/alerts          - list all alerts")
    logger.info("   GET  /api/alerts/<id>     - alert detail + hash check")
    logger.info("   GET  /api/health          - server health")
    logger.info("   GET  /uploads/<file>      - serve evidence")
    logger.info("   POST /api/auth/signup     - create app account")
    logger.info("   POST /api/auth/login      - app auth token")
    logger.info("   GET  /api/user/alerts     - user alerts (app)")
    logger.info("   GET  /api/guardian/alerts  - guardian alerts (app)")
    logger.info("   GET  /api/user/config     - get phone numbers (app)")
    logger.info("   PUT  /api/user/config     - update phone numbers (app)")
    logger.info("   GET  /api/device/config   - Pi config polling")
    logger.info("=" * 55)

    # Start ngrok tunnel in the background
    _start_ngrok(8080)

    # Open the dashboard in the default browser after a short delay
    def _open_browser():
        import time
        time.sleep(2)  # wait for Flask to start
        url = f"https://{NGROK_DOMAIN}"
        logger.info("Opening browser: %s", url)
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()

    # debug=False in production - debug=True exposes the Werkzeug interactive
    # debugger which allows arbitrary code execution.
    app.run(host='0.0.0.0', port=8080, debug=False)