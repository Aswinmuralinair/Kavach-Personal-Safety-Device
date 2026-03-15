"""
main.py — Project Kavach

Full state machine refactor with concurrent trigger threads.

STATE MACHINE:
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  ┌──────────┐   any trigger   ┌──────────────┐                 │
│  │   IDLE   │ ───────────────► │ ALERT_ACTIVE │                 │
│  └──────────┘                 └──────────────┘                 │
│       ▲                              │                          │
│       │        long press 5s         │                          │
│       └─────────────────────────────┘                          │
│                  (safe_sequence)                                │
│                                                                 │
│  Triggers that cause IDLE → ALERT_ACTIVE:                       │
│    • Button single press  → SOS                                 │
│    • Button double press  → MEDICAL                             │
│    • IMU fall detected    → SOS                                 │
│    • Heart rate spike     → SOS                                 │
│    • Audio danger sound   → SOS                                 │
│                                                                 │
│  Trigger that causes ALERT_ACTIVE → IDLE:                       │
│    • Button long press 5s → SAFE (cancel + notify guardian)     │
│                                                                 │
│  While ALERT_ACTIVE:                                            │
│    • All new triggers are BLOCKED (one alert at a time)         │
│    • 60-second GPS + battery + evidence loop runs               │
│    • LoRa broadcasts in parallel if 4G upload fails             │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

CONCURRENT TRIGGER THREADS (all run from boot, daemon threads):
    Thread 1: ButtonPoller      — 50 Hz GPIO poll (button.py FSM)
    Thread 2: IMUMonitor        — 10 Hz BNO055/FakeIMU reads
    Thread 3: HeartRateMonitor  — 0.2 Hz MAX30102/FakeHeartRate reads
    Thread 4: YAMNetAudioThread — continuous mic stream + inference
    Thread 5: LoRaRX            — continuous SX1278 receive loop

All threads call the same central trigger_alert() function.
A threading.Lock ensures only the first caller wins — all others
are silently dropped while an alert is already active.

FIX applied (serial port conflict):
    A single SIM7600 instance is created once at boot (step 2) and stored
    on the KavachStateMachine.  Every call to trigger_alert() and
    trigger_safe() forwards that shared instance into the alert sequence
    functions.  The serial port is therefore opened exactly once and never
    contested between concurrent sequences.
"""

import signal
import threading
import time
import logging
import json
from enum import Enum, auto

from sqlalchemy import create_engine

from database import Alert, Base
from hardware.button import ButtonHandler
from hardware.sensors import SensorManager
from hardware.audio import AudioManager, DetectionEvent
from hardware.lora import LoRaManager, LoRaPacket
from hardware.comms import SIM7600
from alerts import sos_sequence, medical_sequence, safe_sequence

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("kavach.main")

# ─────────────────────────────────────────────────────────────────────────────
# Device States
# ─────────────────────────────────────────────────────────────────────────────

class DeviceState(Enum):
    IDLE         = auto()   # waiting for any trigger, all sensors active
    ALERT_ACTIVE = auto()   # SOS or MEDICAL loop running, new triggers blocked


# ─────────────────────────────────────────────────────────────────────────────
# State Machine
# ─────────────────────────────────────────────────────────────────────────────

