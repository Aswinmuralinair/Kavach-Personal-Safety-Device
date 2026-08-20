"""
secrets_manager.py — Kavach Server

Generates and persists the server's secrets on first boot, so that no
credential ever has to live in source code or in a tracked config file.

Resolution order for every secret:

    1. Environment variable        (highest priority — use this in production)
    2. .server_secrets.json        (auto-generated on first boot, gitignored)
    3. Freshly generated random    (written to .server_secrets.json, printed once)

Nothing here ever falls back to a hard-coded literal. The old published
defaults ('kavach2026', 'kavach-device-key-2026') are recognised only so we
can refuse them loudly — see _reject_known_bad().

Device key hand-off
-------------------
The Raspberry Pi authenticates with X-Device-Key, so a freshly generated
device key has to reach the device or it can no longer talk to the server.
On first generation we look for the device's config.json in the sibling
Personal-Safety-Device-main/ folder and update it in place. If the device
lives on a separate machine, we print the key with copy-paste instructions
instead.
"""

import json
import logging
import os
import secrets
import stat

from werkzeug.security import generate_password_hash

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_FILE = os.path.join(_BASE_DIR, '.server_secrets.json')

# Where to look for the device config when auto-syncing the device key.
# Ordered by likelihood; the first one that exists wins.
_DEVICE_CONFIG_CANDIDATES = [
    os.path.join(_BASE_DIR, '..', 'Personal-Safety-Device-main', 'config.json'),
    os.path.join(_BASE_DIR, '..', 'Personal-Safety-Device', 'config.json'),
]

# Credentials that were published in the public repository. If any of these
# turn up in an env var or a secrets file, we treat them as unset.
_BURNED = {
    'kavach2026',
    'kavach-device-key-2026',
    'admin',
}


# ─────────────────────────────────────────────────────────────────────────────
# Store
# ─────────────────────────────────────────────────────────────────────────────

def _load_store() -> dict:
    if not os.path.exists(SECRETS_FILE):
        return {}
    try:
        with open(SECRETS_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError, IOError):
        logger.warning('[Secrets] %s is unreadable — regenerating.', SECRETS_FILE)
        return {}


def _save_store(store: dict) -> None:
    with open(SECRETS_FILE, 'w') as f:
        json.dump(store, f, indent=2)
    # Best effort on POSIX: make the file owner-only. No-op on Windows.
    try:
        os.chmod(SECRETS_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _reject_known_bad(value: str) -> str:
    """Return '' if the value is one of the credentials published to GitHub."""
    if value and value.strip() in _BURNED:
        logger.error(
            '[Secrets] Refusing a credential that was published in the public '
            'repository. Generating a fresh one instead.'
        )
        return ''
    return value


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

_newly_generated = {}   # name → plaintext, for the one-time startup banner


def get_secret(name: str, env_var: str, nbytes: int = 24) -> str:
    """
    Resolve one secret: env var, then stored value, then generate a new one.

    Newly generated values are recorded in _newly_generated so print_banner()
    can show them once — they are never logged again after that.
    """
    env_val = _reject_known_bad(os.environ.get(env_var, ''))
    if env_val:
        return env_val

    store = _load_store()
    stored = _reject_known_bad(store.get(name, ''))
    if stored:
        return stored

    value = secrets.token_urlsafe(nbytes)
    store[name] = value
    _save_store(store)
    _newly_generated[name] = value
    logger.info('[Secrets] Generated a new %s (saved to .server_secrets.json).', name)
    return value


def get_admin_password_hash() -> str:
    """
    Return the pbkdf2 hash of the admin password.

    If KAVACH_ADMIN_PASS is set, its hash is derived fresh each boot and never
    stored. Otherwise a random password is generated once, its hash persisted,
    and the plaintext shown in the startup banner exactly once.
    """
    env_val = _reject_known_bad(os.environ.get('KAVACH_ADMIN_PASS', ''))
    if env_val:
        return generate_password_hash(env_val)

    store = _load_store()
    stored_hash = store.get('admin_password_hash', '')
    if stored_hash:
        return stored_hash

    plaintext = secrets.token_urlsafe(12)
    hashed = generate_password_hash(plaintext)
    store['admin_password_hash'] = hashed
    _save_store(store)
    _newly_generated['admin_password'] = plaintext
    return hashed


def get_admin_username() -> str:
    env_val = os.environ.get('KAVACH_ADMIN_USER', '').strip()
    if env_val and env_val not in _BURNED:
        return env_val
    store = _load_store()
    stored = store.get('admin_username', '')
    if stored:
        return stored
    # 'kavach-admin' is not a secret — the password is what protects the
    # dashboard — but it is at least not the guessable 'admin'.
    store['admin_username'] = 'kavach-admin'
    _save_store(store)
    return 'kavach-admin'


# ─────────────────────────────────────────────────────────────────────────────
# Device key hand-off
# ─────────────────────────────────────────────────────────────────────────────

def sync_device_key(device_key: str) -> str:
    """
    Write a freshly generated device key into the device's config.json if we
    can find it beside this folder.

    Returns a human-readable description of what happened, for the banner.
    Never raises — a failed sync must not stop the server from booting.
    """
    for candidate in _DEVICE_CONFIG_CANDIDATES:
        path = os.path.abspath(candidate)
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r') as f:
                cfg = json.load(f)
            if cfg.get('device_key') == device_key:
                return f'device config already in sync ({path})'
            cfg['device_key'] = device_key
            with open(path, 'w') as f:
                json.dump(cfg, f, indent=2)
            return f'device config updated automatically ({path})'
        except (json.JSONDecodeError, ValueError, IOError, OSError) as exc:
            return f'could not update {path}: {exc}'
    return 'device config not found beside the server - copy the key manually'


def print_banner(device_key: str) -> None:
    """
    Print any newly generated credentials exactly once, at startup.

    After the first boot this prints nothing, because the secrets are read
    back from .server_secrets.json instead of being regenerated.
    """
    if not _newly_generated:
        return

    line = '=' * 68
    print('\n' + line)
    print(' KAVACH - FIRST-RUN CREDENTIALS (shown once, save them now)')
    print(line)

    if 'admin_password' in _newly_generated:
        print(f"  Dashboard username : {get_admin_username()}")
        print(f"  Dashboard password : {_newly_generated['admin_password']}")
        print()

    if 'device_key' in _newly_generated:
        result = sync_device_key(device_key)
        print(f"  Device key         : {device_key}")
        print(f"  Hand-off           : {result}")
        if 'not found' in result or 'could not' in result:
            print()
            print('  Set this on the Raspberry Pi, in config.json:')
            print(f'      "device_key": "{device_key}"')
        print()

    print('  These are stored in .server_secrets.json, which is gitignored.')
    print('  To choose your own instead, set KAVACH_ADMIN_PASS and')
    print('  KAVACH_DEVICE_KEY as environment variables and restart.')
    print(line + '\n')

    _newly_generated.clear()
