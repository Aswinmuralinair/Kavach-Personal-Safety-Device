# Kavach Mobile App

Flutter mobile app for the Kavach Personal Safety Device. Connects to the Kavach server via REST API to monitor alerts, view evidence, and configure the device remotely.

## Roles

| Role | What they see |
|------|--------------|
| **User** | Dashboard (live device battery + online/offline, alert counts), alert list with detail view + map, location history, settings (change phone numbers remotely) |
| **Guardian** | Dashboard, alert list with evidence viewer (video/audio/images with integrity verification) |

## Features

- **Live device status** — Dashboard polls device battery every 60 seconds, shows "Device Online" or "Device Offline"
- **Alert detail** — GPS location on map, call/SMS status, battery, evidence files with SHA-256 integrity badges (Verified/Tampered/Unchecked)
- **Evidence viewer** — Opens evidence files via signed download URLs (1-hour expiry, no auth headers needed in browser)
- **Location history** — Map view of all GPS coordinates from alert updates
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