class KavachStateMachine:
    """
    Central state machine. Single instance lives in module scope.
    All trigger threads call trigger_alert() or trigger_safe() on it.

    Thread safety:
        _lock guards every state read/write.
        Only one alert sequence can run at a time — the first trigger that
        calls trigger_alert() while IDLE wins. All subsequent calls while
        ALERT_ACTIVE return immediately with a log message.

    SIM7600 ownership:
        The shared `sim` instance is injected at construction time and
        forwarded into every alert sequence. The serial port is opened once,
        in main(), before this machine is started.
    """

    def __init__(self, sim: SIM7600):
        self._state      = DeviceState.IDLE
        self._lock       = threading.Lock()
        self._alert_type = None   # "sos" | "medical" | None
        self._sim        = sim    # shared serial port — never re-opened

    # ── Public properties ─────────────────────────────────────────────────────

    @property
    def state(self) -> DeviceState:
        with self._lock:
            return self._state

    @property
    def is_idle(self) -> bool:
        with self._lock:
            return self._state == DeviceState.IDLE

    @property
    def is_alert_active(self) -> bool:
        with self._lock:
            return self._state == DeviceState.ALERT_ACTIVE

    # ── State transitions ─────────────────────────────────────────────────────

    def trigger_alert(self, alert_type: str, trigger_source: str) -> bool:
        """
        Attempt to transition IDLE → ALERT_ACTIVE and launch the
        appropriate alert sequence in a new daemon thread.

        Returns True  if the alert was accepted and launched.
        Returns False if an alert is already active (trigger dropped).

        alert_type:     "sos" | "medical"
        trigger_source: descriptive string logged to DB, e.g.
                        "button_single", "fall_detected", "audio_Screaming"
        """
        with self._lock:
            if self._state != DeviceState.IDLE:
                logger.warning(
                    "[StateMachine] BLOCKED — %s trigger '%s' dropped "
                    "(already in %s state, active alert: %s).",
                    alert_type.upper(), trigger_source,
                    self._state.name, self._alert_type
                )
                return False

            # Transition: IDLE → ALERT_ACTIVE
            self._state      = DeviceState.ALERT_ACTIVE
            self._alert_type = alert_type
            logger.info(
                "[StateMachine] IDLE → ALERT_ACTIVE | type=%s trigger='%s'",
                alert_type.upper(), trigger_source
            )

        # Launch the alert sequence in a daemon thread (outside the lock)
        if alert_type == "sos":
            target = sos_sequence
            kwargs = {"sim": self._sim, "trigger_source": trigger_source}
        elif alert_type == "medical":
            target = medical_sequence
            kwargs = {"sim": self._sim}
        else:
            logger.error("[StateMachine] Unknown alert_type: '%s'", alert_type)
            with self._lock:
                self._state      = DeviceState.IDLE
                self._alert_type = None
            return False

        threading.Thread(
            target=self._run_alert,
            args=(target, kwargs),
            name=f"Alert_{alert_type.upper()}",
            daemon=True
        ).start()
        return True

    def _run_alert(self, target_fn, kwargs: dict) -> None:
        """
        Wrapper that runs the alert sequence and resets state to IDLE
        when the sequence ends (either naturally or via safe_sequence).
        """
        try:
            target_fn(**kwargs)
        except Exception as exc:
            logger.error(
                "[StateMachine] Alert sequence raised: %s", exc, exc_info=True
            )
        finally:
            with self._lock:
                prev_type        = self._alert_type
                self._state      = DeviceState.IDLE
                self._alert_type = None
            logger.info(
                "[StateMachine] ALERT_ACTIVE → IDLE | completed: %s",
                (prev_type or "unknown").upper()
            )

    def trigger_safe(self) -> None:
        """
        Long press handler. Runs safe_sequence() regardless of current state:
          - If ALERT_ACTIVE: cancels the running loop and sends 'I am safe' SMS
          - If IDLE:         sends a check-in SMS to guardian

        State resets to IDLE via _run_alert's finally block when the cancelled
        sos/medical sequence exits.
        """
        logger.info("[StateMachine] SAFE triggered (long press 5s).")
        with self._lock:
            was_active = self._alert_type   # capture before the thread reads it

        threading.Thread(
            target=safe_sequence,
            kwargs={"sim": self._sim, "was_active_type": was_active},
            name="Safe",
            daemon=True
        ).start()

    def status_line(self) -> str:
        with self._lock:
            return f"State={self._state.name} ActiveAlert={self._alert_type or 'none'}"


# ─────────────────────────────────────────────────────────────────────────────
# Config loader
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open('config.json', 'r') as f:
        return json.load(f)


def validate_config(config: dict) -> None:
    """Log CRITICAL warnings if config still contains placeholder values."""
    placeholders = {
        'guardian_number': '+91XXXXXXXXXX',
        'medical_number':  '+91YYYYYYYYYY',
        'server_url':      'http://your-server-ip:8080/api/alerts',
        'server_public_url': 'http://your-server-ip:8080/uploads/',
    }
    for key, placeholder in placeholders.items():
        if config.get(key) == placeholder:
            logger.critical(
                "[Config] PLACEHOLDER NOT REPLACED: '%s' is still '%s'. "
                "Update config.json before using the device in the field.",
                key, placeholder
            )


# ─────────────────────────────────────────────────────────────────────────────
# Module-level state machine instance placeholder
# (assigned after SIM7600 is constructed in __main__)
# ─────────────────────────────────────────────────────────────────────────────

kavach: KavachStateMachine = None   # type: ignore  — set in __main__
lora_manager: LoRaManager  = None   # type: ignore  — set in __main__


# ─────────────────────────────────────────────────────────────────────────────
# TRIGGER THREAD 1 — Button (runs inside hardware/button.py's polling thread)
# ─────────────────────────────────────────────────────────────────────────────

