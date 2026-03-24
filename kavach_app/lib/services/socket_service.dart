import 'dart:async';
import 'package:socket_io_client/socket_io_client.dart' as io;
import 'api_service.dart';

/// SocketIO service for real-time GPS streaming and alert notifications.
///
/// Connects to the Kavach server via WebSocket. Listens for:
///   - 'location' events: live GPS updates from the device
///   - 'alert' events: real-time alert broadcasts
///
/// Usage:
///   SocketService.connect(token);
///   SocketService.joinDevice('KAVACH-001');
///   SocketService.locationStream.listen((data) => ...);
///   SocketService.alertStream.listen((data) => ...);
///   SocketService.disconnect();
class SocketService {
  static io.Socket? _socket;
  static final _locationController =
      StreamController<Map<String, dynamic>>.broadcast();
  static final _alertController =
      StreamController<Map<String, dynamic>>.broadcast();

  /// Stream of real-time GPS updates from the device.
  static Stream<Map<String, dynamic>> get locationStream =>
      _locationController.stream;

  /// Stream of real-time alert events.
  static Stream<Map<String, dynamic>> get alertStream =>
      _alertController.stream;

  /// Connect to the SocketIO server with auth token.
  static void connect(String authToken) {
    if (_socket != null) return; // already connected

    _socket = io.io(
      ApiService.baseUrl,
      io.OptionBuilder()
          .setTransports(['websocket', 'polling'])
          .setAuth({'token': authToken})
          .enableAutoConnect()
          .enableReconnection()
          .build(),
    );

    _socket!.onConnect((_) {
      // ignore: avoid_print
      print('[SocketIO] Connected');
    });

    _socket!.on('location', (data) {
      if (data is Map<String, dynamic>) {
        _locationController.add(data);
      }
    });

    _socket!.on('alert', (data) {
      if (data is Map<String, dynamic>) {
        _alertController.add(data);
      }
    });

    _socket!.on('joined', (data) {
      // ignore: avoid_print
      print('[SocketIO] Joined device room: $data');
    });

    _socket!.onDisconnect((_) {
      // ignore: avoid_print
      print('[SocketIO] Disconnected');
    });
  }

  /// Subscribe to real-time updates for a specific device.
  static void joinDevice(String deviceId) {
    _socket?.emit('join_device', {'device_id': deviceId});
  }

  /// Disconnect from the SocketIO server.
  static void disconnect() {
    _socket?.disconnect();
    _socket?.dispose();
    _socket = null;
  }

  /// Check if currently connected.
  static bool get isConnected => _socket?.connected ?? false;
}
