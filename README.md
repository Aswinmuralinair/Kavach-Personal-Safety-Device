# Kavach — Personal Safety Device

Kavach is a Raspberry Pi-based personal safety device that detects emergencies (falls, heart rate spikes, danger sounds, button presses) and automatically calls the police/ambulance, sends SMS with GPS location to your guardian, and uploads encrypted evidence to a server.

## Architecture

The project has two parts that run on separate machines:

| Part | Folder | Runs on |
|------|--------|---------|
| **Device** (sensors + alerts) | `Personal-Safety-Device-main/` | Raspberry Pi |
| **Server** (stores alerts + evidence) | `Kavach-Server-main/` | Any PC (Windows/Linux) |

**All data is encrypted end-to-end:**
- Telemetry (GPS, battery, alert type) → ChaCha20-Poly1305 encrypted
- Evidence files (video clips, images) → ChaCha20-Poly1305 encrypted
- File integrity → SHA-256 hashes verified server-side after decryption

---

## How the Device Works

When you run `python main.py` on the Pi, it starts 8 subsystems simultaneously:

1. **Physical Button** (GPIO 23) — polled 50 times/second for press patterns
2. **IMU** (BNO055 via I2C) — checks for falls 10 times/second
3. **Heart Rate** (MAX30102 via I2C) — reads BPM every 5 seconds
4. **Microphone + AI** (YAMNet TFLite model) — listens for screaming, gunshots, explosions
5. **Pi Camera** (CSI interface via picamera2) — records 30-second H264 evidence clips during alerts
6. **Microphone Recorder** (sounddevice) — records 30-second WAV audio evidence during alerts
7. **LoRa Radio** (SX1278 via SPI) — receives SOS from nearby Kavach devices
8. **Keyboard** — `f`, `h`, `a` keys to simulate sensors without hardware (for demos)

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
Step 1 → START CAMERA + MICROPHONE RECORDING (30-sec video + audio clips to evidence/)
Step 2 → CALL POLICE (rings 15 seconds, hangs up)
Step 3 → SMS to guardian: "SOS ALERT - Emergency triggered"
Step 4 → WhatsApp alert: location + help message (via CallMeBot)
Step 5 → Get location (GPS first, cell tower fallback if GPS fails)
         → SMS: "Location: https://maps.google.com/?q=12.97,77.59"
Step 6 → LOOP every 60 seconds until cancelled:
           ├── Check battery (WhatsApp alert if < 15%, once per boot)
           ├── Retry any queued failed uploads
           ├── Get location (5 GPS attempts → cell tower fallback)
           ├── Send updated GPS/tower location SMS
           ├── Send battery percentage SMS
           ├── Encrypt + upload new evidence files to server
           └── SMS guardian the download link (queue if server unreachable)
Step 7 → Long press button → STOP CAMERA + MIC → SMS: "I AM SAFE, alert cancelled"
         → Device returns to IDLE
```

MEDICAL alert is the same but calls the medical number and sends "MEDICAL EMERGENCY" messages.

**Only one alert can run at a time.** If SOS is already active and another trigger fires, it is ignored.

### Location Strategy

The device uses a **GPS-first, cell-tower-fallback** approach:

1. **GPS** (via SIM7600 AT+CGPSINFO) — tries 5 times (~20 seconds). Accuracy: 2-10 metres.
2. **Cell Tower** (via Unwired Labs Cloud LBS API) — if GPS fails, reads cell tower IDs (MCC, MNC, LAC, CID) from the SIM7600 via `AT+CPSI?`, then sends them to the Unwired Labs API to get coordinates. Accuracy: 100-2000 metres. Requires `api_token` in config.json (free tier: 100 requests/day at [unwiredlabs.com](https://unwiredlabs.com)).

This repeats every 60-second cycle during an active alert — so if GPS becomes available later (e.g. user moves outdoors), it automatically switches back to GPS.

---

## How the Server Works

The server is a Flask web app with these endpoints:

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/` | Admin web dashboard (map + stats + alerts table) |
| `POST` | `/api/alerts` | Receive encrypted telemetry + encrypted evidence from device |
| `GET` | `/api/alerts` | List all alerts (for dashboard) |
| `GET` | `/api/alerts/<id>` | Single alert detail with file integrity verification |
| `GET` | `/api/health` | Server + database health check |
| `GET` | `/uploads/<file>` | Serve decrypted evidence files |
| `POST` | `/api/auth/login` | Get auth token for mobile app |
| `GET` | `/api/user/alerts` | User's alerts (token auth) |
| `GET` | `/api/guardian/alerts` | Guardian's SOS/MEDICAL alerts (token auth) |
| `GET` | `/api/user/locations` | Location history (token auth) |
| `GET` | `/api/guardian/evidence/<id>` | Evidence files for alert (token auth) |

