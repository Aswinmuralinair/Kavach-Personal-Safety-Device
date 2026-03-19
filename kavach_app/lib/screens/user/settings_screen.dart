import 'package:flutter/material.dart';
import '../../services/api_service.dart';

class UserSettingsScreen extends StatefulWidget {
  const UserSettingsScreen({super.key});

  @override
  State<UserSettingsScreen> createState() => _UserSettingsScreenState();
}

class _UserSettingsScreenState extends State<UserSettingsScreen> {
  final _policeController = TextEditingController();
  final _guardianController = TextEditingController();
  final _medicalController = TextEditingController();
  final _whatsappController = TextEditingController();

  bool _loading = true;
  bool _saving = false;
  String? _error;
  String? _success;

  @override
  void initState() {
    super.initState();
    _loadConfig();
  }

  Future<void> _loadConfig() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final result = await ApiService.getConfig();
      if (result['status'] == 'ok') {
        final config = result['config'] as Map<String, dynamic>;
        _policeController.text = config['police_number'] ?? '';
        _guardianController.text = config['guardian_number'] ?? '';
        _medicalController.text = config['medical_number'] ?? '';
        _whatsappController.text = config['whatsapp_number'] ?? '';
      } else {
        _error = result['message'] ?? 'Failed to load config';
      }
    } catch (e) {
      _error = 'Cannot connect to server';
    }
    if (mounted) setState(() => _loading = false);
  }

  Future<void> _saveConfig() async {
    setState(() {
      _saving = true;
      _error = null;
      _success = null;
    });
    try {
      final result = await ApiService.updateConfig({
        'police_number': _policeController.text.trim(),
        'guardian_number': _guardianController.text.trim(),
        'medical_number': _medicalController.text.trim(),
        'whatsapp_number': _whatsappController.text.trim(),
      });
      if (result['status'] == 'ok') {
        _success = 'Settings saved! Changes will sync to your device.';
      } else {
        _error = result['message'] ?? 'Failed to save';
      }
    } catch (e) {
      _error = 'Cannot connect to server';
    }
    if (mounted) setState(() => _saving = false);
  }

  @override
  void dispose() {
    _policeController.dispose();
    _guardianController.dispose();
    _medicalController.dispose();
    _whatsappController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }

    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        // Header
        Card(
          color: theme.colorScheme.primaryContainer.withAlpha(76),
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Row(
              children: [
                Icon(Icons.info_outline,
                    color: theme.colorScheme.primary),
                const SizedBox(width: 12),
                Expanded(
                  child: Text(
                    'These numbers are used by your Kavach device when an alert is triggered. Changes sync to the Raspberry Pi automatically.',
                    style: theme.textTheme.bodySmall,
                  ),
                ),
              ],
            ),
          ),
        ),
        const SizedBox(height: 24),

        // Phone numbers
        Text(
          'Emergency Numbers',
          style: theme.textTheme.titleMedium
              ?.copyWith(fontWeight: FontWeight.bold),
        ),
        const SizedBox(height: 12),

        _PhoneField(
          controller: _policeController,
          label: 'Police Number',
          hint: 'e.g. 100',
          icon: Icons.local_police,
        ),
        const SizedBox(height: 12),

        _PhoneField(
          controller: _guardianController,
          label: 'Guardian Number',
          hint: 'e.g. +91XXXXXXXXXX',
          icon: Icons.family_restroom,
        ),
        const SizedBox(height: 12),

        _PhoneField(
          controller: _medicalController,
          label: 'Medical Number',
          hint: 'e.g. +91XXXXXXXXXX',
          icon: Icons.local_hospital,
        ),
        const SizedBox(height: 12),

        _PhoneField(
          controller: _whatsappController,
          label: 'WhatsApp Number',
          hint: 'e.g. +91XXXXXXXXXX',
          icon: Icons.chat,
        ),
        const SizedBox(height: 24),

        // Error/Success messages
        if (_error != null)
          Container(
            padding: const EdgeInsets.all(12),
            margin: const EdgeInsets.only(bottom: 12),
            decoration: BoxDecoration(
              color: theme.colorScheme.errorContainer,
              borderRadius: BorderRadius.circular(12),
            ),
            child: Row(
              children: [
                Icon(Icons.error_outline,
                    color: theme.colorScheme.error, size: 20),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(_error!,
                      style: TextStyle(color: theme.colorScheme.error)),
                ),
              ],
            ),
          ),

        if (_success != null)
          Container(
            padding: const EdgeInsets.all(12),
            margin: const EdgeInsets.only(bottom: 12),
            decoration: BoxDecoration(
              color: Colors.green.shade50,
              borderRadius: BorderRadius.circular(12),
            ),
            child: Row(
              children: [
                const Icon(Icons.check_circle, color: Colors.green, size: 20),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(_success!,
                      style: const TextStyle(color: Colors.green)),
                ),
              ],
            ),
          ),

        // Save button
        SizedBox(
          width: double.infinity,
          child: ElevatedButton.icon(
            onPressed: _saving ? null : _saveConfig,
            icon: _saving
                ? const SizedBox(
                    width: 20,
                    height: 20,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.save),
            label: Text(_saving ? 'Saving...' : 'Save Settings'),
            style: ElevatedButton.styleFrom(
              backgroundColor: theme.colorScheme.primary,
              foregroundColor: theme.colorScheme.onPrimary,
            ),
          ),
        ),
      ],
    );
  }
}

class _PhoneField extends StatelessWidget {
  final TextEditingController controller;
  final String label;
  final String hint;
  final IconData icon;

  const _PhoneField({
    required this.controller,
    required this.label,
    required this.hint,
    required this.icon,
  });

  @override
  Widget build(BuildContext context) {
    return TextField(
      controller: controller,
      keyboardType: TextInputType.phone,
      decoration: InputDecoration(
        labelText: label,
        hintText: hint,
        prefixIcon: Icon(icon),
      ),
    );
  }
}
