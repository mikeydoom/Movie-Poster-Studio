import 'dart:async';
import 'dart:io';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:path/path.dart' as p;
import '../services/app_state.dart';
import '../services/config_manager.dart';
import '../services/poster_converter.dart';
import '../theme/retrowave.dart';
import '../widgets/neon_button.dart';
import '../widgets/neon_field.dart';
import '../widgets/neon_panel.dart';
import '../widgets/retro_progress.dart';

class ConverterScreen extends StatefulWidget {
  final AppState state;
  const ConverterScreen({super.key, required this.state});

  @override
  State<ConverterScreen> createState() => _ConverterScreenState();
}

class _ConverterScreenState extends State<ConverterScreen> {
  late final TextEditingController _src;
  late final TextEditingController _out;
  late final TextEditingController _normOverlay;
  late final TextEditingController _nrOverlay;
  late String _profile;
  late bool _overwrite;

  // Live session state shown in the status panel.
  final List<String> _sessionLog = [];
  StreamSubscription<String>? _logSub;
  StreamSubscription<String>? _doneSub;
  bool _finished = false;
  int _errorLines = 0;
  String _finalSummary = '';

  @override
  void initState() {
    super.initState();
    final c = widget.state.config.posterConverter;
    _src = TextEditingController(text: c.sourceFolder);
    _out = TextEditingController(text: c.outputFolder);
    _normOverlay = TextEditingController(text: c.normalOverlay);
    _nrOverlay = TextEditingController(text: c.nrOverlay);
    _profile = c.profile;
    _overwrite = c.overwrite;
  }

  @override
  void dispose() {
    _logSub?.cancel();
    _doneSub?.cancel();
    _src.dispose();
    _out.dispose();
    _normOverlay.dispose();
    _nrOverlay.dispose();
    super.dispose();
  }

  /// `posters/generic/` for generic, `posters/new_releases/` for new_release.
  /// Anchored to the app's base dir so portability is preserved.
  String _defaultSrcFor(String profile) => p.join(
        ConfigManager.baseDir(),
        'posters',
        profile == 'generic' ? 'generic' : 'new_releases',
      );

  void _onProfileChange(String? v) {
    if (v == null) return;
    setState(() {
      _profile = v;
      // Always reset the source to the profile's canonical folder when the
      // user toggles the dropdown. Previously this was conditional ("only
      // update if source is empty or missing"), which let the source field
      // stick to the wrong profile's folder after a switch. If the user
      // wants a custom source for this profile, they can Browse again after
      // switching — explicit and recoverable.
      _src.text = _defaultSrcFor(v);
    });
    _persist();
  }

  Future<void> _pickDir(TextEditingController target) async {
    final dir = await FilePicker.platform.getDirectoryPath();
    if (dir != null) {
      setState(() => target.text = dir);
      _persist();
    }
  }

  void _persist() {
    final c = widget.state.config.posterConverter;
    c.sourceFolder = _src.text.trim();
    c.outputFolder = _out.text.trim();
    c.normalOverlay = _normOverlay.text.trim();
    c.nrOverlay = _nrOverlay.text.trim();
    c.profile = _profile;
    c.overwrite = _overwrite;
    widget.state.save();
  }

  /// Validate paths and overlay-folder contents BEFORE handing off to the
  /// background runner. Returns null on success, or a user-facing error
  /// string. Doing the checks up-front means hard failures land in a dialog
  /// the user actually sees, rather than the hidden log buffer.
  String? _preflight({
    required String src,
    required String overlayFolder,
  }) {
    if (!Directory(src).existsSync()) {
      return 'Source folder does not exist:\n$src';
    }
    final genreFolders = Directory(src)
        .listSync()
        .whereType<Directory>()
        .toList();
    final nested = Directory(p.join(src, 'posters'));
    if (genreFolders.isEmpty && !nested.existsSync()) {
      return 'Source folder has no genre subfolders:\n$src\n\n'
          'Expected layout: <src>/<genre>/*.png (e.g. action, comedy, …).';
    }
    if (!Directory(overlayFolder).existsSync()) {
      return 'Overlay folder not found:\n$overlayFolder';
    }
    final ov1 = File(p.join(overlayFolder, 'overlay1.png'));
    if (!ov1.existsSync()) {
      return 'Overlay folder is missing overlay1.png:\n$overlayFolder\n\n'
          'Place overlay1.png (dark variant) in this folder. '
          'overlay2.png (light variant) is optional — it will be auto-inverted from overlay1 if missing.';
    }
    return null;
  }

