# Kavach — Personal Safety Device

Kavach is a Raspberry Pi-based personal safety device that detects emergencies (falls, heart rate spikes, danger sounds, button presses) and automatically calls the police/ambulance, sends SMS with GPS location to your guardian, and uploads evidence to a server.

## Architecture

The project has two parts that run on separate machines:

| Part | Folder | Runs on |
|------|--------|---------|
| **Device** (sensors + alerts) | `Personal-Safety-Device-main/` | Raspberry Pi |
| **Server** (stores alerts + evidence) | `Kavach-Server-main/` | Any PC (Windows/Linux) |

The device encrypts all data with **ChaCha20-Poly1305** before sending it to the server. Evidence files are verified with **SHA-256** hashes to prove they haven't been tampered with.

---

## How the Device Works

When you run `python main.py` on the Pi, it starts 6 subsystems simultaneously:

1. **Physical Button** (GPIO 23) — polled 50 times/second for press patterns
2. **IMU** (BNO055 via I2C) — checks for falls 10 times/second
3. **Heart Rate** (MAX30102 via I2C) — reads BPM every 5 seconds
4. **Microphone + AI** (YAMNet TFLite model) — listens for screaming, gunshots, explosions
5. **LoRa Radio** (SX1278 via SPI) — receives SOS from nearby Kavach devices
6. **Keyboard** — `f`, `h`, `a` keys to simulate sensors without hardware (for demos)

If any sensor hardware is not connected, the code **auto-detects** and falls back to a simulator that does nothing until triggered by keyboard.

### Triggers

| Trigger | How | Result |
|---------|-----|--------|
| Button **single press** (hold < 5s, release) | GPIO pin 23 | SOS |
| Button **double press** (two quick taps) | GPIO pin 23 | MEDICAL alert |
| Button **long press** (hold 5+ seconds) | GPIO pin 23 | SAFE — cancels active alert |
| Fall detected | IMU sensor or press `f` | SOS |
| Heart rate >= 140 BPM | Heart sensor or press `h` | SOS |
| Danger sound (screaming, gunshot, etc.) | Microphone or press `a` | SOS |
| LoRa packet received | Another Kavach device nearby | Mesh relay |

### SOS Sequence

```
Step 1 → CALL POLICE (rings 15 seconds, hangs up)
Step 2 → SMS to guardian: "SOS ALERT - Emergency triggered"
Step 3 → Get GPS → SMS: "Location: https://maps.google.com/?q=12.97,77.59"
Step 4 → LOOP every 60 seconds until cancelled:
           ├── Send updated GPS location SMS
           ├── Send battery percentage SMS
           ├── Upload new evidence files to server (encrypted)
           └── SMS guardian the download link
Step 5 → Long press button → SMS: "I AM SAFE, alert cancelled"
         → Device returns to IDLE
```

MEDICAL alert is the same but calls the medical number and sends "MEDICAL EMERGENCY" messages.

**Only one alert can run at a time.** If SOS is already active and another trigger fires, it is ignored.

---

## How the Server Works

The server is a Flask web app with these endpoints:

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `POST` | `/api/alerts` | Receive encrypted telemetry + evidence from device |
| `GET` | `/api/alerts` | List all alerts (for a dashboard) |
| `GET` | `/api/alerts/<id>` | Single alert detail with file integrity verification |
| `GET` | `/api/health` | Server + database health check |
| `GET` | `/uploads/<file>` | Serve evidence files (guardian clicks SMS link) |

When the device sends data:
1. Server receives the encrypted payload
2. Decrypts it using the shared ChaCha20 key
3. Saves evidence files to `uploads/`
4. Verifies SHA-256 hashes match what the device sent
5. Stores alert metadata in SQLite database (`kavach.db`)

---

## Setup Guide

### 1. Generate the Encryption Key (once)

Run this on either machine:

```bash
python -c "
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
import os, pathlib
pathlib.Path('keys').mkdir(exist_ok=True)
key = ChaCha20Poly1305.generate_key()
open('keys/chacha.key', 'wb').write(key)
print('Key saved to keys/chacha.key')
"
```

Copy the **exact same key file** to both machines:
```
Pi:      Personal-Safety-Device-main/keys/chacha.key
Server:  Kavach-Server-main/keys/chacha.key
```

Both must be identical — if they don't match, decryption will fail.

### 2. Install Dependencies

**On your Windows/Linux PC (server):**
```bash
cd Kavach-Server-main
pip install -r Requirements.txt
```

**On the Raspberry Pi (device):**
```bash
cd Personal-Safety-Device-main
pip install -r requirements.txt
```

Then install the TFLite model runner separately (depends on your Python version):
```bash
# Python 3.9–3.12:
pip install tflite-runtime

# Python 3.13+ (tflite-runtime not available):
pip install tensorflow
```

### 3. Find Your Server PC's IP Address

On the server machine:
- **Windows:** Open Command Prompt → run `ipconfig` → look for IPv4 Address (e.g. `192.168.1.50`)
- **Linux:** Run `ip addr` or `hostname -I`

Both machines must be on the same network.

### 4. Edit config.json (on the Pi)

Open `Personal-Safety-Device-main/config.json`:

```json
{
  "device_id": "KAVACH-001",
  "serial_port": "/dev/ttyUSB2",
  "baud_rate": 115200,
  "sos_button_pin": 23,
  "police_number": "100",
  "guardian_number": "+919876543210",
  "medical_number": "+919876543211",
  "server_url": "http://192.168.1.50:8080/api/alerts",
  "server_public_url": "http://192.168.1.50:8080/uploads/",
  "evidence_dir": "evidence"
}
```

