import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:latlong2/latlong.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../../services/api_service.dart';
import '../../models/alert_model.dart';

class GuardianDashboardScreen extends StatefulWidget {
  const GuardianDashboardScreen({super.key});

  @override
  State<GuardianDashboardScreen> createState() =>
      _GuardianDashboardScreenState();
}

class _GuardianDashboardScreenState extends State<GuardianDashboardScreen> {
  bool _loading = true;
  String? _error;
  List<AlertModel> _alerts = [];
  AlertModel? _latestAlert;
  Timer? _refreshTimer;
  Timer? _statusTimer;
  String? _deviceId;
  String _deviceBattery = 'N/A';
  bool _deviceOnline = false;
  final MapController _mapController = MapController();

  @override
  void initState() {
    super.initState();
    _loadData();
    _initDeviceStatusPolling();
    _refreshTimer = Timer.periodic(
      const Duration(seconds: 10),
      (_) => _loadData(),
    );
  }

  @override
  void dispose() {
    _refreshTimer?.cancel();
    _statusTimer?.cancel();
    super.dispose();
  }

  Future<void> _initDeviceStatusPolling() async {
    final prefs = await SharedPreferences.getInstance();
    _deviceId = prefs.getString('device_id') ?? 'KAVACH-001';
    _fetchDeviceStatus();
    _statusTimer = Timer.periodic(
      const Duration(seconds: 10),
      (_) => _fetchDeviceStatus(),
    );
  }

  Future<void> _fetchDeviceStatus() async {
    if (_deviceId == null) return;
    try {
      final result = await ApiService.getDeviceStatus(_deviceId!);
      if (result['status'] == 'ok' && mounted) {
        setState(() {
          _deviceOnline = result['online'] == true;
          _deviceBattery = result['battery'] ?? 'N/A';
        });
      }
    } catch (_) {
      if (mounted) {
        setState(() {
          _deviceOnline = false;
          _deviceBattery = 'N/A';
        });
      }
    }
  }

  Future<void> _loadData() async {
    // Only show full-screen spinner on initial load — update live after that
    if (_loading) {
      setState(() => _error = null);
    }
    try {
      final result = await ApiService.getGuardianAlerts();
      if (result['status'] == 'ok') {
        _alerts = (result['alerts'] as List)
            .map((a) => AlertModel.fromJson(a))
            .toList();
        if (_alerts.isNotEmpty) {
          _latestAlert = _alerts.first;
          // Move map to new location if available
          if (_latestAlert!.latitude != null && _latestAlert!.longitude != null) {
            try {
              _mapController.move(
                LatLng(_latestAlert!.latitude!, _latestAlert!.longitude!),
                _mapController.camera.zoom,
              );
            } catch (_) {
              // MapController not yet attached — ignore
            }
          }
        }
        _error = null;
      } else {
        if (_alerts.isEmpty) _error = result['message'] ?? 'Failed to load';
      }
    } catch (e) {
      if (_alerts.isEmpty) _error = 'Cannot connect to server';
    }
    if (mounted) setState(() => _loading = false);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }

