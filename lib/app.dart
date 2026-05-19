import 'dart:math' as math;
import 'package:flutter/material.dart';
import 'screens/duplicates_screen.dart';
import 'screens/export_screen.dart';
import 'screens/gallery_screen.dart';
import 'screens/tmdb_screen.dart';
import 'services/app_state.dart';
import 'theme/retrowave.dart';
import 'widgets/retro_background.dart';
import 'widgets/retro_progress.dart';

const List<String> _credits = [
  'Advanno',
  'Detective 45',
  'eagleofsin',
  'Gundelack',
  'heelturnmerch',
  'KG',
  'MagicMastaBlasta',
  'meincologne',
  'Retro Kid',
];

/// Design baseline (matches RR_VHS_Tool's 1080p reference). The whole UI is
/// scaled by min(width/baseW, height/baseH), clamped to a sane range, so every
/// widget remains visible when the window shrinks below 1080p.
const double _baseDesignWidth = 1280;
const double _baseDesignHeight = 800;
const double _minScale = 0.7;
const double _maxScale = 1.4;

class MoviePosterStudioApp extends StatelessWidget {
  final AppState state;
  const MoviePosterStudioApp({super.key, required this.state});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Movie Poster Studio',
      debugShowCheckedModeBanner: false,
      theme: Retrowave.themeData(),
      builder: (ctx, child) {
        if (child == null) return const SizedBox.shrink();
        return LayoutBuilder(
          builder: (ctx, box) {
            final scale = math
                .min(box.maxWidth / _baseDesignWidth,
                    box.maxHeight / _baseDesignHeight)
                .clamp(_minScale, _maxScale);
            final mq = MediaQuery.of(ctx);
            return MediaQuery(
              data: mq.copyWith(textScaler: TextScaler.linear(scale)),
              child: child,
            );
          },
        );
      },
      home: HomeShell(state: state),
    );
  }
}

class HomeShell extends StatefulWidget {
  final AppState state;
  const HomeShell({super.key, required this.state});

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  int _index = 0;
  final GlobalKey<GalleryScreenState> _galleryKey = GlobalKey<GalleryScreenState>();
  late final List<_TabSpec> _tabs;

  @override
  void initState() {
    super.initState();
    _tabs = [
      _TabSpec('TMDB SCRAPER', Icons.cloud_download,
          (s) => TmdbScreen(state: s), Retrowave.cyan),
      _TabSpec('GALLERY', Icons.grid_view,
          (s) => GalleryScreen(key: _galleryKey, state: s), Retrowave.purple),
      _TabSpec('DUPLICATES', Icons.copy_all,
          (s) => DuplicatesScreen(state: s), Retrowave.hotPink),
      _TabSpec('RR EXPORT', Icons.ios_share,
          (s) => ExportScreen(state: s), Retrowave.gold),
    ];
    widget.state.addListener(_onStateChanged);
    widget.state.bus.done.listen((tag) {
      if (tag == 'tmdb' || tag == 'converter') {
        _galleryKey.currentState?.loadAll();
      }
    });
    if (!widget.state.config.hasShownCredits) {
      WidgetsBinding.instance.addPostFrameCallback((_) => _showCredits());
    }
  }

  Future<void> _showCredits() async {
    if (!mounted) return;
    await showDialog<void>(
      context: context,
      barrierDismissible: false,
      builder: (_) => const _CreditsDialog(),
    );
    widget.state.config.hasShownCredits = true;
    await widget.state.save();
  }

  void _onStateChanged() {
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    widget.state.removeListener(_onStateChanged);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.state;
    final showBottom = _index != 2; // hide log on gallery
    return Scaffold(
      backgroundColor: Retrowave.bg,
      body: RetroBackground(
        child: Column(
          children: [
            _TitleBar(),
            _TabBar(
              tabs: _tabs,
              activeIndex: _index,
              onTap: (i) => setState(() => _index = i),
            ),
            Expanded(
              child: IndexedStack(
                index: _index,
                children: [for (final t in _tabs) t.build(s)],
              ),
            ),
            if (showBottom) _BottomDock(state: s),
          ],
        ),
      ),
    );
  }
}

class _TabSpec {
  final String label;
  final IconData icon;
  final Widget Function(AppState) builder;
  final Color color;
  _TabSpec(this.label, this.icon, this.builder, this.color);
  Widget build(AppState s) => builder(s);
}

class _TitleBar extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(
          horizontal: Retrowave.sp4, vertical: Retrowave.sp2),
      decoration: const BoxDecoration(
        color: Retrowave.panel,
        border: Border(bottom: BorderSide(color: Retrowave.border)),
      ),
      child: Row(
        children: [
          Text('📼 ', style: TextStyle(fontSize: Retrowave.fsApp)),
          Text('MOVIE',
              style: Retrowave.heading(Retrowave.fsApp, Retrowave.pink)),
          const SizedBox(width: 4),
          Text('POSTER',
              style: Retrowave.heading(Retrowave.fsApp, Retrowave.cyan)),
          const SizedBox(width: 4),
          Text('STUDIO',
              style: Retrowave.heading(Retrowave.fsApp, Retrowave.text)),
          const Spacer(),
          Text('TMDB // RETRO REWIND', style: Retrowave.label()),
        ],
      ),
    );
  }
}

