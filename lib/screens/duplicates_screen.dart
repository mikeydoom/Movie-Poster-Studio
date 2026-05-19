import 'dart:io';
import 'package:flutter/material.dart';
import '../services/app_state.dart';
import '../services/config_manager.dart';
import '../services/duplicate_finder.dart';
import '../theme/retrowave.dart';
import '../widgets/neon_button.dart';

class DuplicatesScreen extends StatefulWidget {
  final AppState state;
  const DuplicatesScreen({super.key, required this.state});

  @override
  State<DuplicatesScreen> createState() => _DuplicatesScreenState();
}

class _DuplicatesScreenState extends State<DuplicatesScreen> {
  DupReport? _report;
  bool _scanning = false;
  String? _filterGenre; // null = ALL; otherwise lowercase folder name

  @override
  void initState() {
    super.initState();
    _scan();
  }

  // ── Genre helpers ─────────────────────────────────────────────────────────

  /// Cached sorted list of genres that appear in at least one duplicate group.
  /// Recomputed via [_updateGenreCache] whenever [_report] changes or a group
  /// is resolved — avoids iterating every group on every build pass.
  List<String> _cachedGenres = [];

  void _updateGenreCache() {
    final r = _report;
    if (r == null) {
      _cachedGenres = [];
      return;
    }
    final genres = <String>{};
    for (final g in [...r.crossSet, ...r.crossGenre]) {
      for (final loc in g.locations) {
        genres.add(loc.genre.toLowerCase());
      }
    }
    _cachedGenres = genres.toList()..sort();
  }

  bool _groupMatches(DupGroup g) {
    if (_filterGenre == null) return true;
    return g.locations.any((l) => l.genre.toLowerCase() == _filterGenre);
  }

  List<DupGroup> get _visibleSet =>
      _report?.crossSet.where(_groupMatches).toList() ?? [];

  List<DupGroup> get _visibleGenre =>
      _report?.crossGenre.where(_groupMatches).toList() ?? [];

  static String _displayGenre(String folderName) {
    final lower = folderName.toLowerCase();
    for (final k in Retrowave.genreColors.keys) {
      if (k.toLowerCase() == lower) return Retrowave.displayName(k);
    }
    return folderName.isEmpty
        ? folderName
        : folderName[0].toUpperCase() + folderName.substring(1);
  }

  // ── Scanning ──────────────────────────────────────────────────────────────

  Future<void> _scan() async {
    if (_scanning) return;
    setState(() {
      _scanning = true;
      _report = null;
      _filterGenre = null;
    });
    widget.state.duplicatesRunning = true;
    widget.state.appendLog('=== Duplicate Scan ===');
    widget.state.resetProgress();
    final report = await scanDuplicates(ConfigManager.baseDir(), widget.state.bus);
    if (!mounted) return;
    setState(() {
      _scanning = false;
      _report = report;
      _updateGenreCache();
    });
  }

  // ── Resolution — single group (no confirm needed, ~1-2 files) ─────────────

  Future<void> _resolveSet(DupGroup group, String keepPath) async {
    final toDelete = group.locations
        .where((l) => l.path != keepPath)
        .map((l) => l.path)
        .toList();
    await applyDeletions(toDelete, widget.state.bus);
    if (!mounted) return;
    setState(() {
      _report!.crossSet.remove(group);
      _updateGenreCache();
    });
  }

  Future<void> _resolveGenre(DupGroup group, String keepPath) async {
    final toDelete = group.locations
        .where((l) => l.path != keepPath)
        .map((l) => l.path)
        .toList();
    await applyDeletions(toDelete, widget.state.bus);
    if (!mounted) return;
    setState(() {
      _report!.crossGenre.remove(group);
      _updateGenreCache();
    });
  }

  // ── Bulk actions (confirm required) ───────────────────────────────────────

  Future<void> _keepAllGeneric() async {
    final groups = _visibleSet
        .where((g) => g.locations.any((l) => l.set == setGeneric))
        .toList();
    final toDelete = [
      for (final g in groups)
        ...g.locations.where((l) => l.set == setNewRelease).map((l) => l.path),
    ];
    if (toDelete.isEmpty) return;
    if (!await _confirmBulk(toDelete.length)) return;
    await applyDeletions(toDelete, widget.state.bus);
    if (!mounted) return;
    setState(() {
      for (final g in groups) { _report!.crossSet.remove(g); }
      _updateGenreCache();
    });
  }

