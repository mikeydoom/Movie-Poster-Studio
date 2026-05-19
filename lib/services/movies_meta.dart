import 'dart:convert';
import 'dart:io';
import 'package:path/path.dart' as p;
import 'config_manager.dart';
import 'rr_rating.dart';

/// Per-poster metadata captured from TMDB. Keyed by filename so a single
/// movie that lives in multiple genres / both poster sets gets one entry.
class MovieMeta {
  final int? tmdbId;
  final String title; // human-readable, spaces preserved
  final String year;
  final double voteAverage;
  final double stars;
  final int last2;
  final bool isOld;

  MovieMeta({
    required this.tmdbId,
    required this.title,
    required this.year,
    required this.voteAverage,
    required this.stars,
    required this.last2,
    this.isOld = false,
  });

  factory MovieMeta.fromVote({
    int? tmdbId,
    required String title,
    required String year,
    required double voteAverage,
  }) {
    final stars = rrStarsFromVoteAverage(voteAverage);
    return MovieMeta(
      tmdbId: tmdbId,
      title: title,
      year: year,
      voteAverage: voteAverage,
      stars: stars,
      last2: rrStarToLast2(stars),
    );
  }

  Map<String, dynamic> toJson() => {
        if (tmdbId != null) 'tmdb_id': tmdbId,
        'title': title,
        'year': year,
        'vote_average': voteAverage,
        'stars': stars,
        'last2': last2,
        if (isOld) 'is_old': true,
      };

  static MovieMeta fromJson(Map<String, dynamic> j) => MovieMeta(
        tmdbId: (j['tmdb_id'] as num?)?.toInt(),
        title: j['title'] as String? ?? '',
        year: j['year']?.toString() ?? '',
        voteAverage: (j['vote_average'] as num?)?.toDouble() ?? 0.0,
        stars: (j['stars'] as num?)?.toDouble() ?? 0.0,
        last2: (j['last2'] as num?)?.toInt() ?? 2,
        isOld: j['is_old'] as bool? ?? false,
      );
}

/// Persists MovieMeta records to `<baseDir>/metadata/movies.json`.
class MoviesMetaStore {
  static File _file() =>
      File(p.join(ConfigManager.metadataDir(), 'movies.json'));

  /// Pre-`metadata/` location. Auto-migrated to the new path on first load.
  static File _legacyFile() =>
      File(p.join(ConfigManager.baseDir(), 'movies.json'));

  /// If a movies.json from before the metadata/ reshuffle exists at the root,
  /// move it into the new folder. Run inside load() so we never need a
  /// separate migration step.
  static Future<void> _migrateLegacyLocation() async {
    final legacy = _legacyFile();
    final current = _file();
    if (await current.exists()) return;
    if (!await legacy.exists()) return;
    await Directory(ConfigManager.metadataDir()).create(recursive: true);
    try {
      await legacy.rename(current.path);
    } catch (_) {
      // Cross-volume rename (rare on a portable install) — fall back to copy+delete.
      await current.writeAsBytes(await legacy.readAsBytes());
      await legacy.delete();
    }
  }

  static Future<Map<String, MovieMeta>> load() async {
    await _migrateLegacyLocation();
    final f = _file();
    if (!await f.exists()) return {};
    try {
      final raw = await f.readAsString();
      final j = jsonDecode(raw) as Map<String, dynamic>;
      return j.map((k, v) => MapEntry(k, MovieMeta.fromJson(v as Map<String, dynamic>)));
    } catch (e) {
      stderr.writeln('movies.json parse error — returning empty store. Cause: $e');
      return {};
    }
  }

  static Future<void> save(Map<String, MovieMeta> entries) async {
    const enc = JsonEncoder.withIndent('  ');
    final sortedKeys = entries.keys.toList()..sort();
    final out = <String, dynamic>{for (final k in sortedKeys) k: entries[k]!.toJson()};
    await Directory(ConfigManager.metadataDir()).create(recursive: true);
    await _file().writeAsString(enc.convert(out));
  }

  /// Merge new entries into the existing store and persist.
  static Future<void> upsert(Iterable<MapEntry<String, MovieMeta>> newEntries) async {
    final existing = await load();
    for (final e in newEntries) {
      existing[e.key] = e.value;
    }
    await save(existing);
  }

  /// Update the star rating for a set of filenames. Creates a minimal
  /// MovieMeta entry (title/year parsed from the filename) if no row exists
  /// yet. `vote_average` is stored as `stars * 2` so a subsequent rescrape's
  /// bucketing would map back to the same star tier.
  static Future<void> setRatingFor(
      Iterable<String> filenames, double stars) async {
    final last2 = rrStarToLast2(stars);
    final existing = await load();
    for (final fn in filenames) {
      final cur = existing[fn];
      if (cur != null) {
        existing[fn] = MovieMeta(
          tmdbId: cur.tmdbId,
          title: cur.title,
          year: cur.year,
          voteAverage: stars * 2,
          stars: stars,
          last2: last2,
          isOld: cur.isOld,
        );
      } else {
        final parsed = parseFilename(fn);
        existing[fn] = MovieMeta(
          tmdbId: null,
          title: parsed.title,
          year: parsed.year,
          voteAverage: stars * 2,
          stars: stars,
          last2: last2,
        );
      }
    }
    await save(existing);
  }

  /// Toggle the Old flag for a set of filenames. Creates a minimal entry if
  /// none exists yet.
  static Future<void> setOldFor(
      Iterable<String> filenames, bool isOld) async {
    final existing = await load();
    for (final fn in filenames) {
      final cur = existing[fn];
      if (cur != null) {
        existing[fn] = MovieMeta(
          tmdbId: cur.tmdbId,
          title: cur.title,
          year: cur.year,
          voteAverage: cur.voteAverage,
          stars: cur.stars,
          last2: cur.last2,
          isOld: isOld,
        );
      } else {
        final parsed = parseFilename(fn);
        existing[fn] = MovieMeta(
          tmdbId: null,
          title: parsed.title,
          year: parsed.year,
          voteAverage: 0.0,
          stars: 0.0,
          last2: 2,
          isOld: isOld,
        );
      }
    }
    await save(existing);
  }

  /// Parse `Title_With_Underscores_1986.png` → ("Title With Underscores", "1986").
  /// Used as a fallback when a poster has no movies.json entry.
  static ({String title, String year}) parseFilename(String filename) {
    final stem = p.basenameWithoutExtension(filename);
    final m = RegExp(r'^(.+?)_(\d{4})$').firstMatch(stem);
    if (m != null) {
      return (title: m.group(1)!.replaceAll('_', ' '), year: m.group(2)!);
    }
    return (title: stem.replaceAll('_', ' '), year: '');
  }
}
