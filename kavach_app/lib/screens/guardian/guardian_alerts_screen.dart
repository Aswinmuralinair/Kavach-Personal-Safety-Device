import 'dart:async';
import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';
import '../../services/api_service.dart';
import '../../models/alert_model.dart';

class GuardianAlertsScreen extends StatefulWidget {
  const GuardianAlertsScreen({super.key});

  @override
  State<GuardianAlertsScreen> createState() => _GuardianAlertsScreenState();
}

class _GuardianAlertsScreenState extends State<GuardianAlertsScreen> {
  bool _loading = true;
  String? _error;
  List<AlertModel> _alerts = [];
  Timer? _refreshTimer;

  @override
  void initState() {
    super.initState();
    _loadAlerts();
    _refreshTimer = Timer.periodic(
      const Duration(seconds: 10),
      (_) => _loadAlerts(),
    );
  }

  @override
  void dispose() {
    _refreshTimer?.cancel();
    super.dispose();
  }

  Future<void> _loadAlerts() async {
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
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }

    if (_error != null) {
      return Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Text(_error!),
            const SizedBox(height: 16),
            ElevatedButton(
                onPressed: _loadAlerts, child: const Text('Retry')),
          ],
        ),
      );
    }

    if (_alerts.isEmpty) {
      return Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.shield, size: 64, color: Colors.green.shade300),
            const SizedBox(height: 16),
            const Text('No alerts. The user is safe.'),
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
          return _GuardianAlertCard(alert: alert);
        },
      ),
    );
  }
}

class _GuardianAlertCard extends StatefulWidget {
  final AlertModel alert;
  const _GuardianAlertCard({required this.alert});

  @override
  State<_GuardianAlertCard> createState() => _GuardianAlertCardState();
}

class _GuardianAlertCardState extends State<_GuardianAlertCard> {
  bool _expanded = false;
  bool _loadingEvidence = false;
  List<Map<String, dynamic>> _evidence = [];

  Future<void> _loadEvidence() async {
    if (_evidence.isNotEmpty) return;
    setState(() => _loadingEvidence = true);
    try {
      final result =
          await ApiService.getGuardianEvidence(widget.alert.id);
      if (result['status'] == 'ok') {
        _evidence =
            List<Map<String, dynamic>>.from(result['evidence'] ?? []);
      }
    } catch (_) {}
    if (mounted) setState(() => _loadingEvidence = false);
  }

  void _openFile(String signedUrl) async {
    // signedUrl is e.g. "/uploads/file.mp4?token=xxx" — prepend base URL
    final fullUrl = '${ApiService.baseUrl}$signedUrl';
    try {
      await launchUrl(Uri.parse(fullUrl), mode: LaunchMode.externalApplication);
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Could not open evidence file')),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final alert = widget.alert;
    final isSOS = alert.alertType == 'SOS';
    final color = isSOS ? Colors.red : Colors.blue;

    return Card(
      margin: const EdgeInsets.only(bottom: 12),
      child: Column(
        children: [
          // Main info
          InkWell(
            onTap: () {
              setState(() => _expanded = !_expanded);
              if (_expanded) _loadEvidence();
            },
            borderRadius: const BorderRadius.vertical(top: Radius.circular(16)),
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Container(
                        padding: const EdgeInsets.symmetric(
                            horizontal: 10, vertical: 4),
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
                      Text('#${alert.id}',
                          style: theme.textTheme.bodySmall),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      const Icon(Icons.access_time, size: 16),
                      const SizedBox(width: 4),
                      Text(alert.formattedTime),
                    ],
                  ),
                  if (alert.gpsLocation != null) ...[
                    const SizedBox(height: 4),
                    Row(
                      children: [
                        const Icon(Icons.location_on, size: 16),
                        const SizedBox(width: 4),
                        Expanded(
                          child: Text(
                            '${alert.gpsLocation!}${alert.locationSource != null ? '  (${alert.locationSource})' : ''}',
                            style: theme.textTheme.bodySmall,
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                      ],
                    ),
                  ],
                  const SizedBox(height: 4),
                  Row(
                    children: [
                      Icon(
                        _expanded
                            ? Icons.expand_less
                            : Icons.expand_more,
                        size: 20,
                        color: theme.colorScheme.primary,
                      ),
                      Text(
                        _expanded ? 'Hide details' : 'Show details',
                        style: TextStyle(
                          color: theme.colorScheme.primary,
                          fontSize: 12,
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),

          // Expanded details
          if (_expanded) ...[
            const Divider(height: 1),
            Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  // Status
                  _InfoRow('Trigger', alert.triggerSource ?? 'Unknown'),
                  _InfoRow('Call Placed',
                      alert.callPlacedStatus ? 'Yes' : 'No'),
                  _InfoRow('SMS Sent',
                      alert.guardianSmsStatus ? 'Yes' : 'No'),
                  if (alert.batteryPercentage != null)
                    _InfoRow('Battery',
                        '${alert.batteryPercentage!.toStringAsFixed(0)}%'),

                  // Evidence
                  if (_loadingEvidence)
                    const Padding(
                      padding: EdgeInsets.all(8),
                      child: Center(
                          child:
                              CircularProgressIndicator(strokeWidth: 2)),
                    )
                  else if (_evidence.isNotEmpty) ...[
                    const SizedBox(height: 8),
                    Text('Evidence Files',
                        style: theme.textTheme.titleSmall
                            ?.copyWith(fontWeight: FontWeight.bold)),
                    const SizedBox(height: 4),
                    ..._evidence.map((e) {
                      final fname = e['filename'] ?? '';
                      final exists = e['file_exists'] == true;
                      IconData icon = Icons.insert_drive_file;
                      if (fname.endsWith('.wav')) icon = Icons.audiotrack;
                      if (fname.endsWith('.h264') ||
                          fname.endsWith('.mp4')) {
                        icon = Icons.videocam;
                      }

                      return ListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        leading: Icon(icon, size: 20),
                        title: Text(fname,
                            style: const TextStyle(fontSize: 12)),
                        trailing: exists
                            ? IconButton(
                                icon: const Icon(Icons.open_in_new,
                                    size: 18),
                                onPressed: () => _openFile(e['url'] ?? '/uploads/$fname'),
                              )
                            : const Icon(Icons.error_outline,
                                size: 18, color: Colors.red),
                      );
                    }),
                  ] else if (alert.evidenceFiles.isEmpty)
                    const Text('No evidence files',
                        style: TextStyle(
                            fontSize: 12, color: Colors.grey)),
                ],
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _InfoRow extends StatelessWidget {
  final String label;
  final String value;
  const _InfoRow(this.label, this.value);

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(
        children: [
          Text('$label: ',
              style: const TextStyle(
                  fontWeight: FontWeight.w500, fontSize: 13)),
          Text(value, style: const TextStyle(fontSize: 13)),
        ],
      ),
    );
  }
}
