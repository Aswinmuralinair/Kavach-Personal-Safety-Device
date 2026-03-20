"""
database.py — Kavach Server

SQLAlchemy model for the Alert table. Used by both the Flask API and the
admin web dashboard.
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