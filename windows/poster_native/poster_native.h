#pragma once
#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

// Initialize libvips. Must be called once before convert. Pass argv[0] or "".
// Returns 1 on success, 0 on failure.
__declspec(dllexport) int poster_native_init(const char* argv0);

// Returns 1 if libvips initialised successfully, 0 otherwise.
__declspec(dllexport) int poster_native_available(void);

// GPU/SIMD-accelerated poster conversion using libvips.
//
// Replicates the Dart _convertSync algorithm:
//   1. Load source image from input_path.
//   2. Resize to poster_w (cubic), preserving aspect ratio.
//   3. Paste at (0,0) on an opaque-black canvas_w x canvas_h canvas.
//   4. Sample mean luminance of the canvas overlay region.
//   5. Composite the dark overlay (luma > 140) or light overlay at bottom.
//   6. Save as PNG (compression level 6) to output_path.
//
// dark_png / light_png are the raw PNG bytes already in memory.
// out_status: caller-allocated buffer. Filled with "luma=X.X DARK|LIGHT" on
//             success, or an error description on failure.
// Returns 0 on success, non-zero on failure.
__declspec(dllexport) int poster_native_convert(
    const char*    input_path,
    const char*    output_path,
    const uint8_t* dark_png,   int dark_len,
    const uint8_t* light_png,  int light_len,
    int canvas_w,  int canvas_h, int poster_w,
    char* out_status, int status_len
);

#ifdef __cplusplus
}
#endif