When the device sends data:
1. Server receives the encrypted telemetry payload
2. Decrypts it using the shared ChaCha20 key
3. Receives encrypted evidence files
4. Decrypts evidence files using the shared ChaCha20 key
5. Verifies SHA-256 hashes match what the device sent (on the decrypted file)
6. Saves decrypted evidence files to `uploads/`
7. Stores alert metadata in SQLite database (`kavach.db`)

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

**On your Windows PC (server):**
```bash
cd Kavach-Server-main
pip install -r Requirements.txt
```

**On the Raspberry Pi (device):**
```bash
cd Personal-Safety-Device-main
pip install SQLAlchemy requests pyserial cryptography numpy sounddevice RPi.GPIO smbus2 spidev adafruit-circuitpython-bno055 adafruit-circuitpython-busdevice tensorflow
```

> **Note:** `picamera2` is pre-installed on Raspberry Pi OS. If missing: `sudo apt install python3-picamera2`

### 3. Find Your Windows PC's IP Address

On the server machine, open Command Prompt and run:
```
ipconfig
```

Look for your Wi-Fi or Ethernet adapter's **IPv4 Address** (e.g. `192.168.1.50`). Both machines must be on the same network.

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
  "evidence_dir": "evidence",
  "whatsapp_number": "+919876543210",
  "whatsapp_apikey": "YOUR_CALLMEBOT_APIKEY",
  "api_token": "YOUR_UNWIREDLABS_API_TOKEN"
}
```

Replace:
- `guardian_number` — your guardian's real phone number
- `medical_number` — a medical contact's number
- `192.168.1.50` — your server PC's actual IP address
- `serial_port` — check with `ls /dev/ttyUSB*` after plugging in the SIM7600
- `whatsapp_number` — your WhatsApp number with country code (for alerts)
- `whatsapp_apikey` — your CallMeBot API key (see WhatsApp setup in Features section)
- `api_token` — your Unwired Labs API token (sign up free at [unwiredlabs.com](https://unwiredlabs.com), get 100 requests/day)

### 5. Download the Audio AI Model (on the Pi, once)

```bash
cd Personal-Safety-Device-main
python setup_audio.py
```

This downloads `yamnet.tflite` and `yamnet_class_map.csv` into `models/`.

### 6. Connect the Pi Camera

Plug the Pi Camera Module into the CSI port on the Raspberry Pi. Verify it works:
```bash
libcamera-hello --timeout 5000
```

If you see a 5-second preview window, the camera is working. If not connected, the code falls back to `FakeCameraRecorder` (no crash, just no video evidence).

### 7. Wire the Hardware

```
Physical Button   → GPIO 23 (BCM) + GND
Pi Camera Module  → CSI port (ribbon cable)
SIM7600 module    → USB (/dev/ttyUSB2)
BNO055 (IMU)      → I2C (SDA/SCL)
MAX30102 (Heart)  → I2C (SDA/SCL)
INA219 (Battery)  → I2C (SDA/SCL)
SX1278 (LoRa)     → SPI + GPIO pins
Microphone        → USB or 3.5mm (via sounddevice)
```

Any sensor not connected will be automatically replaced by a simulator — the device will not crash.

---

## Running the Project

### Start the Server FIRST (on Windows PC)

```bash
cd Kavach-Server-main
python app.py
```

Expected output:
```
Kavach Server v2.0 starting
Database: kavach.db
Upload dir: ...\uploads
GET  /                  — admin dashboard
POST /api/alerts        — receive telemetry
GET  /api/alerts        — list all alerts
GET  /api/alerts/<id>   — alert detail + hash check
GET  /api/health        — server health
GET  /uploads/<file>    — serve evidence
POST /api/auth/login    — app auth token
GET  /api/user/alerts   — user alerts (app)
GET  /api/guardian/alerts — guardian alerts (app)
```

Verify: open `http://localhost:8080/` in your browser to see the admin dashboard, or `http://localhost:8080/api/health` for a health check.

### Start the Device (on the Raspberry Pi)

```bash
cd Personal-Safety-Device-main
python main.py
```