def _on_sos():
    """Single press → SOS."""
    kavach.trigger_alert("sos", "button_single")

def _on_medical():
    """Double press → Medical alert."""
    kavach.trigger_alert("medical", "button_double")

def _on_safe():
    """Long press 5 s → Safe / cancel."""
    kavach.trigger_safe()


# ─────────────────────────────────────────────────────────────────────────────
# TRIGGER THREAD 2 — IMU fall detection
# ─────────────────────────────────────────────────────────────────────────────

def _imu_monitor(sensor_manager: SensorManager) -> None:
    """
    Polls the IMU at 10 Hz (every 100 ms).
    Triggers SOS if the acceleration magnitude exceeds the fall threshold.
    Falls back to FakeIMU data automatically — no code changes needed.
    """
    logger.info("[IMU] Monitor thread started.")
    while True:
        try:
            reading = sensor_manager.imu.read()
            if reading.is_fall_detected:
                logger.warning(
                    "[IMU] FALL DETECTED magnitude=%.2f m/s² hardware=%s",
                    reading.accel_magnitude,
                    "REAL" if reading.is_real_hardware else "FAKE"
                )
                kavach.trigger_alert("sos", "fall_detected")
        except Exception as exc:
            logger.error("[IMU] Read error: %s", exc)
        time.sleep(0.1)


# ─────────────────────────────────────────────────────────────────────────────
# TRIGGER THREAD 3 — Heart rate / SpO2 monitoring
# ─────────────────────────────────────────────────────────────────────────────

def _heart_rate_monitor(sensor_manager: SensorManager) -> None:
    """
    Reads heart rate every 5 seconds.
    Triggers SOS if BPM >= SensorManager.BPM_DISTRESS_THRESHOLD (default 140).
    Falls back to FakeHeartRate data automatically.
    """
    logger.info("[HeartRate] Monitor thread started.")
    while True:
        try:
            reading = sensor_manager.heart_rate.read()
            if reading.is_valid:
                logger.info(
                    "[HeartRate] BPM=%.1f SpO2=%.1f%% hardware=%s",
                    reading.bpm, reading.spo2,
                    "REAL" if reading.is_real_hardware else "FAKE"
                )
                if reading.is_distress_detected:
                    logger.warning(
                        "[HeartRate] DISTRESS DETECTED BPM=%.1f", reading.bpm
                    )
                    kavach.trigger_alert("sos", "heartrate_spike")
        except Exception as exc:
            logger.error("[HeartRate] Read error: %s", exc)
        time.sleep(5)


# ─────────────────────────────────────────────────────────────────────────────
# TRIGGER THREAD 4 — Audio danger sound detection (runs inside audio.py)
# ─────────────────────────────────────────────────────────────────────────────

def _on_audio_detection(event: DetectionEvent) -> None:
    """
    Called by AudioManager's background YAMNet inference thread.
    Fires SOS for screaming, gunshots, explosions, crashes, alarms, etc.
    Only events with event.should_trigger_sos=True are acted on.
    """
    if event.should_trigger_sos:
        logger.warning(
            "[Audio] DANGER SOUND: '%s' category=%s conf=%.0f%%",
            event.sound_class, event.category, event.confidence * 100
        )
        safe_name = (
            event.sound_class
            .replace(' ', '_')
            .replace(',', '')
            .replace('/', '_')
        )
        kavach.trigger_alert("sos", f"audio_{safe_name}")
    else:
        logger.info(
            "[Audio] Sound event (below trigger threshold): '%s' %.0f%%",
            event.sound_class, event.confidence * 100
        )


# ─────────────────────────────────────────────────────────────────────────────
# TRIGGER THREAD 5 — LoRa mesh relay (runs inside lora.py's RX thread)
# ─────────────────────────────────────────────────────────────────────────────

