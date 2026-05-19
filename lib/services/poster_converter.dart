import 'dart:async';
import 'dart:io';
import 'dart:math' as math;
import 'dart:typed_data';
import 'package:flutter/foundation.dart' show compute;
import 'package:image/image.dart' as img;
import 'package:path/path.dart' as p;
import 'poster_converter_ffi.dart';
import 'progress_bus.dart';

const _pngCompressLevel = 6;
const _supportedExt = ['.png', '.webp', '.jpg', '.jpeg', '.bmp', '.tiff'];
const _brightnessThreshold = 140.0;

/// Worker concurrency per genre: 85% of available cores, floored, never
/// below 1. Each worker pushes a single file through `compute()` (which
/// spawns its own isolate), so this maps directly to the number of
/// in-flight isolates at any moment. Leaves ~15% of cores free for the
/// UI thread and the OS — going to 100% locks up window paints on
/// machines without spare hyperthreads.
int _conversionWorkerCount() {
  final cores = Platform.numberOfProcessors;
  final n = (cores * 0.85).floor();
  // Clamp upper bound: spawning >64 concurrent isolates rarely helps and
  // starts to thrash. Most real machines hit the lower clamp anyway.
  return n.clamp(1, 64);
}

/// Per-profile conversion target. Same algorithm everywhere — only canvas /
/// poster-width differ.
class _ProfileSpec {
  final int canvasW;
  final int canvasH;
  final int posterW;
  const _ProfileSpec({
    required this.canvasW,
    required this.canvasH,
    required this.posterW,
  });
}

const _profiles = <String, _ProfileSpec>{
  'generic':     _ProfileSpec(canvasW: 979, canvasH: 1665, posterW: 979),
  'new_release': _ProfileSpec(canvasW: 600, canvasH: 1200, posterW: 600),
};

const _defaultProfile = 'generic';

class _Overlays {
  final Uint8List darkPng;
  final Uint8List lightPng;
  _Overlays(this.darkPng, this.lightPng);
}

Future<_Overlays> _loadOverlays(String folder) async {
  final darkFile = File(p.join(folder, 'overlay1.png'));
  final lightFile = File(p.join(folder, 'overlay2.png'));
  if (!await darkFile.exists()) {
    throw FileSystemException('overlay1.png not found in', folder);
  }
  final darkBytes = await darkFile.readAsBytes();

  Uint8List lightBytes;
  if (await lightFile.exists()) {
    lightBytes = await lightFile.readAsBytes();
  } else {
    final dark = img.decodePng(darkBytes)!;
    final inverted = img.Image.from(dark);
    for (final px in inverted) {
      px
        ..r = 255 - px.r.toInt()
        ..g = 255 - px.g.toInt()
        ..b = 255 - px.b.toInt();
    }
    lightBytes = Uint8List.fromList(img.encodePng(inverted));
  }
  return _Overlays(darkBytes, lightBytes);
}

class _Task {
  final String inputPath;
  final String outputPath;
  final Uint8List darkPng;
  final Uint8List lightPng;
  // Profile dims travel with the task so _convertSync has no implicit
  // dependency on a calling-scope global — important because compute() runs
  // it in a separate isolate.
  final int canvasW;
  final int canvasH;
  final int posterW;
  // When true, _convertSync tries the native libvips path first.
  // Set by the main isolate after probeNativeAvailable() succeeds.
  final bool useNative;
  _Task(
    this.inputPath,
    this.outputPath,
    this.darkPng,
    this.lightPng,
    this.canvasW,
    this.canvasH,
    this.posterW, {
    this.useNative = false,
  });
}

class ConvertResult {
  final bool ok;
  final String message;
  ConvertResult(this.ok, this.message);
}

double _regionLuminance(img.Image im, int x, int y, int w, int h) {
  final endX = math.min(x + w, im.width);
  final endY = math.min(y + h, im.height);
  final startX = math.max(0, x);
  final startY = math.max(0, y);
  double sum = 0;
  int count = 0;
  for (var yi = startY; yi < endY; yi++) {
    for (var xi = startX; xi < endX; xi++) {
      final px = im.getPixel(xi, yi);
      sum += 0.299 * px.r + 0.587 * px.g + 0.114 * px.b;
      count++;
    }
  }
  return count == 0 ? 0 : sum / count;
}

