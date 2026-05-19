// poster_native.cpp — GPU/SIMD-accelerated poster conversion via libvips.
//
// Exposed API: poster_native_init / poster_native_available / poster_native_convert.
// See poster_native.h for parameter docs.
//
// Algorithm mirrors _convertSync in poster_converter.dart exactly:
//   load → resize (cubic) → black canvas → paste poster → sample luma →
//   choose overlay → composite OVER → save PNG (level 6).

#include "poster_native.h"
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <vips/vips.h>

#include <algorithm>
#include <cstdio>
#include <cstring>

// ─── Global init state ──────────────────────────────────────────────────────

static bool g_vips_ok = false;

// GLib log handler installed before VIPS_INIT: swallows all log output so
// g_error() / g_critical() never call abort(). After a successful init we
// leave it in place — we surface errors through our own out_status buffer.
static void _vips_log_sink(const gchar*, GLogLevelFlags, const gchar*, gpointer) {}

int poster_native_init(const char* argv0) {
    if (g_vips_ok) return 1;

    // Prevent GLib's g_error() from calling abort() before VIPS_INIT runs.
    // g_log_set_always_fatal(0) makes NO log level fatal; combined with our
    // custom handler this means VIPS_INIT failures surface as a non-zero
    // return value rather than TerminateProcess.
    g_log_set_always_fatal(static_cast<GLogLevelFlags>(0));
    g_log_set_default_handler(_vips_log_sink, nullptr);

    // __try / __except catches any SEH exception (access violation, illegal
    // instruction, etc.) that would otherwise kill the host process. With
    // /EHsc this is valid in functions that have no local objects needing
    // C++ unwinding — which is the case here.
    int result = 0;
    __try {
        if (VIPS_INIT(argv0 && argv0[0] ? argv0 : "poster_native") == 0) {
            vips_cache_set_max(100);   // keep a modest op-cache; 0 = unlimited
            g_vips_ok = true;
            result = 1;
        } else {
            vips_error_clear();
            result = 0;
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        vips_error_clear();
        result = 0;
    }
    return result;
}

int poster_native_available(void) {
    return g_vips_ok ? 1 : 0;
}

// ─── RAII wrapper for VipsImage* ────────────────────────────────────────────

struct VI {
    VipsImage* p = nullptr;
    VI() = default;
    explicit VI(VipsImage* img) : p(img) {}
    ~VI() { reset(); }
    VI(const VI&) = delete;
    VI& operator=(const VI&) = delete;
    void reset() { if (p) { g_object_unref(p); p = nullptr; } }
    VipsImage** operator&() { reset(); return &p; }
    operator VipsImage*() const { return p; }
    operator bool() const { return p != nullptr; }
};

// ─── Conversion ─────────────────────────────────────────────────────────────

// Forward declaration for the inner (non-SEH) worker so the outer shell
// can stay SEH-safe even though the worker uses C++ objects (VI structs).
static int _convert_inner(
    const char*    input_path,
    const char*    output_path,
    const uint8_t* dark_png,   int dark_len,
    const uint8_t* light_png,  int light_len,
    int canvas_w,  int canvas_h, int poster_w,
    char* out_status, int status_len);

// Outer shell: catches any SEH exception from the inner C++ worker so a
// native crash (access violation, libvips assert) doesn't kill Flutter.
int poster_native_convert(
    const char*    input_path,
    const char*    output_path,
    const uint8_t* dark_png,   int dark_len,
    const uint8_t* light_png,  int light_len,
    int canvas_w,  int canvas_h, int poster_w,
    char* out_status, int status_len
) {
    if (!g_vips_ok) {
        snprintf(out_status, status_len, "libvips not initialised");
        return -1;
    }
    int rc = -1;
    __try {
        rc = _convert_inner(
            input_path, output_path,
            dark_png, dark_len, light_png, light_len,
            canvas_w, canvas_h, poster_w,
            out_status, status_len);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        snprintf(out_status, status_len, "native crash (SEH caught)");
        vips_error_clear();
        rc = -1;
    }
    return rc;
}

// Inner worker: uses C++ objects (VI RAII wrappers) — cannot mix with SEH.
static int _convert_inner(
    const char*    input_path,
    const char*    output_path,
    const uint8_t* dark_png,   int dark_len,
    const uint8_t* light_png,  int light_len,
    int canvas_w,  int canvas_h, int poster_w,
    char* out_status, int status_len
) {
    // ── 1. Load source image ──────────────────────────────────────────────
    // vips_image_new_from_file returns VipsImage* (or NULL on error) — it
    // does NOT write through an output pointer like the other vips ops do.
    //
    // Use VIPS_ACCESS_RANDOM (not SEQUENTIAL) because the canvas derived
    // from this image is read twice: once when sampling luma for the overlay
    // region (step 8) and again during vips_composite2 (step 12). Sequential
    // mode only allows a single top-to-bottom pass; trying to re-read from
    // line 0 after the luma crop causes "out of order read" and a -1 return.
    // Posters are small (< 1 MB decoded) so random access is fine here.
    VI src;
    src.p = vips_image_new_from_file(input_path,
            "access", VIPS_ACCESS_RANDOM, NULL);
    if (!src.p) {
        snprintf(out_status, status_len, "load: %s", vips_error_buffer());
        vips_error_clear();
        return -1;
    }

    // ── 2. Flatten to sRGB (handles RGBA/grayscale/palette source) ────────
    VI flat;
    {
        VI tmp;
        // Convert colourspace to sRGB first (handles palette, CMYK, etc.)
        if (vips_colourspace(src, &tmp.p, VIPS_INTERPRETATION_sRGB, NULL) != 0) {
            snprintf(out_status, status_len, "colourspace: %s", vips_error_buffer());
            vips_error_clear();
            return -1;
        }
        src.reset();
        // Remove alpha by compositing against black (no-op if no alpha).
        if (vips_image_hasalpha(tmp)) {
            if (vips_flatten(tmp, &flat.p, NULL) != 0) {
                snprintf(out_status, status_len, "flatten: %s", vips_error_buffer());
                vips_error_clear();
                return -1;
            }
        } else {
            flat.p = tmp.p; tmp.p = nullptr;
        }
    }

    // ── 3. Resize to poster_w (cubic), preserving aspect ratio ───────────
    VI poster;
    {
        double scale = static_cast<double>(poster_w)
                     / vips_image_get_width(flat);
        if (vips_resize(flat, &poster.p, scale,
                "kernel", VIPS_KERNEL_CUBIC, NULL) != 0) {
            snprintf(out_status, status_len, "resize: %s", vips_error_buffer());
            vips_error_clear();
            return -1;
        }
        flat.reset();
    }

    // ── 4. Create opaque-black sRGB canvas (canvas_w × canvas_h) ─────────
    // vips_black creates a MULTIBAND image by default. We must stamp it as
    // VIPS_INTERPRETATION_sRGB so that vips_composite2 (step 12) can match
    // colorspaces with the sRGB overlay; otherwise composite fails with
    // "no known route from multiband to srgb".
    VI canvas_rgb;
    {
        VI raw;
        if (vips_black(&raw.p, canvas_w, canvas_h, "bands", 3, NULL) != 0) {
            snprintf(out_status, status_len, "black: %s", vips_error_buffer());
            vips_error_clear();
            return -1;
        }
        if (vips_copy(raw, &canvas_rgb.p,
                "interpretation", VIPS_INTERPRETATION_sRGB, NULL) != 0) {
            snprintf(out_status, status_len, "canvas srgb: %s", vips_error_buffer());
            vips_error_clear();
            return -1;
        }
    }

    // ── 5. Insert poster at (0,0) — clips to canvas bounds ───────────────
    VI with_poster;
    if (vips_insert(canvas_rgb, poster, &with_poster.p, 0, 0, NULL) != 0) {
        snprintf(out_status, status_len, "insert: %s", vips_error_buffer());
        vips_error_clear();
        return -1;
    }
    canvas_rgb.reset();
    poster.reset();

    // ── 6. Add opaque alpha channel (alpha = 255 everywhere) ─────────────
    VI canvas_rgba;
    if (vips_addalpha(with_poster, &canvas_rgba.p, NULL) != 0) {
        snprintf(out_status, status_len, "addalpha: %s", vips_error_buffer());
        vips_error_clear();
        return -1;
    }
    with_poster.reset();

    // ── 7. Load overlay PNGs from memory ──────────────────────────────────
    VI dark_ov, light_ov;
    if (vips_pngload_buffer(
            const_cast<void*>(static_cast<const void*>(dark_png)),
            static_cast<size_t>(dark_len), &dark_ov.p, NULL) != 0) {
        snprintf(out_status, status_len, "dark overlay: %s", vips_error_buffer());
        vips_error_clear();
        return -1;
    }
    if (vips_pngload_buffer(
            const_cast<void*>(static_cast<const void*>(light_png)),
            static_cast<size_t>(light_len), &light_ov.p, NULL) != 0) {
        snprintf(out_status, status_len, "light overlay: %s", vips_error_buffer());
        vips_error_clear();
        return -1;
    }

    // ── 8. Sample mean luminance in the overlay region ───────────────────
    int overlay_h = vips_image_get_height(dark_ov);
    int overlay_y = canvas_h - overlay_h;
    if (overlay_y < 0) overlay_y = 0;
    int crop_h = std::min(overlay_h, canvas_h - overlay_y);

    double luma = 0.0;
    {
        // Crop the overlay region from the composited canvas.
        VI region;
        if (vips_crop(canvas_rgba, &region.p,
                0, overlay_y, canvas_w, crop_h, NULL) != 0) {
            snprintf(out_status, status_len, "crop: %s", vips_error_buffer());
            vips_error_clear();
            return -1;
        }

        // Extract RGB bands only (discard alpha).
        VI region_rgb;
        if (vips_extract_band(region, &region_rgb.p, 0, "n", 3, NULL) != 0) {
            snprintf(out_status, status_len, "extract_band: %s", vips_error_buffer());
            vips_error_clear();
            return -1;
        }
        region.reset();

        // Convert to B_W (Rec.601: 0.299R + 0.587G + 0.114B) — matches Dart.
        VI grey;
        if (vips_colourspace(region_rgb, &grey.p,
                VIPS_INTERPRETATION_B_W, NULL) != 0) {
            snprintf(out_status, status_len, "grey: %s", vips_error_buffer());
            vips_error_clear();
            return -1;
        }
        region_rgb.reset();

        if (vips_avg(grey, &luma, NULL) != 0) {
            snprintf(out_status, status_len, "avg: %s", vips_error_buffer());
            vips_error_clear();
            return -1;
        }
    }

    // ── 9. Choose overlay (mirrors Dart threshold 140.0) ─────────────────
    bool use_dark = luma > 140.0;
    VipsImage* chosen_raw = use_dark ? dark_ov.p : light_ov.p;

    // ── 10. Ensure chosen overlay has alpha ───────────────────────────────
    VI chosen_ov;
    if (vips_image_hasalpha(chosen_raw)) {
        chosen_ov.p = chosen_raw;
        g_object_ref(chosen_ov.p);
    } else {
        if (vips_addalpha(chosen_raw, &chosen_ov.p, NULL) != 0) {
            snprintf(out_status, status_len, "ov addalpha: %s", vips_error_buffer());
            vips_error_clear();
            return -1;
        }
    }
    dark_ov.reset();
    light_ov.reset();

    // ── 11. Embed overlay at (0, overlay_y) in canvas-sized image ─────────
    // EXTEND_BLACK on an RGBA image fills transparent (0,0,0,0) outside.
    VI ov_embedded;
    if (vips_embed(chosen_ov, &ov_embedded.p,
            0, overlay_y, canvas_w, canvas_h, NULL) != 0) {
        snprintf(out_status, status_len, "embed: %s", vips_error_buffer());
        vips_error_clear();
        return -1;
    }
    chosen_ov.reset();

    // ── 12. Composite overlay OVER canvas (alpha-aware) ───────────────────
    VI result;
    if (vips_composite2(canvas_rgba, ov_embedded, &result.p,
            VIPS_BLEND_MODE_OVER, NULL) != 0) {
        snprintf(out_status, status_len, "composite: %s", vips_error_buffer());
        vips_error_clear();
        return -1;
    }
    canvas_rgba.reset();
    ov_embedded.reset();

    // ── 13. Save PNG with compression level 6 ────────────────────────────
    if (vips_pngsave(result, output_path,
            "compression", 6, NULL) != 0) {
        snprintf(out_status, status_len, "pngsave: %s", vips_error_buffer());
        vips_error_clear();
        return -1;
    }

    snprintf(out_status, status_len, "luma=%.1f %s", luma,
             use_dark ? "DARK" : "LIGHT");
    return 0;
}
