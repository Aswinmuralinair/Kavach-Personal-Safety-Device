import 'dart:async';
import 'package:flutter/material.dart';
import '../../services/api_service.dart';

class GuardianManageScreen extends StatefulWidget {
  const GuardianManageScreen({super.key});

  @override
  State<GuardianManageScreen> createState() => _GuardianManageScreenState();
}

class _GuardianManageScreenState extends State<GuardianManageScreen> {
  final _deviceIdController = TextEditingController();
  bool _loading = true;
  bool _inviting = false;
  String? _error;
  List<Map<String, dynamic>> _links = [];
  Timer? _refreshTimer;

  @override
  void initState() {
    super.initState();
    _loadLinks();
    _refreshTimer = Timer.periodic(
      const Duration(seconds: 10),
      (_) => _loadLinks(),
    );
  }

  @override
  void dispose() {
    _refreshTimer?.cancel();
    _deviceIdController.dispose();
    super.dispose();
  }

  Future<void> _loadLinks() async {
    try {
      final result = await ApiService.getGuardianLinks();
      if (result['status'] == 'ok' && mounted) {
        setState(() {
          _links = List<Map<String, dynamic>>.from(result['links'] ?? []);
          _loading = false;
          _error = null;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = 'Could not load guardian links';
          _loading = false;
        });
      }
    }
  }

  Future<void> _inviteGuardian() async {
    final deviceId = _deviceIdController.text.trim();
    if (deviceId.isEmpty) return;

    setState(() => _inviting = true);
    try {
      final result = await ApiService.inviteGuardian(deviceId);
      if (mounted) {
        if (result['status'] == 'ok') {
          _deviceIdController.clear();
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              content: Text('Invite sent to $deviceId'),
              backgroundColor: Colors.green,
            ),
          );
          _loadLinks();
        } else {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              content: Text(result['message'] ?? 'Failed to send invite'),
              backgroundColor: Colors.red,
            ),
          );
        }
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Network error')),
        );
      }
    }
    if (mounted) setState(() => _inviting = false);
  }

  Future<void> _revokeLink(int linkId) async {
    final confirm = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Revoke Guardian'),
        content: const Text('Are you sure you want to remove this guardian?'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: TextButton.styleFrom(foregroundColor: Colors.red),
            child: const Text('Revoke'),
          ),
        ],
      ),
    );

    if (confirm != true) return;

    try {
      final result = await ApiService.revokeGuardian(linkId);
      if (mounted) {
        if (result['status'] == 'ok') {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(
              content: Text('Guardian revoked'),
              backgroundColor: Colors.orange,
            ),
          );
          _loadLinks();
        }
      }
    } catch (_) {}
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Scaffold(
      appBar: AppBar(title: const Text('Guardian Management')),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : RefreshIndicator(
              onRefresh: _loadLinks,
              child: ListView(
                padding: const EdgeInsets.all(16),
                children: [
                  // ── Invite Form ──────────────────────────────────────
                  Card(
                    child: Padding(
                      padding: const EdgeInsets.all(16),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            'Invite Guardian',
                            style: theme.textTheme.titleMedium?.copyWith(
                              fontWeight: FontWeight.bold,
                            ),
                          ),
                          const SizedBox(height: 4),
                          Text(
                            'Enter the device ID that the guardian signed up with.',
                            style: theme.textTheme.bodySmall,
                          ),
                          const SizedBox(height: 12),
                          Row(
                            children: [
                              Expanded(
                                child: TextField(
                                  controller: _deviceIdController,
                                  decoration: const InputDecoration(
                                    hintText: 'e.g. KAVACH-001',
                                    prefixIcon: Icon(Icons.devices),
                                    isDense: true,
                                  ),
                                  textCapitalization:
                                      TextCapitalization.characters,
                                ),
                              ),
                              const SizedBox(width: 8),
                              ElevatedButton.icon(
                                onPressed: _inviting ? null : _inviteGuardian,
                                icon: _inviting
                                    ? const SizedBox(
                                        width: 16,
                                        height: 16,
                                        child: CircularProgressIndicator(
                                            strokeWidth: 2),
                                      )
                                    : const Icon(Icons.send),
                                label: const Text('Invite'),
                              ),
                            ],
                          ),
                        ],
                      ),
                    ),
                  ),
                  const SizedBox(height: 24),

                  // ── Links List ───────────────────────────────────────
                  Text(
                    'Your Guardians',
                    style: theme.textTheme.titleMedium?.copyWith(
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                  const SizedBox(height: 8),

                  if (_links.isEmpty)
                    Card(
                      child: Padding(
                        padding: const EdgeInsets.all(32),
                        child: Column(
                          children: [
                            Icon(Icons.people_outline,
                                size: 48, color: Colors.grey.shade400),
                            const SizedBox(height: 8),
                            const Text('No guardians yet. Invite one above!'),
                          ],
                        ),
                      ),
                    )
                  else
                    ..._links.map((link) => _GuardianLinkTile(
                          link: link,
                          onRevoke: () => _revokeLink(link['id']),
                        )),

                  if (_error != null) ...[
                    const SizedBox(height: 16),
                    Text(_error!,
                        style: TextStyle(color: theme.colorScheme.error)),
                  ],
                ],
              ),
            ),
    );
  }
}

class _GuardianLinkTile extends StatelessWidget {
  final Map<String, dynamic> link;
  final VoidCallback onRevoke;

  const _GuardianLinkTile({required this.link, required this.onRevoke});

  @override
  Widget build(BuildContext context) {
    final status = link['status'] ?? 'unknown';
    final guardianId = link['guardian_device_id'] ?? '?';
    final createdAt = link['created_at'] ?? '';

    Color statusColor;
    IconData statusIcon;
    switch (status) {
      case 'active':
        statusColor = Colors.green;
        statusIcon = Icons.check_circle;
        break;
      case 'pending':
        statusColor = Colors.orange;
        statusIcon = Icons.hourglass_top;
        break;
      case 'revoked':
        statusColor = Colors.red;
        statusIcon = Icons.cancel;
        break;
      default:
        statusColor = Colors.grey;
        statusIcon = Icons.help_outline;
    }

    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        leading: CircleAvatar(
          backgroundColor: statusColor.withAlpha(25),
          child: Icon(statusIcon, color: statusColor),
        ),
        title: Text(
          guardianId,
          style: const TextStyle(fontWeight: FontWeight.w600),
        ),
        subtitle: Row(
          children: [
            Container(
              padding:
                  const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
              decoration: BoxDecoration(
                color: statusColor.withAlpha(25),
                borderRadius: BorderRadius.circular(8),
              ),
              child: Text(
                status.toUpperCase(),
                style: TextStyle(
                    fontSize: 11,
                    fontWeight: FontWeight.bold,
                    color: statusColor),
              ),
            ),
            const SizedBox(width: 8),
            Flexible(
              child: Text(
                createdAt.length > 10 ? createdAt.substring(0, 10) : createdAt,
                style: const TextStyle(fontSize: 11),
                overflow: TextOverflow.ellipsis,
              ),
            ),
          ],
        ),
        trailing: status == 'active'
            ? IconButton(
                icon: const Icon(Icons.person_remove, color: Colors.red),
                tooltip: 'Revoke',
                onPressed: onRevoke,
              )
            : null,
      ),
    );
  }
}
