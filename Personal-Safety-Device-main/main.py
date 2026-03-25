"""
main.py — Project Kavach

Full state machine with concurrent trigger threads.

STATE MACHINE:
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  ┌──────────┐  any trigger  ┌──────────────┐                   │
│  │   IDLE   │ ────────────► │ ALERT_ACTIVE │                   │
│  └──────────┘               └──────────────┘                   │
│        ▲                           │                           │
│        │       long press 5s       │                           │
│        └───────────────────────────┘                           │
│                 (safe_sequence)                                 │
│                                                                 │
│  Triggers that cause IDLE → ALERT_ACTIVE:                      │
│    • Button single press  → SOS                                │
│    • Button double press  → MEDICAL                            │
│    • IMU fall detected    → SOS                                │
│    • Heart rate spike     → SOS                                │
│    • Audio danger sound   → SOS                                │
│                                                                 │
│  Trigger that causes ALERT_ACTIVE → IDLE:                      │
│    • Button long press 5s → SAFE (cancel + notify guardian)    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

CONCURRENT TRIGGER THREADS (all run from boot, daemon threads):
  Thread 1: ButtonPoller       — 50 Hz GPIO poll
  Thread 2: IMUMonitor         — 10 Hz BNO055/FakeIMU reads
  Thread 3: HeartRateMonitor   — 0.2 Hz MAX30102/FakeHeartRate reads
  Thread 4: YAMNetAudioThread  — continuous mic stream + inference
  Thread 5: LoRaRX             — continuous SX1278 receive loop (disabled if no hardware)

EVIDENCE CAPTURE (started/stopped per alert, daemon threads):
  CameraManager        — 25-second MP4 clips via rpicam-vid / FakeCameraRecorder
  AudioRecorderManager — 42-second WAV clips via real microphone (disabled if no physical mic)

BACKGROUND SERVICES (daemon threads):
  ConfigSync           — polls server every 10s for config changes + sends battery heartbeat
                         (sleep is at the TOP of the loop so `continue` never skips it)
  KeyboardDemo         — listens for f/h/a/s/d/l/q demo keys (works on Pi and desktop)

FIX applied (serial port conflict):
  A single SIM7600 instance is created once at boot (step 3) and stored on
  the KavachStateMachine.  Every call to trigger_alert() and trigger_safe()
  forwards that shared instance into the alert sequence functions.  The serial
  port is therefore opened exactly once and never contested.
"""

import threading
import time
import logging
import json
import os

from enum import Enum, auto
from database import Base
from hardware.button         import ButtonHandler
from hardware.sensors        import SensorManager
from hardware.audio          import AudioManager, DetectionEvent
from hardware.lora           import LoRaManager, LoRaPacket
from hardware.comms          import SIM7600
from hardware.camera         import CameraManager
from hardware.audio_recorder import AudioRecorderManager
from alerts import sos_sequence, medical_sequence, safe_sequence

