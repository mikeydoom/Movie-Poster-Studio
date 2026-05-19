import 'dart:convert';
import 'dart:io';
import 'package:path/path.dart' as p;

/// Stored path → absolute, resolved against the current app directory.
/// Empty stays empty.
String _absPath(String stored) {
  if (stored.isEmpty) return stored;
  if (p.isAbsolute(stored)) return stored;
  return p.normalize(p.join(ConfigManager.baseDir(), stored));
}

/// Absolute path → stored form. Paths under baseDir become relative so the
/// app folder is portable; everything else stays absolute.
String _relPath(String absolute) {
  if (absolute.isEmpty) return absolute;
  final base = ConfigManager.baseDir();
  try {
    final rel = p.relative(absolute, from: base);
    if (!rel.startsWith('..') && !p.isAbsolute(rel)) return rel;
  } catch (_) {}
  return absolute;
}

class AppConfig {
  String apiKey;
  PosterConverterConfig posterConverter;
  TmdbFetcherConfig tmdbFetcher;
  DuplicateFinderConfig duplicateFinder;
  RrExportPrefs rrExport;
  bool hasShownCredits;

  AppConfig({
    required this.apiKey,
    required this.posterConverter,
    required this.tmdbFetcher,
    required this.duplicateFinder,
    required this.rrExport,
    required this.hasShownCredits,
  });

  Map<String, dynamic> toJson() => {
        'api_key': apiKey,
        'poster_converter': posterConverter.toJson(),
        'tmdb_fetcher': tmdbFetcher.toJson(),
        'duplicate_finder': duplicateFinder.toJson(),
        'rr_export': rrExport.toJson(),
        'has_shown_credits': hasShownCredits,
      };

  static AppConfig fromJson(Map<String, dynamic> j, AppConfig def) {
    return AppConfig(
      apiKey: j['api_key'] as String? ?? def.apiKey,
      posterConverter: PosterConverterConfig.fromJson(
          j['poster_converter'] as Map<String, dynamic>? ?? {}, def.posterConverter),
      tmdbFetcher: TmdbFetcherConfig.fromJson(
          j['tmdb_fetcher'] as Map<String, dynamic>? ?? {}, def.tmdbFetcher),
      duplicateFinder: DuplicateFinderConfig.fromJson(
          j['duplicate_finder'] as Map<String, dynamic>? ?? {}, def.duplicateFinder),
      rrExport: RrExportPrefs.fromJson(
          j['rr_export'] as Map<String, dynamic>? ?? {}, def.rrExport),
      hasShownCredits: j['has_shown_credits'] as bool? ?? def.hasShownCredits,
    );
  }
}

class RrExportPrefs {
  String outputDir;
  String rarity;
  String standeeShape;
  int defaultLs;
  int defaultLsc;

  RrExportPrefs({
    required this.outputDir,
    required this.rarity,
    required this.standeeShape,
    required this.defaultLs,
    required this.defaultLsc,
  });

  Map<String, dynamic> toJson() => {
        'output_dir': _relPath(outputDir),
        'rarity': rarity,
        'standee_shape': standeeShape,
        'default_ls': defaultLs,
        'default_lsc': defaultLsc,
      };

  static RrExportPrefs fromJson(Map<String, dynamic> j, RrExportPrefs def) {
    return RrExportPrefs(
      outputDir: _absPath(j['output_dir'] as String? ?? def.outputDir),
      rarity: j['rarity'] as String? ?? def.rarity,
      standeeShape: j['standee_shape'] as String? ?? def.standeeShape,
      defaultLs: (j['default_ls'] as num?)?.toInt() ?? def.defaultLs,
      defaultLsc: (j['default_lsc'] as num?)?.toInt() ?? def.defaultLsc,
    );
  }
}

class PosterConverterConfig {
  String sourceFolder;
  String outputFolder;
  String normalOverlay;
  String nrOverlay;
  String profile;
  bool overwrite;

  PosterConverterConfig({
    required this.sourceFolder,
    required this.outputFolder,
    required this.normalOverlay,
    required this.nrOverlay,
    required this.profile,
    required this.overwrite,
  });

  Map<String, dynamic> toJson() => {
        'source_folder': _relPath(sourceFolder),
        'output_folder': _relPath(outputFolder),
        'normal_overlay': _relPath(normalOverlay),
        'nr_overlay': _relPath(nrOverlay),
        'profile': profile,
        'overwrite': overwrite,
      };

  static PosterConverterConfig fromJson(Map<String, dynamic> j, PosterConverterConfig def) {
    return PosterConverterConfig(
      sourceFolder: _absPath(j['source_folder'] as String? ?? def.sourceFolder),
      outputFolder: _absPath(j['output_folder'] as String? ?? def.outputFolder),
      normalOverlay: _absPath(j['normal_overlay'] as String? ?? def.normalOverlay),
      nrOverlay: _absPath(j['nr_overlay'] as String? ?? def.nrOverlay),
      profile: j['profile'] as String? ?? def.profile,
      overwrite: j['overwrite'] as bool? ?? def.overwrite,
    );
  }
}

class TmdbFetcherConfig {
  String outputFolder;
  String targetType;
  int maxMovies;
  int startYear;
  int endYear;
  String posterSize;
  String originCountry;
  String originalLanguage;
  /// 'any' | 'good_critic' | 'bad_critic' — maps to a TMDB vote_average
  /// window in tmdb_fetcher.dart. Matches RR_VHS_Tool's critic-tag buckets:
  ///   good_critic = stars 4.0–4.5  → vote_average 7.5..<9.5
  ///   bad_critic  = stars 0.0–1.5  → vote_average <3.0
  String ratingFilter;
  List<String> selectedGenres;
  Map<String, String> genreLimits;

  TmdbFetcherConfig({
    required this.outputFolder,
    required this.targetType,
    required this.maxMovies,
    required this.startYear,
    required this.endYear,
    required this.posterSize,
    required this.originCountry,
    required this.originalLanguage,
    required this.ratingFilter,
    required this.selectedGenres,
    required this.genreLimits,
  });

  Map<String, dynamic> toJson() => {
        'output_folder': _relPath(outputFolder),
        'target_type': targetType,
        'max_movies': maxMovies,
        'start_year': startYear,
        'end_year': endYear,
        'poster_size': posterSize,
        'origin_country': originCountry,
        'original_language': originalLanguage,
        'rating_filter': ratingFilter,
        'selected_genres': selectedGenres,
        'genre_limits': genreLimits,
      };

  static TmdbFetcherConfig fromJson(Map<String, dynamic> j, TmdbFetcherConfig def) {
    return TmdbFetcherConfig(
      outputFolder: _absPath(j['output_folder'] as String? ?? def.outputFolder),
      targetType: j['target_type'] as String? ?? def.targetType,
      maxMovies: (j['max_movies'] as num?)?.toInt() ?? def.maxMovies,
      startYear: (j['start_year'] as num?)?.toInt() ?? def.startYear,
      endYear: (j['end_year'] as num?)?.toInt() ?? def.endYear,
      posterSize: j['poster_size'] as String? ?? def.posterSize,
      originCountry: j['origin_country'] as String? ?? def.originCountry,
      originalLanguage: j['original_language'] as String? ?? def.originalLanguage,
      ratingFilter: j['rating_filter'] as String? ?? def.ratingFilter,
      selectedGenres: (j['selected_genres'] as List?)?.cast<String>() ?? def.selectedGenres,
      genreLimits: (j['genre_limits'] as Map?)?.map((k, v) => MapEntry(k.toString(), v.toString())) ??
          def.genreLimits,
    );
  }
}

class DuplicateFinderConfig {
  String scanFolder;
  DuplicateFinderConfig({required this.scanFolder});
  Map<String, dynamic> toJson() => {'scan_folder': _relPath(scanFolder)};
  static DuplicateFinderConfig fromJson(Map<String, dynamic> j, DuplicateFinderConfig def) {
    return DuplicateFinderConfig(
        scanFolder: _absPath(j['scan_folder'] as String? ?? def.scanFolder));
  }
}

class ConfigManager {
  static String? _baseDirOverride;

  static String baseDir() {
    if (_baseDirOverride != null) return _baseDirOverride!;
    return p.dirname(Platform.resolvedExecutable);
  }

  static void setBaseDir(String dir) {
    _baseDirOverride = dir;
  }

  /// Single source of truth for where the app stashes derived/tracking data
  /// (movies.json, csv_lists/, future sidecar files). Lives next to the .exe
  /// so the portable-install invariant still holds.
  static String metadataDir() => p.join(baseDir(), 'metadata');

  static AppConfig defaultConfig() {
    final base = baseDir();
    return AppConfig(
      apiKey: 'PASTEAPIHERE',
      posterConverter: PosterConverterConfig(
        // Layout: <base>/posters/<set>/<genre>/*.png for raw scrapes,
        //        <base>/posters/converted/<set>/<genre>/*.png for converted.
        sourceFolder: p.join(base, 'posters', 'generic'),
        outputFolder: p.join(base, 'posters', 'converted', 'generic'),
        normalOverlay: p.join(base, 'overlays', 'generic'),
        nrOverlay: p.join(base, 'overlays', 'new_releases'),
        profile: 'generic',
        overwrite: false,
      ),
      tmdbFetcher: TmdbFetcherConfig(
        outputFolder: base,
        targetType: 'generic',
        maxMovies: 100,
        startYear: 1980,
        endYear: 1990,
        posterSize: 'w780',
        originCountry: 'US',
        originalLanguage: 'en',
        ratingFilter: 'any',
        selectedGenres: const [
          'Action', 'Adult', 'Comedy', 'Police', 'Drama', 'Fantasy',
          'Horror', 'Kids', 'Romance', 'Sci-Fi', 'Western', 'Xmas'
        ],
        genreLimits: {},
      ),
      duplicateFinder: DuplicateFinderConfig(
        scanFolder: base,
      ),
      rrExport: RrExportPrefs(
        outputDir: p.join(base, 'RR_Export'),
        rarity: 'Common',
        standeeShape: 'C',
        defaultLs: 0,
        defaultLsc: 4,
      ),
      hasShownCredits: false,
    );
  }

  static File _configFile() => File(p.join(baseDir(), 'config.json'));

  static Future<AppConfig> load() async {
    final def = defaultConfig();
    final f = _configFile();
    if (!await f.exists()) return def;
    try {
      final raw = await f.readAsString();
      final j = jsonDecode(raw) as Map<String, dynamic>;
      return AppConfig.fromJson(j, def);
    } catch (_) {
      return def;
    }
  }

  static Future<void> save(AppConfig cfg) async {
    try {
      const encoder = JsonEncoder.withIndent('  ');
      await _configFile().writeAsString(encoder.convert(cfg.toJson()));
    } catch (_) {}
  }
}
