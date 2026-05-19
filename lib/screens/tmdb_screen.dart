import 'dart:io';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:path/path.dart' as p;
import '../services/app_state.dart';
import '../services/config_manager.dart';
import '../services/poster_converter.dart';
import '../services/tmdb_fetcher.dart';
import '../theme/retrowave.dart';
import '../widgets/neon_button.dart';
import '../widgets/neon_field.dart';
import '../widgets/neon_panel.dart';

class TmdbScreen extends StatefulWidget {
  final AppState state;
  const TmdbScreen({super.key, required this.state});

  @override
  State<TmdbScreen> createState() => _TmdbScreenState();
}

class _TmdbScreenState extends State<TmdbScreen> {
  static const _countries = <MapEntry<String, String>>[
    MapEntry('', 'Any'),
    MapEntry('US', 'United States'),
    MapEntry('GB', 'United Kingdom'),
    MapEntry('CA', 'Canada'),
    MapEntry('AU', 'Australia'),
    MapEntry('IE', 'Ireland'),
    MapEntry('NZ', 'New Zealand'),
    MapEntry('FR', 'France'),
    MapEntry('DE', 'Germany'),
    MapEntry('IT', 'Italy'),
    MapEntry('ES', 'Spain'),
    MapEntry('PT', 'Portugal'),
    MapEntry('NL', 'Netherlands'),
    MapEntry('BE', 'Belgium'),
    MapEntry('SE', 'Sweden'),
    MapEntry('NO', 'Norway'),
    MapEntry('DK', 'Denmark'),
    MapEntry('FI', 'Finland'),
    MapEntry('IS', 'Iceland'),
    MapEntry('PL', 'Poland'),
    MapEntry('CZ', 'Czech Republic'),
    MapEntry('RU', 'Russia'),
    MapEntry('UA', 'Ukraine'),
    MapEntry('TR', 'Turkey'),
    MapEntry('GR', 'Greece'),
    MapEntry('JP', 'Japan'),
    MapEntry('KR', 'South Korea'),
    MapEntry('CN', 'China'),
    MapEntry('HK', 'Hong Kong'),
    MapEntry('TW', 'Taiwan'),
    MapEntry('IN', 'India'),
    MapEntry('TH', 'Thailand'),
    MapEntry('PH', 'Philippines'),
    MapEntry('ID', 'Indonesia'),
    MapEntry('MX', 'Mexico'),
    MapEntry('BR', 'Brazil'),
    MapEntry('AR', 'Argentina'),
    MapEntry('CL', 'Chile'),
    MapEntry('CO', 'Colombia'),
    MapEntry('ZA', 'South Africa'),
    MapEntry('NG', 'Nigeria'),
    MapEntry('EG', 'Egypt'),
    MapEntry('IL', 'Israel'),
    MapEntry('IR', 'Iran'),
    MapEntry('SA', 'Saudi Arabia'),
  ];

  static const _languages = <MapEntry<String, String>>[
    MapEntry('', 'Any'),
    MapEntry('en', 'English'),
    MapEntry('es', 'Spanish'),
    MapEntry('fr', 'French'),
    MapEntry('de', 'German'),
    MapEntry('it', 'Italian'),
    MapEntry('pt', 'Portuguese'),
    MapEntry('nl', 'Dutch'),
    MapEntry('sv', 'Swedish'),
    MapEntry('no', 'Norwegian'),
    MapEntry('da', 'Danish'),
    MapEntry('fi', 'Finnish'),
    MapEntry('is', 'Icelandic'),
    MapEntry('pl', 'Polish'),
    MapEntry('cs', 'Czech'),
    MapEntry('ru', 'Russian'),
    MapEntry('uk', 'Ukrainian'),
    MapEntry('tr', 'Turkish'),
    MapEntry('el', 'Greek'),
    MapEntry('ja', 'Japanese'),
    MapEntry('ko', 'Korean'),
    MapEntry('zh', 'Chinese'),
    MapEntry('hi', 'Hindi'),
    MapEntry('ta', 'Tamil'),
    MapEntry('te', 'Telugu'),
    MapEntry('th', 'Thai'),
    MapEntry('id', 'Indonesian'),
    MapEntry('ar', 'Arabic'),
    MapEntry('he', 'Hebrew'),
    MapEntry('fa', 'Persian'),
  ];