# ─────────────────────────────────────────────────────────────────────────────
# Base directory — all relative paths (DB, config, evidence) resolve from here
# so the device works regardless of the current working directory (e.g. systemd).
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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
    Central state machine.  Single instance lives in module scope.
    All trigger threads call trigger_alert() or trigger_safe() on it.

    Thread safety:
      _lock guards every state read/write.
      Only one alert sequence can run at a time — the first trigger that
      calls trigger_alert() while IDLE wins.  All subsequent calls while
      ALERT_ACTIVE return immediately with a log message.

    SIM7600 ownership:
      The shared `sim` instance is injected at construction time and forwarded
      into every alert sequence.  The serial port is opened once, in main(),
      before this machine is started.
    """

    def __init__(self, sim: SIM7600, cam: CameraManager = None,
                 mic: AudioRecorderManager = None, audio_manager=None):
        self._state         = DeviceState.IDLE
        self._lock          = threading.Lock()
        self._alert_type    = None   # "sos" | "medical" | None
        self._sim           = sim    # shared serial port — never re-opened
        self._cam           = cam    # evidence video recording
        self._mic           = mic    # evidence audio recording
        self._audio_manager = audio_manager  # YAMNet — paused during alerts to free RAM
        self._audio_cb      = None           # stored so we can resume after alert

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
        Attempt to transition IDLE → ALERT_ACTIVE and launch the appropriate
        alert sequence in a new daemon thread.

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
            kwargs = {"sim": self._sim, "trigger_source": trigger_source,
                      "cam": self._cam, "mic": self._mic,
                      "_audio_cb": self._audio_cb}
        elif alert_type == "medical":
            target = medical_sequence
            kwargs = {"sim": self._sim, "cam": self._cam, "mic": self._mic,
                      "_audio_cb": self._audio_cb}
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
        Pauses YAMNet during the alert to free RAM for evidence encryption/upload.
        """
        # Pause YAMNet and force-free TFLite memory before starting camera
        if self._audio_manager:
            try:
                self._audio_manager.stop()
                logger.info("[StateMachine] YAMNet paused to free RAM for evidence processing.")
            except Exception:
                pass
        # Force garbage collection to reclaim TFLite model memory (~50 MB)
        # before rpicam-vid subprocess + crypto encryption compete for RAM.
        # The interpreter was explicitly deleted in audio.py shutdown(),
        # so gc.collect() can now actually free the C-level memory.
        import gc
        gc.collect()
        time.sleep(2)  # let OS fully reclaim freed pages before crypto runs
        try:
            target_fn(**kwargs)
        except Exception as exc:
            logger.error("[StateMachine] Alert sequence raised: %s", exc, exc_info=True)
        finally:
            # Resume YAMNet after alert completes
            if self._audio_manager:
                try:
                    self._audio_manager.start(on_detection=kwargs.get('_audio_cb'))
                    logger.info("[StateMachine] YAMNet resumed.")
                except Exception:
                    pass
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
        Long press handler.  Runs safe_sequence() regardless of current state:
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
            kwargs={"sim": self._sim, "was_active_type": was_active,
                    "cam": self._cam, "mic": self._mic},
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
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, 'config.json')
    with open(config_path, 'r') as f:
        return json.load(f)


def validate_config(config: dict) -> None:
    """Log CRITICAL warnings if config still contains placeholder values."""
    placeholders = {
        'guardian_number':   '+91XXXXXXXXXX',
        'medical_number':    '+91YYYYYYYYYY',
        'whatsapp_number':   '+91XXXXXXXXXX',
        'whatsapp_apikey':   'YOUR_CALLMEBOT_APIKEY',
    }
    for key, placeholder in placeholders.items():
        if config.get(key) == placeholder:
            logger.critical(
                "[Config] PLACEHOLDER NOT REPLACED: '%s' is still '%s'. "
                "Update config.json before using the device in the field.",
                key, placeholder
            )


# ─────────────────────────────────────────────────────────────────────────────
# Module-level instances (assigned in __main__ after hardware init)
# ─────────────────────────────────────────────────────────────────────────────
kavach:         KavachStateMachine   = None   # type: ignore — set in __main__
lora_manager:   LoRaManager          = None   # type: ignore — set in __main__
camera_manager: CameraManager        = None   # type: ignore — set in __main__
audio_recorder: AudioRecorderManager = None   # type: ignore — set in __main__


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG POLLING — syncs remote config changes from the Kavach server
# ─────────────────────────────────────────────────────────────────────────────
CONFIG_POLL_INTERVAL = 10   # seconds between polls

