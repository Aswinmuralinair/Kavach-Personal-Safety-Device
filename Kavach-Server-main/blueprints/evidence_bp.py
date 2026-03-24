"""
blueprints/evidence_bp.py — Kavach Server

Evidence Chain Ledger API endpoints. Provides evidence listing per alert,
individual evidence verification, and full ledger integrity verification.

All endpoints require authentication (admin session or Bearer token).
"""

from flask import Blueprint, jsonify, request, send_from_directory
import os

evidence_bp = Blueprint('evidence', __name__, url_prefix='/api/evidence')


def _check_auth():
    """Import auth check from app module (avoids circular imports)."""
    from app import _check_any_auth
    return _check_any_auth()


def _get_upload_dir():
    from app import UPLOAD_DIR
    return UPLOAD_DIR


@evidence_bp.route('/alert/<int:alert_id>', methods=['GET'])
def list_evidence_for_alert(alert_id):
    """List all evidence files for a specific alert, with hash verification."""
    if not _check_auth():
        return jsonify({'status': 'error', 'message': 'Authentication required'}), 401

    from database import DB, Evidence
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
        return jsonify({'status': 'error', 'message': 'Authentication required'}), 401

    from database import Evidence
    ev = Evidence.query.get(evidence_id)
    if not ev:
        return jsonify({'status': 'error', 'message': 'Evidence not found'}), 404
    return jsonify({'status': 'ok', 'evidence': ev.to_dict()}), 200


@evidence_bp.route('/<int:evidence_id>/verify', methods=['GET'])
def verify_evidence(evidence_id):
    """Re-hash the stored file and compare with the recorded SHA-256 hash."""
    if not _check_auth():
        return jsonify({'status': 'error', 'message': 'Authentication required'}), 401

    from database import Evidence
    from utils import compute_sha256

    ev = Evidence.query.get(evidence_id)
    if not ev:
        return jsonify({'status': 'error', 'message': 'Evidence not found'}), 404

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
        return jsonify({'status': 'error', 'message': 'Authentication required'}), 401

    from database import Evidence
    ev = Evidence.query.get(evidence_id)
    if not ev:
        return jsonify({'status': 'error', 'message': 'Evidence not found'}), 404

    directory = os.path.dirname(ev.file_path) or _get_upload_dir()
    filename = os.path.basename(ev.file_path)
    return send_from_directory(directory, filename, as_attachment=True)


@evidence_bp.route('/ledger/verify', methods=['GET'])
def verify_ledger():
    """Verify the integrity of the entire evidence chain ledger."""
    if not _check_auth():
        return jsonify({'status': 'error', 'message': 'Authentication required'}), 401

    from evidence import verify_ledger_integrity
    intact, message = verify_ledger_integrity()
    return jsonify({
        'status': 'ok',
        'intact': intact,
        'message': message,
    }), 200


@evidence_bp.route('/ledger', methods=['GET'])
def get_ledger():
    """Return the full ledger (admin/debug endpoint)."""
    if not _check_auth():
        return jsonify({'status': 'error', 'message': 'Authentication required'}), 401

    from evidence import get_ledger_entries
    entries = get_ledger_entries()
    return jsonify({
        'status': 'ok',
        'count': len(entries),
        'entries': entries,
    }), 200
