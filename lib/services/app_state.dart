import 'package:flutter/foundation.dart';
import 'config_manager.dart';
import 'progress_bus.dart';

class AppState extends ChangeNotifier {
  AppConfig config;
  final ProgressBus bus = ProgressBus();
  final List<String> log = [];
  double stepProgress = 0;
  double fileProgress = 0;
  int stepCurrent = 0;
  int stepTotal = 0;
  String stepLabel = '';
  int fileCurrent = 0;
  int fileTotal = 0;
  bool tmdbRunning = false;
  bool converterRunning = false;
  bool duplicatesRunning = false;

  AppState(this.config) {
    bus.log.listen((m) {
      log.add(m);
      if (log.length > 500) log.removeRange(0, log.length - 500);
      notifyListeners();
    });
    bus.step.listen((p) {
      stepProgress = p.value;
      stepCurrent = p.current;
      stepTotal = p.total;
      if (p.label.isNotEmpty) stepLabel = p.label;
      notifyListeners();
    });
    bus.file.listen((p) {
      fileProgress = p.value;
      fileCurrent = p.current;
      fileTotal = p.total;
      notifyListeners();
    });
    bus.done.listen((tag) {
      final base = tag.split(':').first;
      switch (base) {
        case 'tmdb':
          tmdbRunning = false;
          break;
        case 'converter':
          converterRunning = false;
          break;
        case 'duplicates':
          duplicatesRunning = false;
          break;
      }
      stepProgress = 0;
      fileProgress = 0;
      stepLabel = '';
      notifyListeners();
    });
  }

  Future<void> save() async {
    await ConfigManager.save(config);
  }

  void resetProgress() {
    stepProgress = 0;
    fileProgress = 0;
    stepCurrent = 0;
    stepTotal = 0;
    fileCurrent = 0;
    fileTotal = 0;
    stepLabel = '';
    notifyListeners();
  }

  void appendLog(String m) {
    log.add(m);
    notifyListeners();
  }

  @override
  void dispose() {
    bus.dispose();
    super.dispose();
  }
}