def _config_poll_loop(config: dict, power_monitor=None) -> None:
    """
    Background thread: polls the server's /api/device/config/<device_id>
    endpoint periodically. If the server has updated phone numbers (from
    the mobile app), merges them into the local config.json.

    Also sends a heartbeat with the current battery percentage via the
    X-Battery header so the mobile app can display live device status.

    Only syncs phone number fields — serial_port, baud_rate, etc. are
    never overwritten by the server.
    """
    from alerts import _read_battery

    device_id  = config.get('device_id', 'KAVACH-001')
    server_url = config.get('server_url', '')

    # Derive base URL from the alerts endpoint (strip /api/alerts)
    if '/api/alerts' in server_url:
        base_url = server_url.rsplit('/api/alerts', 1)[0]
    elif server_url.endswith('/'):
        base_url = server_url.rstrip('/')
    else:
        base_url = server_url

    poll_url   = f"{base_url}/api/device/config/{device_id}"
    device_key = config.get('device_key', '')
    logger.info("[ConfigSync] Polling %s every %ds.", poll_url, CONFIG_POLL_INTERVAL)

    syncable_keys = {'police_number', 'guardian_number', 'medical_number', 'whatsapp_number'}

    while True:
        time.sleep(CONFIG_POLL_INTERVAL)
        try:
            import requests
            battery_str = _read_battery(power_monitor)
            r = requests.get(
                poll_url,
                headers={
                    'X-Device-Key': device_key,
                    'X-Battery':    battery_str,
                },
                timeout=10,
            )
            if r.status_code != 200:
                logger.debug("[ConfigSync] Server returned %d — skipping.", r.status_code)
                continue

            remote = r.json().get('config', {})
            if not remote:
                logger.debug("[ConfigSync] No remote config yet — skipping.")
                continue

            # Check if any syncable field differs from local config
            local_config  = load_config()
            changes       = {}
            for key in syncable_keys:
                remote_val = remote.get(key, '').strip()
                local_val  = local_config.get(key, '').strip()
                if remote_val and remote_val != local_val:
                    changes[key] = remote_val

            if not changes:
                logger.debug("[ConfigSync] No changes detected.")
                continue

            # Merge changes into local config.json
            local_config.update(changes)
            config_path = os.path.join(BASE_DIR, 'config.json')
            with open(config_path, 'w') as f:
                json.dump(local_config, f, indent=2)

            logger.info(
                "[ConfigSync] Config updated from server: %s",
                ', '.join(f"{k}={v}" for k, v in changes.items())
            )

        except Exception as exc:
            logger.debug("[ConfigSync] Poll failed (server may be offline): %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# KEYBOARD DEMO — console key listener for testing without hardware
# ─────────────────────────────────────────────────────────────────────────────

def _keyboard_listener(sensor_manager: SensorManager, audio_manager: AudioManager) -> None:
    """
    Reads single-character key presses from stdin for desktop demo/testing.

    Key bindings:
      f  → Fake fall event   (IMU spike → triggers SOS)
      h  → Fake heart spike  (BPM 150+  → triggers SOS)
      a  → Fake danger sound (screaming → triggers SOS)
      s  → Simulate SOS      (single button press)
      d  → Simulate MEDICAL  (double button press)
      l  → Simulate SAFE     (long press cancel)
      q  → Quit

    Only active when running on desktop (no GPIO) — on the Pi, real
    hardware triggers are used instead and this thread is not started.
    """
    import sys
    import select

    # Cross-platform single-char reader
    try:
        # Unix: use termios for raw unbuffered input
        import tty
        import termios
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)

        def read_key():
            tty.setraw(fd)
            try:
                ch = sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            return ch

    except (ImportError, AttributeError):
        # Windows: use msvcrt for unbuffered input
        try:
            import msvcrt

            def read_key():
                if msvcrt.kbhit():
                    return msvcrt.getch().decode('utf-8', errors='ignore').lower()
                time.sleep(0.1)
                return ''
        except ImportError:
            logger.warning("[Keyboard] No keyboard input method available — demo keys disabled.")
            return

    logger.debug("[Keyboard] Demo key listener active.")

    while True:
        try:
            key = read_key()
            if not key:
                continue

            key = key.lower()
            if key == 'f':
                logger.info("[Keyboard] 'f' pressed → triggering fake fall event")
                if hasattr(sensor_manager.imu, 'trigger_fall_now'):
                    sensor_manager.imu.trigger_fall_now()
                else:
                    logger.warning("[Keyboard] IMU is real hardware — cannot simulate fall.")

            elif key == 'h':
                logger.info("[Keyboard] 'h' pressed → triggering fake heart rate spike")
                if hasattr(sensor_manager.heart_rate, 'trigger_distress_now'):
                    sensor_manager.heart_rate.trigger_distress_now()
                else:
                    logger.warning("[Keyboard] Heart rate is real hardware — cannot simulate.")

            elif key == 'a':
                logger.info("[Keyboard] 'a' pressed → triggering fake danger sound (screaming)")
                audio_manager.simulate_detection("screaming")

            elif key == 's':
                logger.info("[Keyboard] 's' pressed → triggering SOS (single press)")
                kavach.trigger_alert("sos", "keyboard_demo")

            elif key == 'd':
                logger.info("[Keyboard] 'd' pressed → triggering MEDICAL (double press)")
                kavach.trigger_alert("medical", "keyboard_demo")

            elif key == 'l':
                logger.info("[Keyboard] 'l' pressed → triggering SAFE (long press cancel)")
                kavach.trigger_safe()

            elif key == 'q':
                logger.info("[Keyboard] 'q' pressed → shutdown requested")
                # Raise KeyboardInterrupt in main thread
                import _thread
                _thread.interrupt_main()
                break

        except (EOFError, OSError):
            # stdin closed (running as service, piped, etc.)
            logger.debug("[Keyboard] stdin closed — demo keys disabled.")
            break
        except Exception as exc:
            logger.debug("[Keyboard] Error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# TRIGGER THREAD 1 — Button
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
    Falls back to FakeIMU data automatically.
    """
    logger.debug("[IMU] Monitor thread started.")
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
    logger.debug("[HeartRate] Monitor thread started.")
    while True:
        try:
            reading = sensor_manager.heart_rate.read()
            if reading.is_valid:
                # Only log when real hardware is present (suppress fake data noise)
                if reading.is_real_hardware:
                    logger.info(
                        "[HeartRate] BPM=%.1f SpO2=%.1f%%",
                        reading.bpm, reading.spo2,
                    )
                if reading.is_distress_detected:
                    logger.warning("[HeartRate] DISTRESS DETECTED BPM=%.1f", reading.bpm)
                    kavach.trigger_alert("sos", "heartrate_spike")
        except Exception as exc:
            logger.error("[HeartRate] Read error: %s", exc)
        time.sleep(5)


# ─────────────────────────────────────────────────────────────────────────────
# TRIGGER THREAD 4 — Audio danger sound detection
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
# TRIGGER THREAD 5 — LoRa mesh relay
# ─────────────────────────────────────────────────────────────────────────────
def _on_lora_packet(packet: LoRaPacket) -> None:
    """
    Called when a LoRa packet arrives from another Kavach device.
    Relay logic:
      SOS/MEDICAL → re-broadcast with hop_count + 1  (max 3 hops)
      SAFE        → log only
      HEARTBEAT   → log only
    """
    if lora_manager is None:
        logger.warning("[LoRa] Packet received but lora_manager is None — ignoring.")
        return

    logger.info(
        "[LoRa] RX: type=%-8s from=%-15s hop=%d gps=%s",
        packet.packet_type, packet.device_id,
        packet.hop_count,   packet.gps_location
    )

    if packet.packet_type in ("SOS", "MEDICAL"):
        if packet.hop_count < 3:
            relay = LoRaPacket(
                packet_type  = packet.packet_type,
                device_id    = packet.device_id,
                trigger      = packet.trigger,
                gps_location = packet.gps_location,
                battery      = packet.battery,
                timestamp    = packet.timestamp,
                hop_count    = packet.hop_count + 1,
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
if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info(" Project Kavach — starting up")
    logger.info("=" * 60)

    # ── 1. Database ───────────────────────────────────────────────────────────
    # Engine is created once in alerts.py (_ENGINE); no need to duplicate here.
    db_path = os.path.join(BASE_DIR, 'alerts.db')
    logger.info("[Boot] Database ready at %s.", db_path)

    # ── 2. Config ─────────────────────────────────────────────────────────────
    config = load_config()
    validate_config(config)
    logger.info("[Boot] Config loaded. Device ID: %s", config.get('device_id'))

    # ── 3. Single shared SIM7600 instance ─────────────────────────────────────
    sim = SIM7600(port=config['serial_port'], baud=config['baud_rate'])
    logger.info("[Boot] SIM7600 initialized on %s.", config['serial_port'])

    # ── 4. Evidence recorders (camera + microphone) ──────────────────────────
    evidence_dir = os.path.join(BASE_DIR, config.get('evidence_dir', 'evidence'))
    os.makedirs(evidence_dir, exist_ok=True)

    camera_manager = CameraManager(evidence_dir=evidence_dir)
    logger.info("[Boot] %s", camera_manager.status_string())

    audio_recorder = AudioRecorderManager(evidence_dir=evidence_dir)
    logger.info("[Boot] %s", audio_recorder.status_string())

    # ── 5. State machine — inject the shared sim + evidence recorders ──────
    kavach = KavachStateMachine(sim=sim, cam=camera_manager, mic=audio_recorder)

    # ── 6. Sensor manager ─────────────────────────────────────────────────────
    sensor_manager = SensorManager()
    sensor_manager.start()
    logger.info("[Boot] %s", sensor_manager.status_string())

    # ── 7. Button handler ─────────────────────────────────────────────────────
    button = ButtonHandler(
        pin             = config['sos_button_pin'],
        on_sos_press    = _on_sos,
        on_medical_press= _on_medical,
        on_safe_press   = _on_safe,
    )
    button.start()
    logger.info("[Boot] Button handler started on pin %d.", config['sos_button_pin'])

    # ── 8. Audio detection ────────────────────────────────────────────────────
    audio_manager = AudioManager()
    audio_manager.start(on_detection=_on_audio_detection)
    logger.info("[Boot] %s", audio_manager.status_string())
    # Link audio manager to state machine so it pauses YAMNet during alerts
    kavach._audio_manager = audio_manager
    kavach._audio_cb = _on_audio_detection

    # ── 9. LoRa off-grid backup ───────────────────────────────────────────────
    lora_manager = LoRaManager()
    lora_manager.start(on_packet_received=_on_lora_packet)
    logger.info("[Boot] %s", lora_manager.status_string())

    # ── 10. Power monitor for heartbeat battery reporting ───────────────────
    from alerts import _init_power_monitor
    power_monitor = _init_power_monitor()
    if power_monitor:
        logger.info("[Boot] Power monitor initialised for heartbeat reporting.")
    else:
        logger.info("[Boot] No power monitor — heartbeat will report battery as N/A.")

    # ── 11. Config sync (polls server for app-pushed config changes) ─────────
    threading.Thread(
        target=_config_poll_loop,
        args=(config, power_monitor),
        name="ConfigSync",
        daemon=True
    ).start()
    logger.info("[Boot] Config sync thread started (polling every %ds).", CONFIG_POLL_INTERVAL)

    # ── 12. IMU + Heart rate monitor threads ──────────────────────────────────
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

    # ── 13. Keyboard demo (desktop only — skipped on Pi) ────────────────────
    try:
        import RPi.GPIO  # noqa: F401
        _on_pi = True
    except (ImportError, RuntimeError):
        _on_pi = False

    threading.Thread(
        target=_keyboard_listener,
        args=(sensor_manager, audio_manager),
        name="KeyboardDemo",
        daemon=True,
    ).start()
    logger.debug("[Boot] Keyboard listener started.")

    # ── 14. Ready ─────────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info(" Kavach ARMED — %s", kavach.status_line())
    logger.info(" Keys: s=SOS  d=MEDICAL  l=SAFE  f=fall  h=heart  a=audio  q=quit")
    logger.info("=" * 60)

    # ── 14. Main thread sleeps — all work is in daemon threads ────────────────
    # signal.pause() is Unix-only — use a cross-platform sleep loop instead
    # so the device firmware runs identically on Windows (dev) and Pi (prod).
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("\n[Boot] Shutdown requested.")
    finally:
        logger.info("[Boot] Shutting down all subsystems...")
        button.stop()
        sensor_manager.stop()
        audio_manager.stop()
        lora_manager.stop()
        camera_manager.shutdown()
        audio_recorder.shutdown()
        sim.close()   # cleanly release the serial port
        logger.info("[Boot] Kavach shutdown complete.")
        logger.info("=" * 60)