Replace:
- `guardian_number` — your guardian's real phone number
- `medical_number` — a medical contact's number
- `192.168.1.50` — your server PC's actual IP address
- `serial_port` — check with `ls /dev/ttyUSB*` after plugging in the SIM7600

### 5. Download the Audio AI Model (on the Pi, once)

```bash
cd Personal-Safety-Device-main
python setup_audio.py
```

This downloads `yamnet.tflite` and `yamnet_class_map.csv` into `models/`.

### 6. Create the Evidence Folder (on the Pi)

```bash
mkdir -p Personal-Safety-Device-main/evidence
```

### 7. Wire the Hardware

```
Physical Button  → GPIO 23 (BCM) + GND
SIM7600 module   → USB (/dev/ttyUSB2)
BNO055 (IMU)     → I2C (SDA/SCL)
MAX30102 (Heart) → I2C (SDA/SCL)
INA219 (Battery) → I2C (SDA/SCL)
SX1278 (LoRa)    → SPI + GPIO pins
Microphone       → USB or 3.5mm (via sounddevice)
```

Any sensor not connected will be automatically replaced by a simulator — the device will not crash.

---

## Running the Project

### Start the Server (on your PC)

```bash
cd Kavach-Server-main
python app.py
```

Expected output:
```
Kavach Server v2.0 starting
Database: kavach.db
Upload dir: ...\uploads
POST /api/alerts        — receive telemetry
GET  /api/health        — server health
```

Verify: open `http://localhost:8080/api/health` in your browser. You should see `"status": "ok"`.

### Start the Device (on the Raspberry Pi)

```bash
cd Personal-Safety-Device-main
python main.py
```

Expected output:
```
Kavach ARMED — State=IDLE ActiveAlert=none

Keyboard shortcuts (sensors without hardware):
  f → Fall detected      h → Heart rate spike
  a → Audio danger       q → Quit
Button functions (SOS / Medical / Safe) → physical GPIO button only
```

---

## Presentation Demo Guide

| Step | Action | What Happens |
|------|--------|-------------|
| 1 | Start server on PC | Show health endpoint in browser |
| 2 | Start device on Pi | Show boot logs, all subsystems initializing |
| 3 | **Single press** the physical button | SOS: call police + SMS + GPS loop starts |
| 4 | **Long press** the button (5s) | "I AM SAFE" SMS sent, alert cancelled |
| 5 | Press `f` on keyboard | Fall detection triggers SOS |
| 6 | Press `h` on keyboard | Heart rate spike triggers SOS |
| 7 | Press `a` on keyboard | Audio danger (screaming) triggers SOS |
| 8 | **Double tap** the button quickly | MEDICAL alert: calls medical number |
| 9 | Open `http://<server-ip>:8080/api/alerts` | Show all alerts stored in database |
| 10 | Open `http://<server-ip>:8080/api/alerts/1` | Show SHA-256 hash verification |

Remember to cancel each alert with a **long press** before triggering the next one — only one alert can run at a time.

---

## Project Structure

```
Kavach/
├── Personal-Safety-Device-main/    ← Runs on Raspberry Pi
│   ├── main.py                     ← Entry point, state machine, keyboard handler
│   ├── alerts.py                   ← SOS, Medical, Safe sequences
│   ├── database.py                 ← SQLAlchemy models (Alert table)
│   ├── crypto_utils.py             ← ChaCha20-Poly1305 encryption
│   ├── config.json                 ← Phone numbers, server URL, device settings
│   ├── requirements.txt            ← Python dependencies
│   ├── setup_audio.py              ← Downloads YAMNet model files
│   ├── hardware/
│   │   ├── comms.py                ← SIM7600: calls, SMS, GPS, HTTP upload
│   │   ├── sensors.py              ← BNO055 (IMU/fall) + MAX30102 (heart rate)
│   │   ├── audio.py                ← YAMNet microphone listener
│   │   ├── button.py               ← GPIO button with single/double/long press
│   │   ├── lora.py                 ← SX1278 LoRa mesh radio
│   │   └── power.py                ← INA219 battery voltage monitor
│   ├── models/                     ← YAMNet TFLite model (after setup_audio.py)
│   ├── evidence/                   ← Evidence files to upload (photos, audio)
│   └── keys/
│       └── chacha.key              ← Shared encryption key
│
├── Kavach-Server-main/             ← Runs on server PC
│   ├── app.py                      ← Flask API (receive, store, serve alerts)
│   ├── database.py                 ← SQLAlchemy models (Alert table)
│   ├── crypto_utils.py             ← ChaCha20-Poly1305 decryption
│   ├── utils.py                    ← File saving + SHA-256 hashing
│   ├── Requirements.txt            ← Python dependencies
│   ├── uploads/                    ← Stored evidence files
│   └── keys/
│       └── chacha.key              ← Same shared encryption key
│
└── README.md                       ← This file
```

---

## Security Features

- **End-to-end encryption** — All data encrypted with ChaCha20-Poly1305 before transmission
- **Evidence integrity** — SHA-256 hashes verify files weren't tampered with in transit
- **Live re-verification** — Server re-computes hashes on demand when viewing an alert
- **No plaintext** — GPS coordinates, alert type, battery status are all encrypted in the payload
