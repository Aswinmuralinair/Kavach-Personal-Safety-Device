"""
blueprints/evidence_bp.py — Kavach Server

Evidence Chain Ledger API endpoints. Provides evidence listing per alert,
individual evidence verification, and full ledger integrity verification.

Authorization
-------------
Being authenticated is not enough on any of these routes. Evidence is the
most sensitive data in the system — audio and video captured during someone's
emergency — so every route resolves the alert that owns the file and checks
that the caller is entitled to that specific device.

The admin dashboard session is cross-device by design; an app token is
confined to the device it was issued for. Where a record exists but belongs to
someone else, these routes answer 404 rather than 403, so that walking the
integer IDs cannot be used to discover how much evidence other devices hold.
"""

from flask import Blueprint, jsonify, request, send_from_directory
import os

evidence_bp = Blueprint('evidence', __name__, url_prefix='/api/evidence')


def _check_auth():
    """True if the request is authenticated at all (admin session or token)."""
    from app import _check_any_auth
    return _check_any_auth()


def _is_admin():
    from app import _is_admin as _admin
    return _admin()


def _may_access_alert(alert_id: int) -> bool:
    """True if the caller may see data belonging to this alert's device."""
    from app import _authorize_device, _device_id_for_alert
    device_id = _device_id_for_alert(alert_id)
    if device_id is None:
        return False
    return _authorize_device(device_id)


def _may_access_evidence(ev) -> bool:
    """True if the caller may see this Evidence row."""
    return _may_access_alert(ev.alert_id)


def _get_upload_dir():
    from app import UPLOAD_DIR
    return UPLOAD_DIR


def _unauthenticated():
    return jsonify({'status': 'error', 'message': 'Authentication required'}), 401


def _not_found(what='Evidence'):
    return jsonify({'status': 'error', 'message': f'{what} not found'}), 404


@evidence_bp.route('/alert/<int:alert_id>', methods=['GET'])
def list_evidence_for_alert(alert_id):
    """List all evidence files for a specific alert, with hash verification."""
    if not _check_auth():
        return _unauthenticated()
    if not _may_access_alert(alert_id):
        return _not_found('Alert')

    from database import Evidence
    items = Evidence.query.filter_by(alert_id=alert_id).order_by(Evidence.created_at).all()
    return jsonify({
        'status': 'ok',
        'alert_id': alert_id,
        'count': len(items),
        'evidence': [e.to_dict() for e in items],
    }), 200


@evidence_bp.route('/<int:evidence_id>', methods=['GET'])
def get_evidence(evidence_id):
    """Get details of a single evidence file."""
    if not _check_auth():
        return _unauthenticated()

    from database import DB, Evidence
    ev = DB.session.get(Evidence, evidence_id)
    if not ev or not _may_access_evidence(ev):
        return _not_found()
    return jsonify({'status': 'ok', 'evidence': ev.to_dict()}), 200


@evidence_bp.route('/<int:evidence_id>/verify', methods=['GET'])
def verify_evidence(evidence_id):
    """Re-hash the stored file and compare with the recorded SHA-256 hash."""
    if not _check_auth():
        return _unauthenticated()

    from database import DB, Evidence
    from utils import compute_sha256

    ev = DB.session.get(Evidence, evidence_id)
    if not ev or not _may_access_evidence(ev):
        return _not_found()

    if not os.path.exists(ev.file_path):
        return jsonify({
            'status': 'ok',
            'verified': False,
            'error': 'File not found on disk',
            'stored_hash': ev.sha256_hash,
        }), 200

    current_hash = compute_sha256(ev.file_path)
    match = current_hash == ev.sha256_hash

    return jsonify({
        'status': 'ok',
        'verified': match,
        'stored_hash': ev.sha256_hash,
        'current_hash': current_hash,
        'integrity': 'verified' if match else 'tampered',
    }), 200


@evidence_bp.route('/<int:evidence_id>/download', methods=['GET'])
def download_evidence(evidence_id):
    """Download an evidence file."""
    if not _check_auth():
        return _unauthenticated()

    from database import DB, Evidence
    ev = DB.session.get(Evidence, evidence_id)
    if not ev or not _may_access_evidence(ev):
        return _not_found()

    # Serve strictly from the uploads directory using the basename, so a
    # stored file_path can never be used to read elsewhere on disk.
    filename = os.path.basename(ev.file_path)
    return send_from_directory(_get_upload_dir(), filename, as_attachment=True)


@evidence_bp.route('/ledger/verify', methods=['GET'])
def verify_ledger():
    """
    Verify the integrity of the entire evidence chain ledger.

    Admin only: the ledger spans every device, so there is no way to scope
    this result to one household.
    """
    if not _check_auth():
        return _unauthenticated()
    if not _is_admin():
        return jsonify({
            'status': 'error',
            'message': 'Ledger verification is available to the operator only.',
        }), 403

    from evidence import verify_ledger_integrity
    intact, message = verify_ledger_integrity()
    return jsonify({
        'status': 'ok',
        'intact': intact,
        'message': message,
    }), 200


@evidence_bp.route('/ledger', methods=['GET'])
def get_ledger():
    """Return the full ledger. Admin only — it lists every device's evidence."""
    if not _check_auth():
        return _unauthenticated()
    if not _is_admin():
        return jsonify({
            'status': 'error',
            'message': 'The ledger is available to the operator only.',
        }), 403

    from evidence import get_ledger_entries
    entries = get_ledger_entries()
    return jsonify({
        'status': 'ok',
        'count': len(entries),
        'entries': entries,
    }), 200
