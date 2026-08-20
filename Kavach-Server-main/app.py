"""
app.py — Kavach Server v4.1

Flask API with admin dashboard, mobile app auth, device telemetry,
evidence chain ledger, and FCM push notifications.
All data routes are authenticated.

Core endpoints:
  GET  /                          — admin dashboard (session login)
  POST /api/alerts                — receive encrypted telemetry + evidence
  GET  /api/alerts                — list alerts (auth required)
  GET  /api/alerts/<id>           — alert detail + hash verification
  GET  /api/health                — server + database health check
  GET  /uploads/<file>            — serve evidence files (auth or signed token)

Auth endpoints:
  POST /api/auth/signup           — create mobile app account (user/guardian)
  POST /api/auth/login            — get auth token for mobile app
  PUT  /api/auth/fcm-token        — register FCM push notification token

Role-based endpoints:
  GET  /api/user/alerts           — user's alerts (Bearer token)
  GET  /api/guardian/alerts       — guardian's alerts (Bearer token)
  GET  /api/user/locations        — location history
  GET  /api/user/config           — get device phone numbers
  PUT  /api/user/config           — update phone numbers from app
  GET  /api/device/config/<id>    — Pi config polling (X-Device-Key)
  GET  /api/guardian/evidence/<id>— evidence files (guardian role)

Evidence Chain Ledger (Blueprint):
  GET  /api/evidence/alert/<id>   — list evidence for an alert
  GET  /api/evidence/<id>/verify  — verify individual evidence hash
  GET  /api/evidence/ledger/verify— verify entire ledger integrity
"""

from flask import Flask, request, jsonify, send_from_directory, render_template, session, redirect, url_for
from flask_cors import CORS
from functools import wraps
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import os
import re
import sys
import json
import base64
import hmac
import logging
import datetime
import uuid
import webbrowser
import threading
import subprocess

from database import DB, Alert, Evidence
from utils import save_file_safe, compute_sha256, decrypt_file_in_place
from crypto_utils import chacha_decrypt_text
from evidence import file_type_from_ext, append_to_ledger
from notifications import notify_device_alerts, store_fcm_token
import pairing
import secrets_manager
from ratelimit import rate_limit

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

# ── Register Blueprints ───────────────────────────────────────────────────
from blueprints.evidence_bp import evidence_bp
app.register_blueprint(evidence_bp)

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
        'last_seen': datetime.datetime.now(_IST),
    }


def _get_device_status(device_id: str) -> dict:
    """Return device battery and online/offline status."""
    info = _device_status.get(device_id)
    if not info:
        return {'battery': None, 'online': False, 'last_seen': None}
    elapsed = (datetime.datetime.now(_IST) - info['last_seen']).total_seconds()
    return {
        'battery':   info['battery'],
        'online':    elapsed <= _DEVICE_ONLINE_TIMEOUT,
        'last_seen': info['last_seen'].isoformat(),
    }


_DEVICE_ID_RE = re.compile(r'^[A-Za-z0-9._-]{1,64}$')


def _safe_device_id(device_id: str) -> str:
    """
    Validate a device_id before it is used to build a filesystem path.

    device_id arrives from URL segments and JSON bodies, and both
    _load_device_config and _save_device_config interpolate it straight into a
    filename. Without this, '../../..' style values would read and write
    outside device_configs/.
    """
    device_id = (device_id or '').strip()
    if not _DEVICE_ID_RE.match(device_id):
        raise ValueError('Invalid device ID.')
    return device_id


def _load_device_config(device_id: str) -> dict:
    """Load device config from JSON file."""
    path = os.path.join(CONFIG_DIR, f'{_safe_device_id(device_id)}.json')
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return {}


def _save_device_config(device_id: str, config: dict):
    """Save device config to JSON file."""
    path = os.path.join(CONFIG_DIR, f'{_safe_device_id(device_id)}.json')
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
# IST alias — all timestamps stored and returned in Indian Standard Time
# ─────────────────────────────────────────────────────────────────────────────
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name='IST')

# Boot time - used in /api/health uptime calculation
_SERVER_START_TIME = datetime.datetime.now(_IST)