ConvertResult _convertSync(_Task t) {
  // ── Native path (libvips GPU/SIMD) ────────────────────────────────────────
  // tryConvertNative re-opens the DLL (cheap — already mapped by OS) and
  // calls the C function. Returns null on any failure so we fall through.
  if (t.useNative) {
    final native = tryConvertNative(
      inputPath:  t.inputPath,
      outputPath: t.outputPath,
      darkPng:    t.darkPng,
      lightPng:   t.lightPng,
      canvasW:    t.canvasW,
      canvasH:    t.canvasH,
      posterW:    t.posterW,
    );
    if (native != null) {
      return ConvertResult(native.$1, native.$2);
    }
    // Fall through to Dart image path.
  }

  // ── Dart image fallback (CPU) ──────────────────────────────────────────────
  try {
    final bytes = File(t.inputPath).readAsBytesSync();
    var src = img.decodeImage(bytes);
    if (src == null) return ConvertResult(false, 'decode failed');
    if (src.numChannels != 4) {
      src = src.convert(numChannels: 4);
    }

    final posterH = (src.height * (t.posterW / src.width)).round();
    final poster = img.copyResize(
      src,
      width: t.posterW,
      height: posterH,
      interpolation: img.Interpolation.cubic,
    );

    final canvas = img.Image(width: t.canvasW, height: t.canvasH, numChannels: 4);
    img.fill(canvas, color: img.ColorRgba8(0, 0, 0, 255));
    img.compositeImage(canvas, poster, dstX: 0, dstY: 0);

    final darkOv = img.decodePng(t.darkPng);
    final lightOv = img.decodePng(t.lightPng);
    if (darkOv == null || lightOv == null) {
      return ConvertResult(false, 'overlay decode failed');
    }
    final overlayH = math.min(darkOv.height, t.canvasH);
    final overlayY = math.max(0, t.canvasH - overlayH);

    final luma = _regionLuminance(canvas, 0, overlayY, t.canvasW, overlayH);
    final bright = luma > _brightnessThreshold;
    final chosen = bright ? darkOv : lightOv;

    img.compositeImage(canvas, chosen, dstX: 0, dstY: overlayY);

    final outBytes = img.encodePng(canvas, level: _pngCompressLevel);
    File(t.outputPath).writeAsBytesSync(outBytes);
    return ConvertResult(true, 'luma=${luma.toStringAsFixed(1)} ${bright ? "DARK" : "LIGHT"}');
  } catch (e) {
    return ConvertResult(false, '$e');
  }
}

Future<int> _processGenre(
  String genre,
  String inputDir,
  String outputDir,
  _Overlays overlays,
  _ProfileSpec spec,
  bool overwrite,
  bool useNative,
  ProgressBus bus,
) async {
  await Directory(outputDir).create(recursive: true);
  final files = <MapEntry<String, String>>[];
  final dir = Directory(inputDir);
  if (!await dir.exists()) return 0;
  for (final entity in dir.listSync()) {
    if (entity is! File) continue;
    final name = p.basename(entity.path);
    final ext = p.extension(name).toLowerCase();
    if (!_supportedExt.contains(ext)) continue;
    final outPath = p.join(outputDir, '${p.basenameWithoutExtension(name)}.png');
    if (overwrite || !File(outPath).existsSync()) {
      files.add(MapEntry(entity.path, outPath));
    }
  }
  if (files.isEmpty) {
    bus.emitLog('  No new images to convert in $genre');
    return 0;
  }

  var processed = 0;
  var completed = 0;
  final total = files.length;
  final queue = List<MapEntry<String, String>>.from(files);

  Future<void> worker() async {
    while (queue.isNotEmpty) {
      final mp = queue.removeAt(0);
      final task = _Task(
        mp.key,
        mp.value,
        overlays.darkPng,
        overlays.lightPng,
        spec.canvasW,
        spec.canvasH,
        spec.posterW,
        useNative: useNative,
      );
      // compute() takes a top-level function reference + a sendable message.
      // Using Isolate.run with a closure here transitively captures the
      // surrounding Flutter scope (WidgetsFlutterBinding) and fails.
      final res = await compute(_convertSync, task);
      if (res.ok) {
        processed++;
        bus.emitLog('  ${p.basename(mp.key)} → ${res.message}');
      } else {
        bus.emitLog('  ERROR ${p.basename(mp.key)}: ${res.message}');
      }
      completed++;
      bus.emitFile(completed, total);
    }
  }

  // Don't spawn more workers than files — saves cold-start cost on small
  // genres. Pool size is otherwise the global 85%-of-cores target.
  final poolSize = math.min(_conversionWorkerCount(), total);
  await Future.wait(List.generate(poolSize, (_) => worker()));
  bus.emitLog('  Converted $processed/$total posters in $genre');
  return processed;
}