class _TabBar extends StatelessWidget {
  final List<_TabSpec> tabs;
  final int activeIndex;
  final ValueChanged<int> onTap;
  const _TabBar({required this.tabs, required this.activeIndex, required this.onTap});

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: const BoxDecoration(
        color: Retrowave.bg,
        border: Border(bottom: BorderSide(color: Retrowave.border)),
      ),
      padding: const EdgeInsets.symmetric(horizontal: Retrowave.sp2),
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        child: Row(
          children: [
            for (var i = 0; i < tabs.length; i++)
              _TabButton(
                  spec: tabs[i], active: i == activeIndex, onTap: () => onTap(i)),
          ],
        ),
      ),
    );
  }
}

class _TabButton extends StatefulWidget {
  final _TabSpec spec;
  final bool active;
  final VoidCallback onTap;
  const _TabButton({required this.spec, required this.active, required this.onTap});

  @override
  State<_TabButton> createState() => _TabButtonState();
}

class _TabButtonState extends State<_TabButton> {
  bool _hover = false;
  @override
  Widget build(BuildContext context) {
    final active = widget.active;
    final fg = active
        ? Retrowave.cyan
        : (_hover ? Retrowave.text : Retrowave.text3);
    return MouseRegion(
      cursor: SystemMouseCursors.click,
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: GestureDetector(
        onTap: widget.onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(
              horizontal: Retrowave.sp3, vertical: Retrowave.sp2),
          decoration: BoxDecoration(
            color: active ? Retrowave.panel : Colors.transparent,
            border: Border(
              bottom: BorderSide(
                color: active ? Retrowave.cyan : Colors.transparent,
                width: 2,
              ),
            ),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(widget.spec.icon, size: Retrowave.fsBody, color: fg),
              const SizedBox(width: Retrowave.sp2),
              Text(widget.spec.label,
                  style: Retrowave.heading(Retrowave.fsBody, fg)),
            ],
          ),
        ),
      ),
    );
  }
}

class _BottomDock extends StatelessWidget {
  final AppState state;
  const _BottomDock({required this.state});

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.fromLTRB(Retrowave.sp4, 0, Retrowave.sp4, 0),
      decoration: const BoxDecoration(
        color: Retrowave.panel,
        border: Border(top: BorderSide(color: Retrowave.border)),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          RetroProgressBar(
            label: state.stepLabel.isNotEmpty ? state.stepLabel : 'Stage',
            value: state.stepProgress,
            color: Retrowave.cyan,
            current: state.stepCurrent,
            total: state.stepTotal,
          ),
          const SizedBox(height: Retrowave.sp2),
          RetroProgressBar(
            label: 'File',
            value: state.fileProgress,
            color: Retrowave.pink,
            current: state.fileCurrent,
            total: state.fileTotal,
          ),
        ],
      ),
    );
  }
}

class _CreditsDialog extends StatelessWidget {
  const _CreditsDialog();

  @override
  Widget build(BuildContext context) {
    return Dialog(
      backgroundColor: Retrowave.panel,
      shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.all(Radius.circular(2)),
          side: BorderSide(color: Retrowave.cyan, width: 1)),
      child: SizedBox(
        width: 420,
        child: Padding(
          padding: const EdgeInsets.all(Retrowave.sp6),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text('MOVIE POSTER STUDIO',
                  style: Retrowave.display(Retrowave.fsApp),
                  textAlign: TextAlign.center),
              const SizedBox(height: Retrowave.sp1),
              Text('Built for the Retro Rewind community',
                  style: Retrowave.body(Retrowave.fsMeta, Retrowave.text2),
                  textAlign: TextAlign.center),
              const SizedBox(height: Retrowave.sp6),
              Container(
                height: 1,
                color: Retrowave.border,
              ),
              const SizedBox(height: Retrowave.sp4),
              Text('SPECIAL THANKS',
                  style: Retrowave.heading(Retrowave.fsMeta, Retrowave.gold),
                  textAlign: TextAlign.center),
              const SizedBox(height: Retrowave.sp3),
              for (final name in _credits) ...[
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 3),
                  child: Row(
                    children: [
                      Container(
                          width: 3, height: 14, color: Retrowave.cyan),
                      const SizedBox(width: Retrowave.sp3),
                      Text(name, style: Retrowave.body(Retrowave.fsBody)),
                    ],
                  ),
                ),
              ],
              const SizedBox(height: Retrowave.sp6),
              Container(height: 1, color: Retrowave.border),
              const SizedBox(height: Retrowave.sp4),
              TextButton(
                onPressed: () => Navigator.of(context).pop(),
                style: TextButton.styleFrom(
                  backgroundColor: Retrowave.cyan,
                  shape: const RoundedRectangleBorder(
                      borderRadius: BorderRadius.all(Radius.circular(2))),
                  padding: const EdgeInsets.symmetric(vertical: Retrowave.sp3),
                ),
                child: Text('LETS GO',
                    style: Retrowave.heading(Retrowave.fsBody, Retrowave.textInv)),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
