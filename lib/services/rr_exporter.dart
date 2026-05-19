import 'dart:convert';
import 'dart:io';
import 'package:path/path.dart' as p;
import 'duplicate_finder.dart' show setFolders, setGeneric, setNewRelease;
import 'movies_meta.dart';
import 'progress_bus.dart';
import 'rr_rating.dart';

/// UI display name → RR DataTable asset name. Only Kids→Kid differs.
const Map<String, String> _genreDataTable = {
  'Action': 'Action',
  'Adult': 'Adult',
  'Adventure': 'Adventure',
  'Comedy': 'Comedy',
  'Documentary': 'Documentary',
  'Drama': 'Drama',
  'Fantasy': 'Fantasy',
  'History': 'History',
  'Horror': 'Horror',
  'Kids': 'Kid',
  'Music': 'Music',
  'Police': 'Police',
  'Romance': 'Romance',
  'Sci-Fi': 'Sci-Fi',
  'Sports': 'Sports',
  'Thriller': 'Thriller',
  'Western': 'Western',
  'Xmas': 'Xmas',
};

/// 3-letter code per GENRES table in RR_VHS_Tool.
const Map<String, String> _genreCode = {
  'Action': 'Act',
  'Adult': 'Adu',
  'Adventure': 'Adv',
  'Comedy': 'Com',
  'Documentary': 'Doc',
  'Drama': 'Dra',
  'Fantasy': 'Fan',
  'History': 'His',
  'Horror': 'Hor',
  'Kids': 'Kid',
  'Music': 'Mus',
  'Police': 'Pol',
  'Romance': 'Rom',
  'Sci-Fi': 'Sci',
  'Sports': 'Spo',
  'Thriller': 'Thr',
  'Western': 'Wst',
  'Xmas': 'Xma',
};

/// Per-genre NR genre_byte. Adult intentionally absent (game blocks Adult NR).
const Map<String, int> _nrGenreByte = {
  'Action': 0x01,
  'Adventure': 0x02,
  'Comedy': 0x03,
  'Drama': 0x04,
  'Horror': 0x05,
  'Sci-Fi': 0x06,
  'Fantasy': 0x07,
  'Thriller': 0x08,
  'Music': 0x09,
  'Romance': 0x0A,
  'History': 0x0B,
  'Kids': 0x0C,
  'Documentary': 0x0D,
  'Police': 0x0E,
  'Sports': 0x0F,
  'Western': 0x11,
  'Xmas': 0x12,
};

/// First T_Sub index reserved for custom slots (TSUB_CUSTOM_BASE in RR_VHS_Tool).
/// The global slot counter passed in must be 1-based and never reset between genres.
const int _tsubCustomBase = 78;

String _subTexFor(int globalSlotIdx) => 'T_Sub_${_tsubCustomBase + globalSlotIdx - 1}';

String _capGenre(String folderName) {
  if (folderName.isEmpty) return folderName;
  final lower = folderName.toLowerCase();
  // Legacy alias: pre-v3 scrapes used "Crime" / "crime" folder; v3 uses "Police".
  if (lower == 'crime') return 'Police';
  for (final g in _genreCode.keys) {
    if (g.toLowerCase() == lower) return g;
  }
  return folderName[0].toUpperCase() + folderName.substring(1);
}

class RrExportConfig {
  final String libraryRoot; // contains converted posters/ + nr converted posters/
  final String outputDir; // where the three JSONs land
  final String rarity; // Common / Common (Old) / Limited Edition (holo) / Random
  final String standeeShape; // A / B / C
  final int defaultLs;
  final int defaultLsc;
  final bool overwrite; // overwrite existing JSONs without prompting

  const RrExportConfig({
    required this.libraryRoot,
    required this.outputDir,
    this.rarity = 'Common',
    this.standeeShape = 'A',
    this.defaultLs = 1, // FIXED_REGULAR_LAYOUT = 1 in RR_VHS_Tool v3
    this.defaultLsc = 4,
    this.overwrite = true,
  });
}

