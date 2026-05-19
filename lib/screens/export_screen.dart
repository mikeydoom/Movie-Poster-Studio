import 'dart:io';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import '../services/app_state.dart';
import '../services/config_manager.dart';
import '../services/rr_exporter.dart';
import '../theme/retrowave.dart';
import '../widgets/neon_button.dart';
import '../widgets/neon_field.dart';
import '../widgets/neon_panel.dart';

class ExportScreen extends StatefulWidget {
  final AppState state;
  const ExportScreen({super.key, required this.state});

  @override
  State<ExportScreen> createState() => _ExportScreenState();
}

class _ExportScreenState extends State<ExportScreen> {
  late final TextEditingController _libRoot;
  late final TextEditingController _outDir;
  late final TextEditingController _ls;
  late final TextEditingController _lsc;
  late String _rarity;
  late String _standee;
  bool _running = false;
  RrExportResult? _last;

  @override
  void initState() {
    super.initState();
    final cfg = widget.state.config.rrExport;
    _libRoot = TextEditingController(text: ConfigManager.baseDir());
    _outDir = TextEditingController(text: cfg.outputDir);
    _ls = TextEditingController(text: cfg.defaultLs.toString());
    _lsc = TextEditingController(text: cfg.defaultLsc.toString());
    _rarity = cfg.rarity;
    _standee = cfg.standeeShape;
  }

  @override
  void dispose() {
    _libRoot.dispose();
    _outDir.dispose();
    _ls.dispose();
    _lsc.dispose();
    super.dispose();
  }

  void _persist() {
    final c = widget.state.config.rrExport;
    c.outputDir = _outDir.text.trim();
    c.rarity = _rarity;
    c.standeeShape = _standee;
    c.defaultLs = int.tryParse(_ls.text) ?? c.defaultLs;
    c.defaultLsc = int.tryParse(_lsc.text) ?? c.defaultLsc;
    widget.state.save();
  }

  Future<void> _pickOut() async {
    final dir = await FilePicker.platform.getDirectoryPath();
    if (dir != null) {
      setState(() => _outDir.text = dir);
      _persist();
    }
  }

  Future<void> _pickLib() async {
    final dir = await FilePicker.platform.getDirectoryPath();
    if (dir != null) {
      setState(() => _libRoot.text = dir);
    }
  }

  Future<void> _run() async {
    _persist();
    final lib = _libRoot.text.trim();
    final out = _outDir.text.trim();
    if (!Directory(lib).existsSync()) {
      _errorDialog('Library root does not exist: $lib');
      return;
    }
    setState(() {
      _running = true;
      _last = null;
    });
    widget.state.resetProgress();
    try {
      final res = await exportToRrWorkshop(
        RrExportConfig(
          libraryRoot: lib,
          outputDir: out,
          rarity: _rarity,
          standeeShape: _standee,
          defaultLs: int.tryParse(_ls.text) ?? 0,
          defaultLsc: int.tryParse(_lsc.text) ?? 4,
        ),
        widget.state.bus,
      );
      setState(() => _last = res);
    } catch (e) {
      widget.state.appendLog('Export error: $e');
      _errorDialog('Export failed: $e');
    } finally {
      if (mounted) setState(() => _running = false);
    }
  }