# ─────────────────────────────────────────────────────────────────────────────
# Admin credentials
#
# There is deliberately no hard-coded fallback here. secrets_manager resolves
# env var → .server_secrets.json → freshly generated, and prints anything it
# generates exactly once at startup. The password is only ever held as a
# pbkdf2 hash in this process.
# ─────────────────────────────────────────────────────────────────────────────
ADMIN_USERNAME      = secrets_manager.get_admin_username()
ADMIN_PASSWORD_HASH = secrets_manager.get_admin_password_hash()

# ─────────────────────────────────────────────────────────────────────────────
# Device API key — the Raspberry Pi sends this in the X-Device-Key header
# when polling /api/device/config. Generated on first boot and synced into the
# device's config.json automatically when the device folder sits beside this
# one; otherwise printed with copy-paste instructions.
# ─────────────────────────────────────────────────────────────────────────────
KAVACH_DEVICE_KEY = secrets_manager.get_secret('device_key', 'KAVACH_DEVICE_KEY')

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

def _is_admin() -> bool:
    """True if this request carries the dashboard's admin session cookie."""
    return bool(session.get('admin_logged_in'))


def _token_identity():
    """
    Return (device_id, role) for a valid Bearer token, or None.

    Unlike _verify_token this never raises, so it can be used in routes that
    accept either an admin session or a token.
    """
    try:
        return _verify_token(request)
    except ValueError:
        return None


def _check_any_auth() -> bool:
    """
    True if the request is authenticated at all — admin session or a valid
    Bearer token.

    AUTHENTICATION ONLY. This answers "is this someone we know?" and NOT
    "may they see this particular device's data". Every route that returns
    device-scoped data must use _authorize_device() instead; this helper is
    reserved for routes whose response contains nothing device-specific.
    """
    return _is_admin() or _token_identity() is not None


def _authorize_device(device_id: str) -> bool:
    """
    True if the caller may access data belonging to device_id.

    The admin dashboard is intentionally cross-device — that is what an
    operator console is for. An app token is confined to the single device it
    was issued for, so one household's account can never read another's
    location history or evidence.
    """
    if _is_admin():
        return True
    identity = _token_identity()
    if identity is None:
        return False
    token_device, _role = identity
    return bool(device_id) and token_device == device_id


def _device_id_for_alert(alert_id: int):
    """Owning device_id for an alert, or None if the alert does not exist."""
    alert = DB.session.get(Alert, alert_id)
    return alert.device_id if alert else None


def _device_id_for_filename(filename: str):
    """
    Owning device_id for an uploaded evidence file, or None if unknown.

    Looks in the Evidence table first, which is the authoritative record.
    Falls back to scanning Alert.uploaded_files so that files uploaded before
    the Evidence table existed remain reachable by their rightful owner.
    """
    name = os.path.basename(filename or '')
    if not name:
        return None

    ev = Evidence.query.filter_by(filename=name).first()
    if ev:
        return _device_id_for_alert(ev.alert_id)

    match = Alert.query.filter(Alert.uploaded_files.contains(name)).first()
    return match.device_id if match else None


def _check_device_key() -> bool:
    """Constant-time comparison of the X-Device-Key header against the server key."""
    supplied = request.headers.get('X-Device-Key', '')
    if not supplied or not KAVACH_DEVICE_KEY:
        return False
    return hmac.compare_digest(supplied, KAVACH_DEVICE_KEY)


def _create_download_token(filename: str, device_id: str) -> str:
    """
    Create a short-lived signed token for downloading one evidence file.

    Needed because the Flutter app hands evidence URLs to an external browser,
    which cannot send an Authorization header. The token binds both the
    filename and the owning device, so a link leaked from one household is
    still useless against another's files.
    """
    return _token_serializer.dumps({
        'filename':  os.path.basename(filename),
        'device_id': device_id,
        'type':      'download',
    })


