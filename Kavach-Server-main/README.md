# Kavach Server

The Flask service that receives encrypted alerts from the device, stores the
evidence, and serves both the admin dashboard and the mobile app.

New here? Start with [`../SETUP.md`](../SETUP.md) — it walks through the whole
system in order. This file is the server-specific reference.

---

## Quick start

```bash
pip install -r Requirements.txt
python app.py
```

On Windows, `start.bat` does the same and creates a virtual environment first.

**On the very first run the server prints credentials once. Save them.**

```
====================================================================
 KAVACH - FIRST-RUN CREDENTIALS (shown once, save them now)
====================================================================
  Dashboard username : kavach-admin
  Dashboard password : 0Vftyhj0E75rSsUL

  Device key         : YplR8Hc0Kc_6m8UGM20hBeb34vafz394
  Hand-off           : device config updated automatically (...)
====================================================================
```

The dashboard password is stored only as a hash, so this is the one chance to
read it. The device key is written into `../Personal-Safety-Device-main/config.json`
automatically when that folder is present.

Prefer your own values? Set them before starting:

```bash
set KAVACH_ADMIN_USER=your-username
set KAVACH_ADMIN_PASS=your-strong-password
set KAVACH_DEVICE_KEY=your-device-key
set KAVACH_SECRET_KEY=your-token-signing-key
```

---

## Before it can decrypt anything

Put the shared ChaCha20 key at `keys/chacha.key`, byte-identical to the one on
the device. See [`../SETUP.md`](../SETUP.md) Part 1. Without it every upload
fails with `InvalidTag`.

---

## Pairing codes

Creating an app account requires the device's pairing code. Find it:

- printed in this server's console at startup
- on the dashboard, on each device card (click to copy)
- printed by the Raspberry Pi when it polls for config

A code is created the first time a device checks in. Regenerate one to stop
new accounts being created; existing accounts are unaffected.

```bash
curl -X POST https://your-domain/api/admin/pairing-code/KAVACH-001 \
     --cookie "session=<admin session cookie>"
```

---

## Files

| File | Purpose |
|---|---|
| `app.py` | Routes, auth, alert ingestion, ngrok bootstrap |
| `database.py` | SQLAlchemy models — `Alert`, `Evidence` |
| `crypto_utils.py` | ChaCha20-Poly1305 encrypt/decrypt helpers |
| `utils.py` | Safe file saving, SHA-256, evidence decryption |
| `evidence.py` | Evidence chain ledger |
| `notifications.py` | Firebase Cloud Messaging (stub unless configured) |
| `secrets_manager.py` | Generates and persists server credentials |
| `pairing.py` | Device pairing codes |
| `ratelimit.py` | In-memory failed-attempt throttling |
| `blueprints/evidence_bp.py` | Evidence ledger API |

Generated at runtime, all gitignored: `kavach.db`, `uploads/`,
`device_configs/`, `.secret_key`, `.server_secrets.json`, `app_users.json`,
`pairing_codes.json`, `fcm_tokens.json`, `evidence_ledger.json`.

---

## Endpoints

### Public

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/alerts` | Device telemetry. Authenticated by ChaCha20 decryption succeeding. |
| `GET` | `/api/health` | Liveness only. Detail requires an admin session. |
| `GET/POST` | `/login` | Dashboard login. Rate limited. |

### App (Bearer token)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/auth/signup` | Needs `pairing_code`. Rate limited. |
| `POST` | `/api/auth/login` | Rate limited. |
| `PUT` | `/api/auth/fcm-token` | Register a push token. |
| `GET` | `/api/user/alerts` | User role. |
| `GET` | `/api/guardian/alerts` | Guardian role. |
| `GET` | `/api/user/locations` | User role. |
| `GET` | `/api/guardian/locations` | Guardian role. |
| `GET` | `/api/user/config` | Current emergency numbers. |
| `PUT` | `/api/user/config` | Needs `current_password`. |
| `GET` | `/api/guardian/evidence/<alert_id>` | Guardian role, own device only. |
| `GET` | `/api/device/status/<device_id>` | Own device only. |

### Device (`X-Device-Key`)

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/device/config/<device_id>` | Config poll, heartbeat, pairing code. |

### Admin session only

| Method | Path |
|---|---|
| `GET` | `/` — dashboard |
| `GET` | `/api/admin/pairing-codes` |
| `GET/POST` | `/api/admin/pairing-code/<device_id>` |
| `GET` | `/api/evidence/ledger`, `/api/evidence/ledger/verify` |

### Mixed scope

`GET /api/alerts`, `GET /api/alerts/<id>`, `/api/evidence/*`, and
`/uploads/<file>` accept either an admin session (cross-device) or an app
token (its own device only). A record belonging to another device answers
`404`, so sequential IDs cannot be used to probe.

---

## Authorization rules

Two helpers carry the whole model, in `app.py`:

- `_check_any_auth()` — *authentication only*. Use it solely on routes whose
  response contains nothing device-specific.
- `_authorize_device(device_id)` — *authorization*. Admin passes; an app token
  passes only for its own device.

**Any new route that returns device data must call `_authorize_device()`.**
Using `_check_any_auth()` alone on such a route is the bug class that let one
account read every household's evidence.

---

## Running for real

`app.py` uses Flask's development server. For anything beyond a demo:

```bash
pip install waitress                                   # Windows
waitress-serve --port=8080 app:app

pip install gunicorn                                   # Linux
gunicorn -w 4 -b 0.0.0.0:8080 app:app
```

Note that `ratelimit.py` keeps counters in process memory, so with multiple
workers each holds its own. Move it to Redis if you scale out.

---

## Tests

The integration suite covering authorization, pairing, rate limiting, and the
full device-upload path lives in `test_kavach.py`:

```bash
python test_kavach.py
```

It uses Flask's test client, so it needs no running server and no ngrok — but
it does need `keys/chacha.key` to exist.
