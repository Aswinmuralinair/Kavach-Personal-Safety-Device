import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:latlong2/latlong.dart';
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

  @override
  void initState() {
    super.initState();
    _loadData();
  }

  Future<void> _loadData() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final result = await ApiService.getGuardianAlerts();
      if (result['status'] == 'ok') {
        _alerts = (result['alerts'] as List)
            .map((a) => AlertModel.fromJson(a))
            .toList();
        if (_alerts.isNotEmpty) {
          _latestAlert = _alerts.first;
        }
      } else {
        _error = result['message'] ?? 'Failed to load';
      }
    } catch (e) {
      _error = 'Cannot connect to server';
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
      onRefresh: _loadData,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
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
                      'Location: ${_latestAlert!.gpsLocation}',
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