Expected output:
```
Kavach ARMED — State=IDLE ActiveAlert=none
Triggers active:
  Button single press  → SOS
  Button double press  → MEDICAL ALERT
  Button long press 5s → SAFE (cancel + notify)
  IMU fall detected    → SOS
  Heart rate spike     → SOS
  Audio danger sound   → SOS (YAMNet)
  Camera               → Video evidence recording during alerts
  Microphone           → Audio evidence recording during alerts
  LoRa RX              → Mesh relay

Keyboard shortcuts (sensors without hardware):
  f → Fall detected      h → Heart rate spike
  a → Audio danger       q → Quit
Button functions (SOS / Medical / Safe) → physical GPIO button only
```

---

## Presentation Demo Guide

| Step | Action | What Happens |
|------|--------|-------------|
| 1 | Start server on Windows PC | Show health endpoint in browser |
| 2 | Start device on Pi | Show boot logs, all subsystems initializing |
| 3 | **Single press** the physical button | SOS: camera starts, call police + SMS + GPS loop |
| 4 | Wait 60 seconds | Show evidence upload + GPS update cycle in logs |
| 5 | **Long press** the button (5s) | Camera stops, "I AM SAFE" SMS, alert cancelled |
| 6 | Press `f` on keyboard | Fall detection triggers SOS (camera + call + SMS) |
| 7 | **Long press** to cancel | |
| 8 | Press `h` on keyboard | Heart rate spike triggers SOS |
| 9 | **Long press** to cancel | |
| 10 | Press `a` on keyboard | Audio danger (screaming) triggers SOS |
| 11 | **Long press** to cancel | |
| 12 | **Double tap** the button quickly | MEDICAL alert: calls medical number |
| 13 | **Long press** to cancel | |
| 14 | Open `http://<server-ip>:8080/` | Show admin dashboard with map, stats, and alerts |
| 15 | Open `http://<server-ip>:8080/api/alerts/1` | Show SHA-256 hash verification of evidence |
| 16 | Open `http://<server-ip>:8080/uploads/<filename>` | Show actual evidence file (decrypted video/audio) |

**Remember:** Cancel each alert with a **long press** before triggering the next one — only one alert can run at a time.

---

## Project Structure

```
Kavach/
├── Personal-Safety-Device-main/    ← Runs on Raspberry Pi
│   ├── main.py                     ← Entry point, state machine, keyboard handler
│   ├── alerts.py                   ← SOS, Medical, Safe sequences
│   ├── database.py                 ← SQLAlchemy models (Alert table)
│   ├── crypto_utils.py             ← ChaCha20-Poly1305 encryption (text + file bytes)
│   ├── config.json                 ← Phone numbers, server URL, device settings
│   ├── requirements.txt            ← Python dependencies
│   ├── setup_audio.py              ← Downloads YAMNet model files
│   ├── hardware/
│   │   ├── comms.py                ← SIM7600: calls, SMS, GPS, cell tower, upload
│   │   ├── sensors.py              ← BNO055 (IMU/fall) + MAX30102 (heart rate)
│   │   ├── audio.py                ← YAMNet microphone listener
│   │   ├── button.py               ← GPIO button with single/double/long press
│   │   ├── camera.py               ← Pi Camera: 30-sec H264 clip recording
│   │   ├── audio_recorder.py       ← Microphone: 30-sec WAV clip recording
│   │   ├── whatsapp.py             ← CallMeBot WhatsApp API wrapper
│   │   ├── lora.py                 ← SX1278 LoRa mesh radio
│   │   └── power.py                ← INA219 battery voltage monitor
│   ├── models/                     ← YAMNet TFLite model (after setup_audio.py)
│   ├── evidence/                   ← Evidence files: video clips, photos
│   └── keys/
│       └── chacha.key              ← Shared encryption key
│
├── Kavach-Server-main/             ← Runs on server PC (Windows)
│   ├── app.py                      ← Flask API (receive, decrypt, store, serve, dashboard)
│   ├── database.py                 ← SQLAlchemy models (Alert table)
│   ├── crypto_utils.py             ← ChaCha20-Poly1305 decryption (text + file bytes)
│   ├── utils.py                    ← File saving, decryption, SHA-256 hashing
│   ├── Requirements.txt            ← Python dependencies
│   ├── templates/
│   │   └── dashboard.html          ← Admin web dashboard (Leaflet.js map + stats)
│   ├── uploads/                    ← Decrypted evidence files
│   └── keys/
│       └── chacha.key              ← Same shared encryption key
│
└── README.md                       ← This file
```

