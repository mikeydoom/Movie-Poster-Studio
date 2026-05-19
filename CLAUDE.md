# Movie Poster Studio — agent notes

Flutter Windows desktop app. Ported from a Python/Tkinter tool that scrapes
TMDB posters, composites them with overlays, dedupes results, and exports
slot data for **Movie Genre Workshop** (`RR_VHS_Tool.py`). Single
window, five tabs, no server, all data lives next to the .exe.

## Build & run

```bash
flutter build windows --release            # → build/windows/x64/runner/Release/movie_poster_studio.exe
flutter analyze                            # CI gate
```

Smoke-test pattern (the binary won't return on its own — it's a GUI):

```bash
"$RELEASE_DIR/movie_poster_studio.exe" &
sleep 4
tasklist //FI "IMAGENAME eq movie_poster_studio.exe" //NH | grep -qi movie_poster_studio && echo OK || echo DEAD
taskkill //IM movie_poster_studio.exe //F
```

**Stale-build trap — verify mtime after every build.** `flutter build
windows --release` reports `√ Built …` even when the link step
fails to overwrite an in-use exe. After every build that's supposed to
ship a change, check:

```bash
stat -c '%y' "$RELEASE_DIR/movie_poster_studio.exe"   # launcher (changes when runner/ or plugins change)
stat -c '%y' "$RELEASE_DIR/data/app.so"               # AOT Dart snapshot (changes when Dart sources change)
stat -c '%y' "$RELEASE_DIR/poster_native.dll"         # native plugin (changes when C++ sources change)
```

On Windows Flutter builds, **only `data/app.so` gets a new mtime when
Dart-only sources change** — the exe is a thin launcher that doesn't
relink unless runner/ or a plugin changes. If any of the above timestamps
are older than your most recent edit, the build was cached/stale. Fix
with `flutter clean && flutter build windows --release` (~40 s but
actually replaces everything). Don't claim "shipped" until mtimes are
fresh.

`dart run <one-off-test>.dart` is fair game for verifying **pure-Dart logic**
(exporter, rating mapping, config manager, movies-meta store) without
spinning up the window. **NOT** the converter or duplicate finder — both
import `package:flutter/foundation.dart` for `compute()`, which transitively
pulls `dart:ui` and can't load from a pure-Dart console. See git history for
`test_*.dart` examples — they're deliberately not committed.

## Portable-install invariant — **do not break**

The app is a portable install. Everything (config, posters, overlays,
metadata, exports) sits next to the .exe.

- `ConfigManager.baseDir()` returns `dirname(Platform.resolvedExecutable)`
  ([config_manager.dart](lib/services/config_manager.dart)). All defaults are
  rooted there.
- `ConfigManager.metadataDir()` returns `<baseDir>/metadata`. Single source
  of truth for derived/tracking artifacts. New sidecar files should go here.
- `config.json` is written with **relative paths** for anything under
  `baseDir` via `_relPath`/`_absPath` helpers. In memory paths are absolute;
  the JSON layer converts. This is what lets a user move the `Release/`
  folder to a thumb drive and have it just work.
- New path fields must be wrapped with `_relPath`/`_absPath` in their
  `toJson`/`fromJson`. Forgetting this is the canonical way to silently
  break portability.

## Architecture map

