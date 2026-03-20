import 'dart:async';
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../../services/api_service.dart';
import '../../models/alert_model.dart';
import 'alert_detail_screen.dart';

class UserDashboardScreen extends StatefulWidget {
  const UserDashboardScreen({super.key});

  @override
  State<UserDashboardScreen> createState() => _UserDashboardScreenState();
}

class _UserDashboardScreenState extends State<UserDashboardScreen> {
  bool _loading = true;
  String? _error;
  List<AlertModel> _allAlerts = [];
  List<AlertModel> _recentAlerts = [];
  Map<String, dynamic>? _health;

  // Live device status (battery + online/offline)
  String _deviceBattery = 'N/A';
  bool _deviceOnline = false;
  Timer? _statusTimer;
  String? _deviceId;

  @override
  void initState() {
    super.initState();
    _loadData();
    _initDeviceStatusPolling();
  }

  @override
  void dispose() {
    _statusTimer?.cancel();
    super.dispose();
  }

  /// Start polling device status every 10 seconds
  Future<void> _initDeviceStatusPolling() async {
    final prefs = await SharedPreferences.getInstance();
    _deviceId = prefs.getString('device_id') ?? 'KAVACH-001';

    // Fetch immediately, then every 10s
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
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final results = await Future.wait([
        ApiService.getUserAlerts(),
        ApiService.getHealth(),
      ]);

      final alertsData = results[0];
      final healthData = results[1];

      if (alertsData['status'] == 'ok') {
        final list = (alertsData['alerts'] as List)
            .map((a) => AlertModel.fromJson(a))
            .toList();
        _allAlerts = list;
        _recentAlerts = list.take(5).toList();
      }

      _health = healthData;
    } catch (e) {
      _error = 'Failed to connect to server';
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
            Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
            const SizedBox(height: 16),
            ElevatedButton.icon(
              onPressed: _loadData,
              icon: const Icon(Icons.refresh),
              label: const Text('Retry'),
            ),
          ],
        ),
      );
    }

    final totalAlerts = _health?['database']?['total_alerts'] ?? _allAlerts.length;

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

          // Stats row - use _allAlerts for accurate counts
          Row(
            children: [
              Expanded(
                child: _StatCard(
                  icon: Icons.warning_amber_rounded,
                  label: 'Total Alerts',
                  value: '$totalAlerts',
                  color: Colors.orange,
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: _StatCard(
                  icon: Icons.sos,
                  label: 'SOS Alerts',
                  value: '${_allAlerts.where((a) => a.alertType == 'SOS').length}',
                  color: Colors.red,
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: _StatCard(
                  icon: Icons.local_hospital,
                  label: 'Medical',
                  value: '${_allAlerts.where((a) => a.alertType == 'MEDICAL').length}',
                  color: Colors.blue,
                ),
              ),
            ],
          ),
          const SizedBox(height: 24),

          // Recent alerts
          Text(
            'Recent Alerts',
            style: theme.textTheme.titleMedium?.copyWith(
              fontWeight: FontWeight.bold,
            ),
          ),
          const SizedBox(height: 8),

          if (_recentAlerts.isEmpty)
            Card(
              child: Padding(
                padding: const EdgeInsets.all(32),
                child: Column(
                  children: [
                    Icon(Icons.check_circle_outline,
                        size: 48, color: Colors.green.shade300),
                    const SizedBox(height: 8),
                    const Text('No alerts yet. You\'re safe!'),
                  ],
                ),
              ),
            )
          else
            ...(_recentAlerts.map((alert) => _AlertTile(
                  alert: alert,
                  onTap: () {
                    Navigator.push(
                      context,
                      MaterialPageRoute(
                        builder: (_) =>
                            AlertDetailScreen(alertId: alert.id),
                      ),
                    );
                  },
                ))),
        ],
      ),
    );
  }
}

class _StatCard extends StatelessWidget {
  final IconData icon;
  final String label;
  final String value;
  final Color color;

  const _StatCard({
    required this.icon,
    required this.label,
    required this.value,
    required this.color,
  });

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          children: [
            Icon(icon, color: color, size: 28),
            const SizedBox(height: 8),
            Text(
              value,
              style: Theme.of(context).textTheme.headlineSmall?.copyWith(
                    fontWeight: FontWeight.bold,
                    color: color,
                  ),
            ),
            Text(
              label,
              style: Theme.of(context).textTheme.bodySmall,
              textAlign: TextAlign.center,
            ),
          ],
        ),
      ),
    );
  }
}

class _AlertTile extends StatelessWidget {
  final AlertModel alert;
  final VoidCallback onTap;

  const _AlertTile({required this.alert, required this.onTap});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isSOS = alert.alertType == 'SOS';

    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        onTap: onTap,
        leading: CircleAvatar(
          backgroundColor: isSOS ? Colors.red.shade100 : Colors.blue.shade100,
          child: Icon(
            isSOS ? Icons.sos : Icons.local_hospital,
            color: isSOS ? Colors.red : Colors.blue,
          ),
        ),
        title: Text(
          '${alert.alertType ?? 'Alert'} - #${alert.id}',
          style: const TextStyle(fontWeight: FontWeight.w600),
        ),
        subtitle: Text(
          '${alert.triggerSource ?? 'Unknown'} - ${alert.formattedTime}',
        ),
        trailing: Icon(
          Icons.chevron_right,
          color: theme.colorScheme.onSurfaceVariant,
        ),
      ),
    );
  }
}
