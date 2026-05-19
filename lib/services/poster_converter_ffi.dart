// poster_converter_ffi.dart
//
// Dart FFI bindings for poster_native.dll (libvips GPU/SIMD-accelerated
// conversion). Used exclusively by poster_converter.dart.
//
// Design rules:
// - This file may NOT import any flutter/* package. It is loaded inside
//   compute() isolates where only dart:* and package:* (non-UI) are safe.
// - DynamicLibrary.open is safe from background isolates — the OS keeps
//   one DLL handle per process. Opening it in each isolate is cheap.
// - All native memory is allocated and freed within the same call frame.

import 'dart:ffi';
import 'dart:io';
import 'dart:typed_data';
import 'package:ffi/ffi.dart';
import 'package:path/path.dart' as p;

// ─── Native function typedefs ────────────────────────────────────────────────

typedef _NativeInit = Int32 Function(Pointer<Utf8> argv0);
typedef _DartInit   = int   Function(Pointer<Utf8> argv0);

typedef _NativeAvailable = Int32 Function();
typedef _DartAvailable   = int   Function();

typedef _NativeConvert = Int32 Function(
    Pointer<Utf8>  inputPath,
    Pointer<Utf8>  outputPath,
    Pointer<Uint8> darkPng,   Int32 darkLen,
    Pointer<Uint8> lightPng,  Int32 lightLen,
    Int32 canvasW, Int32 canvasH, Int32 posterW,
    Pointer<Utf8>  outStatus, Int32 statusLen,
);
typedef _DartConvert = int Function(
    Pointer<Utf8>  inputPath,
    Pointer<Utf8>  outputPath,
    Pointer<Uint8> darkPng,   int darkLen,
    Pointer<Uint8> lightPng,  int lightLen,
    int canvasW, int canvasH, int posterW,
    Pointer<Utf8>  outStatus, int statusLen,
);

// ─── Loader ──────────────────────────────────────────────────────────────────

/// Lightweight wrapper around the three exported symbols.
/// Construct via [PosterNativeFfi.tryLoad]; returns null if the DLL is absent
/// or fails to load — the caller should fall back to the Dart image path.
class PosterNativeFfi {
  final _DartInit      _fnInit;
  final _DartAvailable _fnAvailable;
  final _DartConvert   _fnConvert;

  PosterNativeFfi._(this._fnInit, this._fnAvailable, this._fnConvert);

  /// Try to load poster_native.dll from [exeDir]. Returns null on any failure.
  static PosterNativeFfi? tryLoad(String exeDir) {
    final dllPath = p.join(exeDir, 'poster_native.dll');
    if (!File(dllPath).existsSync()) return null;
    try {
      final lib = DynamicLibrary.open(dllPath);
      final fnInit =
          lib.lookupFunction<_NativeInit, _DartInit>('poster_native_init');
      final fnAvail = lib.lookupFunction<_NativeAvailable, _DartAvailable>(
          'poster_native_available');
      final fnConv =
          lib.lookupFunction<_NativeConvert, _DartConvert>('poster_native_convert');
      return PosterNativeFfi._(fnInit, fnAvail, fnConv);
    } catch (_) {
      return null;
    }
  }

  /// Initialise libvips. Must succeed before [convert] is usable.
  bool initialize(String argv0) {
    final ptr = argv0.toNativeUtf8();
    try {
      return _fnInit(ptr) == 1;
    } finally {
      calloc.free(ptr);
    }
  }

  bool get isAvailable => _fnAvailable() == 1;

  /// Run one poster conversion. Returns (ok, statusMessage).
  (bool, String) convert({
    required String   inputPath,
    required String   outputPath,
    required Uint8List darkPng,
    required Uint8List lightPng,
    required int canvasW,
    required int canvasH,
    required int posterW,
  }) {
    const kStatusLen = 256;
    final statusBuf = calloc<Uint8>(kStatusLen);
    final inPtr     = inputPath.toNativeUtf8();
    final outPtr    = outputPath.toNativeUtf8();
    final darkPtr   = calloc<Uint8>(darkPng.length);
    final lightPtr  = calloc<Uint8>(lightPng.length);

    darkPtr.asTypedList(darkPng.length).setAll(0, darkPng);
    lightPtr.asTypedList(lightPng.length).setAll(0, lightPng);

    try {
      final rc = _fnConvert(
        inPtr, outPtr,
        darkPtr,  darkPng.length,
        lightPtr, lightPng.length,
        canvasW, canvasH, posterW,
        statusBuf.cast<Utf8>(), kStatusLen,
      );
      final msg = statusBuf.cast<Utf8>().toDartString();
      return (rc == 0, msg);
    } finally {
      calloc.free(statusBuf);
      calloc.free(inPtr);
      calloc.free(outPtr);
      calloc.free(darkPtr);
      calloc.free(lightPtr);
    }
  }
}

// ─── Availability probe (called from main isolate) ───────────────────────────

/// Called once in the main isolate by runConverter / convertOneFile before
/// spawning workers. Loads the DLL, calls VIPS_INIT, and returns true if the
/// native path is operational. Workers then pass the result as _Task.useNative
/// so they don't need to re-probe — just re-open the already-loaded DLL.
Future<bool> probeNativeAvailable() async {
  try {
    final exeDir = p.dirname(Platform.resolvedExecutable);
    final ffi = PosterNativeFfi.tryLoad(exeDir);
    if (ffi == null) return false;
    return ffi.initialize(Platform.resolvedExecutable) && ffi.isAvailable;
  } catch (_) {
    return false;
  }
}

// ─── Per-isolate conversion entry (top-level, safe to pass to compute()) ─────

/// Attempt one conversion via the native DLL.  Called from within a
/// compute() isolate — DynamicLibrary.open is safe here (DLL is already
/// mapped into the process from the main-isolate probe).
/// Returns null if the DLL fails to load or vips returns an error, so the
/// caller can fall back to the Dart image path.
(bool, String)? tryConvertNative({
  required String   inputPath,
  required String   outputPath,
  required Uint8List darkPng,
  required Uint8List lightPng,
  required int canvasW,
  required int canvasH,
  required int posterW,
}) {
  try {
    final exeDir = p.dirname(Platform.resolvedExecutable);
    final ffi = PosterNativeFfi.tryLoad(exeDir);
    if (ffi == null || !ffi.isAvailable) return null;
    return ffi.convert(
      inputPath:  inputPath,
      outputPath: outputPath,
      darkPng:    darkPng,
      lightPng:   lightPng,
      canvasW:    canvasW,
      canvasH:    canvasH,
      posterW:    posterW,
    );
  } catch (_) {
    return null;
  }
}