  static const _genreMap = <MapEntry<String, int?>>[
    MapEntry('Action', 28),
    MapEntry('Adult', null),
    MapEntry('Adventure', 12),
    MapEntry('Comedy', 35),
    MapEntry('Police', 80),
    MapEntry('Drama', 18),
    MapEntry('Fantasy', 14),
    MapEntry('Horror', 27),
    MapEntry('History', 36),
    MapEntry('Documentary', 99),
    MapEntry('Kids', 10751),
    MapEntry('Music', null),
    MapEntry('Romance', 10749),
    MapEntry('Sci-Fi', 878),
    MapEntry('Sports', null),
    MapEntry('Thriller', 53),
    MapEntry('Western', 37),
    MapEntry('Xmas', null),
  ];

  late final TextEditingController _apiKey;
  late final TextEditingController _outDir;
  late final TextEditingController _startYear;
  late final TextEditingController _endYear;
  late String _targetType;
  late String _posterSize;
  late String _country;
  late String _language;
  late String _ratingFilter;
  final Map<String, TextEditingController> _genreCtrl = {};
  String? _runError;

  /// Rating-filter dropdown options. Keys match what `_ratingFilterParams`
  /// reads in tmdb_fetcher.dart — keep them in sync.
  static const _ratingOptions = <MapEntry<String, String>>[
    MapEntry('any', 'Any'),
    MapEntry('good_critic', 'Good Critic'),
    MapEntry('bad_critic', 'Bad Critic'),
  ];

  /// Genres disabled when target type is New Releases.
  static const _nrDisabled = {'Adult', 'Thriller', 'Music', 'History', 'Documentary', 'Sports', 'Adventure'};

  /// Shorter label for the per-genre field display. Only Documentary differs.
  static String _shortLabel(String key) =>
      key == 'Documentary' ? 'DOCU' : Retrowave.displayName(key).toUpperCase();
  @override
  void initState() {
    super.initState();
    final c = widget.state.config;
    _apiKey = TextEditingController(text: c.apiKey);
    _outDir = TextEditingController(text: c.tmdbFetcher.outputFolder);
    _startYear = TextEditingController(text: c.tmdbFetcher.startYear.toString());
    _endYear = TextEditingController(text: c.tmdbFetcher.endYear.toString());
    _targetType = c.tmdbFetcher.targetType;
    _posterSize = c.tmdbFetcher.posterSize;
    _country = _countries.any((e) => e.key == c.tmdbFetcher.originCountry)
        ? c.tmdbFetcher.originCountry
        : '';
    _ratingFilter =
        _ratingOptions.any((e) => e.key == c.tmdbFetcher.ratingFilter)
            ? c.tmdbFetcher.ratingFilter
            : 'any';
    _language = _languages.any((e) => e.key == c.tmdbFetcher.originalLanguage)
        ? c.tmdbFetcher.originalLanguage
        : '';
    for (final g in _genreMap) {
      final saved = c.tmdbFetcher.genreLimits[g.key] ?? '';
      _genreCtrl[g.key] = TextEditingController(text: saved);
    }
    if (_targetType == 'new_release') {
      for (final g in _genreMap) {
        if (_nrDisabled.contains(g.key)) {
          _genreCtrl[g.key]?.text = '0';
        }
      }
    }
  }

