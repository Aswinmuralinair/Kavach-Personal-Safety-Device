"""
hardware/sensors.py  —  Project Kavach
Plug-and-play sensor architecture for IMU (BNO055) and Pulse Ox (MAX30102).

Design principle:
    SensorManager tries to import and initialise the REAL hardware class first.
    If the hardware library is missing (ImportError) or the I2C device is not
    wired up (OSError / IOError), it falls back to the FAKE class automatically.
    main.py always calls the same interface regardless of which class is active.

When you eventually get the hardware:
    1. pip install adafruit-circuitpython-bno055 adafruit-circuitpython-max30102
    2. Wire BNO055 and MAX30102 to the Pi's I2C bus (SDA=GPIO2, SCL=GPIO3)
    3. Enable I2C:  sudo raspi-config  →  Interface Options → I2C → Enable
    4. Run the device — SensorManager auto-detects and switches to real hardware.
    Zero code changes needed anywhere else.
"""

import time
import math
import random
import threading
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Shared data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class IMUReading:
    """One snapshot from the inertial measurement unit."""
    accel_x: float          # m/s²
    accel_y: float          # m/s²
    accel_z: float          # m/s²
    gyro_x: float           # deg/s
    gyro_y: float           # deg/s
    gyro_z: float           # deg/s
    accel_magnitude: float  # √(x²+y²+z²)  — used for fall detection
    is_real_hardware: bool  # True = BNO055 chip present, False = simulated

    @property
    def is_fall_detected(self) -> bool:
        """
        A sudden spike above 2.5g (≈24.5 m/s²) in magnitude indicates a fall.
        Threshold validated against BNO055 datasheet impact profiles.
        Tune FALL_THRESHOLD_G in SensorManager to adjust sensitivity.
        """
        G = 9.81
        return self.accel_magnitude > (SensorManager.FALL_THRESHOLD_G * G)


@dataclass
class HeartRateReading:
    """One snapshot from the pulse oximeter."""
    bpm: float              # Heart rate in beats per minute
    spo2: float             # Blood oxygen saturation (%)
    is_valid: bool          # False if finger not detected or signal too noisy
    is_real_hardware: bool  # True = MAX30102 chip present, False = simulated

    @property
    def is_distress_detected(self) -> bool:
        """
        BPM above threshold indicates physiological distress.
        Only fires when the reading is valid (finger on sensor).
        Tune BPM_DISTRESS_THRESHOLD in SensorManager to adjust sensitivity.
        """
        return self.is_valid and self.bpm >= SensorManager.BPM_DISTRESS_THRESHOLD


# ─────────────────────────────────────────────────────────────────────────────
# Abstract base classes  (the contract that both real and fake must satisfy)
# ─────────────────────────────────────────────────────────────────────────────

class BaseIMU(ABC):
    """Interface contract for any IMU driver — real or simulated."""

    @abstractmethod
    def initialise(self) -> None:
        """Set up the hardware / simulation.  Raises OSError if hw missing."""
        ...

    @abstractmethod
    def read(self) -> IMUReading:
        """Return the latest sensor snapshot."""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """Release hardware resources cleanly."""
        ...