  Future<void> _start() async {
    final base = ConfigManager.baseDir();
    // Source is whatever's in the field — driven by the profile dropdown
    // (set in _onProfileChange) or the user's explicit Browse. No silent
    // overrides here; if they pointed it somewhere unexpected, the
    // preflight check will catch it and surface a dialog.
    final src = _src.text.trim();
    final String out;
    final String overlay;

    if (_profile == 'generic') {
      out = _out.text.trim().isEmpty
          ? p.join(base, 'posters', 'converted', 'generic')
          : _out.text.trim();
      _out.text = out;
      overlay = _normOverlay.text.trim();
    } else {
      out = p.join(base, 'posters', 'converted', 'new_releases');
      overlay = _nrOverlay.text.trim();
    }

    final err = _preflight(src: src, overlayFolder: overlay);
    if (err != null) {
      _showError(err);
      return;
    }

    _persist();
    final s = widget.state;
    s.appendLog('=== Poster Converter Started ===');
    s.resetProgress();
    s.converterRunning = true;

    setState(() {
      _sessionLog.clear();
      _finished = false;
      _errorLines = 0;
      _finalSummary = '';
    });

    _logSub?.cancel();
    _doneSub?.cancel();
    _logSub = s.bus.log.listen((line) {
      if (!mounted) return;
      setState(() {
        _sessionLog.add(line);
        if (_sessionLog.length > 200) {
          _sessionLog.removeRange(0, _sessionLog.length - 200);
        }
        if (line.toUpperCase().contains('ERROR')) _errorLines++;
      });
    });
    _doneSub = s.bus.done.listen((tag) {
      if (!tag.startsWith('converter') || !mounted) return;
      final parts = tag.split(':');
      final count = parts.length > 1 ? int.tryParse(parts[1]) : null;
      setState(() {
        _finished = true;
        _finalSummary = _buildSummary(count);
      });
    });

    runConverter(
      srcFolder: src,
      outFolder: out,
      overlaysFolder: overlay,
      profile: _profile,
      overwrite: _overwrite,
      bus: s.bus,
    );
  }

  String _buildSummary(int? count) {
    if (count != null) {
      if (_errorLines == 0) return '✓ $count poster(s) converted, no errors.';
      return '⚠ $count poster(s) converted, $_errorLines line(s) flagged ERROR.';
    }
    if (_errorLines > 0) return '✗ Converter exited with errors — see log below.';
    return 'Converter finished.';
  }