Future<void> runConverter({
  required String srcFolder,
  required String outFolder,
  required String overlaysFolder,
  required String profile,
  required bool overwrite,
  required ProgressBus bus,
}) async {
  var totalConverted = 0;
  try {
    // Probe for poster_native.dll once in the main isolate. Workers inherit
    // the flag via _Task.useNative — they re-open the (already-mapped) DLL.
    final useNative = await probeNativeAvailable();
    bus.emitLog(useNative
        ? 'Native acceleration: ON  (libvips GPU/SIMD)'
        : 'Native acceleration: OFF (Dart image — CPU fallback)');

    final nested = Directory(p.join(srcFolder, 'posters'));
    final postersPath = await nested.exists() ? nested.path : srcFolder;
    bus.emitLog('Source: $postersPath');

    if (!await Directory(postersPath).exists()) {
      bus.emitLog('ERROR: Source folder invalid');
      return;
    }

    final genres = Directory(postersPath)
        .listSync()
        .whereType<Directory>()
        .map((d) => p.basename(d.path))
        .toList()
      ..sort();
    if (genres.isEmpty) {
      bus.emitLog('No genre folders found');
      return;
    }

    final spec = _profiles[profile] ?? _profiles[_defaultProfile]!;
    if (!_profiles.containsKey(profile)) {
      bus.emitLog("Unknown profile '$profile' — falling back to '$_defaultProfile'.");
    }
    bus.emitLog(
        'Profile: $profile  Canvas: ${spec.canvasW}x${spec.canvasH}  Poster width: ${spec.posterW}');
    bus.emitLog(
        'Parallelism: ${_conversionWorkerCount()} worker(s) (85% of ${Platform.numberOfProcessors} cores)');
    bus.emitLog('Overwrite: $overwrite');

    if (!await Directory(overlaysFolder).exists()) {
      bus.emitLog('ERROR: Overlays folder missing: $overlaysFolder');
      return;
    }

    final overlays = await _loadOverlays(overlaysFolder);
    bus.emitLog('Loaded overlays from $overlaysFolder');

    for (var idx = 0; idx < genres.length; idx++) {
      final g = genres[idx];
      final inp = p.join(postersPath, g);
      final outp = p.join(outFolder, g);
      bus.emitLog('\nProcessing $g...');
      final processed =
          await _processGenre(g, inp, outp, overlays, spec, overwrite, useNative, bus);
      totalConverted += processed;
      bus.emitStep(idx + 1, genres.length, 'Converting $g');
    }
    bus.emitLog('\nFinished. Total posters created/overwritten: $totalConverted');
  } catch (e) {
    bus.emitLog('ERROR: $e');
  } finally {
    bus.emitDone('converter:$totalConverted');
  }
}

/// Single-file convert, used by the gallery's set-swap action where we
/// need to re-render one poster from its raw TMDB source into the other
/// set's canvas + overlay rather than just renaming the already-composed
/// converted file (different canvas dims + different overlay between
/// the two profiles → just moving the file gives wrong output).
///
/// Returns a `ConvertResult` carrying ok + a short status message. Caller
/// is responsible for any pre/post bookkeeping (deleting the old file,
/// updating UI, etc).
Future<ConvertResult> convertOneFile({
  required String inputPath,
  required String outputPath,
  required String overlaysFolder,
  required String profile,
}) async {
  final spec = _profiles[profile];
  if (spec == null) {
    return ConvertResult(false, "unknown profile '$profile'");
  }
  late final _Overlays overlays;
  try {
    overlays = await _loadOverlays(overlaysFolder);
  } catch (e) {
    return ConvertResult(false, 'overlay load failed: $e');
  }
  await Directory(p.dirname(outputPath)).create(recursive: true);
  final useNative = await probeNativeAvailable();
  final task = _Task(
    inputPath,
    outputPath,
    overlays.darkPng,
    overlays.lightPng,
    spec.canvasW,
    spec.canvasH,
    spec.posterW,
    useNative: useNative,
  );
  // compute() takes a top-level function reference + sendable message.
  // See "Isolate gotcha" in CLAUDE.md for why we don't use Isolate.run here.
  return compute(_convertSync, task);
}