---

## New Features (v3.0)

### 1. Admin Web Dashboard
Open `http://<server-ip>:8080/` in your browser to see:
- **Stats bar** — total alerts, SOS count, medical count, active devices, evidence files
- **Live map** — Leaflet.js + OpenStreetMap with colored markers (red=SOS, purple=MEDICAL, cyan=other)
- **Recent alerts table** — top 10 alerts with clickable GPS and evidence links
- **Full alerts table** — all alerts with detailed info
- **Auto-refresh** every 30 seconds

### 2. App-ready API Endpoints
REST API endpoints ready for a future mobile app:

| Method | Endpoint | Auth | Purpose |
|--------|----------|------|---------|
| `POST` | `/api/auth/login` | None | Get auth token (device_id + role) |
| `GET` | `/api/user/alerts` | User token | All alerts for the user's device |
| `GET` | `/api/guardian/alerts` | Guardian token | SOS/MEDICAL alerts only |
| `GET` | `/api/user/locations` | User token | Location history |
| `GET` | `/api/guardian/evidence/<id>` | Guardian token | Evidence files for an alert |

Tokens are signed with itsdangerous (bundled with Flask) and expire after 24 hours.

### 3. Audio Evidence Recording
During SOS/MEDICAL alerts, the microphone records 30-second `.wav` clips alongside camera video:
- 16 kHz mono, 16-bit PCM
- Saved to `evidence/` folder
- Encrypted and uploaded with other evidence files
- Uses `sounddevice` library — falls back to `FakeAudioRecorder` if no mic detected

### 4. Offline Upload Queue
If the server is unreachable during an alert:
- Failed uploads are queued in memory
- Each 60-second update cycle retries queued uploads first
- Successfully retried uploads are removed from queue
- Queue resets on device restart (by design — no stale data)

### 5. Low Battery WhatsApp Alert
When battery drops below 15%:
- Sends a WhatsApp message to the configured number via CallMeBot API
- Sent **once per boot** to avoid spam (resets on restart)
- Skipped if WhatsApp is not configured

### 6. WhatsApp Alerts for SOS/MEDICAL
During SOS and MEDICAL alerts:
- Sends location + help message via WhatsApp (CallMeBot API)
- **Only sends location and help message** — no video/evidence on WhatsApp
- Gracefully skips if `whatsapp_number` or `whatsapp_apikey` are placeholder values

**WhatsApp Setup (one-time):**
1. Save `+34 644 51 95 23` in your phone as "CallMeBot"
2. Send "I allow callmebot to send me messages" on WhatsApp to that number
3. You'll receive an API key
4. Put the API key in `config.json` → `"whatsapp_apikey"`
5. Put your WhatsApp number in `config.json` → `"whatsapp_number"` (e.g. `"+919876543210"`)

---

## Security Features

- **End-to-end encryption** — All telemetry AND evidence files encrypted with ChaCha20-Poly1305
- **Evidence file encryption** — Video clips, audio clips, and images are encrypted before leaving the Pi
- **Evidence integrity** — SHA-256 hashes of original files sent alongside; server verifies after decryption
- **Live re-verification** — Server re-computes hashes on demand when viewing an alert detail
- **No plaintext in transit** — GPS coordinates, alert type, battery status, AND evidence files are all encrypted
- **Cell tower fallback** — Even without GPS, approximate location is obtained via cell tower triangulation
- **Persistent auth tokens** — Server SECRET_KEY saved to `.secret_key` file, survives restarts

---

## Bug Fixes (v3.1)

| # | Bug | Fix |
|---|-----|-----|
| 1 | Evidence upload grabbed ALL files in `evidence/`, including files from old alerts | Added `alert_start_time` filter — only uploads files created after the current alert started |
| 2 | Offline retry queue held detached SQLAlchemy objects after `session.close()` | Added `_AlertSnapshot` class — queue stores plain Python objects with copied attributes |
| 3 | Cell tower accuracy log had `\r\nOK` garbage from AT command response | Isolate CLBS data line (split on `\n`) before parsing fields |
| 4 | `has_internet()` downloaded full Google homepage (~100KB) on every check | Changed `requests.get` → `requests.head` (fetches headers only) |
| 5 | Server `SECRET_KEY` regenerated on every restart, invalidating all auth tokens | Key now persists to `.secret_key` file on first run, reloaded on subsequent starts |
