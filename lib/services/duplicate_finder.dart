import 'dart:io';
import 'package:flutter/foundation.dart' show compute;
import 'package:path/path.dart' as p;
import 'progress_bus.dart';

const String setGeneric = 'generic';
const String setNewRelease = 'new_release';

/// Subfolder under baseDir where each converted-poster set lives. Forward
/// slashes are absorbed by path.join() on Windows. Drives the duplicate
/// scanner, the RR exporter's library walk, and any future consumer that
/// needs to find the converted-poster trees.
const Map<String, String> setFolders = {
  setGeneric: 'posters/converted/generic',
  setNewRelease: 'posters/converted/new_releases',
};

class DupLocation {
  final String set;
  final String genre;
  final String path;
  DupLocation(this.set, this.genre, this.path);

  String get setLabel => set == setGeneric ? 'GENERIC' : 'NEW RELEASE';
}

class DupGroup {
  final String filename;
  final List<DupLocation> locations;
  DupGroup(this.filename, this.locations);

  String get displayTitle {
    final stem = p.basenameWithoutExtension(filename);
    return stem.replaceAll('_', ' ');
  }
}

class DupReport {
  final List<DupGroup> crossSet;
  final List<DupGroup> crossGenre;
  DupReport(this.crossSet, this.crossGenre);
  bool get isEmpty => crossSet.isEmpty && crossGenre.isEmpty;
  int get totalGroups => crossSet.length + crossGenre.length;
  int get totalDuplicateFiles {
    var sum = 0;
    for (final g in crossSet) { sum += g.locations.length - 1; }
    for (final g in crossGenre) { sum += g.locations.length - 1; }
    return sum;
  }
}

// ── Isolate workers (top-level so compute() can send them) ───────────────────

/// Walk one poster set directory and return a filename → locations map.
/// Uses async listing so the isolate event loop can interleave I/O from
/// the parallel set scan.
Future<Map<String, List<DupLocation>>> _scanOneSet(
    (String setKey, String folderPath) args) async {
  final (setKey, folderPath) = args;
  final byFile = <String, List<DupLocation>>{};
  final folder = Directory(folderPath);
  if (!await folder.exists()) return byFile;

  final genreDirs = (await folder.list().toList()).whereType<Directory>().toList()
    ..sort((a, b) => p.basename(a.path).compareTo(p.basename(b.path)));

  for (final genreDir in genreDirs) {
    final genre = p.basename(genreDir.path);
    for (final entity in await genreDir.list().toList()) {
      if (entity is! File) continue;
      if (p.extension(entity.path).toLowerCase() != '.png') continue;
      byFile
          .putIfAbsent(p.basename(entity.path), () => [])
          .add(DupLocation(setKey, genre, entity.path));
    }
  }

  return byFile;
}

/// Scan both sets in parallel then classify into cross-set / cross-genre groups.
/// This runs entirely inside the compute() isolate — no ProgressBus here.
Future<DupReport> _scanBody(String baseDir) async {
  // Both sets walk their directory trees concurrently.
  final results = await Future.wait([
    _scanOneSet((setGeneric, p.join(baseDir, setFolders[setGeneric]!))),
    _scanOneSet((setNewRelease, p.join(baseDir, setFolders[setNewRelease]!))),
  ]);

  // Merge the two per-set maps into one filename → [locations] map.
  final byFile = <String, List<DupLocation>>{};
  for (final setResult in results) {
    for (final entry in setResult.entries) {
      byFile.putIfAbsent(entry.key, () => []).addAll(entry.value);
    }
  }

  // Classify: ≥2 sets = cross-set; same set, ≥2 genres = cross-genre.
  final crossSet = <DupGroup>[];
  final crossGenre = <DupGroup>[];
  for (final entry in byFile.entries) {
    if (entry.value.length < 2) continue;
    final sets = entry.value.map((l) => l.set).toSet();
    if (sets.length > 1) {
      crossSet.add(DupGroup(entry.key, entry.value));
    } else {
      crossGenre.add(DupGroup(entry.key, entry.value));
    }
  }

  crossSet.sort((a, b) => a.filename.toLowerCase().compareTo(b.filename.toLowerCase()));
  crossGenre.sort((a, b) => a.filename.toLowerCase().compareTo(b.filename.toLowerCase()));

  return DupReport(crossSet, crossGenre);
}

// ── Public API ────────────────────────────────────────────────────────────────

/// Scan [baseDir] for duplicate converted posters. Heavy work runs in a
/// separate isolate via [compute] so the UI stays responsive throughout.
Future<DupReport> scanDuplicates(String baseDir, ProgressBus bus) async {
  bus.emitLog('Scanning for duplicate posters…');
  bus.emitStep(0, 1);
  final report = await compute(_scanBody, baseDir);
  bus.emitLog(
      'Found ${report.crossSet.length} cross-set group(s), '
      '${report.crossGenre.length} cross-genre group(s).');
  bus.emitStep(1, 1);
  bus.emitDone('duplicates');
  return report;
}

Future<int> applyDeletions(Iterable<String> paths, ProgressBus bus) async {
  var n = 0;
  for (final path in paths) {
    try {
      final f = File(path);
      if (await f.exists()) {
        await f.delete();
        n++;
        bus.emitLog('  Deleted: $path');
      }
    } catch (e) {
      bus.emitLog('  ERROR deleting $path: $e');
    }
  }
  return n;
}
