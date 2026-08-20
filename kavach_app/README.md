# Kavach Mobile App

Flutter mobile app for the Kavach Personal Safety Device. Connects to the Kavach server via REST API to monitor alerts, view evidence, and configure the device remotely.

## Roles

| Role | What they see |
|------|--------------|
| **User** | Dashboard (live device battery + online/offline, alert counts), alert list with detail view + map, location history, settings (change phone numbers remotely) |
| **Guardian** | Dashboard (live device battery + online/offline, latest alert map), alert list with evidence viewer (video/audio/images with integrity verification), location history |

## Features

- **Push notifications** — Polls every 5 seconds for new alerts, shows Android notification with sound when SOS/MEDICAL detected
- **Splash screen** — Animated Kavach logo with "Your Safety, Our Priority" caption on launch
- **Custom launcher icon** — Kavach logo on Android home screen (via flutter_launcher_icons)
- **Live device status** — Dashboard polls device battery every 10 seconds, shows "Device Online" or "Device Offline"
- **Alert detail** — GPS location on map (coordinates rounded to 6 decimal places), call/SMS status, battery, evidence files with SHA-256 integrity badges
- **Evidence viewer** — Opens evidence files via signed download URLs (1-hour expiry, no auth headers needed in browser)
- **Location history** — Map view of all GPS coordinates from alert updates (both user and guardian have dedicated location pages with map + list view)
- **Guardian invite system** — Users can invite guardians, guardians accept/reject invites, either party can revoke
- **Remote config** — Change police, guardian, medical, and WhatsApp numbers from the app (syncs to Pi via server)
- **Secure auth** — Bearer token auth (24-hour expiry), auto-logout on token expiry

## Setup

```bash
cd kavach_app
flutter pub get
flutter build apk --debug
```

The APK will be at `build/app/outputs/flutter-apk/app-debug.apk`.

**Requirements:** Flutter SDK 3.41+, Android SDK, Dart SDK 3.11+.

## Server URL

The server URL is configured in `lib/services/api_service.dart`:
```dart
static const String baseUrl = 'https://your-name.ngrok-free.dev';
```

Change this to your ngrok domain before building.

## Creating an account

Each device supports one **user** account (the person carrying it) and one
**guardian** account (the person watching over them).

Sign-up requires the device's **pairing code** — an 8-character code that proves
you actually have access to the device. Without it, anyone who guessed a device ID
could register as its user and change the numbers it dials in an emergency.

Find the code in any of these places:

- the Raspberry Pi's console, a few seconds after `python main.py` starts
- the Kavach server console at startup
- the admin dashboard, on the device card — click it to copy

Then in the app: **Sign Up** → Device ID → role → pairing code → a password of at
least 8 characters.

Changing the emergency contacts later asks for that password again. That is
deliberate: an unlocked phone with a live session should not be enough to redirect
where an SOS goes.

## Notifications — read this before demoing

The app **polls** the server every few seconds while it is open. There are no push
notifications: if the app is backgrounded or closed, nothing reaches the phone.

The server side of Firebase Cloud Messaging is written and ready, but this app has
no Firebase client wired in. To enable real background alerts, follow *Enabling push
notifications* in [SETUP.md](../SETUP.md).

## Before publishing

The release build is signed with Flutter's **debug key**
(`android/app/build.gradle.kts`). That is fine for sideloading and demos, but you
must add a real signing config before submitting to the Play Store.

## Full setup

For the complete walkthrough — encryption key, server, device, then this app — see
[SETUP.md](../SETUP.md) in the repository root.