  Future<void> _keepAllNr() async {
    final groups = _visibleSet
        .where((g) => g.locations.any((l) => l.set == setNewRelease))
        .toList();
    final toDelete = [
      for (final g in groups)
        ...g.locations.where((l) => l.set == setGeneric).map((l) => l.path),
    ];
    if (toDelete.isEmpty) return;
    if (!await _confirmBulk(toDelete.length)) return;
    await applyDeletions(toDelete, widget.state.bus);
    if (!mounted) return;
    setState(() {
      for (final g in groups) { _report!.crossSet.remove(g); }
      _updateGenreCache();
    });
  }

  Future<void> _cleanAllGenre() async {
    final groups = _visibleGenre;
    if (groups.isEmpty) return;
    final toDelete = [
      for (final g in groups) ...g.locations.skip(1).map((l) => l.path),
    ];
    if (toDelete.isEmpty) return;
    if (!await _confirmBulk(toDelete.length)) return;
    await applyDeletions(toDelete, widget.state.bus);
    if (!mounted) return;
    setState(() {
      for (final g in groups) { _report!.crossGenre.remove(g); }
      _updateGenreCache();
    });
  }

  Future<bool> _confirmBulk(int count) async {
    final result = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        backgroundColor: Retrowave.panel,
        shape: const RoundedRectangleBorder(
            borderRadius: BorderRadius.all(Radius.circular(2))),
        title: Text('CONFIRM DELETION',
            style: Retrowave.heading(Retrowave.fsSec, Retrowave.pink)),
        content: Text(
          'Delete $count file${count == 1 ? '' : 's'}? This cannot be undone.',
          style: Retrowave.body(),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: Text('CANCEL',
                style: Retrowave.body(Retrowave.fsBody, Retrowave.text2)),
          ),
          TextButton(
            onPressed: () => Navigator.pop(context, true),
            child: Text('DELETE',
                style: Retrowave.heading(Retrowave.fsBody, Retrowave.pink)),
          ),
        ],
      ),
    );
    return result == true;
  }

  // ── Build ─────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    final report = _report;
    final visSet = _visibleSet;
    final visGenre = _visibleGenre;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _buildTopBar(report),
        if (report != null && _cachedGenres.isNotEmpty) _buildFilterRow(_cachedGenres),
        if (report != null) const Divider(height: 1, color: Retrowave.border),
        Expanded(
          child: _scanning
              ? const Center(
                  child: Text('SCANNING…',
                      style: TextStyle(
                          color: Retrowave.cyan,
                          fontSize: Retrowave.fsSec,
                          fontFamily: 'Consolas',
                          letterSpacing: 1.5)))
              : report == null
                  ? const SizedBox()
                  : (report.isEmpty && visSet.isEmpty && visGenre.isEmpty)
                      ? Center(
                          child: Text('NO DUPLICATES FOUND',
                              style: Retrowave.heading(
                                  Retrowave.fsSec, Retrowave.cyan)))
                      : _buildResults(visSet, visGenre),
        ),
      ],
    );
  }

  Widget _buildTopBar(DupReport? report) {
    final total = report == null
        ? ''
        : '${report.crossSet.length} cross-set  ·  ${report.crossGenre.length} cross-genre';

    return Container(
      color: Retrowave.panel,
      padding: const EdgeInsets.symmetric(
          horizontal: Retrowave.sp4, vertical: Retrowave.sp3),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text('DUPLICATE CHECKER',
                    style:
                        Retrowave.heading(Retrowave.fsSec, Retrowave.text)),
                if (total.isNotEmpty)
                  Text(total, style: Retrowave.label()),
              ],
            ),
          ),
          NeonButton(
            label: _scanning ? 'Scanning…' : 'Rescan',
            icon: Icons.refresh,
            onPressed: _scanning ? null : _scan,
          ),
        ],
      ),
    );
  }

  Widget _buildFilterRow(List<String> genres) {
    return Container(
      color: Retrowave.surface,
      padding: const EdgeInsets.symmetric(
          horizontal: Retrowave.sp4, vertical: Retrowave.sp2),
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        child: Row(
          children: [
            _GenreChip(
              label: 'ALL',
              selected: _filterGenre == null,
              color: Retrowave.text,
              onTap: () => setState(() => _filterGenre = null),
            ),
            const SizedBox(width: Retrowave.sp2),
            for (final g in genres) ...[
              _GenreChip(
                label: _displayGenre(g).toUpperCase(),
                selected: _filterGenre == g,
                color: Retrowave.genreBg(_displayGenre(g)),
                textColor: Retrowave.genreFg(_displayGenre(g)),
                onTap: () =>
                    setState(() => _filterGenre = _filterGenre == g ? null : g),
              ),
              const SizedBox(width: Retrowave.sp2),
            ],
          ],
        ),
      ),
    );
  }

  Widget _buildResults(List<DupGroup> visSet, List<DupGroup> visGenre) {
    final filterLabel =
        _filterGenre != null ? ' IN ${_displayGenre(_filterGenre!).toUpperCase()}' : '';

    // CustomScrollView + SliverList.builder gives lazy thumbnail loading:
    // _SetCard images are only decoded for cards in (or near) the viewport.
    return CustomScrollView(
      slivers: [
        // ── Cross-set section ──
        if (visSet.isNotEmpty) ...[
          SliverToBoxAdapter(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(
                  Retrowave.sp4, Retrowave.sp4, Retrowave.sp4, Retrowave.sp2),
              child: _SectionBar(
                title: 'CROSS-SET$filterLabel',
                subtitle:
                    'Same movie in both Generic and New Release — pick which to keep',
                count: visSet.length,
                accent: Retrowave.cyan,
                actions: [
                  NeonButton(
                    label: 'Keep All Generic',
                    icon: Icons.filter_1,
                    onPressed: _keepAllGeneric,
                    padding: const EdgeInsets.symmetric(
                        horizontal: Retrowave.sp3, vertical: 6),
                  ),
                  const SizedBox(width: Retrowave.sp2),
                  NeonButton(
                    label: 'Keep All NR',
                    icon: Icons.filter_2,
                    onPressed: _keepAllNr,
                    padding: const EdgeInsets.symmetric(
                        horizontal: Retrowave.sp3, vertical: 6),
                  ),
                ],
              ),
            ),
          ),
          SliverPadding(
            padding: const EdgeInsets.symmetric(horizontal: Retrowave.sp4),
            sliver: SliverList(
              delegate: SliverChildBuilderDelegate(
                (_, i) {
                  final g = visSet[i];
                  return Padding(
                    padding: const EdgeInsets.only(bottom: Retrowave.sp2),
                    child: _SetCard(
                      group: g,
                      onKeepGeneric: () {
                        final loc = g.locations.firstWhere(
                            (l) => l.set == setGeneric,
                            orElse: () => g.locations.first);
                        _resolveSet(g, loc.path);
                      },
                      onKeepNr: () {
                        final loc = g.locations.firstWhere(
                            (l) => l.set == setNewRelease,
                            orElse: () => g.locations.first);
                        _resolveSet(g, loc.path);
                      },
                    ),
                  );
                },
                childCount: visSet.length,
              ),
            ),
          ),
          const SliverToBoxAdapter(child: SizedBox(height: Retrowave.sp4)),
        ],

        // ── Cross-genre section ──
        if (visGenre.isNotEmpty) ...[
          SliverToBoxAdapter(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(
                  Retrowave.sp4, 0, Retrowave.sp4, Retrowave.sp2),
              child: _SectionBar(
                title: 'CROSS-GENRE$filterLabel',
                subtitle:
                    'Same movie in multiple genre folders — tap a genre chip to keep it',
                count: visGenre.length,
                accent: Retrowave.pink,
                actions: [
                  NeonButton(
                    label: 'Clean All',
                    icon: Icons.auto_fix_high,
                    color: Retrowave.pink,
                    onPressed: _cleanAllGenre,
                    padding: const EdgeInsets.symmetric(
                        horizontal: Retrowave.sp3, vertical: 6),
                  ),
                ],
              ),
            ),
          ),
          SliverPadding(
            padding: const EdgeInsets.symmetric(horizontal: Retrowave.sp4),
            sliver: SliverList(
              delegate: SliverChildBuilderDelegate(
                (_, i) {
                  final g = visGenre[i];
                  return Padding(
                    padding: const EdgeInsets.only(bottom: Retrowave.sp2),
                    child: _GenreCard(
                      group: g,
                      onKeep: (path) => _resolveGenre(g, path),
                    ),
                  );
                },
                childCount: visGenre.length,
              ),
            ),
          ),
        ],

        if (visSet.isEmpty && visGenre.isEmpty && _report != null)
          SliverFillRemaining(
            hasScrollBody: false,
            child: Center(
              child: Text(
                _filterGenre != null
                    ? 'NO DUPLICATES IN ${_displayGenre(_filterGenre!).toUpperCase()}'
                    : 'NO DUPLICATES FOUND',
                style: Retrowave.heading(Retrowave.fsSec, Retrowave.cyan),
              ),
            ),
          ),

        const SliverToBoxAdapter(child: SizedBox(height: Retrowave.sp6)),
      ],
    );
  }
}