    if (_error != null) {
      return Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.cloud_off, size: 64, color: theme.colorScheme.error),
            const SizedBox(height: 16),
            Text(_error!),
            const SizedBox(height: 16),
            ElevatedButton(onPressed: _loadData, child: const Text('Retry')),
          ],
        ),
      );
    }

    return RefreshIndicator(
      onRefresh: () async {
        await _loadData();
        await _fetchDeviceStatus();
      },
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          // Device status card — live battery + online/offline
          Card(
            color: _deviceOnline
                ? Colors.green.shade50
                : Colors.red.shade50,
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: Row(
                children: [
                  Container(
                    width: 12,
                    height: 12,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      color: _deviceOnline ? Colors.green : Colors.red,
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          _deviceOnline ? 'Device Online' : 'Device Offline',
                          style: theme.textTheme.titleMedium?.copyWith(
                            fontWeight: FontWeight.bold,
                          ),
                        ),
                        Text(
                          _deviceOnline
                              ? 'Battery: $_deviceBattery'
                              : 'No heartbeat received',
                          style: theme.textTheme.bodySmall,
                        ),
                      ],
                    ),
                  ),
                  Icon(
                    _deviceOnline
                        ? Icons.battery_full
                        : Icons.battery_unknown,
                    color: _deviceOnline ? Colors.green : Colors.red,
                    size: 32,
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),

          // Status banner
          if (_latestAlert != null) ...[
            Container(
              padding: const EdgeInsets.all(20),
              decoration: BoxDecoration(
                gradient: LinearGradient(
                  colors: [Colors.red.shade400, Colors.red.shade700],
                ),
                borderRadius: BorderRadius.circular(16),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      const Icon(Icons.warning_amber_rounded,
                          color: Colors.white, size: 28),
                      const SizedBox(width: 8),
                      Text(
                        'Latest Alert',
                        style: theme.textTheme.titleLarge?.copyWith(
                          color: Colors.white,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 12),
                  Text(
                    '${_latestAlert!.alertType} - ${_latestAlert!.formattedTime}',
                    style: const TextStyle(color: Colors.white, fontSize: 16),
                  ),
                  if (_latestAlert!.gpsLocation != null) ...[
                    const SizedBox(height: 4),
                    Text(
                      'Location: ${_latestAlert!.gpsLocation}${_latestAlert!.locationSource != null ? '  (${_latestAlert!.locationSource})' : ''}',
                      style:
                          TextStyle(color: Colors.white.withAlpha(204), fontSize: 13),
                    ),
                  ],
                ],
              ),
            ),
            const SizedBox(height: 16),
          ],

          // Map showing latest location
          if (_latestAlert?.latitude != null &&
              _latestAlert?.longitude != null) ...[
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Last Known Location',
                        style: theme.textTheme.titleMedium
                            ?.copyWith(fontWeight: FontWeight.bold)),
                    const SizedBox(height: 8),
                    SizedBox(
                      height: 200,
                      child: ClipRRect(
                        borderRadius: BorderRadius.circular(12),
                        child: FlutterMap(
                          mapController: _mapController,
                          options: MapOptions(
                            initialCenter: LatLng(
                              _latestAlert!.latitude!,
                              _latestAlert!.longitude!,
                            ),
                            initialZoom: 15,
                          ),
                          children: [
                            TileLayer(
                              urlTemplate:
                                  'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                              userAgentPackageName: 'com.kavach.app',
                            ),
                            MarkerLayer(
                              markers: [
                                Marker(
                                  point: LatLng(
                                    _latestAlert!.latitude!,
                                    _latestAlert!.longitude!,
                                  ),
                                  width: 40,
                                  height: 40,
                                  child: const Icon(Icons.location_pin,
                                      color: Colors.red, size: 40),
                                ),
                              ],
                            ),
                          ],
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 16),
          ],

          // Summary
          Text(
            'Alert History',
            style: theme.textTheme.titleMedium
                ?.copyWith(fontWeight: FontWeight.bold),
          ),
          const SizedBox(height: 8),

          if (_alerts.isEmpty)
            Card(
              child: Padding(
                padding: const EdgeInsets.all(32),
                child: Column(
                  children: [
                    Icon(Icons.shield, size: 48, color: Colors.green.shade300),
                    const SizedBox(height: 8),
                    const Text('No alerts. The user is safe.'),
                  ],
                ),
              ),
            )
          else
            ..._alerts.take(10).map((alert) => Card(
                  margin: const EdgeInsets.only(bottom: 8),
                  child: ListTile(
                    leading: CircleAvatar(
                      backgroundColor: alert.alertType == 'SOS'
                          ? Colors.red.shade100
                          : Colors.blue.shade100,
                      child: Icon(
                        alert.alertType == 'SOS'
                            ? Icons.sos
                            : Icons.local_hospital,
                        color: alert.alertType == 'SOS'
                            ? Colors.red
                            : Colors.blue,
                      ),
                    ),
                    title: Text('${alert.alertType} #${alert.id}'),
                    subtitle: Text(alert.formattedTime),
                    trailing: Text(
                      alert.evidenceFiles.isNotEmpty
                          ? '${alert.evidenceFiles.length} files'
                          : '',
                      style: theme.textTheme.bodySmall,
                    ),
                  ),
                )),
        ],
      ),
    );
  }
}