```
main.dart            Entry. Sets baseDir, loads config, creates AppState, runs MaterialApp.
app.dart             HomeShell + tab strip + bottom dock (Stage/File progress bars). Auto-scales
                     UI to window via MediaQuery.textScaler against a 1280×800 design baseline.
                     Also owns _CreditsDialog (first-run only) and the _credits const list.
theme/retrowave.dart Design tokens ported from RR_VHS_Tool's DS palette. Includes genreColors,
                     displayName() for UI-facing genre labels, and _matchGenre() which resolves
                     both internal names and display aliases for genreBg/genreFg lookups.
widgets/             Themed primitives. Flat 1px borders, max 2px corner radius, no glow.
services/            All non-UI logic. UI imports services; services do not import widgets.
  progress_bus.dart  Stream broadcast bus (log / step / file / done). Replaces Python's Queue+after.
  app_state.dart     ChangeNotifier wrapping AppConfig + ProgressBus + log buffer + run flags.
  config_manager.dart AppConfig + sub-configs (PosterConverter, TmdbFetcher, DuplicateFinder,
                     RrExportPrefs). _absPath/_relPath helpers. baseDir() / metadataDir() hooks.
                     Also stores hasShownCredits (first-run flag).
  tmdb_fetcher.dart  TMDB /discover/movie. Captures vote_average and upserts movies.json on every
                     fetch. Filename pattern is <safe_title>_<year>.png — the join key between
                     scraper, converter, gallery, dedupe, and RR exporter. Adult genre is
                     special-cased (see below). Rating-filter dropdown maps to vote_average bounds.
                     Pagination: _maxApiPages=500, _fetchBatchSize=20 parallel pages per batch,
                     total_pages read from response to avoid over-fetching.
                     Skip-already-scraped: download queue only enqueues files that don't exist yet.
                     Retry: _getWithRetry wraps all HTTP calls (3 attempts, exponential backoff).
  poster_converter.dart Profile-driven canvas + poster width via _profiles table.
                     generic = 979×1665 / posterW 979. new_release = 600×1200 / posterW 600.
                     Same algorithm in both: poster anchored top-left on opaque-black canvas,
                     overlay at bottom, brightness sampled on the post-composite canvas overlay
                     region to pick dark vs light. Per-file work runs via compute() — see
                     "Isolate gotcha" below. Tries native libvips path first (poster_native.dll);
                     falls back to Dart image package if unavailable.
                     overwrite=false (default) skips files that already exist in the output folder.
  poster_converter_ffi.dart  FFI bindings for poster_native.dll. PosterNativeFfi class loads
                     three symbols (init / available / convert). probeNativeAvailable() called
                     once in main isolate at start of runConverter; result passed as
                     _Task.useNative so workers don't re-probe. tryConvertNative() called from
                     compute() isolates — DynamicLibrary.open is safe across isolates (OS shares
                     one DLL handle per process). All native memory via calloc, freed same frame.
  movies_meta.dart   MovieMeta + JSON-backed store at <baseDir>/metadata/movies.json. Single
                     entry per filename. Auto-migrates from the pre-metadata-folder root location
                     on load. setRatingFor() and setOldFor() handle UI edits. isOld flag stored
                     as "is_old": true (omitted when false to keep JSON minimal).
  rr_rating.dart     TMDB vote_average → RR star bucket → last2 mapping. SKU generator
                     (prefix*10M + slot*10K + step + last2) + LCG-based rarity check
                     (rrSkuIsHolo / rrSkuIsOld). Port of RR_VHS_Tool.generate_sku.
  duplicate_finder.dart Filename-based duplicate scan. Classifies into crossSet (same filename
                     in both poster sets) vs crossGenre (same filename in 2+ genre folders within
                     one set). Scan runs in a compute() isolate; both sets walk in parallel via
                     Future.wait; uses async listing throughout. Public API: scanDuplicates().
  rr_exporter.dart   Walks library and writes custom_slots.json, nr_custom_slots.json,
                     replacements.json, edited_slots.json into <baseDir>/RR_Export/ in the exact
                     shapes Movie Genre Workshop v3 reads. DT name maps Kids→Kid; all others are identity.
                     Only Adult is excluded from NR. "Crime"/"crime" folder names are aliased to
                     "Police" for backward-compat with pre-v3 scrapes. Pre-flight validation
                     checks all poster paths before writing — logs a warning for missing files.
screens/             One per tab. Each owns its own controllers + persists via AppState.save().
                     Heavy work is fire-and-forget against ProgressBus. Per-tab status surfaces
                     should live inline on the screen (see converter_screen.dart's _StatusPanel
                     for the canonical pattern) — there is no global log dock anymore.
windows/poster_native/  Native C++ DLL project (poster_native.dll). See "Native FFI converter"
                     section below for full details.
```

## Folder layout on disk

Everything next to the .exe. Created on demand by the relevant code path.

