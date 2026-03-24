"""
database.py — Kavach Server

SQLAlchemy models:
  - Alert          — SOS/MEDICAL alert records from the device
  - Evidence       — individual evidence files with SHA-256 hashes
  - GuardianLink   — in-app guardian invite system (pending/active/revoked)
"""

from flask_sqlalchemy import SQLAlchemy
import datetime

DB = SQLAlchemy()

_UTC = datetime.timezone.utc   # single alias, works on Python 3.2+


class Alert(DB.Model):
    __tablename__ = 'alerts'

    # ── Primary key ───────────────────────────────────────────────────────────
    id = DB.Column(DB.Integer, primary_key=True)

    # ── When and who ──────────────────────────────────────────────────────────
    # Lambda ensures each row gets the current time, not a fixed import-time value
    timestamp  = DB.Column(DB.DateTime, default=lambda: datetime.datetime.now(_UTC))
    device_id  = DB.Column(DB.String(64), nullable=False)

    # ── Alert classification ──────────────────────────────────────────────────
    alert_type     = DB.Column(DB.String(16), nullable=True)   # "SOS" | "MEDICAL"
    trigger_source = DB.Column(DB.String(64), nullable=True)   # "button_single" | "fall_detected" | ...

    # ── Action status flags ───────────────────────────────────────────────────
    call_placed_status  = DB.Column(DB.Boolean, default=False)
    guardian_sms_status = DB.Column(DB.Boolean, default=False)
    location_sms_status = DB.Column(DB.Boolean, default=False)

    # ── Telemetry ─────────────────────────────────────────────────────────────
    gps_location       = DB.Column(DB.String(255), nullable=True)
    location_source    = DB.Column(DB.String(20),  nullable=True)   # "GPS" | "Cell Tower"
    battery_percentage = DB.Column(DB.String(10),  nullable=True)   # "87%" | "N/A" | "Error"

    # ── Evidence files ────────────────────────────────────────────────────────
    # Comma-separated filenames: "video_20250101_120000.h264,..."
    uploaded_files = DB.Column(DB.String(1024), nullable=True)

    # SHA-256 hash chain: "video_20250101_120000.h264:abc123...,...".
    # Populated server-side when files are received.
    file_hashes = DB.Column(DB.String(2048), nullable=True)

    def __repr__(self):
        return (f"<Alert(id={self.id}, device='{self.device_id}', "
                f"type='{self.alert_type}', trigger='{self.trigger_source}')>")


class Evidence(DB.Model):
    __tablename__ = 'evidence'

    id = DB.Column(DB.Integer, primary_key=True)
    alert_id = DB.Column(DB.Integer, DB.ForeignKey('alerts.id'), nullable=False)
    file_path = DB.Column(DB.Text, nullable=False)          # path on disk (uploads/)
    filename = DB.Column(DB.String(256), nullable=False)     # display filename
    sha256_hash = DB.Column(DB.String(64), nullable=False)   # hex digest
    file_type = DB.Column(DB.String(16), nullable=False)     # 'video', 'audio', 'image'
    file_size = DB.Column(DB.Integer, nullable=True)         # bytes
    created_at = DB.Column(DB.DateTime, default=lambda: datetime.datetime.now(_UTC))

    # Relationship back to Alert
    alert = DB.relationship('Alert', backref=DB.backref('evidence_files', lazy='dynamic'))

    def to_dict(self):
        return {
            'id': self.id,
            'alert_id': self.alert_id,
            'filename': self.filename,
            'sha256_hash': self.sha256_hash,
            'file_type': self.file_type,
            'file_size': self.file_size,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f"<Evidence(id={self.id}, alert={self.alert_id}, file='{self.filename}')>"


class GuardianLink(DB.Model):
    """
    In-app guardian invite system.

    A 'user' invites a 'guardian' by specifying the guardian's device_id.
    The guardian sees the pending invite in their app and can accept or reject.

    Status flow: pending → active (accepted) or revoked (rejected/removed)
    """
    __tablename__ = 'guardian_links'

    id = DB.Column(DB.Integer, primary_key=True)

    # The device owner who sent the invite
    user_device_id = DB.Column(DB.String(64), nullable=False)

    # The guardian who received the invite
    guardian_device_id = DB.Column(DB.String(64), nullable=False)

    # 'pending' = awaiting response, 'active' = accepted, 'revoked' = rejected/removed
    status = DB.Column(DB.String(16), default='pending', nullable=False)

    created_at = DB.Column(DB.DateTime, default=lambda: datetime.datetime.now(_UTC))

    def to_dict(self):
        return {
            'id': self.id,
            'user_device_id': self.user_device_id,
            'guardian_device_id': self.guardian_device_id,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return (f"<GuardianLink(id={self.id}, user='{self.user_device_id}', "
                f"guardian='{self.guardian_device_id}', status='{self.status}')>")