// ── Section bar ───────────────────────────────────────────────────────────────

class _SectionBar extends StatelessWidget {
  final String title;
  final String subtitle;
  final int count;
  final Color accent;
  final List<Widget> actions;

  const _SectionBar({
    required this.title,
    required this.subtitle,
    required this.count,
    required this.accent,
    required this.actions,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(
          horizontal: Retrowave.sp3, vertical: Retrowave.sp2),
      decoration: BoxDecoration(
        color: Retrowave.panel,
        border: Border(left: BorderSide(color: accent, width: 3)),
      ),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Row(
                  children: [
                    Text(title, style: Retrowave.heading(Retrowave.fsSec, accent)),
                    const SizedBox(width: Retrowave.sp2),
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                      color: accent,
                      child: Text('$count',
                          style: Retrowave.heading(
                              Retrowave.fsMeta, Retrowave.textInv)),
                    ),
                  ],
                ),
                const SizedBox(height: 2),
                Text(subtitle, style: Retrowave.label()),
              ],
            ),
          ),
          const SizedBox(width: Retrowave.sp3),
          ...actions,
        ],
      ),
    );
  }
}

// ── Cross-set card ────────────────────────────────────────────────────────────
// Shows the generic and NR copies side by side. Clicking either side's button
// immediately keeps that copy and deletes the other.