// `last_edited_at` / `created_at` deliberately NOT emitted on fresh exports.
// Per RR_VHS_Tool source: both are OPTIONAL. `add_movie_slot` / `add_nr_slot`
// don't set them, and the tool fills `last_edited_at` only when the user actually
// edits a slot post-import. Writing it on every fresh export poisons the
// sort-by-edited view and lies about edit history.

/// Encode JSON with all non-ASCII codeunits escaped as `\uXXXX`. Equivalent
/// to Python's `json.dump(..., ensure_ascii=True)` (the default there) and
/// necessary because RR_VHS_Tool opens these files via `open(path)` without
/// an explicit encoding — on Windows that's cp1252, which barfs on raw
/// UTF-8 bytes from accented movie titles ("Amélie", "L'Étranger", etc.)
/// or smart-quoted strings. Dart's `JsonEncoder` emits raw UTF-8 by default;
/// this wrapper walks the output and re-encodes anything ≥ 0x80.
///
/// Walks codeunits (UTF-16), so supplementary-plane characters (emoji) are
/// emitted as their surrogate pair — also the correct JSON representation
/// per RFC 8259 §7.
String _asciiSafeJson(Object data, {bool indent = true}) {
  final enc = indent ? const JsonEncoder.withIndent('  ') : const JsonEncoder();
  final raw = enc.convert(data);
  final buf = StringBuffer();
  for (var i = 0; i < raw.length; i++) {
    final code = raw.codeUnitAt(i);
    if (code < 0x80) {
      buf.writeCharCode(code);
    } else {
      buf.write('\\u');
      buf.write(code.toRadixString(16).padLeft(4, '0'));
    }
  }
  return buf.toString();
}

class RrExportResult {
  final int customSlots;
  final int nrSlots;
  final int replacements;
  final List<String> warnings;
  final String customSlotsPath;
  final String nrSlotsPath;
  final String replacementsPath;
  final String editedSlotsPath;
  RrExportResult({
    required this.customSlots,
    required this.nrSlots,
    required this.replacements,
    required this.warnings,
    required this.customSlotsPath,
    required this.nrSlotsPath,
    required this.replacementsPath,
    required this.editedSlotsPath,
  });
}

