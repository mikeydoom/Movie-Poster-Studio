import 'dart:io';
import 'package:flutter/material.dart';
import 'package:path/path.dart' as p;
import '../services/app_state.dart';
import '../services/config_manager.dart';
import '../services/movies_meta.dart';
import '../services/poster_converter.dart' show convertOneFile;
import '../services/rr_rating.dart';
import '../theme/retrowave.dart';
import '../widgets/neon_button.dart';
import '../widgets/neon_field.dart';

class GalleryScreen extends StatefulWidget {
  final AppState state;
  const GalleryScreen({super.key, required this.state});

  @override
  State<GalleryScreen> createState() => GalleryScreenState();
}

class GalleryScreenState extends State<GalleryScreen>
    with SingleTickerProviderStateMixin {
  late final TabController _tab;
  final Map<String, List<_PosterItem>> _items = {'generic': [], 'new_release': []};
  // movies.json mirror — filename → metadata (rating, title, etc.). Refreshed
  // alongside loadAll() and after any rating edit so tile overlays stay in sync.
  Map<String, MovieMeta> _meta = const {};
  final Map<String, TextEditingController> _search = {
    'generic': TextEditingController(),
    'new_release': TextEditingController(),
  };
  final Map<String, String?> _selectedGenre = {'generic': null, 'new_release': null};
  // Selection: path → poster item. Per-set so switching tabs doesn't blow it
  // away, and the toolbar shows count for the active tab.
  final Map<String, Map<String, _PosterItem>> _selected = {
    'generic': {},
    'new_release': {},
  };
  final Map<String, ScrollController> _gridScroll = {
    'generic': ScrollController(),
    'new_release': ScrollController(),
  };
  final Map<String, ScrollController> _shelfScroll = {
    'generic': ScrollController(),
    'new_release': ScrollController(),
  };

  // Sort mode per set. Options: title_az, title_za, year_new, year_old,
  // stars_high, stars_low.
  final Map<String, String> _sortMode = {
    'generic': 'title_az',
    'new_release': 'title_az',
  };

  static const List<double> _starTiers = [
    5.0, 4.5, 4.0, 3.5, 3.0, 2.5, 2.0, 1.5, 1.0, 0.5, 0.0
  ];

  @override
  void initState() {
    super.initState();
    _tab = TabController(length: 2, vsync: this);
    loadAll();
  }

  @override
  void dispose() {
    _tab.dispose();
    for (final c in _search.values) {
      c.dispose();
    }
    for (final c in _gridScroll.values) {
      c.dispose();
    }
    for (final c in _shelfScroll.values) {
      c.dispose();
    }
    super.dispose();
  }

  // ─── Data loading ──────────────────────────────────────────

  // Fire-and-forget wrapper so initState and external callers stay synchronous.
  void loadAll() => _refreshAll();

  /// Reload both sets + movies.json metadata and trigger a single rebuild.
  /// Items and metadata are fetched in parallel so the grid, shelf counts,
  /// and tile star rows all update atomically — no intermediate stale frame.
  Future<void> _refreshAll() async {
    final itemsFuture = Future.wait([_loadType('generic'), _loadType('new_release')]);
    final metaFuture = MoviesMetaStore.load();
    final items = await itemsFuture;
    final meta = await metaFuture;
    if (!mounted) return;
    setState(() {
      _items['generic'] = items[0];
      _items['new_release'] = items[1];
      _meta = meta;
      _selected['generic']!.removeWhere((path, _) => !File(path).existsSync());
      _selected['new_release']!.removeWhere((path, _) => !File(path).existsSync());
    });
  }

  Future<List<_PosterItem>> _loadType(String type) async {
    final base = ConfigManager.baseDir();
    // Layout: <base>/posters/converted/<set>/<genre>/*.png.
    final folder = Directory(p.join(
      base,
      'posters',
      'converted',
      type == 'generic' ? 'generic' : 'new_releases',
    ));
    final out = <_PosterItem>[];
    if (await folder.exists()) {
      final genreDirs = (await folder.list().toList()).whereType<Directory>().toList()
        ..sort((a, b) => p.basename(a.path).compareTo(p.basename(b.path)));
      for (final g in genreDirs) {
        for (final f in await g.list().toList()) {
          if (f is File && p.extension(f.path).toLowerCase() == '.png') {
            out.add(_PosterItem(p.basename(g.path), p.basename(f.path), f.path));
          }
        }
      }
    }
    return out;
  }

  Map<String, int> _countsFor(String type) {
    final m = <String, int>{};
    for (final i in _items[type]!) {
      m[i.genre] = (m[i.genre] ?? 0) + 1;
    }
    return m;
  }

  List<_PosterItem> _filteredFor(String type) {
    final filterRaw = _search[type]!.text.trim().toLowerCase();
    final selGenre = _selectedGenre[type];
    final mode = _sortMode[type] ?? 'title_az';

    final list = _items[type]!.where((i) {
      if (selGenre != null && i.genre != selGenre) return false;
      if (filterRaw.isEmpty) return true;
      return i.fileName.toLowerCase().contains(filterRaw) ||
          i.genre.toLowerCase().contains(filterRaw);
    }).toList();

    list.sort((a, b) {
      int titleCmp() =>
          a.fileName.toLowerCase().compareTo(b.fileName.toLowerCase());
      switch (mode) {
        case 'title_za':
          return b.fileName.toLowerCase().compareTo(a.fileName.toLowerCase());
        case 'year_new':
          final ya = _meta[a.fileName]?.year ??
              MoviesMetaStore.parseFilename(a.fileName).year;
          final yb = _meta[b.fileName]?.year ??
              MoviesMetaStore.parseFilename(b.fileName).year;
          final c = yb.compareTo(ya);
          return c != 0 ? c : titleCmp();
        case 'year_old':
          final ya = _meta[a.fileName]?.year ??
              MoviesMetaStore.parseFilename(a.fileName).year;
          final yb = _meta[b.fileName]?.year ??
              MoviesMetaStore.parseFilename(b.fileName).year;
          final c = ya.compareTo(yb);
          return c != 0 ? c : titleCmp();
        case 'stars_high':
          final sa = _meta[a.fileName]?.stars ?? 0.0;
          final sb = _meta[b.fileName]?.stars ?? 0.0;
          final c = sb.compareTo(sa);
          return c != 0 ? c : titleCmp();
        case 'stars_low':
          final sa = _meta[a.fileName]?.stars ?? 0.0;
          final sb = _meta[b.fileName]?.stars ?? 0.0;
          final c = sa.compareTo(sb);
          return c != 0 ? c : titleCmp();
        case 'title_az':
        default:
          return titleCmp();
      }
    });

    return list;
  }

  // ─── Selection ─────────────────────────────────────────────

  void _toggleSelected(String type, _PosterItem item) {
    setState(() {
      final sel = _selected[type]!;
      if (sel.containsKey(item.path)) {
        sel.remove(item.path);
      } else {
        sel[item.path] = item;
      }
    });
  }

  void _clearSelection(String type) {
    setState(() => _selected[type]!.clear());
  }

  void _selectAll(String type) {
    setState(() {
      _selected[type]!.clear();
      for (final item in _filteredFor(type)) {
        _selected[type]![item.path] = item;
      }
    });
  }

  // ─── File ops ──────────────────────────────────────────────

  Future<void> _deleteItems(String type, List<_PosterItem> items) async {
    if (items.isEmpty) return;
    final confirmed = await _confirm(
      'Delete ${items.length} poster${items.length == 1 ? '' : 's'}?',
      'This deletes the PNG file${items.length == 1 ? '' : 's'} from disk. '
          'Cannot be undone.',
      destructive: true,
    );
    if (confirmed != true) return;
    var deleted = 0;
    for (final it in items) {
      try {
        final f = File(it.path);
        if (await f.exists()) {
          await f.delete();
          deleted++;
          // Also delete the source poster (not converted) if this is a
          // converted poster in the gallery.
          final rel = p.relative(it.path, from: ConfigManager.baseDir());
          final srcRel = rel.replaceFirst(RegExp(r'^posters[\\/]converted[\\/]'), 'posters${p.separator}');
          if (srcRel != rel) {
            final srcPath = p.join(ConfigManager.baseDir(), srcRel);
            final src = File(srcPath);
            if (await src.exists()) {
              await src.delete();
              widget.state.appendLog('  Source also deleted: $srcPath');
            }
          }
        }
      } catch (e) {
        widget.state.appendLog('Delete failed for ${it.path}: $e');
      }
    }
    widget.state.appendLog('Deleted $deleted poster(s).');
    _selected[type]!.clear();
    await _refreshAll();
  }

  Future<void> _moveItems(
      String type, List<_PosterItem> items, String targetGenre) async {
    if (items.isEmpty) return;
    final base = ConfigManager.baseDir();
    final setLeaf = type == 'generic' ? 'generic' : 'new_releases';
    final dstDirPath = p.join(
        base, 'posters', 'converted', setLeaf, targetGenre.toLowerCase());
    await Directory(dstDirPath).create(recursive: true);

    var moved = 0;
    var skipped = 0;
    for (final it in items) {
      if (it.genre.toLowerCase() == targetGenre.toLowerCase()) {
        skipped++;
        continue;
      }
      final dst = p.join(dstDirPath, it.fileName);
      try {
        if (await File(dst).exists()) {
          widget.state.appendLog('Skipping ${it.fileName}: already exists in $targetGenre.');
          skipped++;
          continue;
        }
        await File(it.path).rename(dst);
        moved++;
      } catch (e) {
        widget.state.appendLog('Move failed for ${it.fileName}: $e');
      }
    }
    widget.state.appendLog('Moved $moved poster(s) to $targetGenre. Skipped $skipped.');
    _selected[type]!.clear();
    await _refreshAll();
  }

  Future<void> _setRatingForItems(
      String type, List<_PosterItem> items, double stars) async {
    if (items.isEmpty) return;
    // Same filename across multiple genres / both sets → one movies.json
    // entry. Deduplicate before writing.
    final filenames = items.map((e) => e.fileName).toSet();
    await MoviesMetaStore.setRatingFor(filenames, stars);
    widget.state.appendLog(
        'Updated rating to ${stars.toStringAsFixed(1)}★ (last2=${rrStarToLast2(stars)}) for ${filenames.length} title(s).');
    _selected[type]!.clear();
    await _refreshAll();
  }

  Future<void> _setOldForItems(
      String type, List<_PosterItem> items, bool isOld) async {
    if (items.isEmpty) return;
    final filenames = items.map((e) => e.fileName).toSet();
    await MoviesMetaStore.setOldFor(filenames, isOld);
    final verb = isOld ? 'Marked' : 'Unmarked';
    widget.state.appendLog('$verb ${filenames.length} title(s) as Old.');
    _selected[type]!.clear();
    await _refreshAll();
  }

  /// Re-convert posters from one set's profile to the other's. Just moving
  /// the converted PNG would leave wrong canvas dims (979×1665 vs 600×1200)
  /// and the wrong overlay baked in — so instead we locate the raw TMDB
  /// source (in `posters/<set>/<genre>/`), re-run the converter with the
  /// target profile, write to the destination set's `converted/` tree, and
  /// delete the old converted file from the source set on success. Genre
  /// stays the same.
  ///
  /// If the raw source can't be found (was deleted, never scraped), the
  /// item is skipped with a warning — we won't fall back to double-
  /// converting the already-composed file because that produces visible
  /// overlay-on-overlay artifacts at the bottom strip.
  Future<void> _swapItemsToOtherSet(
      String fromType, List<_PosterItem> items) async {
    if (items.isEmpty) return;
    final toType = fromType == 'generic' ? 'new_release' : 'generic';
    final fromLeaf = fromType == 'generic' ? 'generic' : 'new_releases';
    final toLeaf = toType == 'generic' ? 'generic' : 'new_releases';
    final base = ConfigManager.baseDir();
    final cfg = widget.state.config.posterConverter;
    final toOverlayDir =
        toType == 'generic' ? cfg.normalOverlay : cfg.nrOverlay;
    // poster_converter's profile string uses 'new_release' (singular).
    final toProfile = toType == 'generic' ? 'generic' : 'new_release';

    // Preflight: the target overlay folder needs to exist with overlay1.png
    // or every file will fail individually. Show the error up front instead
    // of in the silent log.
    if (!Directory(toOverlayDir).existsSync() ||
        !File(p.join(toOverlayDir, 'overlay1.png')).existsSync()) {
      await _confirm(
        'CANNOT RE-CONVERT',
        'The $toType overlay folder is missing or has no overlay1.png:\n'
            '$toOverlayDir\n\n'
            'Set the correct path in the Poster Converter tab '
            '(overlays/${toType == 'generic' ? 'generic' : 'new_releases'}).',
        destructive: false,
      );
      return;
    }

    var converted = 0;
    var skipped = 0;
    var failed = 0;
    for (final it in items) {
      final dst = p.join(
        base, 'posters', 'converted', toLeaf, it.genre.toLowerCase(),
        it.fileName,
      );
      if (await File(dst).exists()) {
        widget.state.appendLog(
            'Skipping ${it.fileName}: already exists in $toLeaf/${it.genre}');
        skipped++;
        continue;
      }

      // Look for the raw source. Most commonly in the from-side scrape
      // folder (where it was originally downloaded). Fall back to the
      // to-side in case the user scraped that set instead.
      final candidates = [
        p.join(base, 'posters', fromLeaf, it.genre.toLowerCase(), it.fileName),
        p.join(base, 'posters', toLeaf, it.genre.toLowerCase(), it.fileName),
      ];
      String? src;
      for (final c in candidates) {
        if (await File(c).exists()) {
          src = c;
          break;
        }
      }
      if (src == null) {
        widget.state.appendLog(
            'Cannot swap ${it.fileName}: no raw TMDB source at '
            'posters/$fromLeaf/${it.genre}/ or posters/$toLeaf/${it.genre}/. '
            'Re-scrape this movie to recover the original.');
        failed++;
        continue;
      }

      try {
        final res = await convertOneFile(
          inputPath: src,
          outputPath: dst,
          overlaysFolder: toOverlayDir,
          profile: toProfile,
        );
        if (res.ok) {
          // Old converted file in the source set is now stale — delete.
          try {
            await File(it.path).delete();
          } catch (e) {
            widget.state.appendLog(
                'Note: failed to delete old converted ${it.path}: $e');
          }
          converted++;
        } else {
          widget.state.appendLog(
              'Re-convert failed for ${it.fileName}: ${res.message}');
          failed++;
        }
      } catch (e) {
        widget.state.appendLog('Swap convert error for ${it.fileName}: $e');
        failed++;
      }
    }
    widget.state.appendLog(
        'Set-swap $fromType → $toType: $converted re-converted, '
        '$skipped skipped, $failed failed.');
    _selected[fromType]!.clear();
    await _refreshAll();
  }

  // ─── Dialogs ───────────────────────────────────────────────

  Future<bool?> _confirm(String title, String body,
      {bool destructive = false}) {
    return showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        backgroundColor: Retrowave.panel,
        shape: const RoundedRectangleBorder(
            borderRadius: BorderRadius.all(Radius.circular(2))),
        title: Text(title,
            style: Retrowave.heading(
                Retrowave.fsSec, destructive ? Retrowave.pink : Retrowave.cyan)),
        content: Text(body, style: Retrowave.body()),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: Text('CANCEL',
                style: Retrowave.body(Retrowave.fsBody, Retrowave.text2)),
          ),
          TextButton(
            onPressed: () => Navigator.pop(context, true),
            child: Text(destructive ? 'DELETE' : 'OK',
                style: Retrowave.heading(Retrowave.fsBody,
                    destructive ? Retrowave.pink : Retrowave.cyan)),
          ),
        ],
      ),
    );
  }

  // ─── Right-click menu ──────────────────────────────────────

  /// Right-clicking a tile: if the tile is part of the current selection,
  /// operate on the whole selection; otherwise operate on just that tile
  /// (without altering selection). This mirrors most desktop file managers.
  Future<void> _showContextMenu(
      BuildContext ctx, Offset pos, String type, _PosterItem tile) async {
    final selectedMap = _selected[type]!;
    final operatingOn = selectedMap.containsKey(tile.path)
        ? selectedMap.values.toList()
        : <_PosterItem>[tile];
    final overlay = Overlay.of(ctx).context.findRenderObject() as RenderBox;
    final menuPos = RelativeRect.fromLTRB(
      pos.dx,
      pos.dy,
      overlay.size.width - pos.dx,
      overlay.size.height - pos.dy,
    );

    final allOld =
        operatingOn.every((i) => _meta[i.fileName]?.isOld ?? false);
    final action = await showMenu<String>(
      context: ctx,
      position: menuPos,
      color: Retrowave.panel,
      shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.all(Radius.circular(2)),
          side: BorderSide(color: Retrowave.border)),
      items: [
        PopupMenuItem<String>(
          value: '_label',
          enabled: false,
          height: 28,
          child: Text(
            operatingOn.length == 1
                ? operatingOn.first.fileName
                : '${operatingOn.length} selected',
            style: Retrowave.label(),
          ),
        ),
        const PopupMenuDivider(),
        PopupMenuItem<String>(
          value: 'genre',
          child: Row(
            children: [
              const Icon(Icons.swap_horiz, size: 14, color: Retrowave.cyan),
              const SizedBox(width: 8),
              Text('Change Genre…', style: Retrowave.body()),
            ],
          ),
        ),
        PopupMenuItem<String>(
          value: 'rating',
          child: Row(
            children: [
              const Icon(Icons.star, size: 14, color: Retrowave.gold),
              const SizedBox(width: 8),
              Text('Set Rating…', style: Retrowave.body()),
            ],
          ),
        ),
        PopupMenuItem<String>(
          value: 'old',
          child: Row(
            children: [
              const Icon(Icons.history, size: 14, color: Retrowave.gold),
              const SizedBox(width: 8),
              Text(allOld ? 'Unmark as Old' : 'Mark as Old',
                  style: Retrowave.body()),
            ],
          ),
        ),
        PopupMenuItem<String>(
          value: 'swap_set',
          child: Row(
            children: [
              const Icon(Icons.swap_vert, size: 14, color: Retrowave.purple),
              const SizedBox(width: 8),
              Text(
                type == 'generic'
                    ? 'Move to New Releases'
                    : 'Move to Generic',
                style: Retrowave.body(),
              ),
            ],
          ),
        ),
        const PopupMenuDivider(),
        PopupMenuItem<String>(
          value: 'delete',
          child: Row(
            children: [
              const Icon(Icons.delete_outline, size: 14, color: Retrowave.pink),
              const SizedBox(width: 8),
              Text('Delete', style: Retrowave.body(Retrowave.fsBody, Retrowave.pink)),
            ],
          ),
        ),
      ],
    );

    if (!mounted || action == null) return;
    if (action == 'genre') {
      _openGenreMenu(context, type, operatingOn);
    } else if (action == 'rating') {
      _openRatingMenu(context, type, operatingOn);
    } else if (action == 'old') {
      await _setOldForItems(type, operatingOn, !allOld);
    } else if (action == 'swap_set') {
      await _swapItemsToOtherSet(type, operatingOn);
    } else if (action == 'delete') {
      await _deleteItems(type, operatingOn);
    }
  }

  Future<void> _openGenreMenu(
      BuildContext ctx, String type, List<_PosterItem> items) async {
    final genres = Retrowave.genreColors.keys.toList();
    final selected = await showDialog<String>(
      context: ctx,
      builder: (_) => SimpleDialog(
        backgroundColor: Retrowave.panel,
        shape: const RoundedRectangleBorder(
            borderRadius: BorderRadius.all(Radius.circular(2)),
            side: BorderSide(color: Retrowave.border)),
        title: Text('Move to genre',
            style: Retrowave.heading(Retrowave.fsSec, Retrowave.cyan)),
        children: [
          for (final g in genres)
            SimpleDialogOption(
              onPressed: () => Navigator.pop(ctx, g),
              padding: EdgeInsets.zero,
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                child: Row(
                  children: [
                    Container(width: 8, height: 18, color: Retrowave.genreBg(g)),
                    const SizedBox(width: 10),
                    Text(Retrowave.displayName(g).toUpperCase(), style: Retrowave.heading(Retrowave.fsBody)),
                  ],
                ),
              ),
            ),
        ],
      ),
    );
    if (selected != null) await _moveItems(type, items, selected);
  }

  Future<void> _openRatingMenu(
      BuildContext ctx, String type, List<_PosterItem> items) async {
    final selected = await showDialog<double>(
      context: ctx,
      builder: (_) => SimpleDialog(
        backgroundColor: Retrowave.panel,
        shape: const RoundedRectangleBorder(
            borderRadius: BorderRadius.all(Radius.circular(2)),
            side: BorderSide(color: Retrowave.border)),
        title: Text('Set rating',
            style: Retrowave.heading(Retrowave.fsSec, Retrowave.gold)),
        children: [
          for (final s in _starTiers)
            SimpleDialogOption(
              onPressed: () => Navigator.pop(ctx, s),
              padding: EdgeInsets.zero,
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                child: Row(
                  children: [
                    Text('${s.toStringAsFixed(1)} ★',
                        style: Retrowave.heading(Retrowave.fsBody, Retrowave.gold)),
                    const SizedBox(width: 12),
                    Text(_criticTagFor(s),
                        style: Retrowave.body(Retrowave.fsMeta, Retrowave.text2)),
                    const Spacer(),
                    Text('last2=${rrStarToLast2(s)}',
                        style: Retrowave.body(Retrowave.fsMeta, Retrowave.text3)),
                  ],
                ),
              ),
            ),
        ],
      ),
    );
    if (selected != null) await _setRatingForItems(type, items, selected);
  }

  static String _criticTagFor(double stars) {
    if (stars >= 4.0) return 'Good Critic';
    if (stars <= 1.5) return 'Bad Critic';
    return '';
  }

  // ─── Build ─────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    // Combined poster count across both sets. RR caps the total custom-slot
    // pool at 12,987 — beyond that the exporter writes entries the game
    // won't load. Hard-warn the user the moment they're at or over the cap.
    final totalCount =
        _items['generic']!.length + _items['new_release']!.length;
    final overLimit = totalCount >= 12987;

    return Column(
      children: [
        if (overLimit) _SlotLimitBanner(total: totalCount),
        Container(
          color: Retrowave.panel,
          child: TabBar(
            controller: _tab,
            indicatorColor: Retrowave.cyan,
            indicatorWeight: 2,
            labelColor: Retrowave.cyan,
            unselectedLabelColor: Retrowave.text3,
            labelStyle: Retrowave.heading(Retrowave.fsBody),
            dividerColor: Retrowave.border,
            tabs: const [
              Tab(text: 'GENERIC POSTERS'),
              Tab(text: 'NEW RELEASE POSTERS'),
            ],
          ),
        ),
        Expanded(
          child: TabBarView(
            controller: _tab,
            children: [_buildPanel('generic'), _buildPanel('new_release')],
          ),
        ),
      ],
    );
  }

  Widget _buildPanel(String type) {
    final counts = _countsFor(type);
    // Always show the full canonical RR genre list — empty genres show "(0)"
    // and stay clickable. Anything on disk that doesn't match a canonical
    // name (legacy / typo folders) is appended at the end so it's still
    // reachable. Folder names live in lowercase on disk; canonical names
    // are capitalized — match case-insensitively.
    final canonical =
        Retrowave.genreColors.keys.map((g) => g.toLowerCase()).toList();
    final extras = counts.keys.where((g) => !canonical.contains(g)).toList()
      ..sort();
    final genres = [...canonical, ...extras];
    final filtered = _filteredFor(type);

    return Row(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _buildShelf(type, genres, counts, filtered.length),
        Expanded(
          child: Column(
            children: [
              if (_selected[type]!.isNotEmpty)
                _buildSelectionToolbar(type, filtered),
              Expanded(
                child: filtered.isEmpty
                    ? Center(
                        child: Text('NO POSTERS MATCH',
                            style: Retrowave.heading(Retrowave.fsSec, Retrowave.pink)),
                      )
                    : _withMagentaScrollbar(
                        controller: _gridScroll[type]!,
                        child: LayoutBuilder(
                          builder: (ctx, box) {
                            const itemW = 220.0;
                            final cols =
                                (box.maxWidth / (itemW + 12)).floor().clamp(1, 12);
                            // NR posters are 600×1200 on disk but only the
                            // top 600×941 is shown in the gallery (cropped
                            // from top-left, dropping the bottom overlay
                            // strip). Generic stays full 979×1665.
                            final isNr = type == 'new_release';
                            final tileAspect = isNr ? 600 / 941 : 979 / 1665;
                            return GridView.builder(
                              controller: _gridScroll[type]!,
                              padding: const EdgeInsets.all(16),
                              itemCount: filtered.length,
                              gridDelegate:
                                  SliverGridDelegateWithFixedCrossAxisCount(
                                crossAxisCount: cols,
                                crossAxisSpacing: 12,
                                mainAxisSpacing: 12,
                                childAspectRatio: tileAspect,
                              ),
                              itemBuilder: (ctx, i) {
                                final item = filtered[i];
                                final selected =
                                    _selected[type]!.containsKey(item.path);
                                final meta = _meta[item.fileName];
                                final year = (meta?.year.isNotEmpty == true)
                                    ? meta!.year
                                    : MoviesMetaStore.parseFilename(
                                            item.fileName)
                                        .year;
                                return _PosterTile(
                                  item: item,
                                  stars: meta?.stars,
                                  isOld: meta?.isOld ?? false,
                                  year: year,
                                  selected: selected,
                                  cropFromTop: isNr,
                                  onTap: () => _toggleSelected(type, item),
                                  onSecondaryTap: (pos) =>
                                      _showContextMenu(ctx, pos, type, item),
                                );
                              },
                            );
                          },
                        ),
                      ),
              ),
            ],
          ),
        ),
      ],
    );
  }

  Widget _buildShelf(String type, List<String> genres, Map<String, int> counts,
      int filteredCount) {
    return Container(
      width: 240,
      decoration: const BoxDecoration(
        color: Retrowave.panel,
        border: Border(right: BorderSide(color: Retrowave.border)),
      ),
      padding: const EdgeInsets.all(Retrowave.sp3),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('GENRE SHELF',
              style: Retrowave.heading(Retrowave.fsBody, Retrowave.text2)),
          const SizedBox(height: Retrowave.sp2),
          NeonButton(
            label: 'Refresh',
            icon: Icons.refresh,
            onPressed: _refreshAll,
          ),
          const SizedBox(height: Retrowave.sp2),
          NeonField(
              controller: _search[type]!,
              hint: 'Search…',
              onChanged: (_) => setState(() {})),
          const SizedBox(height: Retrowave.sp2),
          _buildSortRow(type),
          const SizedBox(height: Retrowave.sp2),
          Expanded(
            child: _withMagentaScrollbar(
              controller: _shelfScroll[type]!,
              child: ListView(
                controller: _shelfScroll[type]!,
                children: [
                  _genreTile(type, null, 'ALL', filteredCount),
                  ...genres.map(
                      (g) => _genreTile(type, g, Retrowave.displayName(g).toUpperCase(), counts[g] ?? 0)),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  static const _sortOptions = [
    ('title_az', 'Title A→Z'),
    ('title_za', 'Title Z→A'),
    ('year_new', 'Year (Newest)'),
    ('year_old', 'Year (Oldest)'),
    ('stars_high', 'Stars High→Low'),
    ('stars_low', 'Stars Low→High'),
  ];

  Widget _buildSortRow(String type) {
    return Row(
      children: [
        Text('SORT', style: Retrowave.label()),
        const SizedBox(width: Retrowave.sp2),
        Expanded(
          child: DropdownButton<String>(
            value: _sortMode[type],
            isExpanded: true,
            style: Retrowave.body(Retrowave.fsMeta),
            dropdownColor: Retrowave.panel,
            iconEnabledColor: Retrowave.text2,
            underline: Container(height: 1, color: Retrowave.border),
            items: _sortOptions
                .map((o) => DropdownMenuItem(
                      value: o.$1,
                      child:
                          Text(o.$2, style: Retrowave.body(Retrowave.fsMeta)),
                    ))
                .toList(),
            onChanged: (v) {
              if (v != null) setState(() => _sortMode[type] = v);
            },
          ),
        ),
      ],
    );
  }

  Widget _buildSelectionToolbar(String type, List<_PosterItem> filtered) {
    final count = _selected[type]!.length;
    final items = _selected[type]!.values.toList();
    return Container(
      padding: const EdgeInsets.symmetric(
          horizontal: Retrowave.sp3, vertical: Retrowave.sp2),
      decoration: const BoxDecoration(
        color: Retrowave.panel,
        border: Border(bottom: BorderSide(color: Retrowave.pink, width: 1)),
      ),
      child: Row(
        children: [
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
            color: Retrowave.pink,
            child: Text('$count SELECTED',
                style:
                    Retrowave.heading(Retrowave.fsMeta, Retrowave.textInv)),
          ),
          const SizedBox(width: Retrowave.sp3),
          NeonButton(
            label: 'Genre',
            icon: Icons.swap_horiz,
            color: Retrowave.cyan,
            onPressed: () => _openGenreMenu(context, type, items),
          ),
          const SizedBox(width: Retrowave.sp2),
          NeonButton(
            label: 'Rating',
            icon: Icons.star,
            color: Retrowave.gold,
            onPressed: () => _openRatingMenu(context, type, items),
          ),
          const SizedBox(width: Retrowave.sp2),
          NeonButton(
            label: type == 'generic' ? '→ NR' : '→ Generic',
            icon: Icons.swap_vert,
            onPressed: () => _swapItemsToOtherSet(type, items),
          ),
          const SizedBox(width: Retrowave.sp2),
          Builder(builder: (ctx) {
            final allOld =
                items.every((i) => _meta[i.fileName]?.isOld ?? false);
            return NeonButton(
              label: allOld ? 'Unmark Old' : 'Mark Old',
              icon: Icons.history,
              color: Retrowave.gold,
              onPressed: () => _setOldForItems(type, items, !allOld),
            );
          }),
          const SizedBox(width: Retrowave.sp2),
          NeonButton(
            label: 'Delete',
            icon: Icons.delete_outline,
            color: Retrowave.pink,
            onPressed: () => _deleteItems(type, items),
          ),
          const Spacer(),
          NeonButton(
            label: 'Select All',
            icon: Icons.select_all,
            onPressed: () => _selectAll(type),
          ),
          const SizedBox(width: Retrowave.sp2),
          NeonButton(
            label: 'Clear',
            icon: Icons.close,
            onPressed: () => _clearSelection(type),
          ),
        ],
      ),
    );
  }

  Widget _genreTile(String type, String? value, String label, int count) {
    final active = _selectedGenre[type] == value;
    final isAll = value == null;
    final accent = isAll ? Retrowave.cyan : Retrowave.genreBg(value);
    final fg = active
        ? (isAll ? Retrowave.textInv : Retrowave.genreFg(value))
        : accent;
    final bg = active ? accent : Retrowave.panel;
    final stripeColor = active ? Colors.transparent : accent;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: InkWell(
        onTap: () => setState(() => _selectedGenre[type] = value),
        child: Container(
          decoration: BoxDecoration(
            color: bg,
            border: active
                ? Border.all(color: Retrowave.cyan, width: 1)
                : Border.all(color: Retrowave.border, width: 1),
          ),
          child: Row(
            children: [
              Container(width: 4, height: 28, color: stripeColor),
              const SizedBox(width: Retrowave.sp2),
              Expanded(
                child: Text(label, style: Retrowave.body(Retrowave.fsBody, fg)),
              ),
              Padding(
                padding: const EdgeInsets.symmetric(
                    horizontal: Retrowave.sp2, vertical: 2),
                child: Text('$count', style: Retrowave.label()),
              ),
            ],
          ),
        ),
      ),
    );
  }

  /// Wrap a scrollable so its scrollbar renders in neon magenta with the
  /// thumb permanently visible. Local override so we don't change the
  /// global theme's scrollbars elsewhere.
  Widget _withMagentaScrollbar({
    required ScrollController controller,
    required Widget child,
  }) {
    return Theme(
      data: Theme.of(context).copyWith(
        scrollbarTheme: ScrollbarThemeData(
          thumbVisibility: const WidgetStatePropertyAll(true),
          thumbColor: const WidgetStatePropertyAll(Retrowave.pink),
          trackColor: WidgetStatePropertyAll(Retrowave.surface),
          trackBorderColor: WidgetStatePropertyAll(Retrowave.border),
          thickness: const WidgetStatePropertyAll(8),
          radius: const Radius.circular(0),
          minThumbLength: 32,
        ),
      ),
      child: Scrollbar(
        controller: controller,
        thumbVisibility: true,
        child: child,
      ),
    );
  }
}

class _PosterItem {
  final String genre;
  final String fileName;
  final String path;
  _PosterItem(this.genre, this.fileName, this.path);
}

class _PosterTile extends StatefulWidget {
  final _PosterItem item;
  final double? stars;
  final bool isOld;
  final String year;
  final bool selected;
  /// When true, render the source image at full width anchored to the top-left
  /// (used for new-release tiles where we want only the top 600×941 of the
  /// 600×1200 source visible). When false, `BoxFit.cover`-fill the tile.
  final bool cropFromTop;
  final VoidCallback onTap;
  final void Function(Offset position) onSecondaryTap;
  const _PosterTile({
    required this.item,
    required this.stars,
    required this.isOld,
    required this.year,
    required this.selected,
    required this.cropFromTop,
    required this.onTap,
    required this.onSecondaryTap,
  });

  @override
  State<_PosterTile> createState() => _PosterTileState();
}

class _PosterTileState extends State<_PosterTile> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final accent = Retrowave.genreBg(widget.item.genre);
    final borderColor = widget.selected
        ? Retrowave.pink
        : (_hover ? Retrowave.cyan : accent);
    final borderWidth = widget.selected ? 3.0 : (_hover ? 2.0 : 1.0);
    final tile = MouseRegion(
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: GestureDetector(
        onTap: widget.onTap,
        onSecondaryTapDown: (d) => widget.onSecondaryTap(d.globalPosition),
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 100),
          decoration: BoxDecoration(
            border: Border.all(color: borderColor, width: borderWidth),
          ),
          child: ClipRect(
            child: Stack(
              fit: StackFit.expand,
              children: [
                // BoxFit.fitWidth lets the image overflow vertically when its
                // natural height (after width-fit) exceeds the tile height.
                // The outer ClipRect crops that overflow, so anchoring to
                // topCenter shows the top portion of the source — exactly
                // the "crop from top-left to height 941" the NR profile wants.
                Image.file(
                  File(widget.item.path),
                  fit: widget.cropFromTop ? BoxFit.fitWidth : BoxFit.cover,
                  alignment: widget.cropFromTop
                      ? Alignment.topCenter
                      : Alignment.center,
                  gaplessPlayback: true,
                  errorBuilder: (_, _, _) => Container(
                    color: Retrowave.panel,
                    child: const Icon(Icons.broken_image, color: Retrowave.pink),
                  ),
                ),
                // Top-left checkbox indicator.
                Positioned(
                  top: 6,
                  left: 6,
                  child: _SelectionMark(
                      selected: widget.selected,
                      visible: _hover || widget.selected),
                ),
                // Top-right OLD badge.
                if (widget.isOld)
                  Positioned(
                    top: 6,
                    right: 6,
                    child: Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 5, vertical: 2),
                      color: Retrowave.gold,
                      child: Text(
                        'OLD',
                        style: Retrowave.heading(
                            Retrowave.fsMeta, Retrowave.textInv),
                      ),
                    ),
                  ),
                // Bottom strip: genre chip + year (left) + star rating (right).
                Positioned(
                  left: 0,
                  right: 0,
                  bottom: 0,
                  child: Container(
                    padding: const EdgeInsets.fromLTRB(6, 18, 6, 6),
                    decoration: BoxDecoration(
                      gradient: LinearGradient(
                        begin: Alignment.bottomCenter,
                        end: Alignment.topCenter,
                        colors: [
                          Colors.black.withValues(alpha: 0.85),
                          Colors.transparent,
                        ],
                      ),
                    ),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.center,
                      children: [
                        _GenreChip(genre: widget.item.genre),
                        if (widget.year.isNotEmpty) ...[
                          const SizedBox(width: 6),
                          Text(
                            widget.year,
                            style: Retrowave.body(
                                    Retrowave.fsMeta, Retrowave.text2)
                                .copyWith(shadows: const [
                              Shadow(
                                  color: Colors.black,
                                  blurRadius: 3,
                                  offset: Offset(0, 1)),
                            ]),
                          ),
                        ],
                        const Spacer(),
                        _StarRow(stars: widget.stars),
                      ],
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
    return tile;
  }
}

class _GenreChip extends StatelessWidget {
  final String genre;
  const _GenreChip({required this.genre});

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        color: Retrowave.genreBg(genre),
        border: Border.all(color: Colors.black.withValues(alpha: 0.4)),
      ),
      child: Text(
        Retrowave.displayName(genre).toUpperCase(),
        style: Retrowave.heading(Retrowave.fsMeta, Retrowave.genreFg(genre)),
      ),
    );
  }
}

/// 5-slot star row driven by an RR star value (0.0 / 0.5 / 1.0 / … / 5.0).
/// Renders dimmed slots when there's no rating data yet.
class _StarRow extends StatelessWidget {
  final double? stars;
  const _StarRow({required this.stars});

  @override
  Widget build(BuildContext context) {
    final v = stars ?? 0.0;
    final hasRating = stars != null;
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: List.generate(5, (i) {
        final slot = i + 1; // 1..5
        final IconData icon;
        if (v >= slot) {
          icon = Icons.star;
        } else if (v >= slot - 0.5) {
          icon = Icons.star_half;
        } else {
          icon = Icons.star_border;
        }
        return Icon(
          icon,
          size: 14,
          color: hasRating ? Retrowave.gold : Retrowave.text3,
          shadows: const [
            Shadow(color: Colors.black, blurRadius: 2, offset: Offset(0, 1)),
          ],
        );
      }),
    );
  }
}

class _SelectionMark extends StatelessWidget {
  final bool selected;
  final bool visible;
  const _SelectionMark({required this.selected, required this.visible});

  @override
  Widget build(BuildContext context) {
    final showBox = selected || visible;
    return AnimatedOpacity(
      duration: const Duration(milliseconds: 120),
      opacity: showBox ? 1.0 : 0.0,
      child: Container(
        width: 22,
        height: 22,
        decoration: BoxDecoration(
          color: selected ? Retrowave.pink : Colors.black.withValues(alpha: 0.6),
          border: Border.all(
              color: selected ? Retrowave.pink : Retrowave.text2, width: 1.5),
        ),
        alignment: Alignment.center,
        child: selected
            ? const Icon(Icons.check, size: 16, color: Retrowave.textInv)
            : null,
      ),
    );
  }
}


/// Hard-warn banner shown on the Gallery once the combined generic +
/// new-release poster count hits the RR custom-slot ceiling (12,987).
/// Stays visible across both tabs.
class _SlotLimitBanner extends StatelessWidget {
  final int total;
  const _SlotLimitBanner({required this.total});

  @override
  Widget build(BuildContext context) {
    return Container(
      color: Retrowave.pink,
      padding: const EdgeInsets.symmetric(
          horizontal: Retrowave.sp4, vertical: Retrowave.sp2),
      child: Row(
        children: [
          const Icon(Icons.warning_amber_rounded,
              size: 18, color: Retrowave.textInv),
          const SizedBox(width: Retrowave.sp2),
          Expanded(
            child: Text(
              'AT CUSTOM-SLOT LIMIT — $total posters (generic + new release combined). '
              'RR Movie Workshop caps the total at 12,987. '
              'Delete some posters before exporting or the overflow may not load in-game.',
              style: Retrowave.body(Retrowave.fsMeta, Retrowave.textInv),
            ),
          ),
        ],
      ),
    );
  }
}
