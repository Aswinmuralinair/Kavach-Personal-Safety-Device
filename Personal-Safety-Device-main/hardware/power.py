#!/usr/bin/env python3
"""
INA219 monitor for Waveshare UPS HAT B.
Provides battery voltage, current, and percentage.
Adapted from user's script.
"""

import time
try:
    import smbus2 as smbus
except ImportError:
    import smbus

# Registers
_REG_CONFIG = 0x00
_REG_BUSVOLTAGE = 0x02
_REG_CURRENT = 0x04
_REG_CALIBRATION = 0x05

# Defaults tuned to your hardware
I2C_BUS = 1
INA219_ADDRESS = 0x42          # Your HAT
SHUNT_OHM = 0.1

# --- FIX APPLIED ---
# The INA219's widest gain ($\pm$320mV) combined with the
# 0.1 Ohm shunt resistor gives a hard, physical
# measurement limit of 3.2A.
# We set this to 3.2A to make the calibration math
# match the hardware's reality for accurate readings.
MAX_EXPECTED_CURRENT_A = 3.2
# --------------------

SMOOTHING_ALPHA = 0.05
POLL_INTERVAL = 2.0
OBSERVED_MAX_PACK_VOLTAGE = 8.336
CUTOFF_PACK_VOLTAGE = 6.0

class INA219Simple:
    def __init__(self, i2c_bus=I2C_BUS, addr=INA219_ADDRESS,
                 shunt_ohm=SHUNT_OHM, max_current_A=MAX_EXPECTED_CURRENT_A,
                 alpha=SMOOTHING_ALPHA, retries=3, retry_delay=0.05):
        self.addr = addr
        self.retries = retries
        self.retry_delay = retry_delay
        self.shunt_ohm = float(shunt_ohm)
        self.max_current_A = float(max_current_A)
        self.alpha = float(alpha)

        try:
            self.bus = smbus.SMBus(i2c_bus)
        except FileNotFoundError:
            raise FileNotFoundError(f"I2C bus {i2c_bus} not found. Enable I2C in raspi-config.")

        self.current_lsb_A = self.max_current_A / 32768.0
        self.cal_value = int(0.04096 / (self.current_lsb_A * self.shunt_ohm))
        if not (1 <= self.cal_value <= 0xFFFF):
            raise ValueError("cal_value out of range; check shunt and max_current")

        self._configure()
        self._write_register(_REG_CALIBRATION, self.cal_value)

        try:
            self.smoothed_voltage = self._get_raw_bus_voltage_V()
        except Exception:
            self.smoothed_voltage = 0.0

    def _write_register(self, reg: int, value: int):
        data = [(value >> 8) & 0xFF, value & 0xFF]
        for _ in range(self.retries):
            try:
                self.bus.write_i2c_block_data(self.addr, reg, data)
                return
            except Exception:
                time.sleep(self.retry_delay)
        raise IOError("I2C write failed")

    def _read_register(self, reg: int) -> int:
        for _ in range(self.retries):
            try:
                data = self.bus.read_i2c_block_data(self.addr, reg, 2)
                return (data[0] << 8) | data[1]
            except Exception:
                time.sleep(self.retry_delay)
        raise IOError("I2C read failed")

    def _configure(self):
        BUS_VOLTAGE_RANGE_32V = 0x01
        PGA_GAIN_8 = 0x03  # This is the $\pm$320mV gain setting
        ADC_12BIT = 0x0D
        MODE_CONTINUOUS = 0x07
        config = (BUS_VOLTAGE_RANGE_32V << 13) | (PGA_GAIN_8 << 11) | (ADC_12BIT << 7) | (ADC_12BIT << 3) | MODE_CONTINUOUS
        self._write_register(_REG_CONFIG, config)

    def _get_raw_bus_voltage_V(self) -> float:
        raw = self._read_register(_REG_BUSVOLTAGE)
        return ((raw >> 3) * 0.004)  # 4 mV LSB

    def get_voltage_V(self) -> float:
        raw = self._get_raw_bus_voltage_V()
        self.smoothed_voltage = (self.alpha * raw) + ((1.0 - self.alpha) * self.smoothed_voltage)
        return self.smoothed_voltage

    def get_current_mA(self) -> float:
        raw = self._read_register(_REG_CURRENT)
        if raw & 0x8000:
            raw -= 1 << 16
        current_A = raw * self.current_lsb_A
        return current_A * 1000.0

def voltage_to_percentage(pack_v: float) -> float:
    """Simple piecewise mapping tuned to your observed full and cutoff voltages."""
    if pack_v >= OBSERVED_MAX_PACK_VOLTAGE:
        pct = 100.0
    elif pack_v >= 7.75:
        pct = 70.0 + ((pack_v - 7.75) / (OBSERVED_MAX_PACK_VOLTAGE - 7.75)) * 30.0
    elif pack_v >= 7.2:
        pct = 20.0 + ((pack_v - 7.2) / 0.55) * 50.0
    elif pack_v >= 6.8:
        pct = 10.0 + ((pack_v - 6.8) / 0.4) * 10.0
    elif pack_v >= CUTOFF_PACK_VOLTAGE:
        pct = ((pack_v - CUTOFF_PACK_VOLTAGE) / (6.8 - CUTOFF_PACK_VOLTAGE)) * 10.0
    else:
        pct = 0.0
    return max(0.0, min(100.0, round(pct, 1)))

if __name__ == "__main__":
    try:
        ina = INA219Simple()
        print("INA219 Sensor Found. Reading smoothed and calibrated data...")
        print("-" * 30)
        while True:
            try:
                v = ina.get_voltage_V()
                current_mA = ina.get_current_mA()
            except IOError:
                print("Error: I2C read failed.")
                time.sleep(1)
                continue

            pct = voltage_to_percentage(v)
            print(f"Load Voltage: {v:6.3f} V")
            print(f"Current:      {current_mA/1000:6.3f} A ({current_mA:6.0f} mA)")
            print(f"Battery %:    {pct:6.1f} %")
            print("-" * 30)

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\nProgram stopped by user.")
    except Exception as e:
        print(f"Fatal error: {e}")
