# Security Model

What Kavach protects, how, and — just as importantly — what it does not
protect yet. If you are evaluating this project or building on it, read the
[Known limitations](#known-limitations) section; the gaps there are real and
stated deliberately rather than glossed over.

---

## Threat model

Kavach is worn by someone who may be at risk from a person who knows them.
That shapes the priorities:

| We defend against | We do not defend against |
|---|---|
| A stranger claiming someone else's device | An attacker with root on the server |
| One household reading another's data | Someone who physically takes the device and its SD card |
| Passive interception of uploads | A compromised phone the user is logged into |
| Password guessing over the network | A malicious ngrok/network operator (TLS terminates there) |
| Silent redirection of emergency calls | Nation-state traffic analysis |

---

## How each piece is protected

### Transport and payloads

Every alert the device uploads is encrypted with **ChaCha20-Poly1305** before
it leaves the Pi — telemetry as JSON, evidence files as raw bytes. The nonce
is 12 random bytes prepended to the ciphertext; the Poly1305 tag means a
modified payload fails to decrypt rather than decrypting to garbage.

The key lives in `keys/chacha.key` on both machines and is never committed.

### Device to server

The Pi authenticates its config polls with an `X-Device-Key` header, compared
in constant time. The key is generated on the server's first boot and written
into the device config automatically when the two folders sit side by side.

### Accounts

App accounts are per `(device_id, role)` — one **user** and one **guardian**
per device. Passwords are hashed with `pbkdf2` via Werkzeug and never stored
in plaintext. Sessions are `itsdangerous` signed tokens with a 24-hour expiry.

Creating an account requires a **pairing code**, an eight-character value
generated per device and visible only to someone with physical access to the
device, access to the server console, or the admin dashboard. This is the
proof of ownership — without it, guessing `KAVACH-002` would be enough to
claim someone else's device.

### Authorization

Being authenticated is not enough to read data. Every device-scoped route
resolves which device owns the record and compares it against the caller:

- An **app token** may only ever see its own device's alerts, locations,
  evidence, and status.
- The **admin dashboard session** is cross-device by design — it is an
  operator console.
- Where a record exists but belongs to someone else, the response is `404`,
  not `403`, so walking sequential IDs reveals nothing.

Evidence download links are signed and carry both the filename and the owning
device inside the signature, so a link shared out of one household is useless
against another's files.

### Emergency contacts

The four numbers the device dials are the highest-value target in the system —
redirecting them turns the device against its wearer. Changing them requires
re-entering the account password even with a valid session, the change is
logged at `WARNING` on both the server and the device, appended to
`contact_changes.log` on the device, and pushed as a notification to the
paired accounts.

### Brute force

Login, signup, and the dashboard login are rate limited to ten **failed**
attempts per window per client. Successes never count and a success clears the
counter, so ordinary use is never throttled. Failed logins return one generic
message whether the account exists or not.

### Evidence integrity

Each uploaded file is hashed with SHA-256 on the device before encryption and
re-hashed on the server after decryption. Both hashes are recorded, and every
file is appended to a hash-chained ledger in `evidence_ledger.json`.

---

## Known limitations

Stated plainly, because a security claim you cannot back up is worse than no
claim.

### The evidence ledger is corruption-evident, not tamper-evident

`verify_ledger_integrity()` recomputes each `prev_hash` from the *current*
contents of the previous entry. Anyone who can write to `evidence_ledger.json`
can edit an entry and recompute every hash after it, and verification will
report the chain intact.

It reliably detects accidental corruption and partial writes. It does not
detect a deliberate rewrite by someone with filesystem access — which is the
same server that holds the evidence.

Making it genuinely tamper-evident needs an anchor outside the server: signing
each entry with a key the server does not hold, or publishing the head hash
somewhere the server cannot reach.

### Forward secrecy is implemented but not deployed

`ratchet.py` implements a forward-secure key ratchet, and
`forgery_experiment.py` measures it. **Neither is in the live upload path.**
Real uploads call `chacha_encrypt_text()` with one static key shared by every
device and the server.

Consequence: extracting the key from any single device's SD card decrypts all
past and future traffic for every device. Rotating to per-device keys, or
integrating the ratchet into `hardware/comms.py`, would fix this.

### A hash mismatch does not reject the evidence

If the device-supplied SHA-256 disagrees with the server's, the mismatch is
logged at `WARNING` and the file is stored and ledgered anyway. The
`verified: false` signal appears in the API response but does not reach the
database, the ledger, or either interface.

### Tokens are stored in plaintext on the phone

The 24-hour bearer token is kept in `SharedPreferences`, which is unencrypted
XML on Android. It should be `flutter_secure_storage`. A rooted phone or a
device backup exposes it.

### CORS is open

`CORS(app)` permits any origin. Because auth is a bearer token rather than a
cookie, this does not by itself allow a hostile page to act as the user, but
it should be pinned to known origins.

### The server is a development server

`app.run()` is Werkzeug's development server, published to the internet via
ngrok. It is not hardened against slow-read attacks, oversized headers, or
concurrent load. Use `waitress` or `gunicorn` for anything beyond a demo.

### Rate limiting is per-process and in-memory

Counters reset when the server restarts and are not shared between workers.
Correct for the single-process deployment here; move to Redis if you scale out.

---

## If a secret leaks

Removing a secret in a new commit does **not** remove it from the repository —
every earlier commit still contains it, and GitHub keeps them reachable.

1. **Rotate first.** Revoke the API token, change the password, regenerate the
   device key. Assume the old value is compromised the moment it was pushed.
2. **Then rewrite history** with `git filter-repo`:
   ```bash
   pip install git-filter-repo
   git filter-repo --path Personal-Safety-Device-main/config.json --invert-paths --force
   git remote add origin <your-repo-url>
   git push --force --all
   ```
3. **Tell anyone who forked or cloned it.** Their copy still has the secret.

---

## Reporting a vulnerability

This is a student research project, not a deployed product. If you find
something, open an issue describing the impact — please do not include working
exploit code against any live instance.