def _verify_download_token(filename: str) -> bool:
    """Verify the signed ?token= query parameter against this exact file."""
    token = request.args.get('token', '')
    if not token:
        return False
    try:
        data = _token_serializer.loads(token, max_age=3600)  # 1-hour expiry
    except (SignatureExpired, BadSignature, KeyError):
        return False

    if data.get('type') != 'download':
        return False
    if data.get('filename') != os.path.basename(filename):
        return False

    # Tokens minted before device binding existed have no device_id. Reject
    # them rather than honouring an unscoped link.
    token_device = data.get('device_id')
    if not token_device:
        return False

    owner = _device_id_for_filename(filename)
    return owner is not None and owner == token_device


# ─────────────────────────────────────────────────────────────────────────────
# Helper: generate a short request ID for tracing
# ─────────────────────────────────────────────────────────────────────────────
def _request_id() -> str:
    return uuid.uuid4().hex[:8].upper()


# ─────────────────────────────────────────────────────────────────────────────
# Admin Login / Logout
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
@rate_limit(10, 300, scope='admin_login')
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        # Always run the hash check, even when the username is wrong, so the
        # response time does not reveal whether the username exists.
        password_ok = check_password_hash(ADMIN_PASSWORD_HASH, password)
        if username == ADMIN_USERNAME and password_ok:
            session['admin_logged_in'] = True
            logger.info('[Auth] Dashboard login succeeded.')
            return redirect(url_for('dashboard'))
        logger.warning('[Auth] Failed dashboard login attempt.')
        # 401 rather than 200 so the failure actually counts toward the rate
        # limit; the browser still renders the page normally.
        return render_template('login.html', error='Invalid username or password'), 401
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


MIN_PASSWORD_LENGTH = 8


