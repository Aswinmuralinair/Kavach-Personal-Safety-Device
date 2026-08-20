"""
pairing.py — Kavach Server

Device pairing codes: the proof-of-ownership that account signup requires.

Why this exists
---------------
Signup used to accept any device_id with no evidence the caller had ever
touched the hardware. Device IDs are short and sequential, so a stranger
could register as the 'user' of someone else's safety device and — through
PUT /api/user/config — redirect that device's emergency calls.

A pairing code closes that. To create an account for KAVACH-001 you must
present the code for KAVACH-001, and the code is only visible to someone
with physical or administrative access:

  * printed in the server console at startup
  * shown on the admin dashboard, beside the device
  * printed in the Raspberry Pi's console when it polls for config

Codes are 8 characters from an unambiguous alphabet (no O/0, I/1) so they
can be read off a screen and typed on a phone without mistakes.

Lifetime
--------
A code stays valid until it is regenerated, because both the user and the
guardian need to pair, often on different days. Regenerate it from the
dashboard whenever you want to revoke the ability to add new accounts —
existing accounts are unaffected and keep logging in normally.
"""

import json
import logging
import os
import secrets

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAIRING_FILE = os.path.join(_BASE_DIR, 'pairing_codes.json')

# Ambiguous glyphs removed: no O/0, no I/1/L.
_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
_CODE_LEN = 8


def _load() -> dict:
    if not os.path.exists(PAIRING_FILE):
        return {}
    try:
        with open(PAIRING_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError, IOError):
        logger.warning('[Pairing] %s unreadable — starting fresh.', PAIRING_FILE)
        return {}


def _save(data: dict) -> None:
    with open(PAIRING_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def _generate() -> str:
    return ''.join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))


def get_or_create(device_id: str) -> str:
    """Return the pairing code for a device, creating one if it has none."""
    device_id = (device_id or '').strip()
    if not device_id:
        raise ValueError('device_id is required')

    codes = _load()
    if device_id in codes and codes[device_id]:
        return codes[device_id]

    code = _generate()
    codes[device_id] = code
    _save(codes)
    logger.info('[Pairing] Created pairing code for %s.', device_id)
    return code


def regenerate(device_id: str) -> str:
    """Replace a device's pairing code. Existing accounts keep working."""
    codes = _load()
    code = _generate()
    codes[device_id.strip()] = code
    _save(codes)
    logger.info('[Pairing] Regenerated pairing code for %s.', device_id)
    return code


def verify(device_id: str, submitted: str) -> bool:
    """
    Constant-time check of a submitted pairing code.

    Returns False for an unknown device rather than creating a code, so that
    signup cannot be used to enumerate or provision device IDs.
    """
    if not device_id or not submitted:
        return False
    stored = _load().get(device_id.strip(), '')
    if not stored:
        return False
    return secrets.compare_digest(
        stored.strip().upper(),
        submitted.strip().upper().replace(' ', '').replace('-', ''),
    )


def all_codes() -> dict:
    """Every known device_id → code. Used by the dashboard and the banner."""
    return _load()


def print_banner() -> None:
    """Show the known pairing codes at startup so the operator can pair."""
    codes = _load()
    if not codes:
        print(
            '\n[Kavach] No devices have paired yet. A pairing code is created '
            'the first time a device checks in,\n         or from the '
            'dashboard once you log in. You need it to create app accounts.\n'
        )
        return

    line = '-' * 68
    print('\n' + line)
    print(' DEVICE PAIRING CODES - enter these in the app when signing up')
    print(line)
    for device_id, code in sorted(codes.items()):
        print(f'   {device_id:<24} {code}')
    print(line + '\n')
