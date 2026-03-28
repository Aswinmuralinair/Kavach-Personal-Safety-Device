"""
database.py — Personal Safety Device (Raspberry Pi)

SQLAlchemy model for the local Alert table. Stores alert records on the
device before they are uploaded to the Kavach server.
"""

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean
from sqlalchemy.orm import declarative_base
import datetime

Base = declarative_base()

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name='IST')   # Indian Standard Time


class Alert(Base):
    __tablename__ = 'alerts'

    id        = Column(Integer, primary_key=True)

    # Lambda ensures each row gets the current time, not a fixed import-time value
    timestamp = Column(DateTime, default=lambda: datetime.datetime.now(_IST))
    device_id = Column(String(64), nullable=False)

    # Status fields for each action
    call_placed_status  = Column(Boolean, default=False)
    guardian_sms_status = Column(Boolean, default=False)
    location_sms_status = Column(Boolean, default=False)

    # Store the location if acquired
    gps_location    = Column(String(255), nullable=True)
    location_source = Column(String(20),  nullable=True)   # "GPS" | "Cell Tower"

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