@app.route('/api/auth/signup', methods=['POST'])
@rate_limit(10, 900, scope='signup')
def auth_signup():
    """
    Register a new app account.

    Accepts: { "device_id", "role": "user"|"guardian", "password", "pairing_code" }

    The pairing code is what proves the caller actually has access to this
    device. Without it, anyone who guessed a device ID could register as its
    user and then redirect its emergency contacts. Find the code in the server
    console at startup, on the admin dashboard, or in the Pi's console.
    """
    try:
        body = request.get_json(silent=True)
        if not body:
            return jsonify({'status': 'error', 'message': 'JSON body required'}), 400

        device_id    = body.get('device_id', '').strip()
        role         = body.get('role', '').strip()
        password     = body.get('password', '')
        pairing_code = body.get('pairing_code', '')

        if not device_id:
            return jsonify({'status': 'error', 'message': 'Device ID is required'}), 400
        if role not in ('user', 'guardian'):
            return jsonify({'status': 'error', 'message': 'Role must be "user" or "guardian"'}), 400
        if not password or len(password) < MIN_PASSWORD_LENGTH:
            return jsonify({
                'status': 'error',
                'message': f'Password must be at least {MIN_PASSWORD_LENGTH} characters',
            }), 400

        # ── Proof of device ownership ────────────────────────────────────────
        if not pairing_code:
            return jsonify({
                'status': 'error',
                'message': 'A pairing code is required. Find it in the Kavach '
                           'server console, on the dashboard, or on the device.',
            }), 400

        if not pairing.verify(device_id, pairing_code):
            # One message for "no such device" and "wrong code" alike, so this
            # endpoint cannot be used to discover which device IDs exist.
            logger.warning('[Auth] Rejected signup for %s — bad pairing code.', device_id)
            return jsonify({
                'status': 'error',
                'message': 'Invalid device ID or pairing code.',
            }), 403

        users = _load_app_users()
        account_key = f"{device_id}_{role}"

        if account_key in users:
            return jsonify({
                'status': 'error',
                'message': f'A {role} account already exists for {device_id}. Please login instead.',
            }), 409

        users[account_key] = {
            'device_id':     device_id,
            'role':          role,
            'password_hash': _hash_password(password),
            'created_at':    datetime.datetime.now(_IST).isoformat(),
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
        logger.error("[Auth] Signup failed: %s", exc, exc_info=True)
        return jsonify({'status': 'error', 'message': 'Could not create the account.'}), 500


@app.route('/api/auth/login', methods=['POST'])
@rate_limit(10, 300, scope='login')
def auth_login():
    """
    Login endpoint for the mobile app.
    Accepts: { "device_id": "KAVACH-001", "role": "user"|"guardian", "password": "..." }
    Returns: { "token": "...", "role": "...", "device_id": "..." }

    A missing account and a wrong password produce the same 401 and the same
    text, so this endpoint cannot be used to work out which device IDs have
    accounts before trying passwords against them.
    """
    try:
        body = request.get_json(silent=True)
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
        stored = users.get(f"{device_id}_{role}")

        # Hash a throwaway value when the account is missing so both paths cost
        # roughly the same time.
        if stored is None:
            _check_password(generate_password_hash('timing-equaliser'), password)
            password_ok = False
        else:
            password_ok = _check_password(stored['password_hash'], password)

        if not password_ok:
            logger.warning('[Auth] Failed login for %s (%s).', device_id, role)
            return jsonify({
                'status': 'error',
                'message': 'Invalid device ID, role, or password.',
            }), 401

        token = _create_token(device_id, role)
        return jsonify({
            'status':    'ok',
            'token':     token,
            'role':      role,
            'device_id': device_id,
        }), 200
    except Exception as exc:
        logger.error("[Auth] Login failed: %s", exc, exc_info=True)
        return jsonify({'status': 'error', 'message': 'Could not sign you in.'}), 500


@app.route('/api/auth/fcm-token', methods=['PUT'])
def update_fcm_token():
    """
    Register or update the FCM push notification token for this user.
    Accepts: { "fcm_token": "..." }
    The app calls this after login or when the FCM token refreshes.
    """
    try:
        device_id, role = _verify_token(request)
        body = request.get_json()
        if not body or not body.get('fcm_token'):
            return jsonify({'status': 'error', 'message': 'fcm_token is required'}), 400

        store_fcm_token(device_id, role, body['fcm_token'])
        return jsonify({'status': 'ok', 'message': 'FCM token registered'}), 200
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 401
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


@app.route('/api/guardian/locations', methods=['GET'])
def guardian_locations():
    """
    Location history for the device this guardian monitors.

    The app has always called this endpoint from the guardian map screen, but
    it was never implemented server-side, so that screen received a 404 and
    showed no history. Mirrors /api/user/locations with a guardian role check.
    """
    try:
        device_id, role = _verify_token(request)
        if role != 'guardian':
            return jsonify({'status': 'error', 'message': 'Guardian role required'}), 403

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
        logger.error("Guardian locations failed: %s", exc, exc_info=True)
        return jsonify({'status': 'error', 'message': 'Could not load locations.'}), 500


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

        body = request.get_json(silent=True)
        if not body:
            return jsonify({'status': 'error', 'message': 'JSON body required'}), 400

        # ── Re-authentication ────────────────────────────────────────────────
        # These four numbers are what the device dials in an emergency, so
        # changing them is the single most damaging thing an account can do.
        # A stolen or borrowed phone with a live session should not be enough;
        # the account password has to be re-entered.
        current_password = body.get('current_password', '')
        if not current_password:
            return jsonify({
                'status': 'error',
                'message': 'Enter your account password to change emergency contacts.',
                'reauth_required': True,
            }), 401

        users = _load_app_users()
        account = users.get(f"{device_id}_{role}")
        if not account or not _check_password(account['password_hash'], current_password):
            logger.warning('[Config] Failed re-auth on contact change for %s.', device_id)
            return jsonify({
                'status': 'error',
                'message': 'Incorrect password.',
                'reauth_required': True,
            }), 401

        allowed_keys = ['police_number', 'guardian_number', 'medical_number', 'whatsapp_number']
        config = _load_device_config(device_id)
        changes = {}
        for key in allowed_keys:
            if key in body:
                val = str(body[key]).strip()
                if val and not _PHONE_RE.match(val):
                    return jsonify({'status': 'error', 'message': f'Invalid phone number for {key}'}), 400
                if val != config.get(key, ''):
                    changes[key] = val
                config[key] = val
        config['device_id'] = device_id
        config['updated_at'] = datetime.datetime.now(_IST).isoformat()

        _save_device_config(device_id, config)

        # Emergency-contact changes are security-relevant, so they are recorded
        # distinctly rather than folded into a generic "config saved" line.
        if changes:
            logger.warning(
                "[Config] EMERGENCY CONTACTS CHANGED for %s by %s: %s",
                device_id, role, ', '.join(sorted(changes)),
            )
            notify_device_alerts(
                device_id,
                title='Emergency contacts changed — Kavach',
                body=f"The numbers this device calls for help were updated "
                     f"({', '.join(sorted(changes))}). If this wasn't you, "
                     f"change them back and reset your password.",
                data={'type': 'CONFIG_CHANGE'},
            )
        else:
            logger.info("Config saved for device %s (no contact changes)", device_id)

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

    try:
        device_id = _safe_device_id(device_id)
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

    # Capture device heartbeat (battery status)
    battery = request.headers.get('X-Battery', '')
    if battery:
        _update_device_status(device_id, battery)
        logger.debug("[Config] Heartbeat from %s — battery: %s", device_id, battery)

    # A device that can present the device key is trusted to display its own
    # pairing code, so the Pi can print it in its console for whoever is
    # standing next to it. Creating it here means a device that has checked in
    # even once is immediately pairable.
    code = pairing.get_or_create(device_id)

    config = _load_device_config(device_id)
    return jsonify({'status': 'ok', 'config': config, 'pairing_code': code}), 200


@app.route('/api/admin/pairing-codes', methods=['GET'])
@admin_required
def admin_pairing_codes():
    """Every known device's pairing code. Powers the dashboard display."""
    return jsonify({'status': 'ok', 'codes': pairing.all_codes()}), 200


@app.route('/api/admin/pairing-code/<device_id>', methods=['GET', 'POST'])
@admin_required
def admin_pairing_code(device_id: str):
    """
    Read (GET) or regenerate (POST) a device's pairing code.

    Admin session only — this is the code that authorises creating an account
    for the device, so it must never be readable with an app token.
    Regenerating revokes the ability to add new accounts; existing accounts
    are unaffected and keep logging in.
    """
    try:
        device_id = _safe_device_id(device_id)
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

    if request.method == 'POST':
        code = pairing.regenerate(device_id)
    else:
        code = pairing.get_or_create(device_id)
    return jsonify({'status': 'ok', 'device_id': device_id, 'pairing_code': code}), 200


@app.route('/api/device/status/<device_id>', methods=['GET'])
def get_device_status(device_id: str):
    """
    Returns the live battery percentage and online/offline status for a device.
    The Pi reports battery every 60s via the config poll heartbeat.
    If no heartbeat received within 2 minutes, the device is considered offline.
    Requires an admin session, or a Bearer token issued for this same device.
    """
    if not _check_any_auth():
        return jsonify({'status': 'error', 'message': 'Authentication required'}), 401
    if not _authorize_device(device_id):
        return jsonify({'status': 'error', 'message': 'Access denied — wrong device'}), 403
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

        # Read the file list from the Evidence table rather than from the
        # comma-joined Alert.uploaded_files string. The table has one row per
        # file, so it is immune to the separator bug that used to fuse two
        # filenames into one — and it also recovers alerts whose string field
        # was corrupted before that bug was fixed.
        evidence = []
        for ev in alert.evidence_files.order_by(Evidence.created_at).all():
            fpath = os.path.join(UPLOAD_DIR, ev.filename)
            exists = os.path.exists(fpath)
            dl_token = _create_download_token(ev.filename, alert.device_id)
            evidence.append({
                'filename':        ev.filename,
                'url':             f'/uploads/{ev.filename}?token={dl_token}',
                'file_type':       ev.file_type,
                'sha256':          ev.sha256_hash,
                'file_exists':     exists,
                'file_size_bytes': os.path.getsize(fpath) if exists else 0,
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
        logger.error("Guardian evidence failed: %s", exc, exc_info=True)
        return jsonify({'status': 'error', 'message': 'Could not load evidence.'}), 500


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

        # ── 8. Write to database (UPDATE existing or CREATE new) ─────────────
        incoming_alert_id = data.get('alert_id')
        existing_alert = None

        if incoming_alert_id is not None:
            try:
                existing_alert = Alert.query.filter_by(
                    id=int(incoming_alert_id), device_id=device_id
                ).first()
            except (ValueError, TypeError):
                pass  # invalid alert_id — fall through to create new
            if existing_alert:
                logger.info("[%s] Updating existing alert id=%d", rid, existing_alert.id)
            else:
                logger.warning("[%s] alert_id=%s not found for device=%s — creating new.",
                               rid, incoming_alert_id, device_id)

        if existing_alert:
            # Append uploaded files
            if saved_filenames:
                # Join with a separator between the old list and the new one.
                # Without the comma, the last existing filename and the first
                # new one fused into a single unusable name and both files
                # became unreachable from the app.
                prev = (existing_alert.uploaded_files or "").strip(",")
                joined = ",".join(saved_filenames)
                existing_alert.uploaded_files = f"{prev},{joined}" if prev else joined
            # Append file hashes
            if hash_summary:
                prev_h = existing_alert.file_hashes or ""
                existing_alert.file_hashes = (prev_h + "," + hash_summary) if prev_h else hash_summary
            # Update fields only if the new value is truthy
            if location_data:
                existing_alert.gps_location = location_data
            if data.get('location_source'):
                existing_alert.location_source = data['location_source']
            if data.get('battery_percentage'):
                existing_alert.battery_percentage = data['battery_percentage']
            if str(data.get('call_placed_status', 'false')).lower() == 'true':
                existing_alert.call_placed_status = True
            if str(data.get('guardian_sms_status', 'false')).lower() == 'true':
                existing_alert.guardian_sms_status = True
            if str(data.get('location_sms_status', 'false')).lower() == 'true':
                existing_alert.location_sms_status = True
            DB.session.commit()
            alert_obj = existing_alert
            logger.info("[%s] Alert updated in DB - id=%d", rid, alert_obj.id)
        else:
            alert_obj = Alert(
                device_id           = device_id,
                timestamp           = datetime.datetime.now(_IST),
                alert_type          = data.get('alert_type'),
                trigger_source      = data.get('trigger_source'),
                call_placed_status  = str(data.get('call_placed_status',  'false')).lower() == 'true',
                guardian_sms_status = str(data.get('guardian_sms_status', 'false')).lower() == 'true',
                location_sms_status = str(data.get('location_sms_status', 'false')).lower() == 'true',
                gps_location        = location_data,
                location_source     = data.get('location_source'),
                battery_percentage  = data.get('battery_percentage'),
                uploaded_files      = ','.join(saved_filenames),
                file_hashes         = hash_summary or None,
            )
            DB.session.add(alert_obj)
            DB.session.commit()
            logger.info("[%s] Alert saved to DB - id=%d", rid, alert_obj.id)

        # ── 9. Evidence Chain Ledger — create Evidence records + append to ledger
        for fname, info in hash_results.items():
            fpath = os.path.join(UPLOAD_DIR, fname)
            ftype = file_type_from_ext(fname)
            fsize = os.path.getsize(fpath) if os.path.exists(fpath) else 0

            ev = Evidence(
                alert_id=alert_obj.id,
                file_path=fpath,
                filename=fname,
                sha256_hash=info['computed'],
                file_type=ftype,
                file_size=fsize,
            )
            DB.session.add(ev)
            DB.session.commit()

            # Append to the integrity chain ledger
            append_to_ledger(ev.id, info['computed'], fpath, alert_obj.id)

        # ── 10. FCM push notification to user + guardian apps ────────────────
        alert_type = data.get('alert_type', 'ALERT')
        notify_device_alerts(
            device_id,
            title=f'{alert_type} Alert — Kavach',
            body=f'Alert from device {device_id}. Check the app for details.',
            data={'alert_id': str(alert_obj.id), 'type': alert_type},
        )

        # ── 11. Build response ───────────────────────────────────────────────
        response = {
            'status':       'ok',
            'request_id':   rid,
            'alert_id':     alert_obj.id,
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
        uptime_seconds = int(
            (datetime.datetime.now(_IST) - _SERVER_START_TIME).total_seconds()
        )
        # Reachable without authentication, because the app uses it as a
        # connectivity probe before login. So it reports only liveness — no
        # device IDs, no alert counts, no filesystem paths. Authenticated
        # callers get the detail they used to find here.
        payload = {
            'status':         'ok',
            'server':         'Kavach API',
            'version':        '4.2',
            'uptime_seconds': uptime_seconds,
            'uptime_human':   _format_uptime(uptime_seconds),
            'database':       {'status': 'connected'},
        }

        if _is_admin():
            latest_alert = Alert.query.order_by(Alert.id.desc()).first()
            payload['database'].update({
                'total_alerts':        Alert.query.count(),
                'latest_alert_id':     latest_alert.id if latest_alert else None,
                'latest_alert_device': latest_alert.device_id if latest_alert else None,
                'latest_alert_time': (
                    latest_alert.timestamp.isoformat()
                    if latest_alert and latest_alert.timestamp else None
                ),
            })
            payload['upload_dir'] = UPLOAD_DIR
            payload['upload_dir_exists'] = os.path.isdir(UPLOAD_DIR)

        # Confirm the database really answers, without exposing what is in it.
        DB.session.execute(DB.text('SELECT 1'))
        return jsonify(payload), 200
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

        # The admin dashboard may look across devices; an app token may not.
        # For a token, the device filter is forced to its own device rather
        # than taken from the query string.
        if not _is_admin():
            identity = _token_identity()
            if identity is None:
                return jsonify({'status': 'error', 'message': 'Authentication required'}), 401
            token_device, _role = identity
            if device_id and device_id != token_device:
                return jsonify({
                    'status': 'error',
                    'message': 'Access denied — you can only view your own device.',
                }), 403
            device_id = token_device

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

        # Same 404 for "does not exist" and "belongs to someone else", so
        # walking the integer IDs reveals nothing about other devices.
        if not _authorize_device(alert.device_id):
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
                dl_token     = _create_download_token(fname, alert.device_id)
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
        'location_source':     alert.location_source,
        'battery_percentage':  alert.battery_percentage,
        'uploaded_files':      alert.uploaded_files,
        'file_hashes':         alert.file_hashes,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /uploads/<filename>
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    """
    Serve an evidence file to someone entitled to it.

    Three ways in, all of them scoped to the device that owns the file:
      1. Admin session (dashboard) — cross-device by design.
      2. Bearer token issued for the owning device.
      3. Signed ?token= link, which carries the filename and the owning
         device inside the signature.

    A valid token for some *other* device is not enough. That was the hole
    that let any account download every household's recordings.
    """
    if _verify_download_token(filename):
        return send_from_directory(UPLOAD_DIR, filename)

    if not _check_any_auth():
        return jsonify({'status': 'error', 'message': 'Authentication required'}), 401

    owner = _device_id_for_filename(filename)
    if owner is None:
        # Unknown file: only the admin may probe for it.
        if not _is_admin():
            return jsonify({'status': 'error', 'message': 'File not found'}), 404
    elif not _authorize_device(owner):
        return jsonify({'status': 'error', 'message': 'File not found'}), 404

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

    # Show any credentials generated on this boot, exactly once, plus the
    # pairing codes needed to create app accounts.
    secrets_manager.print_banner(KAVACH_DEVICE_KEY)
    pairing.print_banner()

    logger.info("=" * 60)
    logger.info(" Kavach Server v4.2 starting")
    logger.info(" Database: kavach.db")
    logger.info(" Upload dir: %s", UPLOAD_DIR)
    logger.info(" FCM: %s", "configured" if os.environ.get('FIREBASE_CREDENTIALS') else "stub mode")
    logger.info(" Endpoints:")
    logger.info("   POST /api/alerts              — receive telemetry")
    logger.info("   GET  /api/alerts              — list all alerts")
    logger.info("   GET  /api/alerts/<id>         — alert detail + hash check")
    logger.info("   GET  /api/health              — server health")
    logger.info("   PUT  /api/auth/fcm-token      — register FCM token")
    logger.info("   GET  /api/evidence/alert/<id> — evidence list per alert")
    logger.info("   GET  /api/evidence/<id>/verify— verify evidence hash")
    logger.info("   GET  /api/evidence/ledger/verify — verify ledger chain")
    logger.info("=" * 60)

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

    app.run(host='0.0.0.0', port=8080, debug=False)