  @override
  void dispose() {
    _apiKey.dispose();
    _outDir.dispose();
    _startYear.dispose();
    _endYear.dispose();
    for (final c in _genreCtrl.values) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _browseOut() async {
    final dir = await FilePicker.platform.getDirectoryPath();
    if (dir != null) {
      setState(() => _outDir.text = dir);
      widget.state.config.tmdbFetcher.outputFolder = dir;
      await widget.state.save();
    }
  }

  void _persist() {
    final c = widget.state.config;
    c.apiKey = _apiKey.text.trim();
    c.tmdbFetcher.outputFolder = _outDir.text.trim();
    c.tmdbFetcher.targetType = _targetType;
    c.tmdbFetcher.startYear = int.tryParse(_startYear.text) ?? c.tmdbFetcher.startYear;
    c.tmdbFetcher.endYear = int.tryParse(_endYear.text) ?? c.tmdbFetcher.endYear;
    c.tmdbFetcher.posterSize = _posterSize;
    c.tmdbFetcher.originCountry = _country;
    c.tmdbFetcher.originalLanguage = _language;
    c.tmdbFetcher.ratingFilter = _ratingFilter;
    c.tmdbFetcher.genreLimits = {
      for (final e in _genreCtrl.entries) e.key: e.value.text.trim(),
    };
    widget.state.save();
  }

  Future<void> _startFetch() async {
    _persist();
    final s = widget.state;

    // Guard: the default config ships api_key = "PASTEAPIHERE". TMDB returns
    // 401 with that, every poster fails to download, and the user just sees a
    // bunch of error lines in the status log. Surface the cause up front.
    final apiKey = _apiKey.text.trim();
    if (apiKey.isEmpty || apiKey == 'PASTEAPIHERE') {
      await showDialog<void>(
        context: context,
        builder: (_) => AlertDialog(
          backgroundColor: Retrowave.panel,
          shape: const RoundedRectangleBorder(
              borderRadius: BorderRadius.all(Radius.circular(2))),
          title: Text('TMDB API KEY REQUIRED',
              style: Retrowave.heading(Retrowave.fsSec, Retrowave.pink)),
          content: Text(
            'No TMDB API key is set. Paste your v3 API key into the '
            '"TMDB API Key" field at the top of this tab before scraping.\n\n'
            'Get one free at: https://www.themoviedb.org/settings/api',
            style: Retrowave.body(),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context),
              child: Text('OK',
                  style: Retrowave.body(Retrowave.fsBody, Retrowave.cyan)),
            ),
          ],
        ),
      );
      return;
    }

    // Raw-scrape destination: <outputDir>/posters/<set>/<genre>/*.png.
    // tmdb_fetcher.dart calls p.join(outputDir, subfolder, genre); embedded
    // forward slashes are absorbed correctly by path.join.
    final subfolder = _targetType == 'generic'
        ? 'posters/generic'
        : 'posters/new_releases';

    final genres = <GenreRequest>[];
    for (final g in _genreMap) {
      final text = _genreCtrl[g.key]!.text.trim();
      final max = text.isEmpty ? 100 : int.tryParse(text) ?? 100;
      if (max > 0) genres.add(GenreRequest(g.key, g.value, max));
    }

    s.appendLog('=== TMDB Fetcher Started ===');
    s.appendLog('Output: ${_outDir.text.trim()}, Target folder: $subfolder, Years: ${_startYear.text}-${_endYear.text}');
    s.resetProgress();
    s.tmdbRunning = true;
    _runError = null;
    if (mounted) setState(() {});