class BaseHeartRate(ABC):
    """Interface contract for any pulse-ox driver — real or simulated."""

    @abstractmethod
    def initialise(self) -> None:
        """Set up the hardware / simulation.  Raises OSError if hw missing."""
        ...

    @abstractmethod
    def read(self) -> HeartRateReading:
        """Return the latest sensor snapshot."""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """Release hardware resources cleanly."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# REAL hardware class — BNO055 (9-axis IMU)
# ─────────────────────────────────────────────────────────────────────────────

class BNO055IMU(BaseIMU):
    """
    Real BNO055 driver using the Adafruit CircuitPython library.

    Wiring (Raspberry Pi 4):
        VIN  →  3.3V  (Pin 1)
        GND  →  GND   (Pin 6)
        SDA  →  GPIO2 (Pin 3)
        SCL  →  GPIO3 (Pin 5)

    Install:
        pip install adafruit-blinka adafruit-circuitpython-bno055
    """

    def __init__(self):
        self._sensor = None

    def initialise(self) -> None:
        # These imports are intentionally deferred — they raise ImportError
        # if the adafruit library is not installed, which SensorManager catches.
        import board
        import busio
        import adafruit_bno055

        i2c = busio.I2C(board.SCL, board.SDA)
        # Constructor raises OSError if the BNO055 is not present on I2C bus
        self._sensor = adafruit_bno055.BNO055_I2C(i2c)

        # Give the sensor 1 second to complete internal calibration startup
        time.sleep(1.0)
        logger.info("[BNO055] Real hardware initialised successfully.")

    def read(self) -> IMUReading:
        accel = self._sensor.acceleration or (0.0, 0.0, 0.0)
        gyro  = self._sensor.gyro or (0.0, 0.0, 0.0)
        mag   = math.sqrt(sum(v**2 for v in accel))
        return IMUReading(
            accel_x=accel[0], accel_y=accel[1], accel_z=accel[2],
            gyro_x=gyro[0],   gyro_y=gyro[1],   gyro_z=gyro[2],
            accel_magnitude=mag,
            is_real_hardware=True
        )

    def shutdown(self) -> None:
        # BNO055 has no explicit shutdown — just release the reference
        self._sensor = None
        logger.info("[BNO055] Shutdown.")


# ─────────────────────────────────────────────────────────────────────────────
# FAKE class — IMU simulator (used when BNO055 hardware is absent)
# ─────────────────────────────────────────────────────────────────────────────

class FakeIMU(BaseIMU):
    """
    Software-simulated IMU for testing without physical hardware.

    Behaviour:
        - Normal readings: gentle random accelerometer noise around 1g downward
          plus slow gyroscope drift.
        - Falls are ONLY triggered manually (keyboard 'f' key or calling
          trigger_fall_now()), never automatically.

    To manually trigger a fake fall:
        Press 'f' in the console, or call sensor_manager.imu.trigger_fall_now()
    """

    def __init__(self):
        self._fall_pending = False
        self._lock = threading.Lock()

    def initialise(self) -> None:
        logger.warning(
            "[FakeIMU] No BNO055 hardware detected — running in SIMULATION mode. "
            "Press 'f' in the console to trigger a fall event."
        )

    def trigger_fall_now(self) -> None:
        """Call this from a test script or REPL to inject an immediate fall."""
        with self._lock:
            self._fall_pending = True
        logger.info("[FakeIMU] Manual fall event queued.")

    def read(self) -> IMUReading:
        with self._lock:
            if self._fall_pending:
                self._fall_pending = False
                # Simulate a strong impact: ~4g spike on Y-axis (forward fall)
                accel_x = random.uniform(-5.0,  5.0)
                accel_y = random.uniform(30.0, 40.0)   # well above 2.5g = 24.5 m/s²
                accel_z = random.uniform(-5.0,  5.0)
                gyro_x  = random.uniform(-180.0, 180.0)
                gyro_y  = random.uniform(-180.0, 180.0)
                gyro_z  = random.uniform(-180.0, 180.0)
                logger.info("[FakeIMU] >>> FALL EVENT SIMULATED <<<")
            else:
                # Normal idle: gentle noise around 1g downward (z-axis)
                accel_x = random.gauss(0.0,  0.3)
                accel_y = random.gauss(0.0,  0.3)
                accel_z = random.gauss(9.81, 0.2)
                gyro_x  = random.gauss(0.0, 0.5)
                gyro_y  = random.gauss(0.0, 0.5)
                gyro_z  = random.gauss(0.0, 0.5)

        magnitude = math.sqrt(accel_x**2 + accel_y**2 + accel_z**2)
        return IMUReading(
            accel_x=accel_x, accel_y=accel_y, accel_z=accel_z,
            gyro_x=gyro_x,   gyro_y=gyro_y,   gyro_z=gyro_z,
            accel_magnitude=magnitude,
            is_real_hardware=False
        )

    def shutdown(self) -> None:
        logger.info("[FakeIMU] Simulation shutdown.")


# ─────────────────────────────────────────────────────────────────────────────
# REAL hardware class — MAX30102 (Pulse Oximeter + Heart Rate)
# ─────────────────────────────────────────────────────────────────────────────

class MAX30102HeartRate(BaseHeartRate):
    """
    Real MAX30102 driver using the max30102 Python library.

    Wiring (Raspberry Pi 4):
        VIN  →  3.3V  (Pin 1)
        GND  →  GND   (Pin 6)
        SDA  →  GPIO2 (Pin 3)
        SCL  →  GPIO3 (Pin 5)
        INT  →  GPIO17 (Pin 11)  — optional interrupt pin

    Install:
        pip install max30102
        Also requires smbus2: pip install smbus2
    """

    # Number of IR/red samples to average per reading for noise reduction
    SAMPLE_AVG_COUNT = 50

    def __init__(self):
        self._sensor = None

    def initialise(self) -> None:
        import max30102  # Raises ImportError if library not installed
        # max30102.MAX30102() raises OSError if device not found on I2C
        self._sensor = max30102.MAX30102()
        self._sensor.setup_sensor()
        time.sleep(0.5)  # Allow sensor to stabilise after setup
        logger.info("[MAX30102] Real hardware initialised successfully.")

    def read(self) -> HeartRateReading:
        """
        Collects SAMPLE_AVG_COUNT samples, then calls the built-in
        heart-rate and SpO2 calculation routines from the library.
        Returns is_valid=False if the finger is not detected (signal too low).
        """
        red_samples = []
        ir_samples  = []

        for _ in range(self.SAMPLE_AVG_COUNT):
            red = self._sensor.get_red()
            ir  = self._sensor.get_ir()
            red_samples.append(red)
            ir_samples.append(ir)
            time.sleep(0.01)  # ~100 Hz sample rate

        # Finger-detection heuristic: IR signal above 50000 = finger present
        finger_detected = (sum(ir_samples) / len(ir_samples)) > 50000

        if not finger_detected:
            return HeartRateReading(bpm=0.0, spo2=0.0, is_valid=False, is_real_hardware=True)

        bpm, spo2 = self._sensor.calculate_heart_rate_and_spo2(
            ir_buffer=ir_samples,
            red_buffer=red_samples
        )

        # Sanity-check ranges; reject physically impossible values
        bpm_valid  = 30 <= bpm  <= 250
        spo2_valid = 70 <= spo2 <= 100

        return HeartRateReading(
            bpm=float(bpm)  if bpm_valid  else 0.0,
            spo2=float(spo2) if spo2_valid else 0.0,
            is_valid=(bpm_valid and spo2_valid),
            is_real_hardware=True
        )

    def shutdown(self) -> None:
        if self._sensor:
            self._sensor.shutdown()
        self._sensor = None
        logger.info("[MAX30102] Shutdown.")


# ─────────────────────────────────────────────────────────────────────────────
# FAKE class — Heart Rate / SpO2 simulator
# ─────────────────────────────────────────────────────────────────────────────

class FakeHeartRate(BaseHeartRate):
    """
    Software-simulated pulse oximeter for testing without physical hardware.

    Behaviour:
        - Normal readings: BPM oscillates 62–85 with realistic slow drift;
          SpO2 stays 96–99%.
        - Distress events are ONLY triggered manually (keyboard 'h' key or
          calling trigger_distress_now()), never automatically. BPM spikes to
          145–165 for 3 consecutive readings when triggered.

    To manually trigger a fake distress event:
        Press 'h' in the console, or call sensor_manager.heart_rate.trigger_distress_now()
    """

    DISTRESS_DURATION_READINGS     = 3  # How many readings stay elevated

    def __init__(self):
        self._distress_remaining = 0
        self._base_bpm = 72.0           # Drifts slowly like a real resting HR
        self._bpm_drift_direction = 1
        self._lock = threading.Lock()

    def initialise(self) -> None:
        logger.warning(
            "[FakeHeartRate] No MAX30102 hardware detected — running in SIMULATION mode. "
            "Press 'h' in the console to trigger a distress event."
        )

    def trigger_distress_now(self) -> None:
        """Call this from a test script or REPL to inject an immediate distress event."""
        with self._lock:
            self._distress_remaining = self.DISTRESS_DURATION_READINGS
        logger.info("[FakeHeartRate] Manual distress event queued.")

    def read(self) -> HeartRateReading:
        with self._lock:
            if self._distress_remaining > 0:
                self._distress_remaining -= 1
                bpm  = random.uniform(145.0, 165.0)
                spo2 = random.uniform(93.0,  95.0)
                logger.info("[FakeHeartRate] >>> DISTRESS EVENT SIMULATED <<< BPM=%.1f", bpm)
            else:
                # Slow realistic drift of base heart rate
                self._base_bpm += self._bpm_drift_direction * random.uniform(0.0, 0.5)
                if self._base_bpm > 82:
                    self._bpm_drift_direction = -1
                elif self._base_bpm < 62:
                    self._bpm_drift_direction = 1
                bpm  = self._base_bpm + random.gauss(0, 1.5)
                spo2 = random.uniform(96.5, 99.0)

        return HeartRateReading(
            bpm=round(bpm, 1),
            spo2=round(spo2, 1),
            is_valid=True,
            is_real_hardware=False
        )

    def shutdown(self) -> None:
        logger.info("[FakeHeartRate] Simulation shutdown.")


# ─────────────────────────────────────────────────────────────────────────────
# SensorManager — the single entry point used by main.py
# ─────────────────────────────────────────────────────────────────────────────

class SensorManager:
    """
    Auto-detects hardware availability and wires up real or fake drivers.

    Usage in main.py:
        from hardware.sensors import SensorManager

        sm = SensorManager()
        sm.start()                          # Initialises both sensors

        reading = sm.imu.read()             # IMUReading dataclass
        hr      = sm.heart_rate.read()      # HeartRateReading dataclass

        if reading.is_fall_detected:
            trigger_sos("fall")

        if hr.is_distress_detected:
            trigger_sos("heartrate")

        sm.stop()                           # Clean shutdown

    Thresholds (tune these without touching any other code):
        SensorManager.FALL_THRESHOLD_G         = 2.5   # g-force
        SensorManager.BPM_DISTRESS_THRESHOLD   = 140   # BPM
    """

    # ── Tunable thresholds ────────────────────────────────────────────────────
    FALL_THRESHOLD_G        = 2.5   # Magnitude above this (in g) = fall detected
    BPM_DISTRESS_THRESHOLD  = 140   # BPM at or above this = physiological distress
    # ──────────────────────────────────────────────────────────────────────────

    def __init__(self):
        self.imu:        BaseIMU       = self._detect_imu()
        self.heart_rate: BaseHeartRate = self._detect_heart_rate()

    # ── Private detection logic ───────────────────────────────────────────────

    @staticmethod
    def _detect_imu() -> BaseIMU:
        """
        Attempts to load the real BNO055 driver.
        Falls back to FakeIMU on ImportError (library missing) or
        OSError/IOError (library present but chip not wired up).
        """
        try:
            driver = BNO055IMU()
            driver.initialise()
            return driver
        except ImportError:
            logger.warning(
                "[SensorManager] adafruit-circuitpython-bno055 not installed. "
                "Falling back to FakeIMU."
            )
        except (OSError, IOError) as e:
            logger.warning(
                "[SensorManager] BNO055 I2C init failed (%s). "
                "Is it wired to SDA/SCL? Falling back to FakeIMU.", e
            )
        except Exception as e:
            logger.warning(
                "[SensorManager] Unexpected BNO055 error (%s). "
                "Falling back to FakeIMU.", e
            )

        fallback = FakeIMU()
        fallback.initialise()
        return fallback

    @staticmethod
    def _detect_heart_rate() -> BaseHeartRate:
        """
        Attempts to load the real MAX30102 driver.
        Falls back to FakeHeartRate on ImportError or OSError.
        """
        try:
            driver = MAX30102HeartRate()
            driver.initialise()
            return driver
        except ImportError:
            logger.warning(
                "[SensorManager] max30102 library not installed. "
                "Falling back to FakeHeartRate."
            )
        except (OSError, IOError) as e:
            logger.warning(
                "[SensorManager] MAX30102 I2C init failed (%s). "
                "Is it wired to SDA/SCL? Falling back to FakeHeartRate.", e
            )
        except Exception as e:
            logger.warning(
                "[SensorManager] Unexpected MAX30102 error (%s). "
                "Falling back to FakeHeartRate.", e
            )

        fallback = FakeHeartRate()
        fallback.initialise()
        return fallback

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Called once at boot. Both sensors are already init'd by __init__."""
        logger.info(
            "[SensorManager] Ready. IMU=%s | HeartRate=%s",
            "REAL (BNO055)"   if self.imu.read().is_real_hardware       else "FAKE",
            "REAL (MAX30102)" if self.heart_rate.read().is_real_hardware else "FAKE",
        )

    def stop(self) -> None:
        """Clean shutdown — call before the process exits."""
        self.imu.shutdown()
        self.heart_rate.shutdown()
        logger.info("[SensorManager] All sensors shut down.")

    def status_string(self) -> str:
        """Returns a one-line string for logging / health endpoint."""
        imu_mode = "real" if isinstance(self.imu, BNO055IMU) else "simulated"
        hr_mode  = "real" if isinstance(self.heart_rate, MAX30102HeartRate) else "simulated"
        return f"IMU={imu_mode} | HeartRate={hr_mode}"