  void _showError(String msg) {
    showDialog(
      context: context,
      builder: (_) => AlertDialog(
        backgroundColor: Retrowave.panel,
        shape: const RoundedRectangleBorder(
            borderRadius: BorderRadius.all(Radius.circular(2))),
        title: Text('ERROR',
            style: Retrowave.heading(Retrowave.fsSec, Retrowave.pink)),
        content: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 480),
          child: Text(msg, style: Retrowave.body()),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: Text('OK',
                style: Retrowave.body(Retrowave.fsBody, Retrowave.cyan)),
          )
        ],
      ),
    );
  }

  Widget _row(String label, TextEditingController c, VoidCallback browse) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          Expanded(
              child: NeonField(
                  controller: c, label: label, onChanged: (_) => _persist())),
          const SizedBox(width: 12),
          NeonButton(label: 'Browse', icon: Icons.folder_open, onPressed: browse),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.state;
    final running = s.converterRunning;
    final showStatus = running || _finished || _sessionLog.isNotEmpty;

    return SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          NeonPanel(
            title: 'FOLDERS',
            borderColor: Retrowave.cyan,
            child: Column(
              children: [
                _row('Source Folder', _src, () => _pickDir(_src)),
                _row('Output Folder (Generic)', _out, () => _pickDir(_out)),
                _row('overlays/generic', _normOverlay,
                    () => _pickDir(_normOverlay)),
                _row('overlays/new_releases', _nrOverlay,
                    () => _pickDir(_nrOverlay)),
              ],
            ),
          ),
          const SizedBox(height: 16),
          NeonPanel(
            title: 'OPTIONS',
            borderColor: Retrowave.magenta,
            child: Wrap(
              spacing: 24,
              runSpacing: 12,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                NeonDropdown<String>(
                  value: _profile,
                  items: const ['generic', 'new_release'],
                  display: (k) =>
                      k == 'generic' ? 'Generic' : 'New Releases',
                  onChanged: _onProfileChange,
                  label: 'Profile',
                  width: 180,
                ),
                Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Checkbox(
                      value: _overwrite,
                      onChanged: (v) {
                        setState(() => _overwrite = v ?? false);
                        _persist();
                      },
                      side: const BorderSide(color: Retrowave.cyan, width: 2),
                      activeColor: Retrowave.magenta,
                      checkColor: Colors.black,
                    ),
                    Text('OVERWRITE EXISTING', style: Retrowave.label(12)),
                  ],
                ),
              ],
            ),
          ),
          const SizedBox(height: 20),
          Center(
            child: NeonButton(
              label: running ? 'Converting…' : 'Start Conversion',
              icon: Icons.auto_awesome,
              primary: true,
              padding: const EdgeInsets.symmetric(horizontal: 32, vertical: 14),
              onPressed: running ? null : _start,
            ),
          ),
          if (showStatus) ...[
            const SizedBox(height: 20),
            _StatusPanel(
              state: s,
              sessionLog: _sessionLog,
              finished: _finished,
              finalSummary: _finalSummary,
              errorLines: _errorLines,
            ),
          ],
          const SizedBox(height: 24),
        ],
      ),
    );
  }
}

class _StatusPanel extends StatelessWidget {
  final AppState state;
  final List<String> sessionLog;
  final bool finished;
  final String finalSummary;
  final int errorLines;

  const _StatusPanel({
    required this.state,
    required this.sessionLog,
    required this.finished,
    required this.finalSummary,
    required this.errorLines,
  });

  @override
  Widget build(BuildContext context) {
    final running = state.converterRunning;
    final accent = errorLines > 0
        ? Retrowave.pink
        : (finished ? Retrowave.cyan : Retrowave.gold);
    final headline = running
        ? 'CONVERTING…'
        : (finished
            ? (finalSummary.isEmpty ? 'FINISHED' : finalSummary)
            : 'IDLE');

    return NeonPanel(
      title: 'CONVERTER STATUS',
      borderColor: accent,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Container(width: 4, height: 18, color: accent),
              const SizedBox(width: 8),
              Expanded(
                child: Text(headline,
                    style: Retrowave.heading(Retrowave.fsBody, accent)),
              ),
            ],
          ),
          const SizedBox(height: 8),
          RetroProgressBar(
            label: 'Stage',
            value: state.stepProgress,
            color: Retrowave.cyan,
            current: state.stepCurrent,
            total: state.stepTotal,
          ),
          const SizedBox(height: 6),
          RetroProgressBar(
            label: 'File',
            value: state.fileProgress,
            color: Retrowave.pink,
            current: state.fileCurrent,
            total: state.fileTotal,
          ),
          const SizedBox(height: 10),
          Container(
            constraints: const BoxConstraints(maxHeight: 220),
            decoration: BoxDecoration(
              color: Retrowave.bgDeep,
              border: Border.all(color: Retrowave.border),
            ),
            padding: const EdgeInsets.all(8),
            child: sessionLog.isEmpty
                ? Text('Waiting for output…',
                    style: Retrowave.body(Retrowave.fsMeta, Retrowave.text3))
                : Scrollbar(
                    child: ListView.builder(
                      reverse: true,
                      itemCount: sessionLog.length,
                      itemBuilder: (ctx, i) {
                        final line = sessionLog[sessionLog.length - 1 - i];
                        final isErr = line.toUpperCase().contains('ERROR');
                        return Text(
                          line,
                          style: Retrowave.body(
                              Retrowave.fsMeta,
                              isErr ? Retrowave.pink : Retrowave.text2),
                          maxLines: 3,
                          overflow: TextOverflow.ellipsis,
                        );
                      },
                    ),
                  ),
          ),
        ],
      ),
    );
  }
}
