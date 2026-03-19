"""
app.py — Kavach Server

Enhanced Flask API with:
  POST /api/alerts         — receive encrypted telemetry + evidence files
  GET  /api/health         — server + database health check
  GET  /api/alerts         — list all alerts (for future dashboard)
  GET  /api/alerts/<id>    — single alert detail with hash verification status
  GET  /uploads/<file>     — serve evidence files

FIXES APPLIED:
  ① datetime.UTC → datetime.timezone.utc
      datetime.UTC was only added in Python 3.11.
      datetime.timezone.utc has existed since Python 3.2 — safe everywhere.

  ② Alert.query.get(id) → db.session.get(Alert, id)
      Query.get() was deprecated in SQLAlchemy 1.4 and REMOVED in 2.0.
      db.session.get(Model, pk) is the correct 2.x API.

  ③ Negative `limit` values now clamped to 1
      ?limit=-1 previously bypassed the cap and dumped the entire table.
"""

from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_cors import CORS
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import os
import sys
import json
import base64
import logging
import datetime
import uuid

from database import DB, Alert
from utils import save_file_safe, compute_sha256, decrypt_file_in_place
from crypto_utils import chacha_decrypt_text

# ─────────────────────────────────────────────────────────────────────────────
# Logging — structured, goes to stdout (visible in server terminal)
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

UPLOAD_DIR = os.path.join(os.getcwd(), 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# App-ready API auth (for future mobile app)
# Uses itsdangerous (bundled with Flask) for signed tokens — no PyJWT needed.
#
# FIX: SECRET_KEY is persisted to .secret_key file so auth tokens survive
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
    # First run — generate and persist
    new_key = os.urandom(32).hex()
    with open(key_file, 'w') as f:
        f.write(new_key)
    logger.info("[Auth] Generated new SECRET_KEY → saved to .secret_key")
    return new_key

app.config['SECRET_KEY'] = _load_or_create_secret_key()
_token_serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])

# ─────────────────────────────────────────────────────────────────────────────
# FIX ① — use datetime.timezone.utc everywhere instead of datetime.UTC
# datetime.UTC was only added in Python 3.11; timezone.utc works on 3.2+
# ─────────────────────────────────────────────────────────────────────────────
_UTC = datetime.timezone.utc                                   # ← single alias

# Boot time — used in /api/health uptime calculation
_SERVER_START_TIME = datetime.datetime.now(_UTC)


# ─────────────────────────────────────────────────────────────────────────────
# Helper: generate a short request ID for tracing
# ─────────────────────────────────────────────────────────────────────────────
def _request_id() -> str:
    return uuid.uuid4().hex[:8].upper()


# ─────────────────────────────────────────────────────────────────────────────
# GET / — Admin Dashboard
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/')
def dashboard():
    return render_template('dashboard.html')


# ─────────────────────────────────────────────────────────────────────────────
# App-ready API — Auth + Role-based endpoints (for future mobile app)
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


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    """
    Login endpoint for the future mobile app.
    Accepts: { "device_id": "KAVACH-001", "role": "user"|"guardian" }
    Returns: { "token": "...", "role": "...", "device_id": "..." }
    """
    try:
        body = request.get_json()
        if not body:
            return jsonify({'status': 'error', 'message': 'JSON body required'}), 400

        device_id = body.get('device_id')
        role      = body.get('role')

        if not device_id:
            return jsonify({'status': 'error', 'message': 'device_id is required'}), 400
        if role not in ('user', 'guardian'):
            return jsonify({'status': 'error', 'message': 'role must be "user" or "guardian"'}), 400

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
            return jsonify({'status': 'error', 'message': 'Access denied — wrong device'}), 403
        if alert.alert_type not in ('SOS', 'MEDICAL'):
            return jsonify({'status': 'error', 'message': 'Evidence only available for SOS/MEDICAL alerts'}), 403

        evidence = []
        if alert.uploaded_files:
            for fname in alert.uploaded_files.split(','):
                fname = fname.strip()
                if not fname:
                    continue
                fpath = os.path.join(UPLOAD_DIR, fname)
                evidence.append({
                    'filename':    fname,
                    'url':         f'/uploads/{fname}',
                    'exists':      os.path.exists(fpath),
                    'size_bytes':  os.path.getsize(fpath) if os.path.exists(fpath) else 0,
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
    logger.info("[%s] POST /api/alerts — new request", rid)

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
            "[%s] Decrypted alert — device=%s type=%s trigger=%s "
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
                    logger.error("[%s] Failed to decrypt evidence file: %s — skipping.", rid, field_name)
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
                    "[%s] No hash provided for %s — computed and stored: %s",
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
            timestamp           = datetime.datetime.now(_UTC),          # FIX ①
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
        logger.info("[%s] Alert saved to DB — id=%d", rid, new_alert.id)

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
            (datetime.datetime.now(_UTC) - _SERVER_START_TIME).total_seconds()   # FIX ①
        )
        return jsonify({
            'status':        'ok',
            'server':        'Kavach API',
            'version':       '2.0',
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
# FIX ③: clamp limit to [1, 200] — prevents ?limit=-1 dumping the full table
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/api/alerts', methods=['GET'])
def list_alerts():
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

        # FIX ③: max(1, ...) prevents negative limits that bypass the cap
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
    try:
        # FIX ②: Alert.query.get() removed in SQLAlchemy 2.0
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
                public_url   = f"/uploads/{fname}"

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
    return send_from_directory(UPLOAD_DIR, filename)


# ─────────────────────────────────────────────────────────────────────────────
# Boot
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    with app.app_context():
        DB.create_all()

    logger.info("=" * 55)
    logger.info(" Kavach Server v2.0 starting")
    logger.info(" Database: kavach.db")
    logger.info(" Upload dir: %s", UPLOAD_DIR)
    logger.info(" Endpoints:")
    logger.info("   GET  /                  — admin dashboard")
    logger.info("   POST /api/alerts        — receive telemetry")
    logger.info("   GET  /api/alerts        — list all alerts")
    logger.info("   GET  /api/alerts/<id>   — alert detail + hash check")
    logger.info("   GET  /api/health        — server health")
    logger.info("   GET  /uploads/<file>    — serve evidence")
    logger.info("   POST /api/auth/login    — app auth token")
    logger.info("   GET  /api/user/alerts   — user alerts (app)")
    logger.info("   GET  /api/guardian/alerts — guardian alerts (app)")
    logger.info("=" * 55)

    # debug=False in production — debug=True exposes the Werkzeug interactive
    # debugger which allows arbitrary code execution.
    app.run(host='0.0.0.0', port=8080, debug=False)