import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:latlong2/latlong.dart';
import '../../services/api_service.dart';

class GuardianLocationScreen extends StatefulWidget {
  const GuardianLocationScreen({super.key});

  @override
  State<GuardianLocationScreen> createState() => _GuardianLocationScreenState();
}

class _GuardianLocationScreenState extends State<GuardianLocationScreen> {
  bool _loading = true;
  String? _error;
  List<Map<String, dynamic>> _locations = [];
  Timer? _refreshTimer;

  @override
  void initState() {
    super.initState();
    _loadLocations();
    _refreshTimer = Timer.periodic(
      const Duration(seconds: 10),
      (_) => _loadLocations(),
    );
  }

  @override
  void dispose() {
    _refreshTimer?.cancel();
    super.dispose();
  }

  Future<void> _loadLocations() async {
    if (_loading) {
      setState(() => _error = null);
    }
    try {
      // Try dedicated guardian locations endpoint first
      var result = await ApiService.getGuardianLocations();
      if (result['status'] == 'ok') {
        _locations =
            List<Map<String, dynamic>>.from(result['locations'] ?? []);
      } else {
        // Fallback: extract locations from guardian alerts
        result = await ApiService.getGuardianAlerts();
        if (result['status'] == 'ok') {
          final alerts = List<Map<String, dynamic>>.from(
              result['alerts'] ?? []);
          _locations = alerts
              .where((a) =>
                  a['gps_location'] != null &&
                  a['gps_location'].toString().isNotEmpty)
              .map((a) => {
                    'alert_id': a['id'],
                    'alert_type': a['alert_type'] ?? 'ALERT',
                    'gps_location': a['gps_location'],
                    'timestamp': a['timestamp'] ?? '',
                    'location_source': a['location_source'],
                  })
              .toList();
        } else {
          if (_locations.isEmpty) {
            _error = result['message'] ?? 'Failed to load';
          }
        }
      }
    } catch (e) {
      if (_locations.isEmpty) _error = 'Cannot connect to server';
    }
    if (mounted) setState(() => _loading = false);
  }

  List<Marker> _buildMarkers() {
    final markers = <Marker>[];
    for (final loc in _locations) {
      final gps = loc['gps_location']?.toString();
      if (gps == null || !gps.contains(',')) continue;
      final parts = gps.split(',');
      final lat = double.tryParse(parts[0].trim());
      final lon = double.tryParse(parts[1].trim());
      if (lat == null || lon == null) continue;

      final alertType = loc['alert_type'] ?? 'ALERT';
      final isSOS = alertType == 'SOS';

      markers.add(
        Marker(
          point: LatLng(lat, lon),
          width: 40,
          height: 40,
          child: Tooltip(
            message: '$alertType #${loc['alert_id']}',
            child: Icon(
              Icons.location_pin,
              color: isSOS ? Colors.red : Colors.blue,
              size: 36,
            ),
          ),
        ),
      );
    }
    return markers;
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }

    if (_error != null) {
      return Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.cloud_off,
                size: 64, color: Theme.of(context).colorScheme.error),
            const SizedBox(height: 16),
            Text(_error!),
            const SizedBox(height: 16),
            ElevatedButton(
                onPressed: _loadLocations, child: const Text('Retry')),
          ],
        ),
      );
    }

    final markers = _buildMarkers();

    if (markers.isEmpty) {
      return Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.location_off, size: 64, color: Colors.grey.shade400),
            const SizedBox(height: 16),
            const Text('No location data yet'),
            const SizedBox(height: 8),
            Text(
              'Location will appear here when an alert is triggered.',
              style: Theme.of(context).textTheme.bodySmall,
            ),
          ],
        ),
      );
    }

    final center = markers.first.point;

    return Column(
      children: [
        // Map
        Expanded(
          flex: 3,
          child: FlutterMap(
            options: MapOptions(
              initialCenter: center,
              initialZoom: 13,
            ),
            children: [
              TileLayer(
                urlTemplate:
                    'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                userAgentPackageName: 'com.kavach.app',
              ),
              MarkerLayer(markers: markers),
            ],
          ),
        ),

        // Location list
        Expanded(
          flex: 2,
          child: ListView.builder(
            padding: const EdgeInsets.all(8),
            itemCount: _locations.length,
            itemBuilder: (context, index) {
              final loc = _locations[index];
              final alertType = loc['alert_type'] ?? 'ALERT';
              final isSOS = alertType == 'SOS';
              final timestamp = loc['timestamp'] ?? '';

              return ListTile(
                leading: CircleAvatar(
                  backgroundColor:
                      isSOS ? Colors.red.shade100 : Colors.blue.shade100,
                  child: Icon(
                    isSOS ? Icons.sos : Icons.local_hospital,
                    color: isSOS ? Colors.red : Colors.blue,
                    size: 20,
                  ),
                ),
                title: Text('$alertType #${loc['alert_id']}'),
                subtitle: Text(
                  loc['gps_location'] ?? 'No GPS',
                  style: const TextStyle(fontSize: 12),
                ),
                trailing: Text(
                  _formatTime(timestamp),
                  style: const TextStyle(fontSize: 11),
                ),
              );
            },
          ),
        ),
      ],
    );
  }

  String _formatTime(String timestamp) {
    try {
      final dt = DateTime.parse(timestamp);
      return '${dt.day}/${dt.month} ${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';
    } catch (_) {
      return timestamp;
    }
  }
}
