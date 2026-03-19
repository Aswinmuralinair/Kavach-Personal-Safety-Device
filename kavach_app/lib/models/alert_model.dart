class AlertModel {
  final int id;
  final String deviceId;
  final String? timestamp;
  final String? alertType;
  final String? triggerSource;
  final bool callPlacedStatus;
  final bool guardianSmsStatus;
  final bool locationSmsStatus;
  final String? gpsLocation;
  final double? batteryPercentage;
  final String? uploadedFiles;
  final String? fileHashes;

  AlertModel({
    required this.id,
    required this.deviceId,
    this.timestamp,
    this.alertType,
    this.triggerSource,
    this.callPlacedStatus = false,
    this.guardianSmsStatus = false,
    this.locationSmsStatus = false,
    this.gpsLocation,
    this.batteryPercentage,
    this.uploadedFiles,
    this.fileHashes,
  });

  factory AlertModel.fromJson(Map<String, dynamic> json) {
    return AlertModel(
      id: json['id'] ?? 0,
      deviceId: json['device_id'] ?? '',
      timestamp: json['timestamp'],
      alertType: json['alert_type'],
      triggerSource: json['trigger_source'],
      callPlacedStatus: json['call_placed_status'] == true,
      guardianSmsStatus: json['guardian_sms_status'] == true,
      locationSmsStatus: json['location_sms_status'] == true,
      gpsLocation: json['gps_location'],
      batteryPercentage: _parseBattery(json['battery_percentage']),
      uploadedFiles: json['uploaded_files'],
      fileHashes: json['file_hashes'],
    );
  }

  static double? _parseBattery(dynamic val) {
    if (val == null) return null;
    if (val is num) return val.toDouble();
    final s = val.toString().replaceAll('%', '').trim();
    if (s.isEmpty || s == 'N/A' || s == 'Error') return null;
    return double.tryParse(s);
  }

  /// Parse "lat,lon" string to lat/lon doubles
  double? get latitude {
    if (gpsLocation == null || !gpsLocation!.contains(',')) return null;
    return double.tryParse(gpsLocation!.split(',')[0].trim());
  }

  double? get longitude {
    if (gpsLocation == null || !gpsLocation!.contains(',')) return null;
    return double.tryParse(gpsLocation!.split(',')[1].trim());
  }

  /// Get list of evidence filenames
  List<String> get evidenceFiles {
    if (uploadedFiles == null || uploadedFiles!.isEmpty) return [];
    return uploadedFiles!.split(',').map((f) => f.trim()).where((f) => f.isNotEmpty).toList();
  }

  /// Format timestamp for display
  String get formattedTime {
    if (timestamp == null) return 'Unknown';
    try {
      final dt = DateTime.parse(timestamp!);
      final now = DateTime.now();
      final diff = now.difference(dt);

      if (diff.inMinutes < 1) return 'Just now';
      if (diff.inMinutes < 60) return '${diff.inMinutes}m ago';
      if (diff.inHours < 24) return '${diff.inHours}h ago';
      if (diff.inDays < 7) return '${diff.inDays}d ago';

      return '${dt.day}/${dt.month}/${dt.year} ${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';
    } catch (_) {
      return timestamp!;
    }
  }
}
