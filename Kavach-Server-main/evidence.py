"""
evidence.py — Kavach Server

Evidence Chain Ledger: lightweight blockchain-style integrity chain for
evidence files. Each entry records the SHA-256 hash of the evidence file
and a prev_hash linking it to the previous entry, forming a tamper-evident
chain. If any entry is modified, the chain verification will detect it.

Used by:
  - receive_alert() in app.py — appends to ledger after saving evidence
  - GET /api/evidence/ledger/verify — walks chain to verify integrity
"""

import hashlib
import json
import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_UTC = timezone.utc
LEDGER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'evidence_ledger.json')


def file_type_from_ext(filename: str) -> str:
    """Determine evidence file type from extension."""
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    mapping = {
        'mp4': 'video', 'h264': 'video', 'avi': 'video', 'mov': 'video',
        'wav': 'audio', 'mp3': 'audio', 'ogg': 'audio',
        'jpg': 'image', 'jpeg': 'image', 'png': 'image',
    }
    return mapping.get(ext, 'other')


def append_to_ledger(evidence_id: int, sha256_hash: str, file_path: str, alert_id: int) -> dict:
    """
    Append an entry to the append-only evidence ledger.

    Each entry contains:
      - evidence_id, alert_id, sha256, file, timestamp
      - prev_hash: SHA-256 of the JSON-serialized previous entry

    The first entry uses '0' * 64 as prev_hash (genesis block).
    """
    entry = {
        'evidence_id': evidence_id,
        'alert_id': alert_id,
        'sha256': sha256_hash,
        'file': os.path.basename(file_path),
        'timestamp': datetime.now(_UTC).isoformat(),
    }

    # Read existing chain
    entries = []
    if os.path.exists(LEDGER_PATH):
        try:
            with open(LEDGER_PATH, 'r') as f:
                entries = json.load(f)
        except (json.JSONDecodeError, IOError):
            logger.warning("[Ledger] Could not read existing ledger — starting fresh.")
            entries = []

    # Compute prev_hash from the last entry
    if entries:
        prev_raw = json.dumps(entries[-1], sort_keys=True).encode()
        entry['prev_hash'] = hashlib.sha256(prev_raw).hexdigest()
    else:
        entry['prev_hash'] = '0' * 64

    entries.append(entry)

    # Write back
    with open(LEDGER_PATH, 'w') as f:
        json.dump(entries, f, indent=2)

    logger.info(
        "[Ledger] Appended evidence_id=%d alert_id=%d hash=%s prev=%s",
        evidence_id, alert_id, sha256_hash[:16] + "...", entry['prev_hash'][:16] + "..."
    )
    return entry


def verify_ledger_integrity() -> tuple:
    """
    Walk the entire ledger and verify the hash chain is unbroken.

    Returns (is_intact: bool, message: str).
    """
    if not os.path.exists(LEDGER_PATH):
        return True, 'Ledger is empty — no evidence recorded yet'

    try:
        with open(LEDGER_PATH, 'r') as f:
            entries = json.load(f)
    except (json.JSONDecodeError, IOError):
        return False, 'Ledger file is corrupt or unreadable'

    if not entries:
        return True, 'Ledger is empty'

    for i, entry in enumerate(entries):
        if i == 0:
            expected_prev = '0' * 64
        else:
            prev_raw = json.dumps(entries[i - 1], sort_keys=True).encode()
            expected_prev = hashlib.sha256(prev_raw).hexdigest()

        if entry.get('prev_hash') != expected_prev:
            return False, (
                f'Chain broken at entry {i} '
                f'(evidence_id={entry.get("evidence_id")}). '
                f'Expected prev_hash={expected_prev[:16]}..., '
                f'got={entry.get("prev_hash", "MISSING")[:16]}...'
            )

    return True, f'Ledger intact — {len(entries)} entries verified'


def get_ledger_entries() -> list:
    """Return all ledger entries (for admin/debug view)."""
    if not os.path.exists(LEDGER_PATH):
        return []
    try:
        with open(LEDGER_PATH, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []
