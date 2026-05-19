import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;
import 'package:http/http.dart' as http;
import 'package:path/path.dart' as p;
import 'config_manager.dart';
import 'movies_meta.dart';
import 'progress_bus.dart';

/// TMDB's hard server-side cap for /discover/movie pagination.
const int _maxApiPages = 500;
/// Pages fetched in parallel per batch. Keeps well under TMDB's ~40 req/10 s
/// rate limit while still being meaningfully faster than one-at-a-time.
const int _fetchBatchSize = 20;
const int _posterDownloadParallel = 16;
const int _keywordResolveParallel = 8;
const int _resultsPerPage = 20;

/// Single shared HTTP client.
final http.Client _client = http.Client();

const List<String> _adultKeywords = [
  'hardcore', 'sex', 'oral sex', 'lesbian sex', 'anal sex', 'threesome',
  'group sex', 'cunnilingus', 'double penetration', 'female nudity', 'fellatio',
  'hand job', 'blow job', 'fingering', 'masturbation', 'female masturbation',
  'female frontal nudity', 'female rear nudity', 'cumshot', 'ejaculation',
  'dildo', 'labia', 'vagina', 'vulva', 'leg spreading', 'male rear nudity',
  'pubic hair', 'male nudity', 'semen', 'male frontal nudity', 'erection',
  'penis', 'testicles', 'penetration', 'lesbianism', 'deep throat', 'gang bang',
  'double blow job', 'vibrator', 'face sitting', 'cum swallowing', 'sperm',
  'ass to mouth', 'orgy', 'foursome', 'anilingus', 'ejaculation on breasts',
  'lesbian threesome',
];

List<int>? _cachedAdultKeywordIds;

const List<String> _musicKeywords = [
  'musical',
  'stage musical',
];
List<int>? _cachedMusicKeywordIds;

const _xmasKeywordIds = [65];
const _sportsKeywordIds = [6075, 294708];

Future<List<int>> _resolveAdultKeywordIds(String apiKey, ProgressBus bus) async {
  if (_cachedAdultKeywordIds != null) return _cachedAdultKeywordIds!;
  bus.emitLog('Resolving ${_adultKeywords.length} adult keyword IDs from TMDB…');
  final out = List<int?>.filled(_adultKeywords.length, null);
  final queue = List<int>.generate(_adultKeywords.length, (i) => i);

  Future<void> worker() async {
    while (queue.isNotEmpty) {
      final idx = queue.removeAt(0);
      final kw = _adultKeywords[idx];
      try {
        final url = Uri.https('api.themoviedb.org', '/3/search/keyword', {
          'api_key': apiKey,
          'query': kw,
        });
        final r = await _getWithRetry(url);
        if (r.statusCode != 200) continue;
        final j = jsonDecode(r.body) as Map<String, dynamic>;
        final results = (j['results'] as List?) ?? const [];
        if (results.isEmpty) continue;
        int? id;
        for (final m in results) {
          if ((m['name'] as String?)?.toLowerCase() == kw.toLowerCase()) {
            id = (m['id'] as num).toInt();
            break;
          }
        }
        id ??= (results.first['id'] as num?)?.toInt();
        if (id != null) out[idx] = id;
      } catch (_) {}
    }
  }

  await Future.wait(List.generate(_keywordResolveParallel, (_) => worker()));
  final ids = out.whereType<int>().toList();
  bus.emitLog('  → resolved ${ids.length}/${_adultKeywords.length} keyword(s)');
  _cachedAdultKeywordIds = ids;
  return ids;
}

Future<List<int>> _resolveMusicKeywordIds(String apiKey, ProgressBus bus) async {
  if (_cachedMusicKeywordIds != null) return _cachedMusicKeywordIds!;
  bus.emitLog('Resolving ${_musicKeywords.length} music keyword IDs from TMDB…');
  final ids = <int>[];
  for (final kw in _musicKeywords) {
    try {
      final url = Uri.https('api.themoviedb.org', '/3/search/keyword', {
        'api_key': apiKey,
        'query': kw,
      });
      final r = await _getWithRetry(url);
      if (r.statusCode != 200) continue;
      final j = jsonDecode(r.body) as Map<String, dynamic>;
      final results = (j['results'] as List?) ?? const [];
      for (final m in results) {
        if ((m['name'] as String?)?.toLowerCase() == kw.toLowerCase()) {
          ids.add((m['id'] as num).toInt());
          break;
        }
      }
    } catch (_) {}
  }
  bus.emitLog('  → resolved ${ids.length}/${_musicKeywords.length} keyword(s)');
  _cachedMusicKeywordIds = ids;
  return ids;
}

class GenreRequest {
  final String name;
  final int? id;
  final int maxMovies;
  GenreRequest(this.name, this.id, this.maxMovies);
}

class _Movie {
  final String title;
  final String cleanTitle;
  final String year;
  final String? posterPath;
  final int id;
  final double voteAverage;
  final List<int> genreIds;
  _Movie(this.title, this.cleanTitle, this.year, this.posterPath, this.id, this.voteAverage, this.genreIds);
}

String _cleanTitle(String t) => t.replaceAll(RegExp('[:\'"?!]'), '');

String _safeFileName(String stem) =>
    stem.replaceAll(RegExp(r'[<>:"/\\|?*]'), '').replaceAll(' ', '_');

Future<http.Response> _getWithRetry(Uri url, {int retries = 3}) async {
  Object? lastErr;
  for (var attempt = 0; attempt < retries; attempt++) {
    try {
      final r = await _client.get(url).timeout(const Duration(seconds: 20));
      if (r.statusCode >= 500 && r.statusCode <= 504) {
        await Future.delayed(Duration(milliseconds: (500 * (1 << attempt))));
        continue;
      }
      return r;
    } catch (e) {
      lastErr = e;
      await Future.delayed(Duration(milliseconds: (500 * (1 << attempt))));
    }
  }
  throw lastErr ?? Exception('Request failed');
}

Future<(List<dynamic>?, int)> _fetchDiscoverPage(
  String apiKey,
  Map<String, String> filterParams,
  int startYear,
  int endYear,
  int page,
  ProgressBus bus,
) async {
  final params = <String, String>{
    'api_key': apiKey,
    'primary_release_date.gte': '$startYear-01-01',
    'primary_release_date.lte': '$endYear-12-31',
    'sort_by': 'popularity.desc',
    'include_adult': 'true',
    'include_video': 'false',
    'page': '$page',
    ...filterParams,
  };
  final url = Uri.https('api.themoviedb.org', '/3/discover/movie', params);
  try {
    final resp = await _getWithRetry(url);
    if (resp.statusCode != 200) {
      bus.emitLog('  API error ${resp.statusCode} for page $page');
      return (null, 0);
    }
    final data = jsonDecode(resp.body) as Map<String, dynamic>;
    final totalPages = (data['total_pages'] as num?)?.toInt() ?? 1;
    return ((data['results'] as List?) ?? const [], totalPages);
  } catch (e) {
    bus.emitLog('  API fetch error (page $page): $e');
    return (null, 0);
  }
}

_Movie? _movieFromJson(
  dynamic m,
  int startYear,
  int endYear,
  Set<int> usedIds,
) {
  final mid = (m['id'] as num?)?.toInt();
  if (mid == null || usedIds.contains(mid)) return null;
  final rel = (m['release_date'] as String?) ?? '';
  if (rel.length < 4) return null;
  final yearStr = rel.substring(0, 4);
  final yr = int.tryParse(yearStr);
  if (yr == null || yr < startYear || yr > endYear) return null;
  usedIds.add(mid);
  final origTitle = (m['title'] as String?) ?? 'Unknown';
  final rawIds = (m['genre_ids'] as List?) ?? const [];
  final genreIds = rawIds.whereType<num>().map((n) => n.toInt()).toList();
  return _Movie(
    origTitle,
    _cleanTitle(origTitle),
    yearStr,
    m['poster_path'] as String?,
    mid,
    (m['vote_average'] as num?)?.toDouble() ?? 0.0,
    genreIds,
  );
}

Future<List<_Movie>> _fetchMovies(
  String apiKey,
  Map<String, String> filterParams,
  int startYear,
  int endYear,
  int maxMovies,
  Set<int> usedIds,
  ProgressBus bus,
) async {
  final movies = <_Movie>[];
  var apiTotalPages = _maxApiPages;
  var nextPage = 1;

  while (movies.length < maxMovies && nextPage <= apiTotalPages) {
    final remaining = maxMovies - movies.length;
    final batchSize = ((remaining / _resultsPerPage).ceil() + 1)
        .clamp(1, _fetchBatchSize);
    final batchEnd = math.min(nextPage + batchSize - 1, apiTotalPages);

    final rawBatch = await Future.wait([
      for (var pg = nextPage; pg <= batchEnd; pg++)
        _fetchDiscoverPage(apiKey, filterParams, startYear, endYear, pg, bus),
    ]);

    var anyResults = false;
    for (final (results, totalPages) in rawBatch) {
      if (totalPages > 0) {
        apiTotalPages = math.min(totalPages, _maxApiPages);
      }
      if (results == null || results.isEmpty) {
        return movies;
      }
      anyResults = true;
      for (final m in results) {
        if (movies.length >= maxMovies) return movies;
        final mv = _movieFromJson(m, startYear, endYear, usedIds);
        if (mv != null) movies.add(mv);
      }
    }
    if (!anyResults) break;
    nextPage = batchEnd + 1;
  }

  return movies;
}

Future<bool> _downloadPoster(String posterPath, String filepath, String posterSize, ProgressBus bus) async {
  try {
    final url = Uri.parse('https://image.tmdb.org/t/p/$posterSize$posterPath');
    final r = await _getWithRetry(url);
    if (r.statusCode == 200) {
      await File(filepath).writeAsBytes(r.bodyBytes);
      return true;
    }
  } catch (e) {
    bus.emitLog('    Download error for ${p.basename(filepath)}: $e');
  }
  return false;
}

/// Fetch movies for one genre (no download). Returns the movie list.
/// Per-genre usedIds prevents the same movie being double-counted within
/// this genre's pagination; cross-genre dedup happens later via priority.
Map<String, String> _ratingFilterParams(String ratingFilter) {
  switch (ratingFilter) {
    case 'good_critic':
      return {'vote_average.gte': '7.5', 'vote_average.lte': '9.49'};
    case 'bad_critic':
      return {'vote_average.lte': '2.99'};
    case 'any':
    default:
      return const {};
  }
}

Future<void> runTmdbFetcher({
  required String outputDir,
  required String apiKey,
  required List<GenreRequest> genres,
  required int startYear,
  required int endYear,
  required String posterSize,
  required String posterSubfolder,
  required String originCountry,
  required String originalLanguage,
  required String ratingFilter,
  required ProgressBus bus,
}) async {
  final country = originCountry.isEmpty ? 'ANY' : originCountry;
  final lang = originalLanguage.isEmpty ? 'ANY' : originalLanguage;
  bus.emitLog('Starting TMDB fetcher ($startYear-$endYear), posters to: $posterSubfolder');
  bus.emitLog('Filters: country=$country, language=$lang, rating=$ratingFilter');

  final geoFilter = <String, String>{};
  if (originCountry.isNotEmpty) {
    geoFilter['with_origin_country'] = originCountry;
    geoFilter['region'] = originCountry;
  }
  if (originalLanguage.isNotEmpty) {
    geoFilter['with_original_language'] = originalLanguage;
  }
  final ratingParams = _ratingFilterParams(ratingFilter);
  final base = p.absolute(outputDir);

  // ── Phase 1+2: Per-genre sequential fetch ────────────────────────
  // Each genre is scraped independently. All fetched movies stay in the
  // genre they were found by — no genre override resolution.
  final perGenre = <String, List<_DownloadTask>>{};
  final pendingMeta = <String, MovieMeta>{};

  for (var gi = 0; gi < genres.length; gi++) {
    final g = genres[gi];
    final target = g.maxMovies;
    if (target <= 0) continue;

    bus.emitLog('\n${g.name}: scraping up to $target movie(s)...');

    Map<String, String> filterParams;

    if (g.name == 'Adult') {
      final ids = await _resolveAdultKeywordIds(apiKey, bus);
      if (ids.isEmpty) {
        bus.emitLog('  Skipping ${g.name}: no adult keyword IDs resolved');
        bus.emitStep(gi + 1, genres.length, 'Scraping ${g.name}');
        continue;
      }
      filterParams = {
        'with_keywords': ids.join('|'),
        'certification_country': 'US',
        'certification': 'NC-17',
        ...geoFilter,
        ...ratingParams,
      };
    } else if (g.name == 'Music') {
      final ids = await _resolveMusicKeywordIds(apiKey, bus);
      if (ids.isEmpty) {
        bus.emitLog('  Skipping ${g.name}: no music keyword IDs resolved');
        bus.emitStep(gi + 1, genres.length, 'Scraping ${g.name}');
        continue;
      }
      filterParams = {
        'with_keywords': ids.join('|'),
        ...geoFilter,
        ...ratingParams,
      };
    } else if (g.name == 'Xmas') {
      if (_xmasKeywordIds.isEmpty) { bus.emitLog('  Skipping ${g.name}: no keyword IDs'); continue; }
      filterParams = {
        'with_keywords': _xmasKeywordIds.join('|'),
        ...geoFilter,
        ...ratingParams,
      };
    } else if (g.name == 'Sports') {
      if (_sportsKeywordIds.isEmpty) { bus.emitLog('  Skipping ${g.name}: no keyword IDs'); continue; }
      filterParams = {
        'with_keywords': _sportsKeywordIds.join('|'),
        ...geoFilter,
        ...ratingParams,
      };
    } else if (g.id != null) {
      filterParams = {
        'with_genres': '${g.id}',
        ...geoFilter,
        ...ratingParams,
      };
    } else {
      bus.emitLog('  Skipping ${g.name}: no genre ID');
      bus.emitStep(gi + 1, genres.length, 'Scraping ${g.name}');
      continue;
    }

    final usedIds = <int>{};

    while ((perGenre[g.name]?.length ?? 0) < target) {
      final current = perGenre[g.name]?.length ?? 0;
      final needed = target - current;
      // Request at most `needed` movies to avoid overshooting the target.
      // `take(needed)` handles page-rounding edge cases.
      final batchRequest = needed.clamp(20, 200);

      final movies = await _fetchMovies(
          apiKey, filterParams, startYear, endYear, batchRequest, usedIds, bus);

      if (movies.isEmpty) {
        bus.emitLog('  ${g.name}: TMDB exhausted (got ${perGenre[g.name]?.length ?? 0}/$target)');
        break;
      }

      for (final mv in movies.take(needed)) {
        perGenre.putIfAbsent(g.name, () => []);
        final filename = _safeFileName('${mv.cleanTitle}_${mv.year}.png');
        final outPath = p.join(base, posterSubfolder, g.name.toLowerCase(), filename);
        perGenre[g.name]!.add(_DownloadTask(mv, filename, outPath));
        pendingMeta[filename] = MovieMeta.fromVote(
          tmdbId: mv.id,
          title: mv.title,
          year: mv.year,
          voteAverage: mv.voteAverage,
        );
      }

      bus.emitLog('  ${g.name}: fetched ${movies.length}, total ${perGenre[g.name]?.length ?? 0}/$target');
    }

    bus.emitStep(gi + 1, genres.length, 'Scraping ${g.name}');
  }

  // ── Phase 3: Write per-genre CSVs & download posters ────────────
  var totalDownloaded = 0;

  for (final entry in perGenre.entries) {
    final genreName = entry.key;
    final tasks = entry.value;

    // CSV
    final csvDir = Directory(p.join(ConfigManager.metadataDir(), 'csv_lists'));
    await csvDir.create(recursive: true);
    final csvFile = File(p.join(csvDir.path, '${genreName.toLowerCase()}_${startYear}_$endYear.csv'));
    final csvBuf = StringBuffer();
    for (final t in tasks) {
      csvBuf.writeln('${t.movie.title} ${t.movie.year}');
    }
    await csvFile.writeAsString(csvBuf.toString());
    bus.emitLog('\n$genreName: ${tasks.length} movie(s) -> ${p.basename(csvFile.path)}');

    // Create folder
    final posterFolder = Directory(p.join(base, posterSubfolder, genreName.toLowerCase()));
    await posterFolder.create(recursive: true);

    // Build download queue
    final dlQueue = <_DownloadTask>[];
    for (final t in tasks) {
      if (t.movie.posterPath == null) continue;
      if (!await File(t.outPath).exists()) {
        dlQueue.add(t);
      }
    }

    if (dlQueue.isEmpty) {
      bus.emitLog('  All posters already exist for $genreName');
      continue;
    }

    var downloaded = 0;
    var completed = 0;
    final total = dlQueue.length;
    final queue = List<_DownloadTask>.from(dlQueue);

    Future<void> dlWorker() async {
      while (queue.isNotEmpty) {
        final task = queue.removeAt(0);
        final ok = await _downloadPoster(task.movie.posterPath!, task.outPath, posterSize, bus);
        if (ok) downloaded++;
        completed++;
        bus.emitFile(completed, total);
      }
    }

    await Future.wait(List.generate(_posterDownloadParallel, (_) => dlWorker()));
    bus.emitLog('  Posters downloaded for $genreName: $downloaded');
    totalDownloaded += downloaded;
  }

  // ── Phase 4: Persist metadata ───────────────────────────────────
  if (pendingMeta.isNotEmpty) {
    await MoviesMetaStore.upsert(pendingMeta.entries);
    bus.emitLog('Updated movies.json with ${pendingMeta.length} entry(ies).');
  }
  bus.emitLog('\n✅ TMDB fetch complete. Total downloaded: $totalDownloaded');
  bus.emitDone('tmdb');
}

class _DownloadTask {
  final _Movie movie;
  final String filename;
  final String outPath;
  _DownloadTask(this.movie, this.filename, this.outPath);
}
