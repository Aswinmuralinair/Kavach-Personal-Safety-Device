import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:latlong2/latlong.dart';
import '../../services/api_service.dart';

class AlertDetailScreen extends StatefulWidget {
  final int alertId;
  const AlertDetailScreen({super.key, required this.alertId});

  @override
  State<AlertDetailScreen> createState() => _AlertDetailScreenState();
}

class _AlertDetailScreenState extends State<AlertDetailScreen> {
  bool _loading = true;
  String? _error;
  Map<String, dynamic>? _alert;
  List<Map<String, dynamic>> _evidence = [];

  @override
  void initState() {
    super.initState();
    _loadDetail();
  }

  Future<void> _loadDetail() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final result = await ApiService.getAlertDetail(widget.alertId);
      if (result['status'] == 'ok') {
        _alert = result['alert'];
        _evidence = List<Map<String, dynamic>>.from(
            result['alert']['evidence'] ?? []);
      } else {
        _error = result['message'] ?? 'Failed to load';
      }
    } catch (e) {
      _error = 'Cannot connect to server';
    }
    if (mounted) setState(() => _loading = false);
  }

  LatLng? _parseLocation() {
    final loc = _alert?['gps_location'];
    if (loc == null || !loc.toString().contains(',')) return null;
    final parts = loc.toString().split(',');
    final lat = double.tryParse(parts[0].trim());
    final lon = double.tryParse(parts[1].trim());
    if (lat == null || lon == null) return null;
    return LatLng(lat, lon);
  }

  void _openEvidence(String publicUrl) async {
    // publicUrl comes from the server with a signed download token already
    // appended (e.g. "/uploads/file.wav?token=xxx"), so we just prepend the
    // base URL. The token gives time-limited access without needing headers.
    final url = '${ApiService.baseUrl}$publicUrl';
    if (await canLaunchUrl(Uri.parse(url))) {
      await launchUrl(Uri.parse(url), mode: LaunchMode.externalApplication);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Scaffold(
      appBar: AppBar(
        title: Text('Alert #${widget.alertId}'),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _error != null
              ? Center(child: Text(_error!))
              : _buildContent(theme),
    );
  }

  Widget _buildContent(ThemeData theme) {
    final alertType = _alert?['alert_type'] ?? 'ALERT';
    final isSOS = alertType == 'SOS';
    final color = isSOS ? Colors.red : Colors.blue;
    final location = _parseLocation();

    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        // Alert type header
        Container(
          padding: const EdgeInsets.all(20),
          decoration: BoxDecoration(
            gradient: LinearGradient(
              colors: [color.withAlpha(25), color.withAlpha(10)],
            ),
            borderRadius: BorderRadius.circular(16),
            border: Border.all(color: color.withAlpha(51)),
          ),
          child: Row(
            children: [
              Icon(
                isSOS ? Icons.sos : Icons.local_hospital,
                size: 48,
                color: color,
              ),
              const SizedBox(width: 16),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      '$alertType Alert',
                      style: theme.textTheme.headlineSmall?.copyWith(
                        fontWeight: FontWeight.bold,
                        color: color,
                      ),
                    ),
                    Text(
                      'Trigger: ${_alert?['trigger_source'] ?? 'Unknown'}',
                      style: theme.textTheme.bodyMedium,
                    ),
                    Text(
                      _alert?['timestamp'] ?? '',
                      style: theme.textTheme.bodySmall,
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 16),

        // Status grid
        Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Status',
                    style: theme.textTheme.titleMedium
                        ?.copyWith(fontWeight: FontWeight.bold)),
                const Divider(),
                _DetailRow(
                  icon: Icons.call,
                  label: 'Call Placed',
                  value: _alert?['call_placed_status'] == true ? 'Yes' : 'No',
                  valueColor: _alert?['call_placed_status'] == true
                      ? Colors.green
                      : Colors.red,
                ),
                _DetailRow(
                  icon: Icons.sms,
                  label: 'Guardian SMS',
                  value:
                      _alert?['guardian_sms_status'] == true ? 'Sent' : 'Not Sent',
                  valueColor: _alert?['guardian_sms_status'] == true
                      ? Colors.green
                      : Colors.red,
                ),
                _DetailRow(
                  icon: Icons.location_on,
                  label: 'Location SMS',
                  value:
                      _alert?['location_sms_status'] == true ? 'Sent' : 'Not Sent',
                  valueColor: _alert?['location_sms_status'] == true
                      ? Colors.green
                      : Colors.red,
                ),
                _DetailRow(
                  icon: Icons.battery_full,
                  label: 'Battery',
                  value: _alert?['battery_percentage'] != null
                      ? '${_alert!['battery_percentage']}'
                      : 'N/A',
                ),
                _DetailRow(
                  icon: Icons.my_location,
                  label: 'GPS',
                  value: _alert?['gps_location'] ?? 'No GPS data',
                ),
              ],
            ),
          ),
        ),
        const SizedBox(height: 16),

        // Map
        if (location != null) ...[
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Location',
                      style: theme.textTheme.titleMedium
                          ?.copyWith(fontWeight: FontWeight.bold)),
                  const SizedBox(height: 8),
                  SizedBox(
                    height: 200,
                    child: ClipRRect(
                      borderRadius: BorderRadius.circular(12),
                      child: FlutterMap(
                        options: MapOptions(
                          initialCenter: location,
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
                                point: location,
                                width: 40,
                                height: 40,
                                child: Icon(Icons.location_pin,
                                    color: color, size: 40),
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

        // Evidence files
        if (_evidence.isNotEmpty) ...[
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Evidence Files',
                      style: theme.textTheme.titleMedium
                          ?.copyWith(fontWeight: FontWeight.bold)),
                  const SizedBox(height: 8),
                  ..._evidence.map((e) {
                    final fname = e['filename'] ?? '';
                    final exists = e['file_exists'] == true;
                    final size = e['file_size_bytes'] ?? 0;
                    final integrity = e['integrity'] ?? 'not_checked';

                    IconData fileIcon = Icons.insert_drive_file;
                    if (fname.endsWith('.wav') || fname.endsWith('.mp3')) {
                      fileIcon = Icons.audiotrack;
                    } else if (fname.endsWith('.h264') ||
                        fname.endsWith('.mp4')) {
                      fileIcon = Icons.videocam;
                    } else if (fname.endsWith('.jpg') ||
                        fname.endsWith('.png')) {
                      fileIcon = Icons.image;
                    }

                    return ListTile(
                      leading: Icon(fileIcon, color: theme.colorScheme.primary),
                      title: Text(
                        fname,
                        style: const TextStyle(fontSize: 13),
                        overflow: TextOverflow.ellipsis,
                      ),
                      subtitle: Row(
                        children: [
                          Text(_formatSize(size)),
                          const SizedBox(width: 8),
                          Container(
                            padding: const EdgeInsets.symmetric(
                                horizontal: 6, vertical: 1),
                            decoration: BoxDecoration(
                              color: integrity == 'verified'
                                  ? Colors.green.withAlpha(25)
                                  : integrity == 'tampered'
                                      ? Colors.red.withAlpha(25)
                                      : Colors.grey.withAlpha(25),
                              borderRadius: BorderRadius.circular(8),
                            ),
                            child: Text(
                              integrity == 'verified'
                                  ? 'Verified'
                                  : integrity == 'tampered'
                                      ? 'Tampered!'
                                      : 'Unchecked',
                              style: TextStyle(
                                fontSize: 10,
                                color: integrity == 'verified'
                                    ? Colors.green
                                    : integrity == 'tampered'
                                        ? Colors.red
                                        : Colors.grey,
                              ),
                            ),
                          ),
                        ],
                      ),
                      trailing: exists
                          ? IconButton(
                              icon: const Icon(Icons.open_in_new),
                              onPressed: () => _openEvidence(
                                  e['public_url'] ?? '/uploads/$fname'),
                            )
                          : const Icon(Icons.error_outline, color: Colors.red),
                    );
                  }),
                ],
              ),
            ),
          ),
        ],
      ],
    );
  }

  String _formatSize(int bytes) {
    if (bytes < 1024) return '$bytes B';
    if (bytes < 1024 * 1024) return '${(bytes / 1024).toStringAsFixed(1)} KB';
    return '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';
  }
}

class _DetailRow extends StatelessWidget {
  final IconData icon;
  final String label;
  final String value;
  final Color? valueColor;

  const _DetailRow({
    required this.icon,
    required this.label,
    required this.value,
    this.valueColor,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(
        children: [
          Icon(icon, size: 20, color: Colors.grey.shade600),
          const SizedBox(width: 8),
          Text(label, style: const TextStyle(fontWeight: FontWeight.w500)),
          const Spacer(),
          Text(
            value,
            style: TextStyle(
              color: valueColor ?? Theme.of(context).colorScheme.onSurface,
              fontWeight: FontWeight.w600,
            ),
          ),
        ],
      ),
    );
  }
}
