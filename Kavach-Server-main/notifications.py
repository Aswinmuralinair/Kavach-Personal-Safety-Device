"""
notifications.py — Kavach Server

Firebase Cloud Messaging (FCM) push notifications. Sends instant alerts
to the Kavach mobile app when a new alert is received from the device.

Setup:
  1. Create a Firebase project at https://console.firebase.google.com
  2. Generate a service account key (JSON file)
  3. Set FIREBASE_CREDENTIALS env var to the path of that JSON file
  4. Install: pip install firebase-admin

If FIREBASE_CREDENTIALS is not set, notifications are silently disabled
and the server logs stub messages instead.
"""

import os
import json
import logging

logger = logging.getLogger(__name__)

_firebase_app = None
_fcm_tokens = {}   # { "device_id_role": "fcm_token" }  — in-memory cache

# Persistent storage path for FCM tokens
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_FCM_TOKENS_FILE = os.path.join(_APP_DIR, 'fcm_tokens.json')


# ─────────────────────────────────────────────────────────────────────────────
# Firebase initialization (lazy — only when first notification is sent)
# ─────────────────────────────────────────────────────────────────────────────

def _init_firebase() -> bool:
    """Initialize Firebase Admin SDK. Returns True if ready."""
    global _firebase_app
    if _firebase_app is not None:
        return True

    cred_path = os.environ.get('FIREBASE_CREDENTIALS')
    if not cred_path:
        logger.info('[FCM] FIREBASE_CREDENTIALS not set — push notifications disabled.')
        return False

    try:
        import firebase_admin
        from firebase_admin import credentials
        cred = credentials.Certificate(cred_path)
        _firebase_app = firebase_admin.initialize_app(cred)
        logger.info('[FCM] Firebase initialized successfully.')
        return True
    except ImportError:
        logger.warning('[FCM] firebase-admin not installed — push notifications disabled.')
        return False
    except Exception as e:
        logger.error('[FCM] Firebase init failed: %s', e)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# FCM token storage (per device_id + role)
# ─────────────────────────────────────────────────────────────────────────────

def _load_fcm_tokens():
    """Load FCM tokens from disk."""
    global _fcm_tokens
    if os.path.exists(_FCM_TOKENS_FILE):
        try:
            with open(_FCM_TOKENS_FILE, 'r') as f:
                _fcm_tokens = json.load(f)
        except (json.JSONDecodeError, IOError):
            _fcm_tokens = {}


def _save_fcm_tokens():
    """Persist FCM tokens to disk."""
    with open(_FCM_TOKENS_FILE, 'w') as f:
        json.dump(_fcm_tokens, f, indent=2)


def store_fcm_token(device_id: str, role: str, fcm_token: str):
    """Store or update the FCM token for a device_id + role pair."""
    _load_fcm_tokens()
    key = f"{device_id}_{role}"
    _fcm_tokens[key] = fcm_token
    _save_fcm_tokens()
    logger.info('[FCM] Token stored for %s', key)


def get_fcm_tokens_for_device(device_id: str) -> list:
    """Return all FCM tokens (user + guardian) for a device_id."""
    _load_fcm_tokens()
    tokens = []
    for key, token in _fcm_tokens.items():
        if key.startswith(f"{device_id}_") and token:
            tokens.append(token)
    return tokens


# ─────────────────────────────────────────────────────────────────────────────
# Send push notification
# ─────────────────────────────────────────────────────────────────────────────

def send_push(fcm_token: str, title: str, body: str, data: dict = None) -> bool:
    """
    Send a single push notification via FCM.
    Returns True on success, False on failure or if Firebase is not configured.
    """
    if not fcm_token:
        return False

    if not _init_firebase():
        # Stub mode — log what would have been sent
        logger.info(
            '[FCM stub] -> token=%s... | %s: %s | data=%s',
            fcm_token[:20], title, body, data
        )
        return False

    try:
        from firebase_admin import messaging
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in (data or {}).items()},
            token=fcm_token,
        )
        messaging.send(message)
        logger.info('[FCM] Sent: %s -> %s...', title, fcm_token[:20])
        return True
    except Exception as e:
        logger.error('[FCM] Send failed: %s', e)
        return False


def notify_device_alerts(device_id: str, title: str, body: str, data: dict = None) -> int:
    """
    Send push notification to ALL registered tokens (user + guardian) for a device.
    Returns the number of notifications successfully sent.
    """
    tokens = get_fcm_tokens_for_device(device_id)
    if not tokens:
        logger.info('[FCM] No tokens registered for device %s', device_id)
        return 0

    sent = 0
    for token in tokens:
        if send_push(token, title, body, data):
            sent += 1
    return sent
