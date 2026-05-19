import 'dart:async';

class ProgressBus {
  final _logCtrl = StreamController<String>.broadcast();
  final _stepCtrl = StreamController<Progress>.broadcast();
  final _fileCtrl = StreamController<Progress>.broadcast();
  final _doneCtrl = StreamController<String>.broadcast();

  Stream<String> get log => _logCtrl.stream;
  Stream<Progress> get step => _stepCtrl.stream;
  Stream<Progress> get file => _fileCtrl.stream;
  Stream<String> get done => _doneCtrl.stream;

  void emitLog(String msg) => _logCtrl.add(msg);
  void emitStep(int current, int total, [String label = '']) =>
      _stepCtrl.add(Progress(current, total, label));
  void emitFile(int current, int total) => _fileCtrl.add(Progress(current, total));
  void emitDone([String tag = '']) => _doneCtrl.add(tag);

  void dispose() {
    _logCtrl.close();
    _stepCtrl.close();
    _fileCtrl.close();
    _doneCtrl.close();
  }
}

class Progress {
  final int current;
  final int total;
  final String label;
  Progress(this.current, this.total, [this.label = '']);
  double get value => total == 0 ? 0 : current / total;
}