def _on_lora_packet(packet: LoRaPacket) -> None:
    """
    Called when a LoRa packet arrives from another Kavach device.

    Relay logic:
        SOS/MEDICAL from another device → re-broadcast with hop_count + 1
        (max 3 hops to prevent infinite relay loops)
        SAFE      → log only
        HEARTBEAT → log only (future feature)
    """
    if lora_manager is None:
        logger.warning("[LoRa] Packet received but lora_manager is None — ignoring.")
        return

    logger.info(
        "[LoRa] RX: type=%-8s from=%-15s hop=%d gps=%s",
        packet.packet_type, packet.device_id,
        packet.hop_count, packet.gps_location
    )

    if packet.packet_type in ("SOS", "MEDICAL"):
        if packet.hop_count < 3:
            relay = LoRaPacket(
                packet_type=packet.packet_type,
                device_id=packet.device_id,
                trigger=packet.trigger,
                gps_location=packet.gps_location,
                battery=packet.battery,
                timestamp=packet.timestamp,
                hop_count=packet.hop_count + 1,
            )
            threading.Thread(
                target=lora_manager.radio.send_packet,
                args=(relay,),
                daemon=True
            ).start()
            logger.info(
                "[LoRa] Relay forwarded: %s from %s (hop %d → %d).",
                packet.packet_type, packet.device_id,
                packet.hop_count, relay.hop_count
            )
        else:
            logger.warning(
                "[LoRa] Max hops reached for packet from %s — not relayed.",
                packet.device_id
            )
    elif packet.packet_type == "SAFE":
        logger.info("[LoRa] SAFE from %s — no action.", packet.device_id)
    elif packet.packet_type == "HEARTBEAT":
        logger.debug("[LoRa] Heartbeat from %s.", packet.device_id)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info(" Project Kavach — starting up")
    logger.info("=" * 60)

    # ── 1. Database ───────────────────────────────────────────────────────────
    engine = create_engine('sqlite:///alerts.db')
    Base.metadata.create_all(engine)
    logger.info("[Boot] Database ready.")

    # ── 2. Config ─────────────────────────────────────────────────────────────
    config = load_config()
    validate_config(config)
    logger.info("[Boot] Config loaded. Device ID: %s", config.get('device_id'))

    # ── 3. Single shared SIM7600 instance ─────────────────────────────────────
    #       Created ONCE here — never re-opened inside alert sequences.
    sim = SIM7600(port=config['serial_port'], baud=config['baud_rate'])
    logger.info("[Boot] SIM7600 initialized on %s.", config['serial_port'])

    # ── 4. State machine — inject the shared sim ──────────────────────────────
    kavach = KavachStateMachine(sim=sim)

    # ── 5. Sensor manager ─────────────────────────────────────────────────────
    sensor_manager = SensorManager()
    sensor_manager.start()
    logger.info("[Boot] %s", sensor_manager.status_string())

    # ── 6. Button handler ─────────────────────────────────────────────────────
    button = ButtonHandler(
        pin=config['sos_button_pin'],
        on_sos_press=_on_sos,
        on_medical_press=_on_medical,
        on_safe_press=_on_safe,
    )
    button.start()
    logger.info("[Boot] Button handler started on pin %d.", config['sos_button_pin'])

    # ── 7. Audio detection ────────────────────────────────────────────────────
    audio_manager = AudioManager()
    audio_manager.start(on_detection=_on_audio_detection)
    logger.info("[Boot] %s", audio_manager.status_string())

    # ── 8. LoRa off-grid backup ───────────────────────────────────────────────
    lora_manager = LoRaManager()
    lora_manager.start(on_packet_received=_on_lora_packet)
    logger.info("[Boot] %s", lora_manager.status_string())

    # ── 9. IMU + Heart rate monitor threads ───────────────────────────────────
    threading.Thread(
        target=_imu_monitor,
        args=(sensor_manager,),
        name="IMUMonitor",
        daemon=True
    ).start()

    threading.Thread(
        target=_heart_rate_monitor,
        args=(sensor_manager,),
        name="HeartRateMonitor",
        daemon=True
    ).start()

    logger.info("[Boot] Sensor monitor threads started.")

    # ── 10. Ready ─────────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info(" Kavach ARMED — %s", kavach.status_line())
    logger.info(" Triggers active:")
    logger.info("   Button single press  → SOS")
    logger.info("   Button double press  → MEDICAL ALERT")
    logger.info("   Button long press 5s → SAFE (cancel + notify)")
    logger.info("   IMU fall detected    → SOS")
    logger.info("   Heart rate spike     → SOS")
    logger.info("   Audio danger sound   → SOS (YAMNet)")
    logger.info("   LoRa RX              → Mesh relay")
    logger.info("=" * 60)

    # ── 11. Main thread sleeps — all work is in daemon threads ────────────────
    try:
        signal.pause()
    except KeyboardInterrupt:
        logger.info("\n[Boot] Shutdown requested.")
    finally:
        logger.info("[Boot] Shutting down all subsystems...")
        button.stop()
        sensor_manager.stop()
        audio_manager.stop()
        lora_manager.stop()
        sim.close()             # ← cleanly release the serial port
        logger.info("[Boot] Kavach shutdown complete.")
        logger.info("=" * 60)