class _SetCard extends StatelessWidget {
  final DupGroup group;
  final VoidCallback onKeepGeneric;
  final VoidCallback onKeepNr;

  const _SetCard({
    required this.group,
    required this.onKeepGeneric,
    required this.onKeepNr,
  });

  @override
  Widget build(BuildContext context) {
    final genericLocs =
        group.locations.where((l) => l.set == setGeneric).toList();
    final nrLocs =
        group.locations.where((l) => l.set == setNewRelease).toList();
    final genreName = group.locations.isNotEmpty
        ? _displayGenre(group.locations.first.genre)
        : '';

    return Container(
      margin: const EdgeInsets.only(bottom: Retrowave.sp2),
      decoration: BoxDecoration(
        color: Retrowave.panel,
        border: Border.all(color: Retrowave.border),
      ),
      padding: const EdgeInsets.all(Retrowave.sp3),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(group.displayTitle,
                    style: Retrowave.heading(Retrowave.fsBody, Retrowave.text)),
              ),
              if (genreName.isNotEmpty)
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                  color: Retrowave.genreBg(genreName),
                  child: Text(genreName.toUpperCase(),
                      style: Retrowave.heading(
                          Retrowave.fsMeta, Retrowave.genreFg(genreName))),
                ),
            ],
          ),
          const SizedBox(height: Retrowave.sp3),
          IntrinsicHeight(
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Expanded(
                  child: _SetSide(
                    label: 'GENERIC',
                    color: Retrowave.cyan,
                    locations: genericLocs,
                    buttonLabel: 'Keep Generic',
                    onKeep: genericLocs.isNotEmpty ? onKeepGeneric : null,
                  ),
                ),
                const SizedBox(width: Retrowave.sp3),
                Expanded(
                  child: _SetSide(
                    label: 'NEW RELEASE',
                    color: Retrowave.pink,
                    locations: nrLocs,
                    buttonLabel: 'Keep New Release',
                    onKeep: nrLocs.isNotEmpty ? onKeepNr : null,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  static String _displayGenre(String folderName) {
    for (final k in Retrowave.genreColors.keys) {
      if (k.toLowerCase() == folderName.toLowerCase()) return Retrowave.displayName(k);
    }
    return folderName.isEmpty
        ? folderName
        : folderName[0].toUpperCase() + folderName.substring(1);
  }
}

class _SetSide extends StatelessWidget {
  final String label;
  final Color color;
  final List<DupLocation> locations;
  final String buttonLabel;
  final VoidCallback? onKeep;

  const _SetSide({
    required this.label,
    required this.color,
    required this.locations,
    required this.buttonLabel,
    required this.onKeep,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: Retrowave.surface,
        border: Border.all(color: color.withValues(alpha: 0.4)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Container(
            color: color.withValues(alpha: 0.15),
            padding: const EdgeInsets.symmetric(
                horizontal: Retrowave.sp2, vertical: 4),
            child: Text(label,
                style: Retrowave.heading(Retrowave.fsMeta, color)),
          ),
          if (locations.isNotEmpty) ...[
            AspectRatio(
              aspectRatio: 979 / 1665,
              child: Image.file(
                File(locations.first.path),
                fit: BoxFit.cover,
                cacheWidth: 320,
                errorBuilder: (_, _, _) => Container(
                  color: Retrowave.panel,
                  child: const Icon(Icons.broken_image,
                      color: Retrowave.border, size: 32),
                ),
              ),
            ),
            if (locations.length > 1)
              Padding(
                padding: const EdgeInsets.symmetric(
                    horizontal: Retrowave.sp2, vertical: 3),
                child: Text('+${locations.length - 1} more',
                    style: Retrowave.label()),
              ),
          ] else
            Padding(
              padding: const EdgeInsets.all(Retrowave.sp3),
              child: Text('—', style: Retrowave.label()),
            ),
          const Spacer(),
          Padding(
            padding: const EdgeInsets.all(Retrowave.sp2),
            child: NeonButton(
              label: buttonLabel,
              color: color,
              primary: false,
              onPressed: onKeep,
              padding: const EdgeInsets.symmetric(
                  horizontal: Retrowave.sp2, vertical: 6),
            ),
          ),
        ],
      ),
    );
  }
}

// ── Cross-genre card ──────────────────────────────────────────────────────────
// Compact single row: title + genre chips. Tap a chip to keep that copy.

class _GenreCard extends StatelessWidget {
  final DupGroup group;
  final ValueChanged<String> onKeep; // passes the path to keep

  const _GenreCard({required this.group, required this.onKeep});

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.only(bottom: Retrowave.sp2),
      decoration: BoxDecoration(
        color: Retrowave.panel,
        border: Border.all(color: Retrowave.border),
      ),
      padding: const EdgeInsets.symmetric(
          horizontal: Retrowave.sp3, vertical: Retrowave.sp2),
      child: Row(
        children: [
          Expanded(
            child: Text(group.displayTitle,
                style: Retrowave.body(Retrowave.fsBody, Retrowave.text),
                overflow: TextOverflow.ellipsis),
          ),
          const SizedBox(width: Retrowave.sp3),
          Wrap(
            spacing: Retrowave.sp2,
            runSpacing: Retrowave.sp2,
            children: [
              for (final loc in group.locations)
                _KeepChip(location: loc, onTap: () => onKeep(loc.path)),
            ],
          ),
        ],
      ),
    );
  }
}