  void _errorDialog(String msg) {
    showDialog(
      context: context,
      builder: (_) => AlertDialog(
        backgroundColor: Retrowave.panel,
        shape: const RoundedRectangleBorder(
            borderRadius: BorderRadius.all(Radius.circular(2))),
        title: Text('ERROR', style: Retrowave.heading(Retrowave.fsSec, Retrowave.pink)),
        content: Text(msg, style: Retrowave.body()),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context),
              child: Text('OK', style: Retrowave.body(Retrowave.fsBody, Retrowave.cyan))),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      padding: const EdgeInsets.all(Retrowave.sp4),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          NeonPanel(
            title: 'PATHS',
            borderColor: Retrowave.cyan,
            child: Column(
              children: [
                Row(
                  crossAxisAlignment: CrossAxisAlignment.end,
                  children: [
                    Expanded(
                        child: NeonField(
                            controller: _libRoot,
                            label: 'Library Root (contains "converted posters/" + "nr converted posters/")')),
                    const SizedBox(width: Retrowave.sp3),
                    NeonButton(label: 'Browse', icon: Icons.folder_open, onPressed: _pickLib),
                  ],
                ),
                const SizedBox(height: Retrowave.sp2),
                Row(
                  crossAxisAlignment: CrossAxisAlignment.end,
                  children: [
                    Expanded(
                        child: NeonField(
                            controller: _outDir,
                            label: 'Output Folder (custom_slots.json / nr_custom_slots.json / replacements.json)')),
                    const SizedBox(width: Retrowave.sp3),
                    NeonButton(label: 'Browse', icon: Icons.folder_open, onPressed: _pickOut),
                  ],
                ),
              ],
            ),
          ),
          const SizedBox(height: Retrowave.sp3),
          NeonPanel(
            title: 'SLOT DEFAULTS',
            borderColor: Retrowave.pink,
            child: Wrap(
              spacing: Retrowave.sp4,
              runSpacing: Retrowave.sp3,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                NeonDropdown<String>(
                  value: _rarity,
                  items: const [
                    'Common',
                    'Common (Old)',
                    'Limited Edition (holo)',
                    'Random',
                  ],
                  onChanged: (v) => setState(() {
                    _rarity = v ?? _rarity;
                    _persist();
                  }),
                  label: 'Rarity',
                  width: 220,
                ),
                NeonDropdown<String>(
                  value: _standee,
                  items: const ['A', 'B', 'C'],
                  onChanged: (v) => setState(() {
                    _standee = v ?? _standee;
                    _persist();
                  }),
                  label: 'NR Standee Shape',
                  width: 140,
                ),
              ],
            ),
          ),
          // LayoutStyle (ls) + LayoutStyleColor (lsc) pickers were removed
          // pending a better integration. The values still ship to RR Workshop
          // via the config defaults (defaultLs / defaultLsc) — read by _run()
          // through the still-present _ls / _lsc text controllers. No UI to
          // change them at the moment.
          const SizedBox(height: Retrowave.sp4),
          Center(
            child: NeonButton(
              label: _running ? 'Exporting…' : 'Export to RR Workshop',
              icon: Icons.ios_share,
              primary: true,
              padding: const EdgeInsets.symmetric(
                  horizontal: Retrowave.sp6, vertical: Retrowave.sp3),
              onPressed: _running ? null : _run,
            ),
          ),
          if (_last != null) ...[
            const SizedBox(height: Retrowave.sp4),
            _ResultPanel(result: _last!),
          ],
          const SizedBox(height: Retrowave.sp6),
        ],
      ),
    );
  }
}

class _ResultPanel extends StatelessWidget {
  final RrExportResult result;
  const _ResultPanel({required this.result});

  @override
  Widget build(BuildContext context) {
    return NeonPanel(
      title: 'EXPORT RESULT',
      borderColor: Retrowave.cyan,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _row('Genre-shelf slots', '${result.customSlots}', Retrowave.cyan),
          _row('New Release slots', '${result.nrSlots}', Retrowave.pink),
          _row('replacements.json entries', '${result.replacements}', Retrowave.gold),
          const SizedBox(height: Retrowave.sp2),
          Text(result.customSlotsPath, style: Retrowave.body(Retrowave.fsMeta, Retrowave.text2)),
          Text(result.nrSlotsPath, style: Retrowave.body(Retrowave.fsMeta, Retrowave.text2)),
          Text(result.replacementsPath, style: Retrowave.body(Retrowave.fsMeta, Retrowave.text2)),
          Text(result.editedSlotsPath, style: Retrowave.body(Retrowave.fsMeta, Retrowave.text2)),
          if (result.warnings.isNotEmpty) ...[
            const SizedBox(height: Retrowave.sp3),
            Text('${result.warnings.length} WARNING(S)',
                style: Retrowave.heading(Retrowave.fsBody, Retrowave.gold)),
            const SizedBox(height: Retrowave.sp1),
            for (final w in result.warnings.take(12))
              Text('  ! $w', style: Retrowave.body(Retrowave.fsMeta, Retrowave.text2)),
            if (result.warnings.length > 12)
              Text('  … ${result.warnings.length - 12} more',
                  style: Retrowave.body(Retrowave.fsMeta, Retrowave.text3)),
          ],
        ],
      ),
    );
  }

  Widget _row(String label, String value, Color color) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Row(
        children: [
          Container(width: 4, height: 18, color: color),
          const SizedBox(width: Retrowave.sp2),
          Expanded(child: Text(label.toUpperCase(), style: Retrowave.label())),
          Text(value, style: Retrowave.heading(Retrowave.fsBody, color)),
        ],
      ),
    );
  }
}
