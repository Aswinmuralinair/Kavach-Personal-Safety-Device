"""
test_audio.py  —  Project Kavach
Runs on your ROG G14 (or any machine) to verify audio detection is working.

Usage:
    python test_audio.py

What it does:
    1. Starts the AudioManager (real YAMNet if model is present, fake otherwise)
    2. Prints every detection event to the terminal for 60 seconds
    3. Lets you test by making loud sounds near your laptop mic:
       - Shout "HELP" loudly
       - Clap sharply (simulates impact)
       - Snap fingers loudly near the mic
       - Play a gunshot/scream sound from YouTube nearby

No GPIO, no SIM7600, no Pi needed — 100% laptop safe.
"""

import logging
import time
import sys
import os

# Make sure we can import from the project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("test_audio")


def main():
    # Late import so path insertion above takes effect
    from hardware.audio import AudioManager, DetectionEvent

    trigger_count = 0

    def on_detection(event: DetectionEvent) -> None:
        nonlocal trigger_count
        trigger_count += 1

        status = "🚨 SOS TRIGGER" if event.should_trigger_sos else "   (logged only)"
        print(
            f"\n{'='*55}\n"
            f"  DETECTION #{trigger_count}\n"
            f"  Sound:      {event.sound_class}\n"
            f"  Category:   {event.category}\n"
            f"  Confidence: {event.confidence:.0%}\n"
            f"  Action:     {status}\n"
            f"{'='*55}\n"
        )

    print("\n" + "="*55)
    print("  Kavach Audio Detection Test")
    print("="*55)
    print("  Starting AudioManager...")

    audio = AudioManager()
    audio.start(on_detection=on_detection)
    print(f"  Status: {audio.status_string()}")

    # Import both concrete detectors only for explicit runtime reporting.
    # AudioManager still auto-selects REAL vs FAKE detector internally.
    from hardware.audio import YAMNetDetector, FakeAudioDetector

    if isinstance(audio.detector, FakeAudioDetector):
        print("\n  Running in SIMULATION mode (no model/mic found).")
        print("  Fake detections will fire automatically.\n")
        print("  Or trigger manually by pressing Enter:\n")
        sounds = ["screaming", "gunshot, gunfire", "smash, crash",
                  "explosion", "glass", "crying, sobbing"]
        idx = 0
        try:
            while True:
                input(f"  Press Enter to simulate '{sounds[idx % len(sounds)]}' "
                      f"(or Ctrl+C to quit): ")
                audio.simulate_detection(sounds[idx % len(sounds)])
                idx += 1
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    elif isinstance(audio.detector, YAMNetDetector):
        print("\n  Running with REAL YAMNet model + microphone.")
        print("  Make loud sounds near your mic to test:")
        print("    - Shout loudly")
        print("    - Clap sharply")
        print("    - Play a scream/gunshot sound nearby")
        print("\n  Press Ctrl+C to stop.\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        print("\n  Running with detector:", type(audio.detector).__name__)
        print("  Press Ctrl+C to stop.\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    audio.stop()
    print(f"\n  Test complete. Total detections: {trigger_count}")
    print("="*55)


if __name__ == "__main__":
    main()