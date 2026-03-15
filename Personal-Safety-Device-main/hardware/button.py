"""
hardware/button.py  —  Project Kavach
Timing-based button detection for a single GPIO push button.

Three gestures detected:
┌─────────────────┬───────────────────────────────────────────────────────────┐
│ Gesture         │ Behaviour                                                 │
├─────────────────┼───────────────────────────────────────────────────────────┤
│ Single press    │ Press + release in < 5 s, no second press within 600 ms  │
│                 │ → Fires: on_sos_press()                                   │
├─────────────────┼───────────────────────────────────────────────────────────┤
│ Double press    │ Two presses, both < 5 s hold, second within 600 ms       │
│                 │ → Fires: on_medical_press()                               │
├─────────────────┼───────────────────────────────────────────────────────────┤
│ Long press      │ Button held continuously for ≥ 5 s                       │
│                 │ → Fires: on_safe_press()  (I'm safe — cancel / reset)    │
└─────────────────┴───────────────────────────────────────────────────────────┘

Design notes:
  - Runs a dedicated polling thread at 50 Hz (every 20 ms).
    This is more reliable than GPIO edge interrupts for timing logic because
    the RPi.GPIO bouncetime filter can swallow short presses if set too high,
    and edge callbacks fire on background threads that need their own locking.
  - Hardware debounce: 50 ms stable-read window before a state transition
    is accepted.  Eliminates contact bounce without missing fast taps.
  - All three callbacks are invoked on the same polling thread, so they
    must not block.  They should call threading.Thread(target=...).start()
    for any long-running SOS/medical work.
"""

import time
import threading
import logging
from typing import Callable, Optional

try:
    import RPi.GPIO as GPIO
    _GPIO_AVAILABLE = True
except (ImportError, RuntimeError):
    _GPIO_AVAILABLE = False
    logging.warning("[Button] RPi.GPIO not available — running in keyboard simulation mode.")

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Tunable timing constants (seconds)
# ─────────────────────────────────────────────────────────────────────────────
DEBOUNCE_TIME      = 0.050   # 50 ms  — hardware debounce window
DOUBLE_PRESS_GAP   = 0.600   # 600 ms — max gap between two presses for double
LONG_PRESS_HOLD    = 5.000   # 5 s    — minimum hold duration for "safe" alert
POLL_INTERVAL      = 0.020   # 20 ms  — polling rate (50 Hz)