class _KeepChip extends StatefulWidget {
  final DupLocation location;
  final VoidCallback onTap;
  const _KeepChip({required this.location, required this.onTap});

  @override
  State<_KeepChip> createState() => _KeepChipState();
}

class _KeepChipState extends State<_KeepChip> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final genre = widget.location.genre;
    final displayName = _displayGenre(genre);
    final bg = Retrowave.genreBg(displayName);
    final fg = Retrowave.genreFg(displayName);

    return MouseRegion(
      cursor: SystemMouseCursors.click,
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: GestureDetector(
        onTap: widget.onTap,
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 80),
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
          decoration: BoxDecoration(
            color: _hover ? bg : bg.withValues(alpha: 0.75),
            border: Border.all(
              color: _hover ? Retrowave.cyan : Colors.transparent,
              width: 1,
            ),
            borderRadius: BorderRadius.circular(2),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (_hover) ...[
                Icon(Icons.check, size: 10, color: fg),
                const SizedBox(width: 3),
              ],
              Text(
                displayName.toUpperCase(),
                style: Retrowave.heading(Retrowave.fsMeta, fg),
              ),
            ],
          ),
        ),
      ),
    );
  }

  static String _displayGenre(String folderName) {
    for (final k in Retrowave.genreColors.keys) {
      if (k.toLowerCase() == folderName.toLowerCase()) return Retrowave.displayName(k);
    }
    return folderName.isEmpty
        ? folderName
        : folderName[0].toUpperCase() + folderName.substring(1);
  }
}

// ── Genre filter chip ─────────────────────────────────────────────────────────

class _GenreChip extends StatefulWidget {
  final String label;
  final bool selected;
  final Color color;
  final Color? textColor;
  final VoidCallback onTap;

  const _GenreChip({
    required this.label,
    required this.selected,
    required this.color,
    required this.onTap,
    this.textColor,
  });

  @override
  State<_GenreChip> createState() => _GenreChipState();
}

class _GenreChipState extends State<_GenreChip> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final isActive = widget.selected || _hover;
    final fg = widget.textColor ??
        (isActive ? Retrowave.textInv : widget.color);
    final bg = isActive ? widget.color : Colors.transparent;

    return MouseRegion(
      cursor: SystemMouseCursors.click,
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: GestureDetector(
        onTap: widget.onTap,
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 80),
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
          decoration: BoxDecoration(
            color: bg,
            border: Border.all(
              color: widget.selected ? widget.color : Retrowave.border,
              width: 1,
            ),
            borderRadius: BorderRadius.circular(2),
          ),
          child: Text(widget.label,
              style: Retrowave.heading(Retrowave.fsMeta, fg)),
        ),
      ),
    );
  }
}
