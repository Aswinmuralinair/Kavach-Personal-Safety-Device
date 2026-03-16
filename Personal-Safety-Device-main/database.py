"""
database.py — Personal Safety Device (client side)

FIXES APPLIED:
  - `from sqlalchemy.ext.declarative import declarative_base` was removed in
    SQLAlchemy 2.0. Now imports from `sqlalchemy.orm` (works on 1.4 and 2.x).
  - datetime.UTC → datetime.timezone.utc  (Python 3.2+ compatible)
  - Column default is now a callable lambda so each row gets the current time,
    not the single timestamp captured at import time.
"""

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean
from sqlalchemy.orm import declarative_base    # FIX: was sqlalchemy.ext.declarative
from sqlalchemy.orm import sessionmaker
import datetime

Base = declarative_base()

_UTC = datetime.timezone.utc   # FIX: datetime.UTC requires Python 3.11+


class Alert(Base):
    __tablename__ = 'alerts'

    id        = Column(Integer, primary_key=True)

    # FIX: lambda makes the default evaluate per-row, not once at import time
    timestamp = Column(DateTime, default=lambda: datetime.datetime.now(_UTC))
    device_id = Column(String(64), nullable=False)

    # Status fields for each action
    call_placed_status  = Column(Boolean, default=False)
    guardian_sms_status = Column(Boolean, default=False)
    location_sms_status = Column(Boolean, default=False)

    # Store the location if acquired
    gps_location = Column(String(255), nullable=True)

    battery_percentage = Column(String(10), nullable=True)   # e.g., "93.0%" or "N/A"
    trigger_source     = Column(String(64), nullable=True)
    alert_type         = Column(String(16), nullable=True)

    # To track uploaded files
    uploaded_files = Column(String(1024), nullable=True)

    def __repr__(self):
        return f"<Alert(id={self.id}, device='{self.device_id}', time='{self.timestamp}')>"


if __name__ == '__main__':
    engine = create_engine('sqlite:///alerts.db')
    Base.metadata.create_all(engine)
    print("Database table 'alerts' created/updated.")