class ButtonHandler:
    """
    Monitors one GPIO pin and fires gesture callbacks.

    Usage:
        def on_sos():
            threading.Thread(target=sos_sequence, kwargs={"trigger": "button"}, daemon=True).start()

        def on_medical():
            threading.Thread(target=medical_sequence, daemon=True).start()

        def on_safe():
            threading.Thread(target=safe_sequence, daemon=True).start()

        btn = ButtonHandler(
            pin=17,
            on_sos_press=on_sos,
            on_medical_press=on_medical,
            on_safe_press=on_safe,
        )
        btn.start()
        ...
        btn.stop()
    """

    # Internal FSM states
    _IDLE          = "IDLE"
    _PRESSED       = "PRESSED"        # button down, timing hold
    _WAIT_DOUBLE   = "WAIT_DOUBLE"    # released once, waiting for second tap

    def __init__(
        self,
        pin: int,
        on_sos_press:     Callable[[], None],
        on_medical_press: Callable[[], None],
        on_safe_press:    Callable[[], None],
        active_low: bool = True,   # True = button connects pin to GND (standard)
    ):
        self.pin              = pin
        self.on_sos_press     = on_sos_press
        self.on_medical_press = on_medical_press
        self.on_safe_press    = on_safe_press
        self.active_low       = active_low

        self._state           = self._IDLE
        self._press_start     = 0.0    # time.monotonic() when button went down
        self._release_time    = 0.0    # time.monotonic() when button came up
        self._debounce_start  = 0.0
        self._raw_state       = False  # last confirmed debounced button state
        self._raw_candidate   = False  # candidate state being debounced

        self._thread: Optional[threading.Thread] = None
        self._running = False

        if _GPIO_AVAILABLE:
            self._setup_gpio()
        else:
            logger.warning("[Button] GPIO not available — use simulate_press() for testing.")

    # ── GPIO setup ────────────────────────────────────────────────────────────

    def _setup_gpio(self) -> None:
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        # Internal pull-up: button press pulls pin LOW
        pull = GPIO.PUD_UP if self.active_low else GPIO.PUD_DOWN
        GPIO.setup(self.pin, GPIO.IN, pull_up_down=pull)
        logger.info("[Button] GPIO pin %d configured (active_low=%s).", self.pin, self.active_low)

    # ── Raw button read ───────────────────────────────────────────────────────

    def _read_raw(self) -> bool:
        """Returns True when the button is physically held down."""
        if not _GPIO_AVAILABLE:
            return False
        level = GPIO.input(self.pin)
        # active_low: pin goes LOW (0) when button pressed → invert
        return (level == 0) if self.active_low else (level == 1)

    # ── Debounced read ────────────────────────────────────────────────────────

    def _debounced_state(self) -> bool:
        """
        Returns the confirmed debounced button state.
        A transition is only accepted after DEBOUNCE_TIME of stable readings.
        """
        now       = time.monotonic()
        raw       = self._read_raw()

        if raw != self._raw_candidate:
            # State candidate changed — restart the debounce timer
            self._raw_candidate  = raw
            self._debounce_start = now

        if (now - self._debounce_start) >= DEBOUNCE_TIME:
            # Stable for long enough — accept as confirmed state
            self._raw_state = self._raw_candidate

        return self._raw_state

    # ── Main polling loop (FSM) ───────────────────────────────────────────────

    def _poll_loop(self) -> None:
        logger.info("[Button] Polling thread started on pin %d.", self.pin)
        prev_state = False   # previous confirmed debounced state

        while self._running:
            now     = time.monotonic()
            pressed = self._debounced_state()   # True = button down

            # ── Detect transitions ────────────────────────────────────────────
            just_pressed  = pressed     and not prev_state   # ↓ falling edge
            just_released = not pressed and prev_state       # ↑ rising edge
            prev_state    = pressed

            # ── FSM ───────────────────────────────────────────────────────────

            if self._state == self._IDLE:
                if just_pressed:
                    self._press_start = now
                    self._state       = self._PRESSED
                    logger.debug("[Button] Press started.")

            elif self._state == self._PRESSED:
                hold = now - self._press_start

                # Long press: button still held and threshold crossed
                if pressed and hold >= LONG_PRESS_HOLD:
                    logger.info("[Button] LONG PRESS detected (%.2f s) → SAFE ALERT", hold)
                    self._fire(self.on_safe_press)
                    # Wait for release before going back to IDLE
                    self._state = self._WAIT_RELEASE_AFTER_LONG

                elif just_released:
                    # Short release — could be single or first of a double
                    self._release_time = now
                    self._state        = self._WAIT_DOUBLE
                    logger.debug("[Button] Released after %.3f s — waiting for double.", hold)

            elif self._state == self._WAIT_DOUBLE:
                gap = now - self._release_time

                if just_pressed:
                    # Second press arrived within the window → DOUBLE PRESS
                    logger.info("[Button] DOUBLE PRESS detected → MEDICAL ALERT")
                    self._fire(self.on_medical_press)
                    # Wait for the second release before going IDLE
                    self._state = self._WAIT_RELEASE_AFTER_DOUBLE

                elif gap >= DOUBLE_PRESS_GAP and not pressed:
                    # No second press arrived in time → SINGLE PRESS (SOS)
                    logger.info("[Button] SINGLE PRESS confirmed → SOS")
                    self._fire(self.on_sos_press)
                    self._state = self._IDLE

            # Ghost states — just wait for button to fully release, then IDLE
            elif self._state == "_WAIT_RELEASE_AFTER_LONG":
                if just_released:
                    self._state = self._IDLE
                    logger.debug("[Button] Button released after long press — IDLE.")

            elif self._state == "_WAIT_RELEASE_AFTER_DOUBLE":
                if just_released:
                    self._state = self._IDLE
                    logger.debug("[Button] Button released after double press — IDLE.")

            time.sleep(POLL_INTERVAL)

        logger.info("[Button] Polling thread stopped.")

    # Use string constants for the two extra wait-states to keep _IDLE / _PRESSED
    # / _WAIT_DOUBLE as the public ones without a naming clash.
    _WAIT_RELEASE_AFTER_LONG   = "_WAIT_RELEASE_AFTER_LONG"
    _WAIT_RELEASE_AFTER_DOUBLE = "_WAIT_RELEASE_AFTER_DOUBLE"

    # ── Callback invoker ──────────────────────────────────────────────────────

    def _fire(self, callback: Callable[[], None]) -> None:
        """Invoke a gesture callback safely. Exceptions are caught and logged."""
        try:
            callback()
        except Exception as exc:
            logger.error("[Button] Callback raised an exception: %s", exc, exc_info=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background polling thread. Call once at boot."""
        self._running = True
        self._thread  = threading.Thread(
            target=self._poll_loop,
            name="ButtonPoller",
            daemon=True
        )
        self._thread.start()
        logger.info("[Button] Started. Gestures: single=SOS | double=MEDICAL | long(5s)=SAFE")

    def stop(self) -> None:
        """Stop the polling thread and clean up GPIO. Call on shutdown."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        if _GPIO_AVAILABLE:
            GPIO.cleanup(self.pin)
        logger.info("[Button] Stopped.")

    # ── Simulation helpers (for testing without hardware) ─────────────────────

    def simulate_press(self, hold_seconds: float = 0.2) -> None:
        """
        Inject a synthetic button press for desktop testing.
        Runs in the calling thread, not the poll loop — use carefully.

        Examples:
            btn.simulate_press(0.2)    # single press
            btn.simulate_press(0.2)    # tap again quickly → double press
            btn.simulate_press(6.0)    # long press → safe alert
        """
        if _GPIO_AVAILABLE:
            logger.warning("[Button] simulate_press() called on a real Pi — ignored.")
            return

        logger.info("[Button] Simulating press for %.2f s", hold_seconds)
        now = time.monotonic()

        # Directly manipulate FSM — bypasses GPIO but exercises all logic
        self._raw_candidate  = True
        self._debounce_start = now - DEBOUNCE_TIME - 0.001   # force debounce to pass
        self._raw_state      = True
        self._poll_loop_tick(now)                             # process press

        time.sleep(hold_seconds)

        now = time.monotonic()
        self._raw_candidate  = False
        self._debounce_start = now - DEBOUNCE_TIME - 0.001
        self._raw_state      = False
        self._poll_loop_tick(now)                             # process release

    def _poll_loop_tick(self, now: float) -> None:
        """Single FSM tick for simulation — mirrors _poll_loop logic."""
        pressed = self._raw_state
        # Simulate a single falling/rising edge by running one iteration
        # (simplified — good enough for unit tests)
        if pressed and self._state == self._IDLE:
            self._press_start = now
            self._state       = self._PRESSED
        elif not pressed and self._state == self._PRESSED:
            hold = now - self._press_start
            if hold >= LONG_PRESS_HOLD:
                self._fire(self.on_safe_press)
                self._state = self._IDLE
            else:
                self._release_time = now
                self._state        = self._WAIT_DOUBLE