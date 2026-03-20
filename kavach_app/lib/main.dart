import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'screens/login_screen.dart';
import 'screens/user/user_home_screen.dart';
import 'screens/guardian/guardian_home_screen.dart';
import 'services/notification_service.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await NotificationService.init();
  runApp(const KavachApp());
}

class KavachApp extends StatelessWidget {
  const KavachApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Kavach',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorSchemeSeed: const Color(0xFFE53935),
        brightness: Brightness.light,
        textTheme: GoogleFonts.poppinsTextTheme(),
        appBarTheme: const AppBarTheme(
          centerTitle: true,
          elevation: 0,
        ),
        cardTheme: CardThemeData(
          elevation: 2,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
          ),
        ),
        inputDecorationTheme: InputDecorationTheme(
          border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(12),
          ),
          filled: true,
        ),
        elevatedButtonTheme: ElevatedButtonThemeData(
          style: ElevatedButton.styleFrom(
            padding: const EdgeInsets.symmetric(horizontal: 32, vertical: 14),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(12),
            ),
          ),
        ),
      ),
      home: const SplashScreen(),
    );
  }
}

/// Beautiful splash screen with Kavach logo and tagline.
class SplashScreen extends StatefulWidget {
  const SplashScreen({super.key});

  @override
  State<SplashScreen> createState() => _SplashScreenState();
}

class _SplashScreenState extends State<SplashScreen>
    with SingleTickerProviderStateMixin {
  late AnimationController _controller;
  late Animation<double> _fadeIn;
  late Animation<double> _scaleIn;
  late Animation<double> _captionFade;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      duration: const Duration(milliseconds: 2000),
      vsync: this,
    );

    _scaleIn = Tween<double>(begin: 0.8, end: 1.0).animate(
      CurvedAnimation(parent: _controller, curve: const Interval(0, 0.5, curve: Curves.easeOut)),
    );
    _fadeIn = Tween<double>(begin: 0.0, end: 1.0).animate(
      CurvedAnimation(parent: _controller, curve: const Interval(0, 0.4, curve: Curves.easeIn)),
    );
    _captionFade = Tween<double>(begin: 0.0, end: 1.0).animate(
      CurvedAnimation(parent: _controller, curve: const Interval(0.4, 0.8, curve: Curves.easeIn)),
    );

    _controller.forward();

    // Navigate after splash
    Future.delayed(const Duration(milliseconds: 2800), () {
      if (mounted) {
        Navigator.pushReplacement(
          context,
          PageRouteBuilder(
            pageBuilder: (_, __, ___) => const AuthGate(),
            transitionsBuilder: (_, anim, __, child) =>
                FadeTransition(opacity: anim, child: child),
            transitionDuration: const Duration(milliseconds: 500),
          ),
        );
      }
    });
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF0A0C10),
      body: Center(
        child: AnimatedBuilder(
          animation: _controller,
          builder: (context, child) {
            return Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                // Logo
                FadeTransition(
                  opacity: _fadeIn,
                  child: ScaleTransition(
                    scale: _scaleIn,
                    child: Container(
                      width: 130,
                      height: 130,
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(28),
                        boxShadow: [
                          BoxShadow(
                            color: const Color(0xFF00BCD4).withAlpha(60),
                            blurRadius: 50,
                            spreadRadius: 5,
                          ),
                        ],
                      ),
                      child: ClipRRect(
                        borderRadius: BorderRadius.circular(28),
                        child: Image.asset(
                          'assets/images/kavach_logo.jpeg',
                          fit: BoxFit.cover,
                          errorBuilder: (_, __, ___) => Container(
                            decoration: BoxDecoration(
                              gradient: const LinearGradient(
                                colors: [Color(0xFF00BCD4), Color(0xFF7C3AED)],
                                begin: Alignment.topLeft,
                                end: Alignment.bottomRight,
                              ),
                              borderRadius: BorderRadius.circular(28),
                            ),
                            child: const Center(
                              child: Text('K',
                                  style: TextStyle(
                                      fontSize: 56,
                                      fontWeight: FontWeight.w800,
                                      color: Colors.white)),
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
                const SizedBox(height: 28),

                // Title
                FadeTransition(
                  opacity: _fadeIn,
                  child: const Text(
                    'KAVACH',
                    style: TextStyle(
                      fontSize: 38,
                      fontWeight: FontWeight.w800,
                      color: Colors.white,
                      letterSpacing: 10,
                    ),
                  ),
                ),
                const SizedBox(height: 12),

                // Caption
                FadeTransition(
                  opacity: _captionFade,
                  child: const Text(
                    'Your Safety, Our Priority',
                    style: TextStyle(
                      fontSize: 14,
                      color: Color(0xFF00BCD4),
                      letterSpacing: 1.5,
                    ),
                  ),
                ),
                const SizedBox(height: 24),

                // Accent line
                FadeTransition(
                  opacity: _captionFade,
                  child: Container(
                    width: 60,
                    height: 2,
                    decoration: BoxDecoration(
                      gradient: const LinearGradient(
                        colors: [Colors.transparent, Color(0xFF00BCD4), Colors.transparent],
                      ),
                      borderRadius: BorderRadius.circular(1),
                    ),
                  ),
                ),
              ],
            );
          },
        ),
      ),
    );
  }
}

/// Checks if user is already logged in, routes accordingly
class AuthGate extends StatefulWidget {
  const AuthGate({super.key});

  @override
  State<AuthGate> createState() => _AuthGateState();
}

class _AuthGateState extends State<AuthGate> {
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _checkAuth();
  }

  Future<void> _checkAuth() async {
    // Request notification permission once UI is ready
    await NotificationService.requestPermission();

    final prefs = await SharedPreferences.getInstance();
    final token = prefs.getString('auth_token');
    final role = prefs.getString('role');

    if (!mounted) return;

    if (token != null && role != null) {
      NotificationService.startPolling(role: role);
      Navigator.pushReplacement(
        context,
        MaterialPageRoute(
          builder: (_) => role == 'user'
              ? const UserHomeScreen()
              : const GuardianHomeScreen(),
        ),
      );
    } else {
      setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Scaffold(
        body: Center(child: CircularProgressIndicator()),
      );
    }
    return const LoginScreen();
  }
}
