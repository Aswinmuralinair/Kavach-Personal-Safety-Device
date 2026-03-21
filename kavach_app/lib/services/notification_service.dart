import 'dart:async';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'api_service.dart';

class NotificationService {
  static final FlutterLocalNotificationsPlugin _plugin =
      FlutterLocalNotificationsPlugin();
  static Timer? _pollTimer;
  static bool _initialized = false;

  static Future<void> init() async {
    if (_initialized) return;

    const androidSettings =
        AndroidInitializationSettings('@mipmap/ic_launcher');
    const initSettings = InitializationSettings(android: androidSettings);

    await _plugin.initialize(initSettings);
    _initialized = true;
  }

  /// Request notification permission (call after UI is ready)
  static Future<void> requestPermission() async {
    final android = _plugin.resolvePlatformSpecificImplementation<
        AndroidFlutterLocalNotificationsPlugin>();
    await android?.requestNotificationsPermission();
  }

  /// Start polling for new alerts. Call after login.
  static void startPolling({required String role}) {
    stopPolling();
    // Poll immediately, then every 15 seconds
    _checkForNewAlerts(role);
    _pollTimer = Timer.periodic(
      const Duration(seconds: 5),
      (_) => _checkForNewAlerts(role),
    );
  }

  static void stopPolling() {
    _pollTimer?.cancel();
    _pollTimer = null;
  }

  static Future<void> _checkForNewAlerts(String role) async {
    try {
      final result = role == 'user'
          ? await ApiService.getUserAlerts()
          : await ApiService.getGuardianAlerts();

      if (result['status'] != 'ok') return;
      final alerts = result['alerts'] as List?;
      if (alerts == null || alerts.isEmpty) return;

      final latest = alerts.first;
      final latestId = latest['id'] as int? ?? 0;

      final prefs = await SharedPreferences.getInstance();
      final lastSeenId = prefs.getInt('last_seen_alert_id') ?? 0;

      if (latestId > lastSeenId) {
        // New alert(s) detected
        await prefs.setInt('last_seen_alert_id', latestId);

        // Don't notify on first load (no previous baseline)
        if (lastSeenId == 0) return;

        final alertType = latest['alert_type'] ?? 'ALERT';
        final trigger = latest['trigger_source'] ?? '';
        final deviceId = latest['device_id'] ?? '';
        final gps = latest['gps_location'] ?? '';

        // Map raw trigger sources to friendly names
        const triggerLabels = {
          'keyboard_demo': 'SOS Button',
          'button_single': 'SOS Button',
          'button_double': 'Medical Button',
          'fall_detected': 'Fall Detected',
          'heartrate_spike': 'Heart Rate Spike',
          'audio_detection': 'Danger Sound',
          'lora_relay': 'LoRa Mesh Relay',
        };
        // Match partial keys (e.g. audio_screaming → Danger Sound)
        String friendlyTrigger = trigger;
        for (final entry in triggerLabels.entries) {
          if (trigger.toLowerCase().contains(entry.key.toLowerCase()) ||
              entry.key.toLowerCase().contains(trigger.toLowerCase())) {
            friendlyTrigger = entry.value;
            break;
          }
        }
        if (trigger.startsWith('audio_')) {
          friendlyTrigger = 'Danger Sound';
        }

        String body = 'Device: $deviceId';
        if (friendlyTrigger.isNotEmpty) body += ' | $friendlyTrigger';
        if (gps.isNotEmpty) body += '\nLocation: $gps';

        await _showNotification(
          title: '$alertType Alert!',
          body: body,
          id: latestId,
        );
      }
    } catch (_) {
      // Silently fail — don't crash the app for notification polling
    }
  }

  static Future<void> _showNotification({
    required String title,
    required String body,
    required int id,
  }) async {
    const androidDetails = AndroidNotificationDetails(
      'kavach_alerts',
      'Kavach Alerts',
      channelDescription: 'SOS and safety alert notifications',
      importance: Importance.max,
      priority: Priority.high,
      playSound: true,
      enableVibration: true,
      icon: '@mipmap/ic_launcher',
    );
    const details = NotificationDetails(android: androidDetails);
    await _plugin.show(id % 100000, title, body, details);
  }
}
