# Kavach — First-Time Setup

Everything you need to get Kavach running from a fresh copy of this
repository. Follow the parts in order; each one says where to run it.

If you only want to see the system working and don't have the hardware, skip
to [Running without a Raspberry Pi](#running-without-a-raspberry-pi).

---

## What you are setting up

Kavach has three pieces that talk to each other:

| Piece | Runs on | Folder |
|---|---|---|
| **Device** | Raspberry Pi with the sensors | `Personal-Safety-Device-main/` |
| **Server** | Any Windows/Linux/Mac machine | `Kavach-Server-main/` |
| **App** | Android phone | `kavach_app/` |

The device detects an emergency, encrypts what it recorded, and uploads it to
the server. The server stores it and serves it to the app. The user and their
guardian each log into the app to see alerts, location, and evidence.

The Pi and the server do **not** need to be on the same network — an ngrok
tunnel gives the server a public address the Pi can reach from anywhere.

---

## Before you start

You need:

- **Python 3.9–3.12** on the server machine (3.13+ works for the server, but
  the Pi's audio model needs ≤ 3.12 — see [Known constraints](#known-constraints))
- **Flutter SDK** if you want to build the app yourself
- **An ngrok account** (free) for remote access
- **A Raspberry Pi** with the sensors, if you want the real device

---

## Part 1 — The encryption key (do this first, once)

The device and the server share one ChaCha20-Poly1305 key. Everything the
device uploads is encrypted with it. **Generate it once, then copy the same
file to both machines.**

Run this on either machine:

```bash
python -c "from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305; import pathlib; pathlib.Path('keys').mkdir(exist_ok=True); open('keys/chacha.key','wb').write(ChaCha20Poly1305.generate_key()); print('Key written to keys/chacha.key')"
```

Copy the resulting file to **both** of these paths:

```
Kavach-Server-main/keys/chacha.key
Personal-Safety-Device-main/keys/chacha.key
```

Confirm they are byte-for-byte identical:

```bash
sha256sum Kavach-Server-main/keys/chacha.key Personal-Safety-Device-main/keys/chacha.key
```

If the two hashes differ, every upload will fail to decrypt.

> `keys/` is gitignored on purpose. Never commit it. If you are handing this
> project to someone else, they generate their own key — you do not send yours.

---

## Part 2 — The server

### 2.1 Install and start

```bash
cd Kavach-Server-main
pip install -r Requirements.txt
python app.py
```

On Windows you can just double-click `start.bat`, which creates a virtual
environment, installs everything, and starts the server.

### 2.2 Save the credentials it prints

**The first time it starts**, the server generates its own credentials and
prints them once:

```
====================================================================
 KAVACH - FIRST-RUN CREDENTIALS (shown once, save them now)
====================================================================
  Dashboard username : kavach-admin
  Dashboard password : 0Vftyhj0E75rSsUL

  Device key         : YplR8Hc0Kc_6m8UGM20hBeb34vafz394
  Hand-off           : device config updated automatically (...config.json)
====================================================================
```

**Write down the dashboard password.** It is stored only as a hash, so this
is the one time you can read it.

The **device key** is handled for you: if `Personal-Safety-Device-main/` sits
beside `Kavach-Server-main/` (as it does in this repository), the server
writes the new key straight into the device's `config.json`. If your Pi is a
separate machine, the banner prints copy-paste instructions instead.

Nothing is printed on later restarts — the values are kept in
`.server_secrets.json`, which is gitignored.

### 2.3 Choose your own credentials instead (optional)

Set these before starting and the server will use them rather than
generating anything:

```bash
set KAVACH_ADMIN_USER=your-username
set KAVACH_ADMIN_PASS=your-strong-password
set KAVACH_DEVICE_KEY=your-device-key
```

(Use `export` instead of `set` on Linux/Mac.)

### 2.4 Set up ngrok

The Pi needs a public address for the server.

1. Install ngrok — `winget install ngrok.ngrok` on Windows, or from
   [ngrok.com/download](https://ngrok.com/download)
2. Sign up free at [ngrok.com](https://ngrok.com) and copy your authtoken
3. `ngrok config add-authtoken YOUR_TOKEN_HERE`
4. In the ngrok dashboard, go to **Domains → New Domain** and claim your free
   permanent domain, e.g. `your-name.ngrok-free.dev`
5. Open `Kavach-Server-main/app.py`, find `NGROK_DOMAIN` near the bottom, and
   set it to your domain

ngrok now starts automatically with the server.

> If the Pi and the server are on the same Wi-Fi you can skip ngrok entirely
> and use `http://<server-local-ip>:8080` in the device config.

### 2.5 Open the dashboard

Go to `https://your-name.ngrok-free.dev` (or `http://localhost:8080`) and log
in with the credentials from step 2.2.

---

## Part 3 — The device (Raspberry Pi)

### 3.1 Create the config

`config.json` is **not** in the repository, because it holds secrets. Start
from the template:

```bash
cd Personal-Safety-Device-main
cp config.example.json config.json
```

Then edit `config.json`:

| Field | What to put |
|---|---|
| `device_id` | A unique name for this device, e.g. `KAVACH-001` |
| `serial_port` | Where the SIM7600 appears, usually `/dev/ttyUSB3` |
| `sos_button_pin` | BCM pin for the SOS button (default 23) |
| `police_number` | Emergency number to call — `100` in India |
| `guardian_number` | The guardian's phone number, with country code |
| `medical_number` | Ambulance number — `108` in India |
| `server_url` | `https://your-name.ngrok-free.dev/api/alerts` |
| `server_public_url` | `https://your-name.ngrok-free.dev/uploads/` |
| `api_token` | Your Unwired Labs token (see 3.2) — optional |
| `whatsapp_apikey` | Your CallMeBot key — optional |
| `device_key` | Leave as-is if the server auto-synced it; otherwise paste the key from the server banner |

> **Never commit `config.json`.** It is gitignored. If you need to share the
> shape of it with someone, share `config.example.json`.

### 3.2 Cell-tower location fallback (optional)

If GPS cannot get a fix indoors, the device falls back to cell-tower
positioning through Unwired Labs.

1. Sign up at [unwiredlabs.com](https://unwiredlabs.com) (free tier available)
2. Copy your API token — it starts with `pk.`
3. Put it in `config.json` as `api_token`

Leave it as `YOUR_UNWIREDLABS_TOKEN` to skip this; GPS still works, and the
device logs that the fallback is unavailable.

### 3.3 Install dependencies

```bash
cd Personal-Safety-Device-main
pip install -r requirements.txt
sudo apt-get install libportaudio2 portaudio19-dev
```

Then install the audio model runtime for your Python version:

```bash
# Python 3.9–3.12
pip install tflite-runtime

# Python 3.13+
pip install tensorflow
```

### 3.4 Download the audio model

```bash
python setup_audio.py
```

This fetches the YAMNet model into `models/`. Without it, sound-based triggers
are disabled but everything else still runs.

### 3.5 Start the device

```bash
python main.py
```

Watch the console. Within about ten seconds you should see the pairing code:

```
  ┌──────────────────────────────────────────────┐
   Pairing code for KAVACH-001
            K7RQ2MXP
   Enter this in the Kavach app when signing up.
  └──────────────────────────────────────────────┘
```

**Write this down — you need it in Part 4.**

If you instead see repeated `401` responses, the device key does not match the
server's. Re-copy it from the server's `.server_secrets.json`.

---

## Part 4 — The app

### 4.1 Point it at your server

Open `kavach_app/lib/services/api_service.dart` and set:

```dart
static const String baseUrl = 'https://your-name.ngrok-free.dev';
```

### 4.2 Build and install

```bash
cd kavach_app
flutter pub get
flutter run          # phone connected over USB
```

To produce an installable APK:

```bash
flutter build apk --release
```

The APK lands in `build/app/outputs/flutter-apk/app-release.apk`.

> The release build is currently signed with Flutter's debug key. That is fine
> for testing and demos, but you must add a real signing config before putting
> it on the Play Store.

### 4.3 Create the accounts

Each device supports two accounts: one **user** (the person carrying it) and
one **guardian** (the person watching over them).

In the app:

1. Tap **Sign Up**
2. Enter the **Device ID** — the same one from `config.json`, e.g. `KAVACH-001`
3. Pick your role — **Device User** or **Guardian**
4. Enter the **Pairing Code** from Part 3.5
5. Choose a password of at least 8 characters
6. Tap **Create Account**

Repeat on the guardian's phone with the same device ID and the same pairing
code, choosing the Guardian role.

**Where to find the pairing code:**

- The device console prints it (Part 3.5)
- The server console prints all known codes at startup
- The admin dashboard shows it on each device card — click to copy

**Why this exists:** without a pairing code, anyone who guessed a device ID
could register as its user and then change the numbers it calls in an
emergency. The code is the proof that you actually have access to the device.

To revoke the ability to add new accounts, click **regenerate** on the
dashboard, or `POST /api/admin/pairing-code/<device_id>`. Existing accounts
keep working.

---

## Running without a Raspberry Pi

You can demonstrate the whole system on one laptop. The device code detects
missing hardware and substitutes simulators for the IMU, heart-rate sensor,
camera, and microphone.

1. Do Part 1 (encryption key) and Part 2 (server)
2. In `Personal-Safety-Device-main/`, copy the config template and set
   `server_url` to `http://localhost:8080/api/alerts`
3. Run `python main.py` — it will report which sensors fell back to simulation
4. Use the keyboard triggers in that console:

| Key | Simulates |
|---|---|
| `s` | SOS button press |
| `d` | Medical (double press) |
| `f` | A fall detected by the IMU |
| `h` | A heart-rate spike |
| `a` | A danger sound |
| `l` | Long press to cancel |
| `q` | Quit |

The alert flows to the server and shows up on the dashboard and in the app
exactly as a real one would.

---

## Verifying it all works

Run through this after setup. Each step should behave as described.

1. **Server is up** — open `https://your-name.ngrok-free.dev/api/health`, you
   should get `{"status": "ok", ...}`
2. **Dashboard works** — log in at the root URL; you should see the device
   card with its pairing code
3. **Device is talking** — the device card shows **online** with a battery
   percentage, refreshing every 10 seconds
4. **An alert lands** — press `s` in the device console; within seconds a new
   SOS appears on the dashboard and in both apps
5. **Evidence arrives** — the alert should list its recorded clips, and they
   should open when clicked
6. **Contacts sync** — change a number in the app's Settings (it asks for your
   password), then watch the device console log the change within 10 seconds

---

## Known constraints

Things that are deliberately incomplete. Worth knowing before you demo.

**Push notifications are not implemented.** The app polls the server every few
seconds while it is open. If the app is backgrounded or closed, no alert
reaches the phone. The server side of Firebase Cloud Messaging is written and
ready, but the app has no Firebase client. To enable it, see
[Enabling push notifications](#enabling-push-notifications) below.

**The release APK uses a debug signing key.** Fine for sideloading; not
publishable.

**The server runs on Flask's development server.** Fine for a demo behind
ngrok. For anything real, put it behind `waitress` (Windows) or `gunicorn`.

**The evidence ledger detects corruption, not tampering.** It verifies its own
hash chain, so anyone who can edit the ledger file can also recompute it. See
`SECURITY.md`.

**The forward-secrecy ratchet is not in the live path.** `ratchet.py` is
exercised by `forgery_experiment.py` only. Actual uploads use one static
shared key.

---

## Enabling push notifications

Not required to run the project. When you want real background alerts:

1. Create a project at [console.firebase.google.com](https://console.firebase.google.com)
2. Add an Android app with package name `com.kavach.kavach_app`
3. Download `google-services.json` into `kavach_app/android/app/`
4. Add to `kavach_app/pubspec.yaml`:
   ```yaml
   firebase_core: ^3.6.0
   firebase_messaging: ^15.1.3
   ```
5. Add the Google Services plugin to `kavach_app/android/app/build.gradle.kts`
6. On login, call `FirebaseMessaging.instance.getToken()` and send it to
   `PUT /api/auth/fcm-token` — `ApiService` already has the auth headers
7. On the server: uncomment `firebase-admin>=6.0` in `Requirements.txt`,
   `pip install -r Requirements.txt`, generate a service-account key in the
   Firebase console, and set `FIREBASE_CREDENTIALS` to its path

The server already calls `notify_device_alerts()` on every alert, so once a
token is registered the notifications flow with no further server changes.

> **Do not commit `google-services.json` or the service-account key.** Both are
> covered by `.gitignore`.

---

## Troubleshooting

**`InvalidTag` or "decryption failed" on every upload**
The two `chacha.key` files differ. Regenerate one and copy it to both sides.

**Device logs `401` when polling for config**
The `device_key` in the device's `config.json` does not match the server's.
Read the correct value from `Kavach-Server-main/.server_secrets.json` and paste
it in.

**"Invalid device ID or pairing code" when signing up**
Either the device has never checked in with the server (start the device
first, or read the code from the dashboard), or the code was regenerated.
Get the current code from the dashboard.

**"Too many failed attempts. Try again in N seconds."**
Rate limiting after ten failed logins. Only failures count, and one success
clears it, so just wait it out and enter the right password.

**App shows "Cannot connect to server"**
Check `baseUrl` in `api_service.dart` matches your ngrok domain, and that the
server and ngrok tunnel are both running.

**Dashboard is empty but the device says it uploaded**
Check the server console for the decryption step. If the key matches, confirm
you are logged in as the admin — app tokens only ever see their own device.

**`tflite-runtime` will not install**
You are on Python 3.13+. Use `pip install tensorflow` instead, or drop to
Python 3.12.

**Camera or microphone not found**
Expected off a Pi. The device substitutes simulators and logs which ones.

---

## What not to commit

These are all in `.gitignore`. Check before every push:

```
keys/                       the shared encryption key
config.json                 device secrets and phone numbers
.server_secrets.json        generated admin password and device key
.secret_key                 token signing key
pairing_codes.json          device pairing codes
app_users.json              account password hashes
fcm_tokens.json             push notification tokens
uploads/, evidence/         recorded evidence
*.db                        the alert database
google-services.json        Firebase config
```

If you ever do commit a secret, removing it in a later commit is not enough —
it stays in the history. Rotate the secret, then rewrite the history with
`git filter-repo`.
