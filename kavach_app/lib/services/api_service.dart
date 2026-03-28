import 'dart:convert';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

class ApiService {
  // Change this to your ngrok domain
  static const String baseUrl =
      'https://unpropitious-braelyn-blossomy.ngrok-free.dev';

  static Future<String?> _getToken() async {
    final prefs = await SharedPreferences.getInstance();
    return prefs.getString('auth_token');
  }

  static Future<Map<String, String>> _authHeaders() async {
    final token = await _getToken();
    if (token == null || token.isEmpty) {
      return {
        'Content-Type': 'application/json',
        'ngrok-skip-browser-warning': 'true',
      };
    }
    return {
      'Content-Type': 'application/json',
      'Authorization': 'Bearer $token',
      'ngrok-skip-browser-warning': 'true',
    };
  }

  static Map<String, String> _jsonHeaders() {
    return {
      'Content-Type': 'application/json',
      'ngrok-skip-browser-warning': 'true',
    };
  }

  /// Safely parse response, handling non-JSON and token expiry
  static Future<Map<String, dynamic>> _handleResponse(
      http.Response response) async {
    // Handle non-success HTTP codes
    if (response.statusCode >= 500) {
      return {'status': 'error', 'message': 'Server error (${response.statusCode})'};
    }

    // Try parsing JSON
    Map<String, dynamic> body;
    try {
      body = jsonDecode(response.body);
    } catch (_) {
      return {'status': 'error', 'message': 'Invalid server response'};
    }

    // Handle token expiry - redirect to login
    if (response.statusCode == 401) {
      final msg = body['message']?.toString() ?? '';
      if (msg.contains('expired') || msg.contains('Invalid token')) {
        await _clearAuth();
      }
      return {'status': 'error', 'message': msg.isNotEmpty ? msg : 'Unauthorized'};
    }

    return body;
  }

  /// Clear stored credentials
  static Future<void> _clearAuth() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove('auth_token');
    await prefs.remove('role');
    await prefs.remove('device_id');
  }

  /// Public logout method
  static Future<void> logout() async {
    await _clearAuth();
  }

  // ── Auth ──────────────────────────────────────────────────────────────

  static Future<Map<String, dynamic>> login(
      String deviceId, String role, String password) async {
    final response = await http.post(
      Uri.parse('$baseUrl/api/auth/login'),
      headers: _jsonHeaders(),
      body: jsonEncode({
        'device_id': deviceId,
        'role': role,
        'password': password,
      }),
    );
    return _handleResponse(response);
  }

  static Future<Map<String, dynamic>> signup(
      String deviceId, String role, String password) async {
    final response = await http.post(
      Uri.parse('$baseUrl/api/auth/signup'),
      headers: _jsonHeaders(),
      body: jsonEncode({
        'device_id': deviceId,
        'role': role,
        'password': password,
      }),
    );
    return _handleResponse(response);
  }

  // ── Alerts ────────────────────────────────────────────────────────────

  static Future<Map<String, dynamic>> getUserAlerts() async {
    final response = await http.get(
      Uri.parse('$baseUrl/api/user/alerts'),
      headers: await _authHeaders(),
    );
    return _handleResponse(response);
  }

  static Future<Map<String, dynamic>> getGuardianAlerts() async {
    final response = await http.get(
      Uri.parse('$baseUrl/api/guardian/alerts'),
      headers: await _authHeaders(),
    );
    return _handleResponse(response);
  }

  static Future<Map<String, dynamic>> getAlertDetail(int alertId) async {
    final response = await http.get(
      Uri.parse('$baseUrl/api/alerts/$alertId'),
      headers: await _authHeaders(),
    );
    return _handleResponse(response);
  }

  // ── Locations ─────────────────────────────────────────────────────────

  static Future<Map<String, dynamic>> getUserLocations() async {
    final response = await http.get(
      Uri.parse('$baseUrl/api/user/locations'),
      headers: await _authHeaders(),
    );
    return _handleResponse(response);
  }

  /// Get location history for guardian's monitored user
  static Future<Map<String, dynamic>> getGuardianLocations() async {
    final response = await http.get(
      Uri.parse('$baseUrl/api/guardian/locations'),
      headers: await _authHeaders(),
    );
    return _handleResponse(response);
  }

  // ── Evidence ──────────────────────────────────────────────────────────

  static Future<Map<String, dynamic>> getGuardianEvidence(
      int alertId) async {
    final response = await http.get(
      Uri.parse('$baseUrl/api/guardian/evidence/$alertId'),
      headers: await _authHeaders(),
    );
    return _handleResponse(response);
  }

  // ── Config ─────────────────────────────────────────────────────────────

  static Future<Map<String, dynamic>> getConfig() async {
    final response = await http.get(
      Uri.parse('$baseUrl/api/user/config'),
      headers: await _authHeaders(),
    );
    return _handleResponse(response);
  }

  static Future<Map<String, dynamic>> updateConfig(
      Map<String, String> numbers) async {
    final response = await http.put(
      Uri.parse('$baseUrl/api/user/config'),
      headers: await _authHeaders(),
      body: jsonEncode(numbers),
    );
    return _handleResponse(response);
  }

  // ── Health ────────────────────────────────────────────────────────────

  static Future<Map<String, dynamic>> getHealth() async {
    final response = await http.get(
      Uri.parse('$baseUrl/api/health'),
      headers: {'ngrok-skip-browser-warning': 'true'},
    );
    return _handleResponse(response);
  }

  // ── Device Status (live battery + online/offline) ───────────────────

  static Future<Map<String, dynamic>> getDeviceStatus(
      String deviceId) async {
    final response = await http.get(
      Uri.parse('$baseUrl/api/device/status/$deviceId'),
      headers: await _authHeaders(),
    );
    return _handleResponse(response);
  }

  // ── Helper: build full URL for evidence files ─────────────────────────

  static String evidenceUrl(String filename) {
    return '$baseUrl/uploads/$filename';
  }

  // ── Evidence Chain Ledger ────────────────────────────────────────────

  /// Get all evidence files for a specific alert
  static Future<Map<String, dynamic>> getEvidenceForAlert(int alertId) async {
    final response = await http.get(
      Uri.parse('$baseUrl/api/evidence/alert/$alertId'),
      headers: await _authHeaders(),
    );
    return _handleResponse(response);
  }

  /// Verify a single evidence file's SHA-256 hash integrity
  static Future<Map<String, dynamic>> verifyEvidence(int evidenceId) async {
    final response = await http.get(
      Uri.parse('$baseUrl/api/evidence/$evidenceId/verify'),
      headers: await _authHeaders(),
    );
    return _handleResponse(response);
  }

  /// Verify the entire evidence chain ledger integrity
  static Future<Map<String, dynamic>> verifyLedger() async {
    final response = await http.get(
      Uri.parse('$baseUrl/api/evidence/ledger/verify'),
      headers: await _authHeaders(),
    );
    return _handleResponse(response);
  }

  // ── FCM Token ──────────────────────────────────────────────────────

  /// Register or update the FCM push notification token
  static Future<Map<String, dynamic>> updateFcmToken(String fcmToken) async {
    final response = await http.put(
      Uri.parse('$baseUrl/api/auth/fcm-token'),
      headers: await _authHeaders(),
      body: jsonEncode({'fcm_token': fcmToken}),
    );
    return _handleResponse(response);
  }

}
