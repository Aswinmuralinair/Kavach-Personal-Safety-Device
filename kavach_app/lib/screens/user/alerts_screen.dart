import 'package:flutter/material.dart';
import '../../services/api_service.dart';
import '../../models/alert_model.dart';
import 'alert_detail_screen.dart';

class UserAlertsScreen extends StatefulWidget {
  const UserAlertsScreen({super.key});

  @override
  State<UserAlertsScreen> createState() => _UserAlertsScreenState();
}

class _UserAlertsScreenState extends State<UserAlertsScreen> {
  bool _loading = true;
  String? _error;
  List<AlertModel> _alerts = [];

  @override
  void initState() {
    super.initState();
    _loadAlerts();
  }

  Future<void> _loadAlerts() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final result = await ApiService.getUserAlerts();
      if (result['status'] == 'ok') {
        _alerts = (result['alerts'] as List)
            .map((a) => AlertModel.fromJson(a))
            .toList();
      } else {
        _error = result['message'] ?? 'Failed to load alerts';
      }
    } catch (e) {
      _error = 'Cannot connect to server';
    }
    if (mounted) setState(() => _loading = false);
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
            Text(_error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
            const SizedBox(height: 16),
            ElevatedButton(onPressed: _loadAlerts, child: const Text('Retry')),
          ],
        ),
      );
    }

    if (_alerts.isEmpty) {
      return Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.notifications_off_outlined,
                size: 64, color: Colors.grey.shade400),
            const SizedBox(height: 16),
            const Text('No alerts yet'),
          ],
        ),
      );
    }

    return RefreshIndicator(
      onRefresh: _loadAlerts,
      child: ListView.builder(
        padding: const EdgeInsets.all(16),
        itemCount: _alerts.length,
        itemBuilder: (context, index) {
          final alert = _alerts[index];
          return _AlertCard(
            alert: alert,
            onTap: () {
              Navigator.push(
                context,
                MaterialPageRoute(
                  builder: (_) => AlertDetailScreen(alertId: alert.id),
                ),
              );
            },
          );
        },
      ),
    );
  }
}

class _AlertCard extends StatelessWidget {
  final AlertModel alert;
  final VoidCallback onTap;

  const _AlertCard({required this.alert, required this.onTap});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isSOS = alert.alertType == 'SOS';
    final color = isSOS ? Colors.red : Colors.blue;

    return Card(
      margin: const EdgeInsets.only(bottom: 12),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(16),
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Container(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                    decoration: BoxDecoration(
                      color: color.withAlpha(25),
                      borderRadius: BorderRadius.circular(20),
                      border: Border.all(color: color.withAlpha(76)),
                    ),
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Icon(
                          isSOS ? Icons.sos : Icons.local_hospital,
                          size: 16,
                          color: color,
                        ),
                        const SizedBox(width: 4),
                        Text(
                          alert.alertType ?? 'ALERT',
                          style: TextStyle(
                            color: color,
                            fontWeight: FontWeight.bold,
                            fontSize: 12,
                          ),
                        ),
                      ],
                    ),
                  ),
                  const Spacer(),
                  Text(
                    '#${alert.id}',
                    style: theme.textTheme.bodySmall?.copyWith(
                      color: theme.colorScheme.onSurfaceVariant,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              Row(
                children: [
                  Icon(Icons.access_time, size: 16, color: Colors.grey.shade600),
                  const SizedBox(width: 4),
                  Text(alert.formattedTime,
                      style: theme.textTheme.bodySmall),
                  const SizedBox(width: 16),
                  Icon(Icons.touch_app, size: 16, color: Colors.grey.shade600),
                  const SizedBox(width: 4),
                  Text(alert.triggerSource ?? 'Unknown',
                      style: theme.textTheme.bodySmall),
                ],
              ),
              const SizedBox(height: 8),
              Row(
                children: [
                  _StatusChip(
                    icon: Icons.call,
                    label: 'Call',
                    active: alert.callPlacedStatus,
                  ),
                  const SizedBox(width: 8),
                  _StatusChip(
                    icon: Icons.sms,
                    label: 'SMS',
                    active: alert.guardianSmsStatus,
                  ),
                  const SizedBox(width: 8),
                  _StatusChip(
                    icon: Icons.location_on,
                    label: 'GPS',
                    active: alert.gpsLocation != null,
                  ),
                  if (alert.evidenceFiles.isNotEmpty) ...[
                    const SizedBox(width: 8),
                    _StatusChip(
                      icon: Icons.attach_file,
                      label: '${alert.evidenceFiles.length}',
                      active: true,
                    ),
                  ],
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _StatusChip extends StatelessWidget {
  final IconData icon;
  final String label;
  final bool active;

  const _StatusChip({
    required this.icon,
    required this.label,
    required this.active,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: active ? Colors.green.withAlpha(25) : Colors.grey.withAlpha(25),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon,
              size: 14, color: active ? Colors.green : Colors.grey.shade400),
          const SizedBox(width: 2),
          Text(
            label,
            style: TextStyle(
              fontSize: 11,
              color: active ? Colors.green : Colors.grey.shade400,
            ),
          ),
        ],
      ),
    );
  }
}