/// Walks `<libraryRoot>/converted posters/<genre>/` and
/// `<libraryRoot>/nr converted posters/<genre>/`, builds the three RR
/// Workshop JSON files and writes them to `outputDir`.
Future<RrExportResult> exportToRrWorkshop(
  RrExportConfig cfg,
  ProgressBus bus,
) async {
  bus.emitLog('=== RR Workshop Export Started ===');
  bus.emitStep(0, 1);

  final warnings = <String>[];
  final meta = await MoviesMetaStore.load();
  if (meta.isEmpty) {
    warnings.add('movies.json is empty — every slot will fall back to a 0.0★ rating. Run the TMDB Scraper first to populate metadata.');
  }

  // ---------- Generic / genre-shelf scan ----------
  final customSlots = <String, List<Map<String, dynamic>>>{};
  final replacements = <String, Map<String, dynamic>>{};
  final genericRoot = Directory(p.join(cfg.libraryRoot, setFolders[setGeneric]!));
  final usedSkus = <int>{};
  var genericCount = 0;
  var globalCustomSlotIdx = 0; // T_Sub index — never resets between genres

  if (await genericRoot.exists()) {
    final genreDirs = genericRoot.listSync().whereType<Directory>().toList()
      ..sort((a, b) => p.basename(a.path).compareTo(p.basename(b.path)));
    for (final dir in genreDirs) {
      final folderName = p.basename(dir.path);
      final genre = _capGenre(folderName);
      final dtName = _genreDataTable[genre];
      final code = _genreCode[genre];
      if (dtName == null || code == null) {
        warnings.add('Skipping unknown genre folder: $folderName');
        continue;
      }

      final files = dir
          .listSync()
          .whereType<File>()
          .where((f) => p.extension(f.path).toLowerCase() == '.png')
          .toList()
        ..sort((a, b) =>
            p.basename(a.path).toLowerCase().compareTo(p.basename(b.path).toLowerCase()));

      var slotIdx = 0;
      for (final f in files) {
        slotIdx++;
        globalCustomSlotIdx++;
        final filename = p.basename(f.path);
        final m = meta[filename];
        final last2 = m?.last2 ?? 2; // 0.0★ when unknown
        if (m == null) {
          warnings.add('No metadata for $filename — defaulting to 0.0★.');
        }
        final bkgTex = 'T_Bkg_${code}_${slotIdx.toString().padLeft(3, '0')}';
        final pnName = m?.title ?? MoviesMetaStore.parseFilename(filename).title;
        final slotRarity = (m?.isOld == true) ? 'Common (Old)' : cfg.rarity;
        final sku = rrGenerateSku(
          genre: genre,
          slotIndex: slotIdx,
          last2: last2,
          rarity: slotRarity,
          usedSkus: usedSkus,
        );
        usedSkus.add(sku);

        customSlots.putIfAbsent(dtName, () => []).add({
          'bkg_tex': bkgTex,
          'sub_tex': _subTexFor(globalCustomSlotIdx),
          'pn_name': pnName,
          'ls': cfg.defaultLs,
          'lsc': cfg.defaultLsc,
          'sku': sku,
          'ntu': false,
        });

        replacements[bkgTex] = {
          'movies': [pnName],
          'new_release': false,
          'sku': sku,
          'path': f.path.replaceAll('\\', '/'),
          'offset_x': 0,
          'offset_y': -170,
          'zoom': 0.813,
        };
        genericCount++;
      }
      bus.emitLog('  $genre: ${files.length} genre-shelf slot(s)');
    }
  } else {
    warnings.add('Generic poster folder not found: ${genericRoot.path}');
  }

  // ---------- NR scan ----------
  final nrSlots = <Map<String, dynamic>>[];
  final nrUsedSkus = <int>{};
  final nrRoot = Directory(p.join(cfg.libraryRoot, setFolders[setNewRelease]!));
  var nrCount = 0;

  if (await nrRoot.exists()) {
    final genreDirs = nrRoot.listSync().whereType<Directory>().toList()
      ..sort((a, b) => p.basename(a.path).compareTo(p.basename(b.path)));
    for (final dir in genreDirs) {
      final folderName = p.basename(dir.path);
      final genre = _capGenre(folderName);
      final code = _genreCode[genre];
      final genreByte = _nrGenreByte[genre];
      if (code == null || genreByte == null) {
        warnings.add('NR-skipping genre $folderName (Adult excluded; or unknown genre)');
        continue;
      }

      final files = dir
          .listSync()
          .whereType<File>()
          .where((f) => p.extension(f.path).toLowerCase() == '.png')
          .toList()
        ..sort((a, b) =>
            p.basename(a.path).toLowerCase().compareTo(p.basename(b.path).toLowerCase()));

      var texNum = 0;
      for (final f in files) {
        texNum++;
        final filename = p.basename(f.path);
        final m = meta[filename];
        if (m == null) {
          warnings.add('No metadata for NR $filename — defaulting to title from filename.');
        }
        final bkgTex = 'T_New_${code}_${texNum.toString().padLeft(3, '0')}';
        final title = m?.title ?? MoviesMetaStore.parseFilename(filename).title;
        final last2 = m?.last2 ?? 2;
        final slotRarity = (m?.isOld == true) ? 'Common (Old)' : cfg.rarity;
        // NR slots use the same rrGenerateSku formula as generic slots (v3).
        final sku = rrGenerateSku(
          genre: genre,
          slotIndex: texNum,
          last2: last2,
          rarity: slotRarity,
          usedSkus: nrUsedSkus,
        );
        nrUsedSkus.add(sku);

        nrSlots.add({
          'title': title,
          'genre': genre,
          'genre_code': code,
          'genre_byte': genreByte,
          'bkg_tex': bkgTex,
          'sku': sku,
          'standee_shape': cfg.standeeShape,
          'tex_num': texNum,
        });

        replacements['NR_$sku'] = {
          'movies': [title],
          'new_release': true,
          'sku': sku,
          'path': f.path.replaceAll('\\', '/'),
        };
        nrCount++;
      }
      bus.emitLog('  $genre: ${files.length} NR slot(s)');
    }
  } else {
    warnings.add('NR poster folder not found: ${nrRoot.path}');
  }

  // ---------- Write ----------
  await Directory(cfg.outputDir).create(recursive: true);

  final customSlotsFile = File(p.join(cfg.outputDir, 'custom_slots.json'));
  final nrSlotsFile = File(p.join(cfg.outputDir, 'nr_custom_slots.json'));
  final replacementsFile = File(p.join(cfg.outputDir, 'replacements.json'));
  final editedSlotsFile = File(p.join(cfg.outputDir, 'edited_slots.json'));

  // Sort genres in custom_slots by DT name for stable diffs.
  final sortedCustom = <String, List<Map<String, dynamic>>>{};
  for (final k in (customSlots.keys.toList()..sort())) {
    sortedCustom[k] = customSlots[k]!;
  }

  // Sort replacements by key.
  final sortedReplacements = <String, Map<String, dynamic>>{};
  for (final k in (replacements.keys.toList()..sort())) {
    sortedReplacements[k] = replacements[k]!;
  }

  // Pre-flight: verify every replacement path exists on disk.
  var missingPosters = 0;
  for (final entry in sortedReplacements.entries) {
    final path = entry.value['path'] as String?;
    if (path != null && !File(path).existsSync()) {
      warnings.add('Missing converted poster: ${p.basename(path)} (${entry.key})');
      missingPosters++;
    }
  }
  if (missingPosters > 0) {
    bus.emitLog(
        'PRE-FLIGHT: $missingPosters poster file(s) missing from disk — '
        'run the Poster Converter for affected genres before using this export.');
  }

  // All four files use _asciiSafeJson so RR_VHS_Tool's cp1252-default
  // file reads succeed on accented titles. See _asciiSafeJson doc.
  await customSlotsFile.writeAsString(_asciiSafeJson(sortedCustom));
  await nrSlotsFile.writeAsString(_asciiSafeJson(nrSlots));
  await replacementsFile.writeAsString(_asciiSafeJson(sortedReplacements));
  // edited_slots.json: flat JSON array of texture names with pending edits.
  // Fresh export = nothing edited yet, so write []. RR_VHS_Tool populates it
  // as the user makes edits. No indentation: matches the tool's json.dump writer.
  await editedSlotsFile
      .writeAsString(_asciiSafeJson(const <String>[], indent: false));

  bus.emitLog('Wrote ${customSlotsFile.path}');
  bus.emitLog('Wrote ${nrSlotsFile.path}');
  bus.emitLog('Wrote ${replacementsFile.path}');
  bus.emitLog('Wrote ${editedSlotsFile.path}');
  bus.emitLog(
      'Export complete — $genericCount genre-shelf slot(s), $nrCount NR slot(s), ${replacements.length} replacement(s)');
  if (warnings.isNotEmpty) {
    bus.emitLog('${warnings.length} warning(s):');
    for (final w in warnings) {
      bus.emitLog('  ! $w');
    }
  }
  bus.emitStep(1, 1);
  bus.emitDone('rr_export');

  return RrExportResult(
    customSlots: genericCount,
    nrSlots: nrCount,
    replacements: replacements.length,
    warnings: warnings,
    customSlotsPath: customSlotsFile.path,
    nrSlotsPath: nrSlotsFile.path,
    replacementsPath: replacementsFile.path,
    editedSlotsPath: editedSlotsFile.path,
  );
}
