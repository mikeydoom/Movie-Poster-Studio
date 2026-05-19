import 'dart:io';
import 'package:flutter/material.dart';
import 'package:path/path.dart' as p;
import 'package:window_manager/window_manager.dart';
import 'app.dart';
import 'services/app_state.dart';
import 'services/config_manager.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  if (Platform.isWindows) {
    await windowManager.ensureInitialized();
    const opts = WindowOptions(
      size: Size(1400, 900),
      minimumSize: Size(1200, 700),
      center: true,
      title: 'Movie Poster Studio',
      backgroundColor: Color(0xFF05010F),
    );
    await windowManager.waitUntilReadyToShow(opts, () async {
      await windowManager.show();
      await windowManager.focus();
    });
  }

  // Portable install: every data folder + config.json lives next to the .exe.
  final exeDir = p.dirname(Platform.resolvedExecutable);
  ConfigManager.setBaseDir(exeDir);
  debugPrint('[mp] Library root: $exeDir');

  final cfg = await ConfigManager.load();
  final state = AppState(cfg);
  state.appendLog('Library root: $exeDir');

  runApp(MoviePosterStudioApp(state: state));
}