```
posters/generic/<genre>/<Title>_<Year>.png                ← scraper, generic profile
posters/new_releases/<genre>/<Title>_<Year>.png           ← scraper, new_release profile
posters/converted/generic/<genre>/<Title>_<Year>.png      ← converter output, generic
posters/converted/new_releases/<genre>/<Title>_<Year>.png ← converter output, new_release
overlays/generic/overlay1.png                             ← required (dark variant)
overlays/generic/overlay2.png                             ← optional (auto-inverted from overlay1 if missing)
overlays/new_releases/overlay1.png
overlays/new_releases/overlay2.png
metadata/movies.json                                      ← TMDB rating + title cache, single source of truth
metadata/csv_lists/<genre>_<years>.csv                    ← per-fetch CSV record
config.json                                               ← AppConfig serialization (paths stored relative)
RR_Export/custom_slots.json
RR_Export/nr_custom_slots.json
RR_Export/replacements.json
RR_Export/edited_slots.json
etc/                                                      ← libvips runtime data (fontconfig)
share/                                                    ← libvips runtime data (gettext etc.)
poster_native.dll                                         ← GPU/SIMD converter plugin
libvips-42.dll  libglib-2.0-0.dll  (+ ~36 more DLLs)    ← libvips 8.18.2 runtime
```

The `etc/` and `share/` directories are installed by CMake from
`windows/poster_native/vendor/libvips/`. They hold fontconfig config and
gettext catalogs that GLib looks for relative to the exe on Windows.
Without them libvips may emit GLib warnings at startup (non-fatal with our
log sink, but cleaner to include them).

