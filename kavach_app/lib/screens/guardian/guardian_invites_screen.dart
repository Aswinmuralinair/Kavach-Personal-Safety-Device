import 'dart:async';
import 'package:flutter/material.dart';
import '../../services/api_service.dart';

class GuardianInvitesScreen extends StatefulWidget {
  const GuardianInvitesScreen({super.key});

  @override
  State<GuardianInvitesScreen> createState() => _GuardianInvitesScreenState();
}

class _GuardianInvitesScreenState extends State<GuardianInvitesScreen> {
  bool _loading = true;
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
    super.dispose();
  }

  Future<void> _loadLinks() async {
    try {
      final result = await ApiService.getGuardianLinks();
      if (result['status'] == 'ok' && mounted) {
        setState(() {
          _links = List<Map<String, dynamic>>.from(result['links'] ?? []);
          _loading = false;
        });
      }
    } catch (_) {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _respond(int linkId, bool accept) async {
    try {
      final result = await ApiService.respondToInvite(linkId, accept);
      if (mounted) {
        final action = accept ? 'Accepted' : 'Declined';
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text('$action invite'),
            backgroundColor: accept ? Colors.green : Colors.orange,
          ),
        );
        if (result['status'] == 'ok') _loadLinks();
      }
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Network error')),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final pending = _links.where((l) => l['status'] == 'pending').toList();
    final active = _links.where((l) => l['status'] == 'active').toList();

    return Scaffold(
      appBar: AppBar(title: const Text('Guardian Invites')),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : RefreshIndicator(
              onRefresh: _loadLinks,
              child: ListView(
                padding: const EdgeInsets.all(16),
                children: [
                  // ── Pending Invites ──────────────────────────────────
                  if (pending.isNotEmpty) ...[
                    Text(
                      'Pending Invites',
                      style: theme.textTheme.titleMedium?.copyWith(
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                    const SizedBox(height: 8),
                    ...pending.map((link) => Card(
                          margin: const EdgeInsets.only(bottom: 8),
                          child: Padding(
                            padding: const EdgeInsets.all(16),
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Row(
                                  children: [
                                    const Icon(Icons.mail_outline,
                                        color: Colors.orange),
                                    const SizedBox(width: 8),
                                    Expanded(
                                      child: Text(
                                        'Device ${link['user_device_id']} wants you as guardian',
                                        style: const TextStyle(
                                          fontWeight: FontWeight.w600,
                                          fontSize: 15,
                                        ),
                                      ),
                                    ),
                                  ],
                                ),
                                const SizedBox(height: 12),
                                Row(
                                  mainAxisAlignment: MainAxisAlignment.end,
                                  children: [
                                    OutlinedButton(
                                      onPressed: () =>
                                          _respond(link['id'], false),
                                      style: OutlinedButton.styleFrom(
                                        foregroundColor: Colors.red,
                                      ),
                                      child: const Text('Decline'),
                                    ),
                                    const SizedBox(width: 8),
                                    ElevatedButton.icon(
                                      onPressed: () =>
                                          _respond(link['id'], true),
                                      icon: const Icon(Icons.check),
                                      label: const Text('Accept'),
                                      style: ElevatedButton.styleFrom(
                                        backgroundColor: Colors.green,
                                        foregroundColor: Colors.white,
                                      ),
                                    ),
                                  ],
                                ),
                              ],
                            ),
                          ),
                        )),
                    const SizedBox(height: 16),
                  ],

                  // ── Active Guarding ──────────────────────────────────
                  Text(
                    'Actively Guarding',
                    style: theme.textTheme.titleMedium?.copyWith(
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                  const SizedBox(height: 8),

                  if (active.isEmpty)
                    Card(
                      child: Padding(
                        padding: const EdgeInsets.all(32),
                        child: Column(
                          children: [
                            Icon(Icons.shield_outlined,
                                size: 48, color: Colors.grey.shade400),
                            const SizedBox(height: 8),
                            const Text(
                                'No active guardian links.\nAccept an invite to start guarding.'),
                          ],
                        ),
                      ),
                    )
                  else
                    ...active.map((link) => Card(
                          margin: const EdgeInsets.only(bottom: 8),
                          child: ListTile(
                            leading: const CircleAvatar(
                              backgroundColor: Color(0x1A4CAF50),
                              child: Icon(Icons.shield,
                                  color: Colors.green),
                            ),
                            title: Text(
                              link['user_device_id'] ?? '?',
                              style: const TextStyle(
                                  fontWeight: FontWeight.w600),
                            ),
                            subtitle: const Text('Active'),
                          ),
                        )),

                  if (pending.isEmpty && active.isEmpty)
                    Card(
                      child: Padding(
                        padding: const EdgeInsets.all(32),
                        child: Column(
                          children: [
                            Icon(Icons.hourglass_empty,
                                size: 48, color: Colors.grey.shade400),
                            const SizedBox(height: 8),
                            const Text(
                                'No invites yet.\nThe device user will invite you from their app.'),
                          ],
                        ),
                      ),
                    ),
                ],
              ),
            ),
    );
  }
}