    try {
      await runTmdbFetcher(
        outputDir: _outDir.text.trim(),
        apiKey: _apiKey.text.trim(),
        genres: genres,
        startYear: int.tryParse(_startYear.text) ?? s.config.tmdbFetcher.startYear,
        endYear: int.tryParse(_endYear.text) ?? s.config.tmdbFetcher.endYear,
        posterSize: _posterSize,
        posterSubfolder: subfolder,
        originCountry: _country,
        originalLanguage: _language,
        ratingFilter: _ratingFilter,
        bus: s.bus,
      );

      // Fetch done — convert the freshly scraped posters.
      if (!mounted) return;
      final profile =
          _targetType == 'generic' ? 'generic' : 'new_release';
      final setLeaf = profile == 'generic' ? 'generic' : 'new_releases';
      // Source = wherever TMDB just downloaded (may differ from base dir).
      final src = p.join(_outDir.text.trim(), subfolder);
      // Output = standard converted folder (portable location).
      final out = p.join(ConfigManager.baseDir(), 'posters', 'converted', setLeaf);
      // Overlay path from the converter config (user may have customized it).
      final overlay = profile == 'generic'
          ? s.config.posterConverter.normalOverlay
          : s.config.posterConverter.nrOverlay;
      final overwrite = s.config.posterConverter.overwrite;

      if (!Directory(overlay).existsSync()) {
        _runError = 'Overlay folder missing: $overlay\n'
            'Check your overlay paths in config or create the folder.';
        s.appendLog('[AUTO-CONVERT] $_runError');
        if (mounted) setState(() {});
        return;
      }
      if (!Directory(src).existsSync()) {
        _runError = 'Source folder missing: $src\n'
            'The TMDB fetch may have downloaded to a different path.';
        s.appendLog('[AUTO-CONVERT] $_runError');
        if (mounted) setState(() {});
        return;
      }

      s.appendLog('=== Auto-Converting $profile posters ===');
      s.appendLog('Source: $src');
      s.appendLog('Output: $out');
      s.appendLog('Overlay: $overlay');
      s.converterRunning = true;
      if (mounted) setState(() {});
      runConverter(
        srcFolder: src,
        outFolder: out,
        overlaysFolder: overlay,
        profile: profile,
        overwrite: overwrite,
        bus: s.bus,
      );
    } catch (e) {
      _runError = '$e';
      s.appendLog('[AUTO-CONVERT] ERROR: $e');
      s.tmdbRunning = false;
      if (mounted) setState(() {});
    }
  }

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          NeonPanel(
            title: 'TMDB CONNECTION',
            borderColor: Retrowave.cyan,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                NeonField(
                    controller: _apiKey,
                    label: 'TMDB API Key',
                    onChanged: (_) => _persist()),
                const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(child: NeonField(controller: _outDir, label: 'Output Folder')),
                    const SizedBox(width: 12),
                    Padding(
                      padding: const EdgeInsets.only(top: 18),
                      child: NeonButton(
                          label: 'Browse', icon: Icons.folder_open, onPressed: _browseOut),
                    ),
                  ],
                ),
              ],
            ),
          ),
          const SizedBox(height: 18),
          NeonPanel(
            title: 'PARAMETERS',
            borderColor: Retrowave.magenta,
            child: Wrap(
              spacing: 18,
              runSpacing: 14,
              children: [
                NeonDropdown<String>(
                  value: _targetType,
                  items: const ['generic', 'new_release'],
                  display: (k) =>
                      k == 'generic' ? 'Generic' : 'New Releases',
                  onChanged: (v) => setState(() {
                    _targetType = v ?? _targetType;
                    if (_targetType == 'new_release') {
                      for (final g in _genreMap) {
                        if (_nrDisabled.contains(g.key)) {
                          _genreCtrl[g.key]?.text = '0';
                        }
                      }
                    }
                    _persist();
                  }),
                  label: 'Save Posters To',
                  width: 180,
                ),
                NeonField(
                    controller: _startYear,
                    label: 'Start Year',
                    digitsOnly: true,
                    maxLength: 4,
                    width: 100),
                NeonField(
                    controller: _endYear,
                    label: 'End Year',
                    digitsOnly: true,
                    maxLength: 4,
                    width: 100),
                NeonDropdown<String>(
                  value: _ratingFilter,
                  items: _ratingOptions.map((e) => e.key).toList(),
                  display: (k) => _ratingOptions
                      .firstWhere((e) => e.key == k, orElse: () => MapEntry(k, k))
                      .value,
                  onChanged: (v) => setState(() {
                    _ratingFilter = v ?? 'any';
                    _persist();
                  }),
                  label: 'Rating',
                  width: 160,
                ),
                NeonDropdown<String>(
                  value: _posterSize,
                  items: const ['w92', 'w154', 'w185', 'w342', 'w500', 'w780', 'original'],
                  onChanged: (v) => setState(() {
                    _posterSize = v ?? _posterSize;
                    _persist();
                  }),
                  label: 'Poster Size',
                  width: 140,
                ),
                NeonDropdown<String>(
                  value: _country,
                  items: _countries.map((e) => e.key).toList(),
                  display: (k) =>
                      _countries.firstWhere((e) => e.key == k, orElse: () => MapEntry(k, k)).value,
                  onChanged: (v) => setState(() {
                    _country = v ?? '';
                    _persist();
                  }),
                  label: 'Production Country',
                  width: 220,
                ),
                NeonDropdown<String>(
                  value: _language,
                  items: _languages.map((e) => e.key).toList(),
                  display: (k) =>
                      _languages.firstWhere((e) => e.key == k, orElse: () => MapEntry(k, k)).value,
                  onChanged: (v) => setState(() {
                    _language = v ?? '';
                    _persist();
                  }),
                  label: 'Original Language',
                  width: 200,
                ),
              ],
            ),
          ),
          const SizedBox(height: 16),
          NeonPanel(
            title: 'PER-GENRE MAX (BLANK = 100)',
            child: Wrap(
              spacing: 10,
              runSpacing: 8,
              children: [
                for (final g in _genreMap)
                  ..._buildGenreField(g),
              ],
            ),
          ),
          const SizedBox(height: 16),
          Center(
            child: NeonButton(
              label: widget.state.tmdbRunning ? 'Fetching…' : 'Start TMDB Fetch',
              icon: Icons.cloud_download,
              primary: true,
              padding: const EdgeInsets.symmetric(horizontal: 28, vertical: 12),
              onPressed: widget.state.tmdbRunning ? null : _startFetch,
            ),
          ),
          const SizedBox(height: 16),
          // Auto-convert status — visible so the user knows the converter is
          // running after a TMDB fetch, even though the Converter tab is gone.
          _AutoConvertStatus(
            tmdbRunning: widget.state.tmdbRunning,
            converterRunning: widget.state.converterRunning,
            stepCurrent: widget.state.stepCurrent,
            stepTotal: widget.state.stepTotal,
            fileCurrent: widget.state.fileCurrent,
            fileTotal: widget.state.fileTotal,
            error: _runError,
          ),
          const SizedBox(height: 24),
        ],
      ),
    );
  }

  List<Widget> _buildGenreField(MapEntry<String, int?> g) {
    final disabled = _targetType == 'new_release' && _nrDisabled.contains(g.key);
    return [
      SizedBox(
        width: 210,
        child: Opacity(
          opacity: disabled ? 0.35 : 1.0,
          child: Container(
            decoration: BoxDecoration(
              color: Retrowave.surface,
              border: Border.all(color: Retrowave.border),
            ),
            child: Row(
              children: [
                Container(
                  width: 96,
                  padding: const EdgeInsets.symmetric(
                      horizontal: Retrowave.sp2, vertical: Retrowave.sp2),
                  color: Retrowave.genreBg(g.key),
                  child: Text(
                    _shortLabel(g.key),
                    style: Retrowave.heading(
                        Retrowave.fsMeta, Retrowave.genreFg(g.key)),
                  ),
                ),
                Expanded(
                  child: TextField(
                    controller: _genreCtrl[g.key],
                    readOnly: disabled,
                    style: Retrowave.body(Retrowave.fsBody,
                        disabled ? Retrowave.text3 : Retrowave.text),
                    cursorColor: Retrowave.cyan,
                    textAlign: TextAlign.center,
                    decoration: InputDecoration(
                      hintText: '100',
                      border: InputBorder.none,
                      enabledBorder: InputBorder.none,
                      focusedBorder: InputBorder.none,
                      contentPadding: const EdgeInsets.symmetric(
                          horizontal: 6, vertical: 8),
                      isDense: true,
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    ];
  }
}

class _AutoConvertStatus extends StatelessWidget {
  final bool tmdbRunning;
  final bool converterRunning;
  final int stepCurrent;
  final int stepTotal;
  final int fileCurrent;
  final int fileTotal;
  final String? error;

  const _AutoConvertStatus({
    required this.tmdbRunning,
    required this.converterRunning,
    required this.stepCurrent,
    required this.stepTotal,
    required this.fileCurrent,
    required this.fileTotal,
    this.error,
  });

  @override
  Widget build(BuildContext context) {
    if (error != null) {
      return Container(
        decoration: BoxDecoration(
          color: Retrowave.panel,
          border: Border.all(color: Retrowave.pink),
        ),
        padding: const EdgeInsets.all(Retrowave.sp2),
        child: Text(error!, style: Retrowave.body(Retrowave.fsMeta, Retrowave.pink)),
      );
    }
    if (tmdbRunning) {
      return Text('FETCHING POSTERS…',
          style: Retrowave.heading(Retrowave.fsBody, Retrowave.cyan));
    }
    if (converterRunning) {
      return Text('AUTO-CONVERTING…',
          style: Retrowave.heading(Retrowave.fsBody, Retrowave.pink));
    }
    return const SizedBox.shrink();
  }
}