The set-folder string constants live in
[duplicate_finder.dart's `setFolders` map](lib/services/duplicate_finder.dart)
— RR exporter imports from there, and `gallery_screen` / `converter_screen` /
`tmdb_screen` mirror the same path shape. If you ever restructure the layout
again, change `setFolders` first and update each consumer to match.

Folder names are lowercased on disk (`action`, `sci-fi`, …) but
`Retrowave.genreBg/Fg`, the RR exporter, and the gallery all normalize back
to canonical internal names (`Action`, `Sci-Fi`, `Kids`). When adding
genre-aware code, route through the case-insensitive matchers — don't
compare folder names directly.

Filenames use `_safe_file_name`: strip `<>:"/\|?*`, replace spaces with `_`.
Same convention as the original Python so cross-tool interop works.

## Genre display names vs internal tags — **do not conflate**

Three genres have different UI display names vs their internal tags:

| Internal tag | Display name | Folder on disk | Export DT name |
|---|---|---|---|
| `Police` | Crime | `police/` | `Police` |
| `Kids` | Family | `kids/` | `Kid` |
| `Xmas` | Holiday | `xmas/` | `Xmas` |

**Internal tags** are used everywhere that touches data: folder names, config
`selectedGenres`, `genreColors` map keys, exporter DT/code/byte tables,
`rrGenreSkuPrefix`, filter state in screens.

**Display names** are UI-only. All display sites call `Retrowave.displayName(internalTag)`
which reads from `_genreDisplayNames`. The `_matchGenre` helper in `retrowave.dart`
resolves both internal names and display-name aliases so `genreBg("Crime")` correctly
returns the Police color. Never store a display name in config, on disk, or in any
export JSON.

## Isolate gotcha — **read before touching any heavy worker**

Do **not** use `Isolate.run(() => topLevelFn(arg))` inside an async chain
that originated from a widget event handler. The closure transitively
captures `WidgetsFlutterBinding`, which is unsendable across isolate
boundaries. You'll get
`Illegal argument in isolate message: ... <- WidgetsFlutterBinding`.

Use `compute(topLevelFn, arg)` from `package:flutter/foundation.dart`
instead. `compute` takes a **top-level function reference** (not a closure)
plus a single sendable message, so no ambient scope gets captured. See
[poster_converter.dart](lib/services/poster_converter.dart) `_processGenre`
and [duplicate_finder.dart](lib/services/duplicate_finder.dart) `_scanBody`
for canonical patterns.

## TMDB scraper

- Genre IDs live in `_genreMap` ([tmdb_screen.dart](lib/screens/tmdb_screen.dart)).
  Kids = 10751 (Family genre), Adult = `null` (handled specially).
- All other genres use real TMDB genre IDs (Action 28, Horror 27, etc.).
- A `null` id triggers the "no genre ID" skip path **except** for genres with
  their own keyword branch (Adult, Music, Xmas, Sports).
- **Production Country** / **Original Language** dropdowns add
  `with_origin_country` + `region` + `with_original_language`. Empty
  ("Any") drops the param.
- **Rating** dropdown (`Any` / `Good Critic` / `Bad Critic`) maps to TMDB
  `vote_average` bounds matching RR's tag buckets:
  - Good Critic → stars 4.0–4.5 → `vote_average.gte=7.5 & lte=9.49`.
  - Bad Critic → stars 0.0–1.5 → `vote_average.lte=2.99`.
  - The 5.0★ tier and the 2.0–3.5★ band are deliberately excluded from
    both buckets to match `RR_VHS_Tool.STAR_OPTIONS`. Applies to every
    genre uniformly, including Adult.
- **Pagination**: `_maxApiPages = 500` (TMDB's hard server cap). Pages are
  fetched in parallel batches of `_fetchBatchSize = 20`. Each batch reads
  `total_pages` from the response and uses it as an early-exit ceiling so
  we never request pages beyond what TMDB has for that genre/filter combo.
- **Skip already-scraped**: download queue only adds tasks where the output
  file doesn't already exist on disk — re-running a scrape is safe.
- **Retry**: `_getWithRetry` wraps all HTTP calls with 3 attempts and
  exponential backoff for network errors and 5xx responses.
- **Xmas**: uses keyword ID `65` (`_xmasKeywordIds = [65]`).
- **Sports**: uses keyword IDs `[6075, 294708]`.
- **Music**: resolves keyword IDs from `_musicKeywords` list at runtime.

### Adult genre — special case

Adult has no native TMDB genre ID. `runTmdbFetcher` branches on
`g.name == 'Adult'` and substitutes:

```
with_keywords=<id1>|<id2>|… (OR of _adultKeywords, resolved via /3/search/keyword)
certification_country=US
certification=NC-17
```

The keyword list was tightened iteratively with the user — broad terms
like `tattoo`, `nudity`, `bareback`, `stockings`, `tongue`, etc. pull in
R-rated mainstream comedies and were removed. Don't re-add them without
verifying via a test pull. IDs are resolved once per process into
`_cachedAdultKeywordIds`.

NC-17 is US-specific, so `certification_country=US` stays pinned to US
even when the user picks a different Production Country. That filter
acts as the anti-mainstream signal — the keyword set alone produces noisy
results (American Pie, Friends with Benefits, Idiocracy, etc. get tagged
with one or two keywords).

## Movie Genre Workshop interop reference

Source of truth: `RR_VHS_Tool.py` (not in this repo — user has it locally
at `C:\Users\Admin\Downloads\Movie Genre Workshop\…`). What the
exporter produces must exactly match what that tool reads.

- **custom_slots.json** —
  `{ "<DT_name>": [ {bkg_tex, sub_tex, pn_name, ls, lsc, sku, ntu}, … ] }`.
  DT names map `Kids → "Kid"`; all others are identity. `bkg_tex` =
  `T_Bkg_<Code>_<NNN>` (3-digit). `sub_tex` is `T_Sub_78` onwards using a
  **global** counter (`_subTexFor(globalCustomSlotIdx)`) that never resets
  between genres — matches `get_custom_slot_si` in RR_VHS_Tool v3. `ls` is
  always `1` (FIXED_REGULAR_LAYOUT). `ntu` is always `false`.
- **nr_custom_slots.json** — flat list of
  `{title, genre, genre_code, genre_byte, bkg_tex, sku, standee_shape, tex_num}`.
  `bkg_tex` = `T_New_<Code>_<NNN>`. SKU uses the same `rrGenerateSku` formula
  as generic slots (prefix×10M + slot×10K + step + last2) — NOT the old
  50000–59999 range. Only Adult is excluded from NR. `standee_shape ∈ {A,B,C}`.
- **replacements.json** (v3 schema) —
  Generic slots: `{ "T_Bkg_Act_001": {movies: [...], new_release: false, sku: N, path: "C:/…/action/Alien_1979.png", offset_x: 0, offset_y: -170, zoom: 0.813}, … }`.
  NR slots: `{ "NR_<sku>": {movies: [...], new_release: true, sku: N, path: "C:/…/new_releases/action/Alien_1979.png"} }`.
  Keys are sorted. Paths use forward slashes. Pre-flight validation logs a
  warning for any path that doesn't exist on disk before writing the file.

If RR_VHS_Tool changes its schemas, mirror those changes here. Cross-check
the GENRES / NR_GENRE_BYTE / GENRE_DATATABLE / GENRE_SKU_PREFIX tables
when versions bump.

## Conversion algorithm — pinned

Profile dimensions live in `_profiles` ([poster_converter.dart](lib/services/poster_converter.dart)):

| Profile | Canvas | Poster width |
|---|---|---|
| `generic` | 979 × 1665 | 979 |
| `new_release` | 600 × 1200 | 600 |

Dimensions travel through `_Task` (its `canvasW`/`canvasH`/`posterW`
fields) all the way through `compute()` into the worker isolate. Do not
re-introduce module-level dimension constants in `_convertSync` — the
isolate boundary breaks any implicit dependency.

Algorithm (same for both profiles, only dims differ):

1. Decode source → sRGB (flatten alpha).
2. Resize poster to `spec.posterW` (cubic), height = `srcH × posterW / srcW`.
3. Build opaque-black `spec.canvasW × spec.canvasH` canvas (sRGB interpretation).
4. Paste poster at (0, 0).
5. Pick dark/light overlay by sampling luminance of the canvas's overlay
   region (`y = canvasH - overlayH` down to `canvasH`). `mean luminance > 140`
   → dark overlay; else light.
6. Composite overlay at bottom, encode PNG (compress level 6).

The native path (libvips) and the Dart fallback path implement the same
algorithm. The `_convertSync` function in `poster_converter.dart` tries
`tryConvertNative` first; on null return it falls through to the Dart
`image` package path.

**Don't change canvas dimensions without explicit user direction.** Both
sets are pinned by past conversations. The 979×1665 canvas deliberately
does NOT match Movie Genre Workshop's 1024×2048 texture canvas; Movie Genre Workshop
cover-fits whatever it's given. That mismatch is intentional.

## Native FFI converter (poster_native.dll)

**Status: implemented and working.** Replaces the old "deferred future work"
plan. The native path is active by default; the app falls back to the
Dart `image` package automatically if the DLL is absent or fails to init.

### File layout

```
windows/poster_native/
  CMakeLists.txt          builds poster_native.dll; no install() here
  poster_native.h         exported C API (3 symbols)
  poster_native.cpp       implementation (libvips 8.18.2)
  vendor/libvips/
    bin/    ← 38 runtime DLLs (libvips-42.dll, libglib-2.0-0.dll, etc.)
    lib/    ← MSVC import libs (libvips.lib, libglib-2.0.lib, libgobject-2.0.lib)
    include/← vips/ and glib-2.0/ headers
    etc/    ← fontconfig config (installed next to exe by CMake)
    share/  ← gettext catalogs (installed next to exe by CMake)
lib/services/poster_converter_ffi.dart   Dart FFI wrapper
```

CMake install rules live in `windows/CMakeLists.txt` (NOT in
`poster_native/CMakeLists.txt`) because `CMAKE_INSTALL_PREFIX` isn't
resolved when `add_subdirectory("poster_native")` runs. The parent file
installs `poster_native.dll`, all `*.dll` from `vendor/libvips/bin/`,
and the `etc/` and `share/` runtime data directories.

### C API

```c
int poster_native_init(const char* argv0);       // call once; 1=ok 0=fail
int poster_native_available(void);               // 1 if init succeeded
int poster_native_convert(
    const char* input_path, const char* output_path,
    const uint8_t* dark_png,  int dark_len,
    const uint8_t* light_png, int light_len,
    int canvas_w, int canvas_h, int poster_w,
    char* out_status, int status_len            // "luma=X.X DARK/LIGHT" on success
);  // returns 0 on success, -1 on failure
```

### Crash-safety

Both `poster_native_init` and `poster_native_convert` are guarded so that
any failure degrades gracefully to the CPU fallback instead of killing
the Flutter process:

- **`poster_native_init`**: calls `g_log_set_always_fatal(0)` (prevents
  GLib's `g_error()` from calling `abort()`) and installs a silent GLib log
  handler before `VIPS_INIT`. Entire init body is wrapped in
  `__try / __except(EXCEPTION_EXECUTE_HANDLER)` to catch SEH (access
  violations, etc.).
- **`poster_native_convert`**: split into an outer SEH shell
  (`poster_native_convert`) and an inner C++ worker (`_convert_inner`) to
  work around MSVC's restriction on mixing C++ objects and SEH in the same
  function. The outer shell catches any native crash and returns -1 with a
  status message.

### Known libvips pitfalls (already fixed — don't reintroduce)

1. **`vips_image_new_from_file` returns `VipsImage*` directly** — it does
   NOT take a `VipsImage**` output pointer like the other vips ops
   (`vips_resize`, `vips_colourspace`, etc.). The correct call is:
   ```cpp
   src.p = vips_image_new_from_file(input_path, "access", VIPS_ACCESS_RANDOM, NULL);
   if (!src.p) { /* error */ }
   ```
   Passing `&src.p` as a vararg corrupts the argument list and causes a
   NULL dereference crash at offset 0x60 in `vips_colourspace`.

2. **`vips_black` creates a `MULTIBAND` image**, not sRGB. `vips_composite2`
   needs both inputs in a known colorspace. Fix: immediately copy with
   sRGB interpretation after creating the canvas:
   ```cpp
   vips_copy(raw, &canvas_rgb.p, "interpretation", VIPS_INTERPRETATION_sRGB, NULL)
   ```

3. **Use `VIPS_ACCESS_RANDOM`, not `VIPS_ACCESS_SEQUENTIAL`**. The pipeline
   reads `canvas_rgba` twice: once when sampling luma for the overlay region
   (step 8) and again in `vips_composite2` (step 12). Sequential mode only
   allows a single top-to-bottom pass; the second read causes "out of order
   read" and returns -1.

### Testing the native DLL without the GUI

Run from the Release directory using Python (isolates crashes to subprocess):

```python
# probe_convert.py — see build history for full script
import ctypes, os
os.chdir(RELEASE_DIR)
ctypes.WinDLL("kernel32").AddDllDirectory(RELEASE_DIR)
lib = ctypes.CDLL("poster_native.dll")
lib.poster_native_init.restype = ctypes.c_int
lib.poster_native_init(exe_path.encode())
# then lib.poster_native_convert(...)
```

## Gallery

Two-tab structure (Generic Posters / New Release Posters). Each tab:

- **Genre shelf (left, 240 px)**: always renders the **full canonical RR
  genre list** (Action / Adult / Adventure / Comedy / Drama / Fantasy /
  Horror / Kids / Police / Romance / Sci-Fi / Western / Xmas) plus an
  `ALL` entry. Empty genres show `(0)` and remain clickable. Folders on
  disk that don't match a canonical name (typos, legacy folders) are
  appended at the end so they're still reachable. Genre labels shown via
  `Retrowave.displayName()` — Crime / Family / Holiday in the shelf.
- **Sort dropdown**: below the search field in each shelf. Options: Title A→Z
  (default), Title Z→A, Year (Newest), Year (Oldest), Stars High→Low,
  Stars Low→High. State stored in `_sortMode` map per set. Ties fall back to
  alphabetical. Applied in `_filteredFor`.
- **Grid (right)**: aspect-ratio differs per tab.
  - Generic: `979 / 1665` cells, `BoxFit.cover`.
  - New release: `600 / 941` cells, `BoxFit.fitWidth` + `Alignment.topCenter`
    inside the existing `ClipRect`. Renders only the top 600×941 of the
    600×1200 source on disk — bottom overlay strip is intentionally
    cropped out of the thumbnail view. The PNG on disk is unchanged.
- **Tile overlay**: bottom strip with year (center-left), genre chip
  (left, colored from GENRE_COLORS), and 5-star row (right, gold via
  `Icons.star` / `star_half` / `star_border`). Filename moved to a hover
  tooltip. When `MovieMeta.isOld` is true, a gold "OLD" badge is rendered
  at the top-right corner of the tile.
- **Selection**: tap a tile → toggle selection. Selected tiles get a
  magenta border + check overlay. Selection persists when switching tabs —
  each tab's toolbar operates on its own set independently, enabling bulk
  cross-set workflows (select generic, switch to NR, select there, act on each).
- **Selection toolbar** appears at the top of the grid when ≥1 tile is
  selected — `Genre`, `Rating`, `→ NR / → Generic` (set swap), `Mark Old /
  Unmark Old` (gold, `Icons.history`), `Delete`, `Select All`, `Clear`.
- **Right-click context menu**: `Change Genre…` (submenu) / `Set Rating…`
  (RR's 10 star tiers) / `Mark as Old / Unmark as Old` /
  `Move to New Releases / Move to Generic` / `Delete`.
  Operates on the clicked tile alone if it's not in the current selection;
  otherwise operates on the whole selection.
- **Scrollbars**: locally themed magenta via `_withMagentaScrollbar`
  helper — wraps the GridView and ListView in a `Theme` override so
  scrollbars elsewhere in the app keep the default subtle style.

File operations (`_moveItems` / `_deleteItems` / `_setRatingForItems` /
`_setOldForItems` / `_swapItemsToOtherSet`):
- Move-to-genre = `File.rename` to `<base>/<set>/<newGenre>/<filename>`.
  Skips (with a logged warning) if the destination already exists — no
  silent overwrite.
- Set rating writes to movies.json via `MoviesMetaStore.setRatingFor`.
  `vote_average` is stored as `stars × 2` so a future TMDB rescrape will
  re-bucket to the same tier. Filenames are deduplicated before writing
  so multi-selection on the same movie across genres only writes once.
- Mark as Old writes `isOld: true` to movies.json via
  `MoviesMetaStore.setOldFor`. A stub entry is created if none exists for
  the file. The flag is preserved through rating edits (`setRatingFor`
  keeps `isOld: cur.isOld`). Stored as `"is_old": true`; field is omitted
  entirely when false to keep JSON minimal.
- Swap set re-converts from the raw TMDB source using the target profile
  (`convertOneFile`), writes to the destination set's `converted/` tree,
  and deletes the old converted file. Skips with a warning if no raw source
  is found (never scraped / was deleted).
- Delete unlinks files but does **not** touch movies.json — the metadata
  could still apply to copies of the same movie in other genres.

## Duplicate checker

Scan runs in a `compute()` isolate so the UI stays responsive. Both
`posters/converted/generic/` and `posters/converted/new_releases/` are
walked concurrently via `Future.wait` inside the isolate. All directory
listing uses async `await dir.list().toList()` so I/O interleaves correctly.

Results are classified:
- **Cross-set**: same filename appears in both sets → pick which set to keep.
- **Cross-genre**: same filename in 2+ genre folders within one set → pick which genre to keep.

UI state:
- `_cachedGenres` — sorted list of genres with dupes. Computed once when the
  scan result arrives, then updated incrementally after every individual or
  bulk resolution. Never recomputed on each build pass.
- `_updateGenreCache()` — call inside every `setState` that removes groups.
  The filter chips go stale if you forget this.
- Cards render in a `CustomScrollView` with `SliverList` + `SliverChildBuilderDelegate`
  so `Image.file` in `_SetCard` is only decoded for cards near the viewport —
  lazy loading on large duplicate lists.

## MovieMeta schema (movies.json)

```json
{
  "Aliens_1986.png": {
    "tmdb_id": 679,
    "title": "Aliens",
    "year": "1986",
    "vote_average": 7.9,
    "is_old": true        ← omitted entirely when false
  }
}
```

`setOldFor` in `MoviesMetaStore` creates a stub entry (tmdb_id=0, empty
title/year, vote_average=0) if no entry exists yet, so the Old badge works
even for movies that were never scraped via TMDB.

## AppConfig — persisted fields

All fields in `config.json`. Non-path fields pass through as-is; path fields
use `_relPath`/`_absPath`.

Notable fields added recently:
- `has_shown_credits` (`bool`, default `false`) — set to `true` after the
  first-run credits dialog is dismissed. Never resets unless manually edited
  in `config.json`.

## First-run credits dialog

`_CreditsDialog` in `app.dart`. Shown once via `addPostFrameCallback` in
`_HomeShellState.initState` when `config.hasShownCredits == false`. After
dismissal, sets `config.hasShownCredits = true` and saves config.

Credits list is a `const List<String> _credits` at the top of `app.dart`.
To add or remove names, edit that list.

## Rating mapping (source-of-truth concern)

`rrStarsFromVoteAverage` in `rr_rating.dart`. RR has no 3.0★ tier —
`vote=6.0` deliberately rounds **up** to 3.5★ rather than down to 2.5★.
If that judgement call needs to flip, it's a one-line change in that
function. Keep the docstring's table accurate to whatever the function
does.

## Conventions

- All UI strings shown to the user are UPPERCASE for headings/labels and
  mixed-case for body. Match `Retrowave.heading()` vs `Retrowave.body()`.
- Per-tab status surfaces (progress, log tail, result summary) live
  inline on the screen, not in any shared dock. Pattern in
  [converter_screen.dart](lib/screens/converter_screen.dart)'s `_StatusPanel`.
- Preflight checks (paths exist, required files like `overlay1.png`
  present) happen **before** kicking off background work. Failures pop
  an `AlertDialog` with the full path so the user can actually see them.
  Letting errors land only in `state.log` is invisible to the user since
  the log dock was removed.

## Patterns to copy when adding things

- **New tab**: define a `Screen` widget in `lib/screens/`, append a
  `_TabSpec` in `app.dart`. Local state owns its controllers and
  persists via `widget.state.save()` after mutating the appropriate
  sub-config.
- **New persisted config field**: add it to the sub-config class + its
  `toJson`/`fromJson`, then add a sensible default in
  `ConfigManager.defaultConfig()`. Path fields go through
  `_relPath`/`_absPath`. Non-path fields just pass through.
- **New background workflow**: emit `bus.emitLog` / `emitStep` /
  `emitFile` as it progresses, and `bus.emitDone('<tag>')` exactly once
  at the end (use `try/finally`). If the UI needs a "running" flag, add
  one to `AppState` and flip it in the `bus.done` handler — don't track
  it from inside the screen.
- **New sidecar JSON / metadata file**: put it under
  `ConfigManager.metadataDir()`. Auto-create the dir before writing.
  If you're migrating from a previous location, do the migration inside
  the loader (mirror `MoviesMetaStore._migrateLegacyLocation`) so it's
  transparent on first run.
- **New filter on the scraper**: add a config field, a UI dropdown, and
  a helper that translates the choice into TMDB query params. Merge the
  helper's output into `filterParams` in both branches (Adult and
  regular genres) of `runTmdbFetcher`. Pattern in `_ratingFilterParams`.
- **New heavy background task**: use `compute(topLevelFn, sendableArg)`.
  Strip all `ProgressBus` calls from the isolate body — bus is not sendable.
  Emit log/step/done around the `compute()` call in the public wrapper.

## Don'ts

- Don't import `package:flutter/*` from `services/*.dart` unless you need
  `compute`. Currently `poster_converter.dart` and `duplicate_finder.dart`
  both use it — they are the documented exceptions. All other services must
  remain pure Dart so they can be tested with `dart run`.
- Don't change canvas dimensions (generic 979×1665, NR 600×1200) or
  default poster download size (`w780`) without an explicit user
  request — all pinned by past conversations.
- Don't restore the bottom log dock. The user removed it deliberately;
  surface per-tab status inline.
- Don't add emojis to source files or generated docs unless the user asks.
- Don't re-add the Adult/Xmas `_specialGenres` override map that abused
  TMDB genre 99/16. Xmas uses keyword ID 65; Adult uses keyword-OR + NC-17.
- Don't bulk-rebuild for unrelated changes — `flutter build windows
  --release` is ~20 s and is fine, but skip it if you only touched
  markdown/config.
- Don't reintroduce the three libvips bugs that were fixed in
  `poster_native.cpp` (wrong `vips_image_new_from_file` call signature,
  MULTIBAND canvas, SEQUENTIAL access). They are documented in the
  "Known libvips pitfalls" section above.
- Don't cap `_maxApiPages` in `tmdb_fetcher.dart` back to a small
  number. It was raised from 15 → 500 deliberately because 15 pages
  hard-capped results at 300 regardless of what the user requested.
- Don't store display names (Crime, Family, Holiday) anywhere except the
  UI label layer. Internal tags (Police, Kids, Xmas) own all data paths.
- Don't call `_genresWithDupes()` — it was removed. Use `_cachedGenres`
  and call `_updateGenreCache()` inside any `setState` that modifies the
  duplicate report.
