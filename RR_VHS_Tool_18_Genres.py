TOOL_VERSION = "v18.2.3"  # bump this on every release

# Error codes for build diagnostics — shown to users for bug reports
ERROR_CODES = {
    "E001": "texconv.exe not found or failed to convert image",
    "E002": "Image file could not be read or is corrupted",
    "E003": "T_Bkg texture clone failed (uasset patching error)",
    "E004": "DataTable build failed (name table or row construction error)",
    "E005": "DataTable serial_size mismatch (row size or header error)",
    "E006": "New Release DataTable build failed",
    "E007": "Standee blueprint clone failed",
    "E008": "Material Instance creation failed",
    "E009": "repak pack command failed",
    "E010": "Could not copy pak to ~mods folder (file locked)",
    "E011": "AssetRegistry.bin extraction failed",
    "E012": "Base game pak file not accessible",
    "E013": "T_Sub transparent texture injection failed",
    "E014": "New Release texture clone failed (cross-genre)",
    "E015": "Game update detected — row size or structure changed",
}

# Regular VHS layout is now fixed. The game-side layout assets have all been
# replaced with the same frame, so the tool no longer exposes per-movie layout
# style choices. Coordinates below describe the black visible art window in
# the left 1024×2048 half of the 2048×2048 layout texture.
# Detected from NEW_T_Layout_01_bc_full.png: x=22..1001, y=21..1686.
FIXED_REGULAR_LAYOUT = 1
FIXED_LAYOUT_WINDOW = {"top": 21, "bottom": 1686, "left": 22, "right": 1001}
# Use a fresh cache directory so old five-layout PNGs cannot poison preview geometry.
LAYOUT_CACHE_DIRNAME = "layout_cache_fixed"

# Kept for compatibility with older code paths. All entries intentionally
# point at the same geometry because there is only one regular movie layout now.
LAYOUT_OFFSETS = {
    n: {"ox": 0.0, "oy": 0.0, "scale": 1.0}
    for n in range(1, 6)
}

def _fixed_regular_layout(_n=None):
    return FIXED_REGULAR_LAYOUT

import os
import sys
import json
import colorsys
import struct
import struct as _struct
import subprocess
import shutil
import tempfile
import threading
import base64
import io
import zlib
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk, ImageDraw

# ============================================================
# TOOL FOLDER
# ============================================================

if getattr(sys, 'frozen', False):
    # Running as PyInstaller bundle — use exe location
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    # Running as Python script
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE  = os.path.join(SCRIPT_DIR, "config.json")
REPLACE_FILE      = os.path.join(SCRIPT_DIR, "replacements.json")
TITLES_FILE       = os.path.join(SCRIPT_DIR, "title_changes.json")
CUSTOM_SLOTS_FILE = os.path.join(SCRIPT_DIR, "custom_slots.json")
BASE_EDITS_FILE   = os.path.join(SCRIPT_DIR, "base_slot_edits.json")
EDITED_SLOTS_FILE = os.path.join(SCRIPT_DIR, "edited_slots.json")
SHIPPED_SLOTS_FILE = os.path.join(SCRIPT_DIR, "shipped_slots.json")
SORT_PREFS_FILE   = os.path.join(SCRIPT_DIR, "sort_preferences.json")
OUTPUT_DIR   = os.path.join(SCRIPT_DIR, "Output")

# Movie titles are stored as UE name-table entries in the clean DataTable path.
# The game can handle titles longer than the old UI-only 50 character limit;
# keep a generous byte cap so malformed/novel-length names do not break the
# name-table parsers that reject entries above a few hundred bytes.
MOVIE_TITLE_MAX_BYTES = 255

def _movie_title_error(title):
    """Return a user-facing error string if title is too long, else None."""
    used = len((title or "").encode("utf-8"))
    if used > MOVIE_TITLE_MAX_BYTES:
        return (
            f"Title is too long. Max {MOVIE_TITLE_MAX_BYTES} UTF-8 bytes "
            f"(about {MOVIE_TITLE_MAX_BYTES} ASCII characters); got {used}."
        )
    return None


# EXPERIMENTAL: omit all base-game slots from built pak (custom slots only)
# Set True by default in -exp builds so the empty library is visible on startup.
CUSTOM_ONLY_MODE = True

# Parallel texture conversion. Keep this conservative: texconv is CPU-heavy.
TEXTURE_BUILD_WORKERS = max(1, min(8, (os.cpu_count() or 1)))

# Shelf performance guardrail: large custom libraries can contain thousands of
# movies. Creating thousands of Tk widgets in one rebuild can lag or crash the
# app, so the shelf renders in chunks and adds a non-blocking Load More row.
SHELF_RENDER_CHUNK = 500

# ============================================================
# TEXTURE DIMENSIONS & SAFE ZONE (calibrated)
# ============================================================

# T_Bkg textures: 1024x2048 DXT1, external ubulk, in Background/ folder
TEX_WIDTH     = 1024
TEX_HEIGHT    = 2048
# Fixed regular-layout visible art window, in 1024×2048 background coords.
SAFE_Y1       = FIXED_LAYOUT_WINDOW["top"]
SAFE_W        = FIXED_LAYOUT_WINDOW["right"] - FIXED_LAYOUT_WINDOW["left"]
SAFE_H        = FIXED_LAYOUT_WINDOW["bottom"] - FIXED_LAYOUT_WINDOW["top"]
SHIFT_X       = FIXED_LAYOUT_WINDOW["left"]
HIDDEN_TOP    = FIXED_LAYOUT_WINDOW["top"]
HIDDEN_BOTTOM = TEX_HEIGHT - FIXED_LAYOUT_WINDOW["bottom"]
HIDDEN_LEFT   = FIXED_LAYOUT_WINDOW["left"]
# ── Fixed regular-layout visible window ───────────────────────
# The regular VHS layout no longer varies by style. The only visible art
# window is x=22..1001, y=21..1686 in the 1024×2048 background.
LAYOUT_FIT = {
    n: {"fit_top": FIXED_LAYOUT_WINDOW["top"],
        "fit_bottom_hidden": TEX_HEIGHT - FIXED_LAYOUT_WINDOW["bottom"]}
    for n in range(1, 6)
}

HIDDEN_RIGHT  = TEX_WIDTH - FIXED_LAYOUT_WINDOW["right"]

# Kept for compatibility. All layout IDs map to the same fixed window.
LAYOUT_WINDOWS = {n: dict(FIXED_LAYOUT_WINDOW) for n in range(1, 6)}

USE_LAYOUT_CACHE_GEOMETRY = True
LAYOUT_WINDOW_CACHE = {}

def detect_black_window_from_layout_png(path):
    """
    Detect the large black cover-art window from a layout_cache PNG.

    Supports:
      - 2048×2048 *_bc_full layout textures
      - 1024×2048 cropped bc textures

    Returns (top, bottom, left, right) in 1024×2048 bg texture coordinates,
    or None if no reliable window is found.
    """
    if not os.path.exists(path):
        return None

    img = Image.open(path).convert("RGB")
    w, h = img.size

    # For full 2048×2048 layout textures, the regular movie bg is the left half.
    scan_w = min(w, TEX_WIDTH)
    scan_h = min(h, TEX_HEIGHT)

    pix = img.load()

    # A row is considered part of the cover window if it has a long black run.
    BLACK_T = 12
    MIN_RUN = 180

    row_runs = []

    for y in range(scan_h):
        best_start = None
        best_end = None
        cur_start = None

        for x in range(scan_w):
            r, g, b = pix[x, y]
            is_black = (r < BLACK_T and g < BLACK_T and b < BLACK_T)

            if is_black:
                if cur_start is None:
                    cur_start = x
            else:
                if cur_start is not None:
                    run_len = x - cur_start
                    if run_len >= MIN_RUN:
                        if best_start is None or run_len > (best_end - best_start):
                            best_start = cur_start
                            best_end = x
                    cur_start = None

        if cur_start is not None:
            run_len = scan_w - cur_start
            if run_len >= MIN_RUN:
                if best_start is None or run_len > (best_end - best_start):
                    best_start = cur_start
                    best_end = scan_w

        if best_start is not None:
            row_runs.append((y, best_start, best_end))

    if not row_runs:
        return None

    # Group consecutive rows into candidate windows.
    groups = []
    cur = [row_runs[0]]

    for rr in row_runs[1:]:
        if rr[0] == cur[-1][0] + 1:
            cur.append(rr)
        else:
            groups.append(cur)
            cur = [rr]
    groups.append(cur)

    # Pick the tallest/widest solid black region.
    best_group = None
    best_score = 0

    for group in groups:
        top = group[0][0]
        bottom = group[-1][0] + 1
        height = bottom - top
        left = min(r[1] for r in group)
        right = max(r[2] for r in group)
        width = right - left
        score = width * height

        if width >= MIN_RUN and height >= 200 and score > best_score:
            best_score = score
            best_group = group

    if not best_group:
        return None

    top = best_group[0][0]
    bottom = best_group[-1][0] + 1
    left = min(r[1] for r in best_group)
    right = max(r[2] for r in best_group)

    # If image dimensions differ from bg texture dimensions, scale into bg coords.
    sx = TEX_WIDTH / float(scan_w)
    sy = TEX_HEIGHT / float(scan_h)

    return (
        int(round(top * sy)),
        int(round(bottom * sy)),
        int(round(left * sx)),
        int(round(right * sx)),
    )

# Per-layout overlay nudge values (preview-only fine-tuning, in bg texture pixels).
# These were tuned by comparing tool preview vs in-game screenshots.
LAYOUT_OVL_NUDGE_Y = {n: 0 for n in range(1, 6)}
LAYOUT_OVL_NUDGE_X = {n: 0 for n in range(1, 6)}

def get_layout_cache_visible_rect(layout_n):
    """Read the fixed visible black window from the fixed-layout cache, falling back to constants."""
    layout_n = FIXED_REGULAR_LAYOUT

    if layout_n in LAYOUT_WINDOW_CACHE:
        return LAYOUT_WINDOW_CACHE[layout_n]

    layout_dir = os.path.join(SCRIPT_DIR, LAYOUT_CACHE_DIRNAME)
    candidates = [
        os.path.join(layout_dir, f"T_Layout_{layout_n:02d}_bc_full.png"),
        os.path.join(layout_dir, f"T_Layout_{layout_n:02d}_bc.png"),
    ]

    for path in candidates:
        rect = detect_black_window_from_layout_png(path)
        if rect is not None:
            LAYOUT_WINDOW_CACHE[layout_n] = rect
            print(f"[LayoutGeometry] Fixed layout visible rect from cache: {rect}")
            return rect

    rect = (
        FIXED_LAYOUT_WINDOW["top"],
        FIXED_LAYOUT_WINDOW["bottom"],
        FIXED_LAYOUT_WINDOW["left"],
        FIXED_LAYOUT_WINDOW["right"],
    )
    LAYOUT_WINDOW_CACHE[layout_n] = rect
    return rect

def get_layout_visible_rect(layout_n=None):
    """Return the fixed regular VHS visible art window in bg texture coords."""
    return (
        FIXED_LAYOUT_WINDOW["top"],
        FIXED_LAYOUT_WINDOW["bottom"],
        FIXED_LAYOUT_WINDOW["left"],
        FIXED_LAYOUT_WINDOW["right"],
    )

# Standee texture zones — Y pixel ranges where the front panel, title plate,
# and footer are mapped on each standee mesh shape.
# Derived from UV test pattern analysis (April 2026). Values are ESTIMATES.
# "front_end": Y coordinate where the front panel ends and title plate begins
# "title_end": Y coordinate where the title plate ends and footer begins

# Standee shape preview images (cropped game screenshots, 140px tall)
_STANDEE_PREVIEW_A_B64 = (
    "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"
)
_STANDEE_PREVIEW_B_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAADgAAACMCAIAAACmv/0qAAABCGlDQ1BJQ0MgUHJvZmlsZQAAeJxjYGA8wQAELAYMDLl5JUVB7k4K"
    "EZFRCuwPGBiBEAwSk4sLGHADoKpv1yBqL+viUYcLcKakFicD6Q9ArFIEtBxopAiQLZIOYWuA2EkQtg2IXV5SUAJkB4DYRSFBzkB2"
    "CpCtkY7ETkJiJxcUgdT3ANk2uTmlyQh3M/Ck5oUGA2kOIJZhKGYIYnBncAL5H6IkfxEDg8VXBgbmCQixpJkMDNtbGRgkbiHEVBYw"
    "MPC3MDBsO48QQ4RJQWJRIliIBYiZ0tIYGD4tZ2DgjWRgEL7AwMAVDQsIHG5TALvNnSEfCNMZchhSgSKeDHkMyQx6QJYRgwGDIYMZ"
    "AKbWPz9HbOBQAAAja0lEQVR42q1825Ikx3GlXyIiM6uqe6ZBEljARDMtzSCtcSn9/x/oB/QgicaHJUBCBIm5dVVlZkS4+z54RlR2"
    "TzdFrW3DYNNWXZUVF78cP34iEAEAwOBv+UFEQgQzBQAEBASzVz66e90AX3/k04/bX/lugBgiEoZAbx8e1lWIWKSKCICueVVVqWqA"
    "YCBS/tZJ+dN3///1n1cn3H6CAYzTMA5pHNI///M/PV4Lh5BiNDNRAYSSixmaAZiJCKD/K5frfL1czKCUrKLzPKupqZmZqqxrVlNT"
    "AbP/ztyw/2NPPxUAYM1rIFilXh7Pl1XHcSJiIlZTYqbgO04IcJhGIgJQAFhy3R4IkGI0UUBQVRFRsJwLIJiaqq7zNecsqrWUZZnN"
    "jAjXNddaSy65ZFWrtUgVVTFfEkBEdBu7DZSIEICZAhGhMQciJqKA0RAM1NQQjYlqFUTxwYmoqiIiAAQOTKRmZqAKwHw4JGaOMSKi"
    "GpSSiZg5ENphHA2AEAFgrSIiZkZEhGC1XK6Xf/+3f/v++++X5fJ8Rc3QkACJAnMw1aoaEBEBGclQRYUJEIyZN3tCJKr9KWoqZkzB"
    "kAyNKYgZmWktAIBIJRcg4QBggu1HVVWBiJg4pcQhEnOcjm8efvzTn/64LICI3XaDO3MVCYdxGIfBOKWxlFpKQSRflRCCf6aUAgDM"
    "7N/EzKraAoL/Q8yACKZGRCEGUzOAEIICgCkhhrB9qVuz/64qWgBFai2i0h77fEWt5Ax2JCLdvMG3FQAgxhhj9LctyyIiRETM42Fy"
    "GzCDnFciNhAiTimBiRKa2poLEhFRSAkMzISJwRCJEBHRmDd/R/ApEjMD2OeBYhsomNUqRGQmpeRaBQBjJABQH7s/DhE9kKqaKREB"
    "mJkCGHqYJYoxqlgAzFVUDQEU8DANjARWATgX8fEhoi8BEXn0IA6qGkL8PKARAMQYmMjXhigQBaJtiO4u1n622EFEiISEgAhkhogE"
    "iN0CCJGYQmAFAzQEwPYXa+F/exoCEnEIYlpVaxURDSF8HngDAARmZkSCZV5qVSKIMfmM/Yki0u2atl1DAARAM2sj3r6eiMwIkaoU"
    "MxUxIjRVQ/YxqlQwQkQA4xhVVE0V1AyA/An48tYjwjCMKYZSiymXUoZhCCH4mEopOWdmNrNpmoiIiNRsWRZo2z2MIyKYghmu64po"
    "DEZEEQMiBQrQ5umTFxF/PiEBgW+CgZkaEeJLmSxsicBMVFXVgDxwEFFbHvNXeljxT9a6hacQAoABEDPVqrVWJKgqMcQhJgQi4mVZ"
    "fKoAMI6jP1lVc84tFWGIDEBaan/yC86kZj4PVTELROQxqCfi/jvubNGXczOGzdXADIgJwBCBAAOTbmnGQ4T5KH3OVQQREDENiSio"
    "WtFaReyzrBt8bZQRUxxSWkQQwSMUYrc96NvhXwNmtI1LCZiADH0+Hv4NDIyAOOhunj2TudGrKqgCIhCAQtWKCCLV7f6FgXII/slp"
    "Gq81pzSUUtalApGajSkNw+BL4jvliz2k1Be1lgq0zYQICVFETLVWMYDQ8oKqMnM3Kn/RzAIFUwMEBWu54Dn8DADARB4aCVFFzUzV"
    "DIwAVBXQwxGaWhXZMBTA6XTyEavq5XqlEEyVmYdhBLQYYq1yvV4RMYTgruNfWUrxUfqftqlqRSQgVBVEpG3a2McafKJkioiiejMO"
    "AyLiZo4+9s8RpKhuGNns5mwIhGRWt/eIDMPgOEFV13X1mTNzz8a1VMUKRlvA/8zxqbtID8A9zneL3AI8bpDUt8x3sCN57WbtmH7b"
    "hy3u9l3e+6UZ+NKaGRKqms+WCNt62fM4Oo0TM12vc6liAA5EADAwq8gi4h+LMcS4RTQ3a98eDqym0IKXjx+3lG6IfCtliAEEAFWN"
    "CPsu6QalwDwbg76w9X1FVdWhLhG23Qnrukob6Ol06vHocrn6YsQQj/cnBSWkkmWeZzfHlNI0jQCIyA673BFTCubB3aRW6GE7BQa0"
    "kq2KfF6YeBwFUaUWWgKiqjlKctC4haSngcZ/NzOfHCCIiDaE1r6eVTcHdx8KIbSAH3LO/iIRxRApsKFlWl4N+IBb0SjiUFB96j4t"
    "R+BbHQgdbD3BU6pqCB783Dr9Cc3caZfVzMxtRohAdfMEJDQzBSWmF8ussBUVZhxCGoZBa4xRVX1aHvk8OnoWEPE0s2E9d14iNveg"
    "tsb+RY7fHLv4x90vzYyZEbXDHWYmZjUpuewT9bOB1lzoMEFKQyg2pDQv6/U6MzMRHA7HlKKqAeD1erUWhg6HqTvTfL0aIgIS0ziO"
    "gObuMs8rIjLfciYzi7ijECL31zc4ixSY+bO0dHMmESmlbIF+FytVvbDUvW/S7gcRa625ZCQ2swhxGAYDJeKSy7oWH9w4jilt0PFy"
    "uRBRrTXGeDqd3Bnymuu6AlkVSUN6NY5uibgBDA+BzSKt44n9jrg/iUit9dmfzMwzXDfWHi/b7t+qtq1gBENEFUV/5TO3f+LL1sKT"
    "mxcAet3ckjjdxtECuNf8rVqxnrE8HfhrPWs8+7jvT/dxNQME3ZUSnwFnwCHFYUi55GVZc5FxHMdx9OnmnH13AGAYBqLBv2xdcwf8"
    "0zh2rFfWFZiMwMxiIK//RKqZAwPrYcFNbj/0LQLpC2xVcJ7Dv5IIDQCRWnjaQqBHKDMbx9E5BTN7fDz7PsaY7u6OAIZIpdRlniEE"
    "1XUchmkcPWnmnP2xROxZwB/oZYIX5YRkoKC6rIvaS+EJmXxYUmXL982GdkEUfTK93APodulFqW2baYamvdjqMcsjxH5b26dgMzYm"
    "RMwApbwQ8GnzbY9zZqbbcLtziEgHIu4W7a+4/6VnI4Neqm9BCBER3Zl0XxrsU4aI1CqqCggN470U8EWVkGIM40gcYslZZCMgelgB"
    "gOv12scaQmioAJtzGBExogISohd0ohqYfQ9UFVHNQFXc2buBeuG10Ya7ZXq69USmiogxRMI1xXCtdVmWEIKZHo/HYRh8rOfz2dEk"
    "EXWAoqqXy8XT1TAMx9NJ0Zi5lHqZ5xhjqXWaprbdUEpxq3BA3YtHx09mGlN4OYWqVHGAYyYq1grOHmV6ouv7tYtQjoO2eFtr9boF"
    "AKRWFRVyAsbaDsDlMvdoNY6jZ+l5ns3Dk6r760sDVQ1hYGZx60Zs8zE3/25Y9lmQazyVeAbfkyu6wxZ2+4GNAd1F0/5BYg/e9HJ4"
    "crs301qriOScHY/5snp46qvbcZCvdIuIW23p0UPVqIHePeozM0Qi4j5h1VtIuQ33tlLP8CiYkx95XWsVkfl0Ok3Hk9vAPM/n89kR"
    "2ul0ZA6+zPM89xx4Oh1Vt69ZlgUZATFyTIdkYEi0rqsbMRGlFPrOLssVwKs/9oogS6lVXnEmJABQ8fqNkLlvxp4ho40Is/5aXy0i"
    "YkYAq1WXZUFFRAQK7FQa4/nx4pxUCHQ8HnxPcs7rmn2xh2FIKQBaLbSuxVf6eRxFACTaQqZWMC/UxCn0PZh3C+tPwV5Jb4aL9vSn"
    "rbI1RgP2W9zxQ5u2ETocK/ZiZuo0iFOfzneCKTbE3swRntnovrDEW2VnPSkgopoR+DyhVvG61IdYa91Xp8yopp+v5VPGuZR0OIzj"
    "lKEM41RKqdeCSGYYQpimyce2ruu6rh7zHbX44NwEfWz+Zp9GztnA2EJK0QxUkRlKKT0zhcC9aBYRRHOPfJXNW0tNpSB5n8bArNRK"
    "SKqaUvS6ERE6kgoh3N0dWsEkl8sVkcw0pXg4TABExOu6+phM4XA8OLpSs3lZfHoxxukwOovj3Rw3lhTTywNVtWp1Wx7HvGoIW+Xg"
    "Eao1EvhWfKo2C/MqvtuftCLObpAA3KgMkPYkmaoibG/2ACI1c+BXA76qlFo9xFNz730PtOOd/loPq05WttiOIaQWHd2mrY+soXPt"
    "ZkqEKuahjZndfem1mgkRY4puUrXWnLO3ATpF2F2qtweY2SGV735K0ZlRAKxVVT0RILNzYN6mQULymN09T0RMvQeCtVafm6i8zOEz"
    "8+l0iIy1yrqutdbj8S6mARCYaFmWXIqj2tPpjpm9rFqW1b01hHA4HMzUgfP5fGYmVRvH8XBwLKLLsoooAhHz4Th55ynnfLnMCMAc"
    "hjSoGSKo1HVdXwbO/n0iykxO5njgAyABVCSFzbYMwRAEDFT2eV9VAZxoIABTcTswkYIIZizStntrkmywWhUJCQARlAkNDdDj4ys2"
    "WmulQCLScJCCmYGCoaqoCoCj8RvIqLW2Lo+pGjM50BQxIt1cB1BEb3WeGbV2gIl3QcW2pn8gRPXi4rO23c1GRSSM6UZRGFQRJAJv"
    "JREAmG8okXmt4qbWQJY5O+c+tK0dbv2dfSpWryDQkJiIDIp3Hrc0BuCN9FcD/jzPd4fpeDyuFWOKa655XohJ0Q7TYTpMCMjM1+t1"
    "WRZiIqD70xvmLdZcLmdEQPRC7w4RQuDrdcn56kM/Ho9bLBOdl8XAgCiEcDxNCIDG67qaGQaqtaSGaF+o670sAgSRoqoIhgimimZg"
    "So176lhEzY1y14yArY+KZE4GA4LqJuvoTAQyqpd+ImDGgATIRE4BqSiA8+IvgRJCHMZRzeSGG8S7E6CACiImos38O7QSkerm28K+"
    "G7eCaTcKD/4e4/bs0KZWMDQx2AQEqI0BfrkUIXfVTVbRWrJmKgpo1kL3jQtRQ3zSTHeE7+PYWqNApqZWVTAgd9TSi5kbiQStBAY1"
    "Q3tFXrLZ6DQORHS9zqWUKhZCGEIEQ2KqpVZVAEOicRzJ2zOADpzNLMZ4OBzad9s8r8xEVInIazpEXNfV3+D53dRCCLXW8/niCSnG"
    "GAAM8VqyyzmeaXZaZooRtNXvJjcOP4R1Xcu6AhERTtPUShQ8z5/63MdpBANCLlXmeTZjABmGwUETEZ3P515lHQ4HAIwxmFnJYlqR"
    "kAgBUYGI6Zaod2tLAODhz/UtfRNQAUwB1cwQiIHR0ERRDVRBdF/rmKptgUV3zR0zE7MKYBszZeixE9GrKQEkH5UZgiNXMyZGoGfV"
    "fdhqTe/tqZlueLkx4HqrhlXQm1JEzmT3rAZAGwQyEKk7To97ueKG6GIA74eoqtSKiKpeizteo72u6DmRm3NJgZkZmlKkSjUEK+Z5"
    "lTkgBrcND/XDMHT/dQ7Mx+S9L48MpVQRIRKn2yEaAM7z6m8wg3Ea3BRzXhGJmcwspfSy13v5NwzD4TCd55VDkCq1ChBasePxmFIK"
    "zIjhfD63bgk+PDz4LyL18fHKHFQlxnB3d3IId7nM1+vVDIjw7u4UAiGCKlwus3NmKaVhCE66Xy5XAFGlWqX3OV4I+KpGTGoGgC34"
    "AQLeQDFqr432ZMReJuOliKqoiYdDZ6N6wxOw4T0iXx93CGeyHJY1c3pRpaP26fESmEW152hVRQCkAAZmIKKEW1HqsbAbn78ZoKoa"
    "YkIMiLrlr9ZIMYPmHxsF5JtLxCZqBlI9x9da67JcS1mfCfbCFgUM9i3QjRdBANBGaqI87TR04AzgvRjyVRbZ0D4ixhhce1BrbfU3"
    "7kzQ1rWoKgBycC0Cq9ZS68udOyJ++3BA0/k6lyK11pSGISVDA8ZScs6ZmBDweDx2sZa3clzkczicEBkRSimPj4/MAcCmaUxpcsu5"
    "XK7dEd+8eeMmtK7r9Tp7Dh+nkYgQLWeqpb7q9QDoTIknQwDwYjQEqsWht8/4hVZVe6h1AqKRES78sJ67fXUBep9pE2LZVj8qMSAC"
    "Ir/SvkFjdpICekHoD3c6A1oNue8stuIO9rqT9ghtTVFsrWjtwBSRfCiqsNnD1vRDhABAu1LxsxVFBI9hjurd3pEIjRpTjE+H0ps1"
    "vUzd3uAxq4N/J+08NhMBIpZSW5PSYgy9ylN1gIch8KtxtJQSCFNKaxGiKCLzfAUkQ4gxjuPoUoV1XbvcaQMoSGZ6uVycTGTmjpHn"
    "eXYsQkTH47EP6Hq9AqCZppSmafJVuF6v3o0WqSHGV9GTiEzDNE3jvBamuK6LSEVmAxiHIcbo69R7oe74zu2IuEzEAMxf7DvQxQeN"
    "qDFfY+86dO63B+ZGltirvdAQQkwJdoHb4YyzXL2076RD72i5RbaWg3XCtjN1fVbNmWzXAII9soFbnnkFj6pZKSK5WtX+bld2Wk84"
    "bXD7nkv7Pke7LvyN+17H/iOdwfQM1PLaJrhwRAEIJYtIeZkaV7W85pnJVRmmSIQxRkAC2vStXhw7TeKD7mo1ADgeDr5gZuAdEkRM"
    "KTmhgojLsnS24ng8+nqXUuZ5ZiZVl/EQAqiKq6VeZkre3B1NyprzuiwAfDweY0gCFlNalqUrQt6+fdtFr5fLpSN8DiMhI2LOJefi"
    "LEZKyWm2fQewE6K9/Kq1AlgpWZUJqZbqsfXlNrinZnSkxdRlbvuGcd/QfdOxB1cvBmsVaFLoPrj+/s5W7yhs22uMDOylhLKTExEx"
    "kJhusddJ8L2KpLe1n0GnHbmHXdFRa92LDrcewfZm2ytTzZTZ4/9tFbwWeLENboAYQmwKKVAFb76CSpd82c793dq66GRd1i7ESikw"
    "x13IBBc+E7muHv1F17tN09DaI8uGy1RTiPgKSQY558R8OByqLsxcqyyLIJFlOBwOqekFPY66L9/f31NrUSzL4kseY5ymyf1tnufG"
    "TyFRRERmFIHeVPcWuu74tqe79BJJVkqJ9ET+ZmrwdF69x7yHpD1I7TXbe+T7TCLdorq5GCUEaia+GfRWeb0ISogwpZSG9KT0R7Bd"
    "Z7Br458Ls/EJ1eNO+ex1Xzxm6jh117LZVAvuf00+afhaswERpVa9tWvNTBEIkRwJOafJzGqKCNwUBr7MHBjBTwLgPC+NU4aUokf4"
    "ZZkBsNaCSNM0+reUUuZ58fpzHAevFq/Xa86rvZZCzWyel3XNDTgnIjIADqFWKfmKSIB2OB44EKIR8XxdPDgw8fF0MPXWkV6vVyJg"
    "5mEYvPhExGVZzUDVQgDHUESU82Yv26kO8uj0etPW/+gdW/frlFIMQQGIOZciokRgaKrKRoAojYBwmCoiAGhizVFgr8Npsam3etVM"
    "PGl5IOtP8Dj1Yk+MWphEDqGHyU0a4j1ZYi8AtwMXYF022q3WTwHswMet8Oot6s4B7nHCpv7d7NsfaK9KNTa9SxOW3jQpZnwTjjRb"
    "9vb2jt8LIagpge+JMrvOjvr+OCuB6CRodUTrjj9Nk7d4Sik5ryIGYPE1PAqGtVZijCkKVDBy7gQQc62Bw+EwAQK0wwP+SW8l+lde"
    "L7PXpa4ZdTub59k7QSGE02looie6Xi9dReVlBYBVqb5Z+JKg5BZHr9fr2/vTNE25XmIYSimlFCRW03iIHNjMkCjnvC+p94Fmv5v7"
    "MOfb6pF/D6XbvktTAT7vQL+89dooXGi9azBsp4Dad+uT+m6vg34xo3TFYbel/Zs99/b+bIfSrXv7itcfDmNgBkCPiC7NJUWg3VGG"
    "tmD7LvczieE+SxHRMAw+1lqri3M6u+YxuJTia9wpt5zzp48fXyYgHODkUupGURgiEvuJE3SfQECg2/Ejj8w+PmY+TJPjfKn1fD53"
    "BU4IQUQI8eoiHDMO4XQ6+Ux8lM5QpBQ3ekHVicsXhQXGzFrLuuZcqimO4xhCMgNm8m42IRnB3d1dJ+GXWv27N+m7gSHWp8XaTeGz"
    "w3z7NzgN4dSaL1G32ldsVDXn0ltmTQC3ez5hD0kd/uxQszpp3CU9vrO9Zuo1Auxa6Bv1p4IIMQYnE3rb5NUU2pqFisDNKOEpIIIX"
    "8ZjnC1Xbl4/dM5wVdMbKzcaL/WavsVYk2lrAXWT32kCN2NPBUzcHVFNEZGJEMEIXpfogumLMNfkIoGDM7B0SF55uwBnxdDo50K61"
    "njd9nMUYxym1E0DS+vubauhl9LTMSyAaxtFyIQq1SqkzIhrANE4hBjBAwsvl0lHZ3d1dJxccODc5JAIYIahIrZUpYMB9aNxrZ6pU"
    "IpTq3BgRyWd9mycIX5e1vjkemVktMxGgiGgvnjbz1ydxvsfRZzjV1ADcZtWtR033siFoRRWCw0Uz8N7LJmB6tWnrnCPgMyCsAPRM"
    "Pm2vCJB90DtP8sjQWhwGLqPrYMpPDanqsmSpFYBSikRICDnnPWPwOaXDKjeR7X6R9r/0Or2fuWtcbvT87gWdH4SNkYdhdCzSSQBv"
    "53mMK6WsSzYDRImJiR2p/tVyeZoOYLquaykVUUMI3s8kQhFpyjrz043+u4Mgl4EeDpMTTyKS88oUASylQNQ4n13W7RlfRMy8NNVa"
    "C4AB8mv13VaFMrPkWnKRYogWOTCRAQYOa11N1AAMbxKYJy3xrdm9JVFTVDB33K5DepEVMzMpFVARAQH9ENNfq0K34lB6+ebVOoHB"
    "k+4HwjOxm95gynbabrstAa3REK7sqs80Hj1pxRQAIASS6qdEWLW248svDZSZoXWVuqJ+37LeH73s4dr5UWZu9ioANgzJY42I5nz1"
    "k34OUn1ufhDPA/s4uoCa5jlXUUL1b38NlEAphRq9vinlajUAM2Rmd1JDcEHF1lHeGGestc7zQkgGklJMKTqTWIqUIoQEuCma91xQ"
    "g6fVVeviXD5BLwpeBs7zvExDSkPSLDHGWqXk7JKqNKQN+yFmzeKW0CiJTRigZmTOru0rIeeBbTuYSPuT0L2bI6JbtWggJi8WTLfu"
    "skhViIqgJmps7c4OARNTdErWwNqm6zPdMJqaaAUiNmMibtG07hPEvqPSbhaQdjwiIAIR5pw9nrwa8JujwL4/JFUsBNiO65tqdVS1"
    "Xx7PqH1PS8ndlP3Umus2u2UfDgcPqA4g/eNuSGby18OTmcF2/gKoubl5Cul+qlqZqR9FdXvtxaR7Wq3S18P74W5zPTv0yuQZNyhu"
    "pGD7Yz4vaUoOo5os8yxCIhojM7MaMAURKSX7eh0Oh374el2105meu30QtYrLV5176+cB9qTBXtvpiEk1mN9u8NqFKj6DaTqUdalV"
    "VQQJXTlsZmJbavEu2e0wzy7mu3rFKwpVaVSziRACiijSLYz76GHXAUEkJKpFvAx5dobrWXjinHNes6+/CSojEZsakLqGrpEtt669"
    "D8ezIjOaqUjtx3A87/tdJkS4P3Q0z7M/IcZIfvIQcc2ry0Jeabe2OKqiddNQbXDdTFw06ANyl9wPrh/2805zTxzuk0RYSq7VpeEQ"
    "Y3TJrcu6uorPxQ4CIOLn/827JS/X9VUkr2sI5Mc5XCPiBJsBhrAduFE11zj7jqcUnboXUdcy+0lvZnaKptZNNSNSY4yuQZRN5Gi9"
    "dO6u6f1BPy4zDEM/kLBXQDgldrNu1wEghqpaK6YUkACMS6nOebjSrTtEKdUMS8kxRmYGEETKOfuQHEbFOIQQAIKTPDHGlNIwTB0i"
    "ervHJVe/+tWv/uVf/uXjx48dWtzOLjtv43HkfLnmXFKM8zKnlI7Hg/d9Hx/PtdZhSIgoEhsDKmYwTQcROZ1OMUbm7WxhCCHGxMzM"
    "ocs4exN1DxvagTRny/I0DQ8Pb58P1C/98ajGzCLVjxQb2DRN4zjc39/7Uj08fNH1ZP59DktCiCHEnS5mywLN+Xh/oMGZM6+we0eg"
    "1tq0CNXMYvzizZu3AP/nWS9UXQ/y7t37YUj39/ff/sP/ImLHje4uXkM+5ZVc7S2qlnNxB2+F1O2uBzMT2bj6Xop4U9iVQ/ue6ubZ"
    "asuyHo+H/Rk9px0hxEAE8zz78Zkvfvbzt28fcl4b8sDuAa01CCK6V2n2tgkiipReGCFSCJvQ/M2bNzHGUqqZvn///uuvv/7Zz372"
    "5z//+d27d+3YIbmmrZR8OBxijJ6Q90yJEAdETGnLmSHwn//8kYhEXCeEiCCiOa8pDb4Yvsz9fOrxePS1/+qrXzw+Pn7//fcPD198"
    "++23j4/nx8dHM7u7uzsej9999/uHh5//+OOf/PCmU5kd33girVXu78fT6fT+/bvtFDYAeMJ0jx7HpKohxtPp+PHjo59ddtbbDA6H"
    "49///f88ny+//vWvl2X5zW9+880335jZ119/w8z39/d3d3fjOL59+/D+/YdlWc/nyzQdHh8/XS6XGOPvf//7aRr/8z9/eHh4+PHH"
    "/5ym8eHh4aef3jlk2Z+HQKS7u/tPnz5++PBhc8F+bs51wO4E5/M5phRC8BexHYE0s3/8x3/89ttvv/zyy6+++ur9+/e//e1vT6dT"
    "zutXX3317t07M3Mlv5+sMdPz+dGD4vF49PN0tRYi+vWv/zcirmv+xS9+kVJ61k9zGHR/f/+8uCtFdgc16Xy++A1SOWfYrq8gv6fk"
    "L3/5yxdffHG9Xv2uDIdI33//3a9+9ff/+q/nh4c3iFZr/vLLny/L9cOHDx8+vAN4uyzXv/zlR5Hy3Xe/B4Df/e53zHy5XH788c/+"
    "/A6m3IqcDtof5HGEa6oWAqvaMCRm+vR4/uabr72f2Q+C+kweHh5++9vf+rJdr/Mvf/l333333cePHy6Xy/V6nef5crl8+PDh06dP"
    "ntNrrZ8+fQKA8/kRAOd5JmJHx8xshv0swV7CoaoppdPp7ocf/uhpL3Spx8YsAqparfLhw4e7u/unp4sMEf7jP/69lPLp00d39j/9"
    "6Y+qxkR/+MMfmNkfKrWqbfLBVm/IMIzH49F1Pj75Nqwt07Zzck7JWyn5/v7ueDxuTFvjeUHVaGtIhsD08eMn747uDlmry4xEqvsm"
    "szeWTE3f3N+nGA/ThABv3rz58ssv7+/vHVP/8pe/vL+/f/Pmzd3d3a0j31S7WzsTvfqF7T4uhGWZVeXNm/snDTEXinn+9Mx7Pj8y"
    "cc97+/brk2vavGdn5nKnu7u7L774Aok+fvzotLwfKvOg2MQv/NnFAdDTgbYD/o6hTqfj5ze/WD9/VkXO54vZdlNFq86etES2UG9+"
    "NEPfv3/vfzoej+M4TtM0TdPxeIwxiui7d+88eR6ORz9duy+MOonXz7Y6j3S9Xg+Hg/eiwl6M0Z3mdDouy6pq0zQ9Pj7GGPoR1b2u"
    "7da+R8w5j+P48ePHn376KQ0DEf3www9+6Pmnn/4iIn/84x+3fRChdv/ejgNERAghhsD9ZsC7u7ucc0pxnteAXme0/jyiMWMCzqtd"
    "zo/DEN+/z0TPmRInvLdCrJ3EP5/PPvRPj49PiXNwHtgvIwwcUkob0hvGGGNKcRqnmJKDPY8/SLDmfD5f3j78bJ5/CLA7P9Opcd/u"
    "8/nxf3z9zVNRVrvEp50o2hfsfVu8iz5sP2NMgwPClFKKKabEzoUa1Jpdhu0ecr3OIh+9rqq1lJI5+K19beuncUCEKtKLyRTj4+Pj"
    "3/0y9JvDWsbHEAIRJ2IOPKQhpjgM4zgMw7gNLcaGPs0ASYxqKc6gf3p8dJyRc8l51ZqXZalSVc0xzibMc15QlTlcL+cu0KK7u9Oy"
    "LGprr61CDJfLlZl+/vOfhxDGcUhpHIYpxsDMockK2+Vp4MyoiH789ChSxBck51JKrZJz3pCXqSPOLU7t2EAi1x9vfSm/k46J/E6/"
    "4HBvvl5zqbQrgl1KUav95jf/VIpTWVqruPzt0+OnWkuptbT7Ol0lsOFOkX5pI7ZLIjp32caBhsC4SSC9rEPYRLp+4tY5JDfI4HJZ"
    "xQKAhqgboSAO3d+///Dp0+O6ri5aVxV1UldFe3HcGhl9NPt7BLrecd+37eoaAdkxm0/kPNvpsqbeCV2IOq8lF0FozR4AA/nTj39w"
    "nYsbTZfgbkfgG4+6Gxbsna+d3L+lomeaXtxdTivSpMZNvuDX8/gu+Nkc9GsNrF1T6b0zUWEE9CMp/V4Q73q2W3ifHaz+nPh91rTo"
    "uqp+wAlvty2CuEKliqlVqbUUMzifrwgYusC+39DnX1SlIBAYIZJU53mU2FUaN/3UM7Xern+yuy0Ybqvohx09uKlqrcX/c1plzf5L"
    "FdFduwkJKAAYE1IIjErenzdAs0BcS60lxxjRFAQQwQTwBnC2EzfNFJzlB6DeOAADqHVDEbXUUmutVWrNpXhyf61Fu536b5SxmTrj"
    "DGuuPnli5sBWQVUMwcCqbhcY0tbYbzd4At20XKp+HYqolFyqainFF0ZEatWumn91VJsq87MrnVvH8WZJ7pgpxWkcmP1SBDqdTtsD"
    "bLsL1wF5rUWq5pJVVaqUUqUFhM8vim7n/f4btza/tsD7+wk3CwshDENypCMipVQRbYyu/pc3aP8/DOW/HOgzelf/xsu8+022/9/H"
    "9OLP/wXj+FQO/fhNKwAAAABJRU5ErkJggg=="
)
_STANDEE_PREVIEW_C_B64 = (
    "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"
    "SUVORK5CYII="
)

STANDEE_ZONES = {
    "A": {"front_end": 1687, "title_end": 1910, "footer_end": 2006,
           "plate_left": 35, "plate_right": 35,
           "arch_center_y": 256, "arch_radius": 256},  # dome: semicircle at (512, 256) r=256
    "B": {"front_end": 1635, "title_end": 1867, "footer_end": 2003,
           "plate_left": 0, "plate_right": 0,     # no side crop on plate
           "fold_left": 100, "fold_right": 100,    # sides fold backward
           "footer_border_only": True},             # frame color wraps footer only
    "C": {"front_end": 1723, "title_end": 1867, "footer_end": 2020,
           "plate_left": 75, "plate_right": 75,    # not visible on standee
           "title_below_footer": True,
           "corner_radius": 100},                   # rounded corners at top
}

# T_Sub textures: 512x512 DXT1, pixel data inside uexp (no ubulk), in Subject/ folder
# The mod replaces ALL T_Sub_XX with transparent images (T_Sub_01..T_Sub_77).
# Custom slots need their OWN T_Sub textures (T_Sub_78, T_Sub_79...) that don't
# exist in any other pak, so there is zero conflict with existing DataTable rows.
TSUB_CUSTOM = "T_Sub_02"          # kept for legacy compat only
TSUB_CUSTOM_BASE = 78             # first T_Sub number reserved for our custom slots

# Subject texture path inside the pak
TSUB_PAK_PATH = "RetroRewind/Content/VideoStore/asset/prop/vhs/Subject"


def make_transparent_dxt1_512():
    """
    Create a fully transparent 512×512 DXT1 pixel payload (131072 bytes).
    Uses punch-through alpha mode (c0 <= c1, index 3 = transparent black).
    """
    # 8-byte DXT1 block: c0=0x0000 c1=0xFFFF, indices=0xFFFFFFFF (all transparent)
    # Matches the confirmed working format exactly.
    block = struct.pack('<HHI', 0x0000, 0xFFFF, 0xFFFFFFFF)
    return block * (128 * 128)   # 128×128 blocks for 512×512 image


# T_Bkg uexp template (1702 bytes) embedded for all T_Bkg injections.
# All T_Bkg textures share the same uexp structure (1024x2048 DXT1, 5 mip levels,
# lower mips zeroed out, top mip in ubulk). Only the ubulk pixel data varies.
_TBKG_UEXP_TEMPLATE = bytes.fromhex(
    "0000000004050004000000080000610afb56151fde4cbeab931433939daa0000000005000500010000000100000001000000000000006406000000000000000000000000000000000000000000000004000000080000010000000800000050465f4458543100000000000c000000000000000004000000080000010000000100000000020000000400000100000002000000000100000002000001000000030000008000000000010000010000000400000040000000800000000100000005000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000002000000040000000010000000600000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000100000002000000001000000070000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000008000000100000000100000008000000000000000000000000000000000000000400000008000000010000000900000000000000000000000400000004000000010000000a00000000000000000000000400000004000000010000000b0000004110411000000000040000000400000001000000000000000000000000000000c1832a9e"
)
assert len(_TBKG_UEXP_TEMPLATE) == 1702

# Inline mip data offsets in the T_Bkg uexp (1702 bytes).
# Mips 0-4 are external (in ubulk). Mips 5-11 are inline in the uexp.
# These offsets are universal for all 1024x2048 DXT1 T_Bkg textures.
# Between each mip: 16 bytes of structural data (SizeX, SizeY, SizeZ, mip_index).
_UEXP_INLINE_MIP_MAP = [
    # (mip_level, uexp_offset, pixel_data_size)
    (5,  0x00C2, 1024),   # 32x64
    (6,  0x04D2,  256),   # 16x32
    (7,  0x05E2,   64),   # 8x16
    (8,  0x0632,   16),   # 4x8
    (9,  0x0652,    8),   # 2x4
    (10, 0x066A,    8),   # 1x2
    (11, 0x0682,    8),   # 1x1
]

# the embedded T_Sub_01 template uasset (754 bytes) and uexp header (144 bytes) embedded
# directly. These are the confirmed-working source bytes for transparent T_Sub clones.
# The uasset name "T_Sub_01" is replaced per-clone; the uexp header is shared.
_TSUB_SRC_UASSET = bytes.fromhex(
    "c1832a9ef8ffffff0000000000000000000000000000000000000000f2020000310000002f47616d652f566964656f53746f72652f61737365742f70726f702f7668732f5375626a6563742f545f5375625f303100002200800a0000000901000000000000ee0100000000000000000000010000004e02000003000000ee010000ae02000000000000000000000000000000000000a83f321849230446b0df4968741d12e501000000010000000a0000000000000000000000000000000000000000000000000000000000000000000000000000004d4f442000000000b20200008203020000000000000000000000000002000000b602000002000000ffffffffffffffffbe020000050000004e6f6e6500f403c50d0800000050465f445854310059f5f9c6310000002f47616d652f566964656f53746f72652f61737365742f70726f702f7668732f5375626a6563742f545f5375625f3031007f97894d140000002f5363726970742f436f7265554f626a65637400f8492d3e0f0000002f5363726970742f456e67696e65008640854906000000436c61737300747778911300000044656661756c745f5f546578747572653244004a680193080000005061636b616765007347881509000000545f5375625f303100084146110a00000054657874757265324400fefd40d103000000000000000500000000000000feffffff090000000000000000000000030000000000000007000000000000000000000004000000000000000000000004000000000000000900000000000000feffffff060000000000000000000000ffffffff00000000fdffffff0000000008000000000000000b0000009000020000000000f202000000000000000000000000000000000000000000000000000001000000010000000000000000000000000000000000000002000000000000000000000000000000fffffffffdffffff0100000001000000000000007800000000000000ffffffffffffffff000002000000000000000200000000000100000048200000"
)
_TSUB_UEXP_HEADER = bytes.fromhex(
    "00000000040483020303010008000000080000c81b78ca21e4e043a0befac608ca2d75100000000005000500010000000100000001000000000000004c00020000000000000000000000000000000000000000000002000000020000010000000800000050465f44585431000000000001000000000000000000ffffffffffff0000ffffffffffff0000ffffffffffff"
)
assert len(_TSUB_SRC_UASSET) == 754
assert len(_TSUB_UEXP_HEADER) == 144

# --- Material Instance Template ---
# Source: MI_New_Hor_04 (Standee A, genre=Hor, num=04)
# uasset: 1772 bytes, uexp: 33471 bytes (uexp is identical across ALL MIs)
# Template: genre='Hor', num=4, shape='A'
# To create any MI_New_{genre}_{num} with standee shape X:
#   1. Decompress _MI_UASSET_TEMPLATE_B64Z → 1772 bytes
#   2. Replace b"Hor_04" → b"{genre}_{num:02d}" (5 occurrences)
#   3. Replace b"T_Bkg_Hor" → b"T_Bkg_{genre}" (3 occurrences)
#   4. Replace b"T_Standee_A_01_ao" → b"T_Standee_{shape}_01_ao" (2 occurrences)
#   5. Copy _MI_UEXP_TEMPLATE_B64Z verbatim (33471 bytes, shared)
_MI_UASSET_TEMPLATE_B64Z = (
    "eNqtVctPE0EcHgjykJdU3sSkIb5iQoYSo0aN6YuURkurbTmZrMN2aBfa3To7bQEPEolEY2I00QMxwXhQT0YPXoxeuCDxkZQD/gFo"
    "vKgxURAEoc7AEhZoqSFOMvub/b75fbPz5Tezo4OHRuZSqRRI077mA2BjETpQBMMOKYAVL1UIhkhVMYVRokRhPKRCKxJ7gkSJyQHo"
    "E6w9QaFNIdDlFNpxgg+F5sMANIKBBibVkLMiPZ63tgyHQmypUg0fz9/8KYtf9hWf9+e6B8XhsUuVPQ9ztLwG8G9t6nnfDI/vmPbj"
    "6+u5AtaTDC9mMaVre9i7xW10hJVOFDY2GT1KAhOjJdAdU2kEyxQcHHs6y5OsSMVGmxJWCPh++sGFcgZpSecsLqO/owXEcqqm+cx2"
    "hUQY7EJRcNPm6OYmsCng7YRiruRj3EWQSCVFtuMoDVklpILWZG8Td8ant/PkZP+N2mXQE0YqlUTB02ETfJJMhWaTIIOp6aMT9Rl5"
    "giLg9cKRR4blGV6K5ADGgoVTSAH744nhasb42aa0bTAXfLiXxggGr94k9/6nmvh4uWTeul2pdXYcGLnVeyyzkhojXUjEKsxk193J"
    "/rbj287ndt75POC2ZFagK+5xBb3dcLP7QxX3k+Z0QiJ/qCEUwARiOS4RReZFCO19MopIInQJ7ii6GMPMkzgm4NOo6QwvKegViRSl"
    "0MbS/e7ObixSMOdsOlWu41rloCRjMGS+6uRHz8b2qAKa6L3dyN7suAvFwlQQXIhiIqGwU1bZN4vYpiwPKChM3HvP5TZ8QbzdVF3E"
    "YS0PfHv2w1SnAzYJ5Z0wPCnjE9aVyQvvh7OFDPWwMkBBDFTHtaqdvHJXPG2xg6VF80SFdph3a3GWHeAa3SE3aLFai/zOq0rD1+jy"
    "K3X8qn6dDivJwpdm4cuy8OVZ+F1ZeEOacb0W/7D95W/BLzC+YAt+nvGFW/C/GV+k41PaP2ZRi7Uazm/Fl1fW/jeZ2uqFr287WM/d"
    "gC0x/V+s/2R9hvVp7TLn6/4FC666xA=="
)
_MI_UEXP_TEMPLATE_B64Z = (
    "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"
    "OTrvf4YlrOE="
)
_MI_UASSET_TEMPLATE = zlib.decompress(base64.b64decode(_MI_UASSET_TEMPLATE_B64Z))
_MI_UEXP_TEMPLATE = zlib.decompress(base64.b64decode(_MI_UEXP_TEMPLATE_B64Z))
assert len(_MI_UASSET_TEMPLATE) == 1772
assert len(_MI_UEXP_TEMPLATE) == 33471

# T_Standees_Collection thumbnail texture template
# Source: T_Standees_Collection_12286 (SKU=12286, FName num=12287)
# uasset: 813 bytes, uexp header: 137 bytes
# Pixel format: DXT5, 512x512, 262144 bytes inline in uexp
_THUMB_TEX_UASSET_TEMPLATE_B64Z = (
    "eNo72Kw178f///8ZsABdZgYGbyCt756Ym6oflpmSmh9ckl+Uqp9YXJxaol9QlF+gH1ySmJeSmlqsH5JRmpuUl5iZox8SDxOMd87P"
    "yUlNLsnMz4s3NDKyMGNgUGJo4AIaqcwIsUKTCWEdSKgTyGeGir9kwnRSvmzXL4fd5f5toZpz2BOsBRmh+rgYiAOpdyN3gOi3QLN3"
    "MbOgyIGs+8gEof9DwU8ghxXI98vPS2X4wnyUlwPICXCLd4kIMWVobV4X6Eql8GGY+Dj4swjIsODkosyCEn1noEGh/klZQGmGH566"
    "dvxIcq556ZlA97Q5tHqyAYWdc4AWMpSUV0wUBvJcUtMSS3NK4uNDUitKSotSjVwYvDIYJ4MdnpicnZieylDs3iEqBuRjd0nDB+7p"
    "oPBE6P/31+EiMzSUWKH0P2DocCKFHkyeHUmMBQubE0k/G5L8f2gi/AulQe79r8/AwA2ke6G6dZlxxywsJaAD9EQEsgdkB7L6UiQ5"
    "iDtYUFwNUucBxADs07B4"
)
_THUMB_TEX_UEXP_HEADER_B64Z = (
    "eNpjYGBgYGFhZ2ZgAjKA+F1GQBGf4z/nzuS6pm8s0eYCQGEGViBkBNIwDAI+DCwMWAHUIJA6DiAOcIt3iQgxBUvB9AIA6S4JzA=="
)
_THUMB_TEX_TEMPLATE_SKU = 12286
_THUMB_TEX_TEMPLATE_FNAME_NUM = 12287
_THUMB_TEX_FNAME_NUM_OFFSET = 0x29D
_THUMB_TEX_PIXEL_SIZE = 262144
_THUMB_TEX_TRAILING = bytes.fromhex("000200000002000001000000000000000000000000000000c1832a9e")

# Full-resolution standee preview images (512x512 JPEG q75, zlib+base64)
# Used for T_Standees_Collection thumbnail textures in-game
_STANDEE_FULLRES_A_B64Z = (
    "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"
)
_STANDEE_FULLRES_B_B64Z = (
    "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"
)
_STANDEE_FULLRES_C_B64Z = (
    "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"
    "8u8="
)

_MI_TEMPLATE_GENRE = "Hor"
_MI_TEMPLATE_NUM = 4
_MI_TEMPLATE_SHAPE = "A"


def build_transparent_tsub(src_uasset_bytes, src_uexp_bytes, dst_name, src_name=None):
    """
    Clone a T_Sub uasset+uexp for a new name, replacing all occurrences of
    src_name with dst_name and filling pixel data with fully transparent DXT1.

    Uses a hardcoded 144-byte uexp header (correct PF_DXT1 format with inline
    bulk data markers) rather than copying from the base game source, which has
    a different header format that causes FirstMipToSerialize=-1 in-engine.

    src_name: the name embedded in the source files (auto-detected if None).
    dst_name must be the same byte-length as src_name (UE4 FString same-size patch).

    Returns (new_uasset_bytes, new_uexp_bytes).
    """
    # Always use the embedded T_Sub template — ignore src_uasset_bytes/src_uexp_bytes
    # (kept as parameters for API compatibility but not used)
    src_name = "T_Sub_01"
    # T_Sub names must be exactly 8 chars (T_Sub_XX) to fit the fixed binary template.
    # Names like T_Sub_100 (9 chars) would break the byte-level replace. Guard here:
    if len(dst_name) != len(src_name):
        # Fall back to T_Sub_78 — all custom T_Sub textures are identical transparent
        # images, so reusing one is safe.
        print(f"[TSub] WARNING: '{dst_name}' is {len(dst_name)} chars (need 8); "
              f"falling back to T_Sub_78")
        dst_name = "T_Sub_78"

    # Patch uasset: replace "T_Sub_01" with dst_name in the embedded source
    new_uasset = bytearray(_TSUB_SRC_UASSET.replace(
        src_name.encode("utf-8"), dst_name.encode("utf-8")))

    # Build uexp: header + transparent pixels + mip tail metadata + footer.
    # The last 28 bytes of the pixel area are mip dimension metadata, not pixel data.
    # These must be exact or the engine reads SizeX=0, PixelFormat=PF_Unknown.
    _TSUB_MIP_TAIL = bytes.fromhex("ffffffff000200000002000001000000000000000000000000000000")
    PURE_PIXELS = 131072 - 28   # 131044 bytes of actual DXT1 blocks
    UEXP_FOOTER = b"\xc1\x83\x2a\x9e"
    pixels   = make_transparent_dxt1_512()
    new_uexp = _TSUB_UEXP_HEADER + pixels[:PURE_PIXELS] + _TSUB_MIP_TAIL + UEXP_FOOTER

    # serial_size in embedded uasset = 131216 (= 131220 - 4), matching our uexp exactly.
    # No patching needed.

    return bytes(new_uasset), new_uexp



def get_custom_slot_si(slot_index_1based):
    """
    Return the dedicated T_Sub name for a custom slot.
    Slot 1 (first custom) -> T_Sub_78, wraps back to T_Sub_78 after T_Sub_99.
    All custom T_Sub textures are identical transparent 512x512 DXT1 images,
    so sharing is safe — multiple slots can point to the same T_Sub asset.
    The T_Sub name must be exactly 8 chars (T_Sub_XX) to fit the fixed uasset
    binary template; T_Sub_100+ (9 chars) would break the clone operation.
    """
    TSUB_RANGE = 99 - TSUB_CUSTOM_BASE + 1   # 22 distinct names: 78..99
    n = TSUB_CUSTOM_BASE + ((slot_index_1based - 1) % TSUB_RANGE)
    return f"T_Sub_{n:02d}"

# ============================================================
# SKU GENERATOR  (reverse-engineered from game data)
#
# SKU structure:
#   Genre prefix + slot/index band + last two digits.
#
# The game's Film_Data_C.Generate Critic Review Score function uses SKU % 100
# and a set of thresholds to return a score from 0..10. The displayed star
# rating is score / 2, so 3.0★ is valid and maps to score 6.
#
# Effective last-two-digit bands, accounting for the Blueprint's sequential
# inclusive InRange checks:
#   0 or 99 -> 5.0 ★ Good Critic
#   91-98   -> 4.5 ★ Good Critic
#   71-90   -> 4.0 ★ Good Critic
#   41-70   -> 3.5 ★ No tag
#   36-40   -> 3.0 ★ No tag
#   29-35   -> 2.5 ★ No tag
#   23-28   -> 2.0 ★ No tag
#   13-22   -> 1.5 ★ Bad Critic
#   7-12    -> 1.0 ★ Bad Critic
#   3-6     -> 0.5 ★ Bad Critic
#   1-2     -> 0.0 ★ Bad Critic
# ============================================================

# ── Star-rating options: label → confirmed last2 value ──────────────────────
STAR_OPTIONS = {
    "5.0 ★  Good Critic":  0,   # Score 10: fallback band, SKU % 100 == 0
    "4.5 ★  Good Critic": 93,   # Score 9 : 91-98
    "4.0 ★  Good Critic": 83,   # Score 8 : 71-90
    "3.5 ★  No tag":      53,   # Score 7 : 41-70
    "3.0 ★  No tag":      38,   # Score 6 : 36-40
    "2.5 ★  No tag":      33,   # Score 5 : 29-35
    "2.0 ★  No tag":      23,   # Score 4 : 23-28
    "1.5 ★  Bad Critic":  22,   # Score 3 : 13-22
    "1.0 ★  Bad Critic":  12,   # Score 2 : 7-12
    "0.5 ★  Bad Critic":   3,   # Score 1 : 3-6
    "0.0 ★  Bad Critic":   2,   # Score 0 : 1-2
}

STAR_VALUE_TO_LAST2 = {
    5.0: 0,
    4.5: 93,
    4.0: 83,
    3.5: 53,
    3.0: 38,
    2.5: 33,
    2.0: 23,
    1.5: 22,
    1.0: 12,
    0.5: 3,
    0.0: 2,
}


def sku_last2_to_score(last2: int) -> int:
    """Return the game's 0..10 critic score for SKU % 100.

    Mirrors Film_Data_C.Generate Critic Review Score: the Blueprint checks
    inclusive ranges in order, so shared thresholds such as 40 belong to the
    lower score checked first. Values outside 1..98 fall through to score 10.
    """
    last2 %= 100
    if 1 <= last2 <= 2:   return 0
    if 3 <= last2 <= 6:   return 1
    if 7 <= last2 <= 12:  return 2
    if 13 <= last2 <= 22: return 3
    if 23 <= last2 <= 28: return 4
    if 29 <= last2 <= 35: return 5
    if 36 <= last2 <= 40: return 6
    if 41 <= last2 <= 70: return 7
    if 71 <= last2 <= 90: return 8
    if 91 <= last2 <= 98: return 9
    return 10


def stars_to_last2(stars: float) -> int:
    """Return a safe representative SKU suffix for a displayed star rating."""
    return STAR_VALUE_TO_LAST2.get(round(float(stars) * 2) / 2, 93)


def critic_for_score(score: int) -> str:
    if score >= 8:
        return "Good Critic"
    if score <= 3:
        return "Bad Critic"
    return ""


def critic_for_last2(last2: int) -> str:
    return critic_for_score(sku_last2_to_score(last2))


def critic_badge_key(critic: str) -> str:
    if critic == "Good Critic":
        return "GoodCritic"
    if critic == "Bad Critic":
        return "BadCritic"
    return ""

# Rarity options shown in dialogs
RARITY_OPTIONS = [
    "Common",
    "Common (Old)",
    "Limited Edition (holo)",   # holo always implies Old in-game; kept for clarity
    "Random",
]

# Genre prefix for SKU generation (10M-unit format)
GENRE_SKU_PREFIX = {
    "Horror":       5,
    "Drama":        4,
    "Sci-Fi":       6,
    "Action":       1,
    "Comedy":       3,
    "Adult":       16,
    "Kid":         12,    # DataTable asset name
    "Kids":        12,    # GENRES/UI display name (same prefix)
    "Police":      14,
    "Romance":     10,
    "Fantasy":      7,
    "Western":     17,
    "Xmas":        18,
    "Adventure":    2,
    "Thriller":     8,
    "Music":        9,
    "History":     11,
    "Documentary": 13,
    "Sports":      15,
}

# Back-compat alias
CRITIC_OPTIONS = STAR_OPTIONS


def _sku_is_holo(sku: int) -> bool:
    """Return True if the LCG marks this SKU as Limited Edition (holographic)."""
    seed   = (sku * 196314165 + 907633515) & 0xFFFFFFFF
    f_bits = (seed >> 9) | 0x3F800000
    f      = _struct.unpack("<f", _struct.pack("<I", f_bits))[0] - 1.0
    return f < 0.019


def _sku_is_old(sku: int) -> bool:
    """Return True if this SKU will be tagged 'Old' in-game.

    The game seeds a UE4 RandomStream with the SKU and uses the first LCG
    step's float output to determine whether the movie is 'old' (released
    before some in-game year threshold, making it cheaper for NPCs to rent).

    Same LCG as the holo check, higher threshold:
      holo : f < 0.019   (~2% of SKUs)
      old  : f < 0.20    (~20% of SKUs — holo is always a subset of old)

    Confirmed against 36 in-game SKUs (April 2026):
      - 25 confirmed 'Old'    : all had f1 < 0.179
      - 21 confirmed 'Not Old': all had f1 > 0.205
      - Clean gap (0.179, 0.206) with no samples — threshold is 0.20.
    """
    seed   = (sku * 196314165 + 907633515) & 0xFFFFFFFF
    f_bits = (seed >> 9) | 0x3F800000
    f      = _struct.unpack("<f", _struct.pack("<I", f_bits))[0] - 1.0
    return f < 0.20


def _all_used_skus() -> set:
    """Return the set of every SKU currently assigned in CLEAN_DT_SLOT_DATA."""
    used = set()
    for slot_list in CLEAN_DT_SLOT_DATA.values():
        for slot in slot_list:
            s = slot.get("sku")
            if s:
                used.add(s)
    return used


def generate_sku(genre: str, slot_index: int, last2: int = 93,
                 rarity: str = "Common",
                 used_skus: set = None) -> int:
    """
    Generate a SKU satisfying star-rating (last2) and rarity/old tag,
    guaranteed unique across all slots in CLEAN_DT_SLOT_DATA.

    rarity must be one of RARITY_OPTIONS:
      "Common"                 -> not holo, not old
      "Common (Old)"           -> not holo, old
      "Limited Edition (holo)" -> holo (holo always implies old)
      "Random"                 -> any combination

    used_skus: optional pre-built set of already-taken SKUs; if None,
               built automatically from CLEAN_DT_SLOT_DATA.
    """
    if used_skus is None:
        used_skus = _all_used_skus()

    prefix_base = GENRE_SKU_PREFIX.get(genre, 5)

    candidates_match = []
    candidates_other = []
    # AI NOTE: Do NOT restore the old ±2 prefix band scan.
    # Adjacent genres have consecutive prefixes (e.g. Romance=9, Fantasy=10, Western=11).
    # A ±2 band overlaps adjacent genres, causing SKU collisions across genres.
    # Fix: scan 500 candidates within the single exact prefix band only.
    for step in range(0, 50000, 100):   # 500 candidates, single prefix band
        sku     = prefix_base * 10_000_000 + slot_index * 10_000 + step + last2
        if sku in used_skus:            # skip any already-assigned SKU
            continue
        is_holo = _sku_is_holo(sku)
        is_old  = _sku_is_old(sku)

        if rarity == "Common":
            ok = not is_holo and not is_old
        elif rarity == "Common (Old)":
            ok = not is_holo and is_old
        elif rarity == "Limited Edition (holo)":
            ok = is_holo                    # holo always implies old
        else:                               # "Random" or unrecognised
            ok = True

        if ok:
            candidates_match.append(sku)
        else:
            candidates_other.append(sku)

    if rarity == "Random":
        import random
        pool = candidates_match + candidates_other
        return random.choice(pool) if pool else (
            prefix_base * 10_000_000 + slot_index * 10_000 + last2)
    if candidates_match:
        return candidates_match[0]
    # Fallback: constraint unsatisfiable in scan range (should never happen)
    if candidates_other:
        chosen = candidates_other[0]
        print(f"[SKU] WARNING: no '{rarity}' candidate for {genre} slot {slot_index} "
              f"last2={last2}. Using fallback SKU={chosen}.")
        return chosen
    return prefix_base * 10_000_000 + slot_index * 10_000 + last2


def sku_to_info(sku: int) -> tuple:
    """Return (stars_float, critic_str, is_holo) for a given SKU."""
    score = sku_last2_to_score(sku % 100)
    return score / 2.0, critic_for_score(score), _sku_is_holo(sku)


def sku_to_stars(sku: int) -> tuple:
    """Legacy wrapper — returns (stars_float, critic_str)."""
    stars, critic, _ = sku_to_info(sku)
    return stars, critic


# NOTE (v1.8.1, April 2026): generate_police_ingame_sku(), ingame_to_written_police(),
# and police_ingame_sku() were deleted here. They were workarounds for a misdiagnosed
# bug — the real issue was that build() was writing the V2 row schema for Police, so
# SKU ended up 1 byte off from where the V3 parser expected it. Fixed by writing the
# correct V3 schema (with ColorPalette byte). Police now uses generate_sku() directly.
# See SECTION 3 POLICE SKU note in the module docstring for details.


def sku_to_rarity(sku: int) -> str:
    """Return the rarity string for a given SKU."""
    if _sku_is_holo(sku):
        return "Limited Edition (holo)"
    if _sku_is_old(sku):
        return "Common (Old)"
    return "Common"


def sku_display(sku: int) -> str:
    """Compact display string e.g. '4.5★  Good Critic  ·  Limited ✦' or '·  Old'."""
    stars, critic, is_holo = sku_to_info(sku)
    is_old      = _sku_is_old(sku)
    if is_holo:
        rarity_str = "Limited ✦"          # holo always implies old; no need to add "Old"
    elif is_old:
        rarity_str = "Old"
    else:
        rarity_str = "Common"
    critic_part = f"  {critic}" if critic else ""
    return f"{stars:.1f}★{critic_part}  ·  {rarity_str}"

# ============================================================
# TEXTURE MAP
# ============================================================

# Genres hidden from the UI — exist in the game files but are not used in-game.
# AI NOTE: Adventure confirmed unused in-game (April 2026). Do not re-add to UI.
HIDDEN_GENRES = {}

GENRES = {
    # bkg_max = confirmed from binary analysis of AssetRegistry.bin.
    # Slots 01-09: registered as literal FName strings.
    # Slots 10+: registered as FName number-suffix entries (base string + stored_number).
    # stored_number = display_number + 1, so stored 11..79 = slots 10..78.
    #
    # Horror is special: the devs pre-registered slots 10..77 (68 FName-suffix entries)
    # giving it a true cap of 77 (9 literal + 68 suffix).
    # All other genres have their FName-suffix entries end exactly at their current slot count.
    # Adventure (cap 3) and Western (cap 10) have no FName-suffix group at all.
    # bkg_max = max 2-digit slot number (99). AssetRegistry patching not required —
    # cooked UE5 loads textures from pak by path, DataTable drives slot selection.
    "Action":       {"code": "Act", "bkg": 15, "new": 3,  "bkg_max": 999},
    "Adult":        {"code": "Adu", "bkg": 18, "new": 0,  "bkg_max": 999},
    "Adventure":    {"code": "Adv", "bkg": 3,  "new": 0,  "bkg_max": 999},
    "Comedy":       {"code": "Com", "bkg": 12, "new": 1,  "bkg_max": 999},
    "Drama":        {"code": "Dra", "bkg": 19, "new": 3,  "bkg_max": 999},
    "Fantasy":      {"code": "Fan", "bkg": 11, "new": 2,  "bkg_max": 999},
    "Horror":       {"code": "Hor", "bkg": 22, "new": 4,  "bkg_max": 999},
    "Kids":         {"code": "Kid", "bkg": 11, "new": 1,  "bkg_max": 999},
    "Police":       {"code": "Pol", "bkg": 13, "new": 1,  "bkg_max": 999},
    "Romance":      {"code": "Rom", "bkg": 14, "new": 0,  "bkg_max": 999},
    "Sci-Fi":       {"code": "Sci", "bkg": 18, "new": 4,  "bkg_max": 999},
    "Western":      {"code": "Wst", "bkg": 10, "new": 0,  "bkg_max": 999},
    "Xmas":         {"code": "Xma", "bkg": 12, "new": 1,  "bkg_max": 999},
    "Thriller":     {"code": "Thr", "bkg":  0, "new": 0,  "bkg_max": 999},
    "Music":        {"code": "Mus", "bkg":  0, "new": 0,  "bkg_max": 999},
    "History":      {"code": "His", "bkg":  0, "new": 0,  "bkg_max": 999},
    "Documentary":  {"code": "Doc", "bkg":  0, "new": 0,  "bkg_max": 999},
    "Sports":       {"code": "Spo", "bkg":  0, "new": 0,  "bkg_max": 999},
}

def build_texture_list():
    """Build full texture list from CLEAN_DT_SLOT_DATA for genres that have it,
    falling back to sequential generation for genres that don't."""
    textures = []
    for genre, info in GENRES.items():
        code    = info["code"]
        folder  = f"T_Bkg_{code}"
        dt_name = GENRE_DATATABLE.get(genre, genre)

        if dt_name in CLEAN_DT_SLOT_DATA and CLEAN_DT_SLOT_DATA[dt_name]:
            # Use the actual bkg_tex values from slot data — respects gaps in numbering
            for slot in CLEAN_DT_SLOT_DATA[dt_name]:
                textures.append({"genre": genre, "folder": folder,
                                  "name": slot["bkg_tex"], "type": "Background"})
        else:
            # Fallback: sequential generation up to bkg_max
            bkg_count = info["bkg"]
            for i in range(1, bkg_count + 1):
                tex_name = f"T_Bkg_{code}_{i:03d}" if i < 100 else f"T_Bkg_{code}_{i}"
                textures.append({"genre": genre, "folder": folder,
                                  "name": tex_name, "type": "Background"})

        # NR textures (T_New_XXX_NN) are NOT added to the genre shelf list.
        # They are managed separately via the "New Releases" tab.
    return textures

# ALL_TEXTURES is populated after CLEAN_DT_SLOT_DATA is defined (below)
ALL_TEXTURES = []

# ============================================================
# NEW RELEASE SYSTEM — data model + constants
# ============================================================

# Genre byte values for NewRelease_Details DataTable rows (binary offset 40).
# Mapped from base game JSON enum names cross-referenced with binary analysis.
NR_GENRE_BYTE = {
    # Genre byte for the NR DataTable row (offset 40 in 54-byte row).
    # Values confirmed from base game genre DataTable binary analysis
    # (genre_byte field read by CleanDT builder from each genre's uexp).
    "Action":  0x01,  "Comedy":  0x03,  "Drama":   0x04,
    "Horror":  0x05,  "Sci-Fi":  0x06,  "Fantasy": 0x07,
    "Kids":    0x0C,  "Police":  0x0E,  "Xmas":    0x12,
    # Genres without base game T_New textures (confirmed from CleanDT log):
    "Romance": 0x0A,  "Western": 0x11,
    # Adult (0x10) excluded — game logic blocks Adult New Releases from appearing.
    "Adventure":  0x02,  "Thriller":  0x08,  "Music":   0x09,
    "History":  0x0B,  "Documentary":  0x0D,  "Sports":   0x0F,
}

# Genres that support New Releases (have known binary genre byte)
NR_GENRES = list(NR_GENRE_BYTE.keys())

# Standee shapes available
NR_STANDEE_SHAPES = ["A", "B", "C"]

# In-game genre colors (background + text) for UI tabs and badges
GENRE_COLORS = {
    "Action":           {"bg": "#3366cc", "fg": "#E8E8E8"},  
    "Adult":            {"bg": "#000000", "fg": "#E8E8E8"},  
    "Comedy":           {"bg": "#FFCE00", "fg": "#1A1A2E"},  
    "Drama":            {"bg": "#A8BAFF", "fg": "#1A1A2E"},  
    "Fantasy":          {"bg": "#4B590E", "fg": "#E8E8E8"},   
    "Horror":           {"bg": "#cc0000", "fg": "#FFFFFF"},   
    "Kids":             {"bg": "#6600cc", "fg": "#E8E8E8"},  
    "Police":           {"bg": "#00ffff", "fg": "#1A1A2E"},   
    "Romance":          {"bg": "#EF74FF", "fg": "#1A1A2E"},  
    "Sci-Fi":           {"bg": "#00ff00", "fg": "#1A1A2E"},   
    "Western":          {"bg": "#FFB53F", "fg": "#1A1A2E"},   
    "Xmas":             {"bg": "#ff0000", "fg": "#1A1A2E"},   
    "Thriller":         {"bg": "#000099", "fg": "#E8E8E8"},  
    "Music":            {"bg": "#cc00ff", "fg": "#E8E8E8"}, 
    "History":          {"bg": "#663300", "fg": "#E8E8E8"},  
    "Documentary":      {"bg": "#ffffff", "fg": "#1A1A2E"},  
    "Sports":           {"bg": "#006600", "fg": "#E8E8E8"},  
    "Adventure":        {"bg": "#0099ff", "fg": "#E8E8E8"},  
}

def _lighten_color(hex_color, min_lightness=0.65):
    """Lighten a hex color so it's readable on dark backgrounds.
    If the color's HSL lightness is below min_lightness, boost it."""
    h_str = hex_color.lstrip("#")
    r, g, b = int(h_str[:2],16)/255, int(h_str[2:4],16)/255, int(h_str[4:6],16)/255
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if l < min_lightness:
        l = min_lightness
        s = min(s, 0.6)  # desaturate slightly for readability
    r2, g2, b2 = colorsys.hls_to_rgb(h, l, s)
    return f"#{int(r2*255):02x}{int(g2*255):02x}{int(b2*255):02x}"

# Map 3-char codes to genre names for NR badge coloring
_CODE_TO_GENRE = {v["code"]: k for k, v in GENRES.items() if "code" in v}

# New Release slot data — persisted to nr_custom_slots.json
# Each entry: {"title", "genre", "genre_code", "genre_byte",
#              "bkg_tex", "sku", "standee_shape", "tex_num"}
NR_SLOT_DATA = []

NR_SLOTS_FILE = os.path.join(SCRIPT_DIR, "nr_custom_slots.json")

def save_nr_slots():
    """Persist NR_SLOT_DATA to disk."""
    with open(NR_SLOTS_FILE, 'w') as f:
        json.dump(NR_SLOT_DATA, f, indent=2)
    print(f"[NR] Saved {len(NR_SLOT_DATA)} new release slot(s)")

def load_nr_slots():
    """Load NR_SLOT_DATA from disk on startup."""
    global NR_SLOT_DATA
    if os.path.exists(NR_SLOTS_FILE):
        with open(NR_SLOTS_FILE) as f:
            NR_SLOT_DATA = json.load(f)
        # Auto-fix: update genre_byte if NR_GENRE_BYTE was corrected
        _fixed = 0
        for nr in NR_SLOT_DATA:
            g = nr.get("genre", "")
            correct_byte = NR_GENRE_BYTE.get(g)
            if correct_byte is not None and nr.get("genre_byte") != correct_byte:
                old = nr.get("genre_byte")
                nr["genre_byte"] = correct_byte
                _fixed += 1
                print(f"[NR] Fixed genre_byte for '{nr.get('title','')}' "
                      f"({g}): 0x{old:02X} → 0x{correct_byte:02X}")
        # Auto-repair legacy modulo bug: the old add_nr_slot() recycled
        # tex_num modulo base_new_count, so JSONs from previous tool versions
        # often have multiple slots per genre sharing the same tex_num/bkg_tex.
        # Keep the FIRST occurrence of each (genre, tex_num); renumber later
        # duplicates to the next free slot. This preserves user titles/SKUs
        # and only changes which T_New texture each duplicate references.
        seen_per_genre = {}  # genre -> set of tex_num kept so far
        _renumbered = 0
        for nr in NR_SLOT_DATA:
            g = nr.get("genre", "")
            t = nr.get("tex_num", 0)
            code = nr.get("genre_code") or GENRES.get(g, {}).get("code", "")
            used = seen_per_genre.setdefault(g, set())
            if not isinstance(t, int) or t < 1 or t in used:
                new_t = next(i for i in range(1, 1000) if i not in used)
                old_t = t
                nr["tex_num"] = new_t
                if code:
                    nr["bkg_tex"] = f"T_New_{code}_{new_t:03d}"
                used.add(new_t)
                _renumbered += 1
                print(f"[NR] Renumbered '{nr.get('title','')}' ({g}): "
                      f"tex_num {old_t} → {new_t} (bkg_tex={nr.get('bkg_tex','')})")
            else:
                used.add(t)
        # Migrate legacy 2-digit bkg_tex ("T_New_Hor_05") to 3-digit
        # ("T_New_Hor_005"). 3-digit format bypasses the AssetRegistry
        # bottleneck — UE resolves 3-digit names via pak filename lookup, so
        # custom NR slots beyond the base game count actually load in-game.
        # tex_num itself stays the same int — only the bkg_tex string changes.
        _migrated_format = 0
        for nr in NR_SLOT_DATA:
            code = nr.get("genre_code") or GENRES.get(nr.get("genre", ""), {}).get("code", "")
            t = nr.get("tex_num", 0)
            if not code or not isinstance(t, int) or t < 1:
                continue
            expected = f"T_New_{code}_{t:03d}"
            if nr.get("bkg_tex") != expected:
                nr["bkg_tex"] = expected
                _migrated_format += 1
        if _migrated_format:
            print(f"[NR] Migrated {_migrated_format} bkg_tex entries to 3-digit format")
        if _fixed or _renumbered or _migrated_format:
            save_nr_slots()
            if _renumbered:
                print(f"[NR] Auto-repair: {_renumbered} duplicate tex_num(s) "
                      f"renumbered (legacy modulo bug). Re-build pak to apply.")
        print(f"[NR] Loaded {len(NR_SLOT_DATA)} new release slot(s)")
        
def add_nr_slot(genre, title="New Release", standee_shape="A"):
    """Add a new New Release movie slot. Returns the slot dict or None."""
    title_err = _movie_title_error(title)
    if title_err:
        print(f"[NR] ERROR: {title_err}")
        return None
    if genre not in NR_GENRE_BYTE:
        print(f"[NR] Genre '{genre}' not supported for New Releases "
              f"(no base game T_New textures)")
        return None
    code = GENRES[genre]["code"]
    genre_byte = NR_GENRE_BYTE[genre]

    # Assign the smallest unused tex_num for this genre (1, 2, 3, …).
    # bkg_tex uses 3-digit zero-padded format (T_New_<code>_<NN:03d>) — that
    # matches the convention the tool already uses for custom T_Bkg slots
    # (custom_slots.json: "T_Bkg_Hor_001" etc.). UE treats 3-digit names as
    # custom assets and resolves them via pak filename lookup, so they don't
    # need to be in AssetRegistry.bin (which only ships the 20 base game
    # T_New entries). This is what makes our genre-shelf slots work past the
    # base game cap — same trick now applies to NR slots.
    used = {s.get("tex_num") for s in NR_SLOT_DATA if s.get("genre") == genre}
    tex_num = next(i for i in range(1, 1000) if i not in used)
    bkg_tex = f"T_New_{code}_{tex_num:03d}"

    # Generate a 5-digit SKU unique across all NR slots
    existing_skus = {s["sku"] for s in NR_SLOT_DATA}
    import random
    sku = random.randint(50000, 59999)
    while sku in existing_skus:
        sku = random.randint(50000, 59999)

    slot = {
        "title": title,
        "genre": genre,
        "genre_code": code,
        "genre_byte": genre_byte,
        "bkg_tex": bkg_tex,
        "sku": sku,
        "standee_shape": standee_shape,
        "tex_num": tex_num,
    }
    NR_SLOT_DATA.append(slot)
    save_nr_slots()
    print(f"[NR] Added '{title}' ({genre}) as {bkg_tex}, SKU={sku}, standee={standee_shape}")
    return slot

def _rebuild_uasset_with_name_patches(data, package_name_old, package_name_new,
                                      name_table_patches):
    """Generic uasset rebuilder for length-changing FName/PackagePath patches.

    Used by create_mi_for_nr and clone_standee_blueprint when 2-digit slot
    references need to become 3-digit (string grows by 1+ bytes per occurrence,
    breaking length-prefix and offset fields).

    Args:
        data: bytes/bytearray of the source uasset
        package_name_old: the current PackageName FString in the header (e.g.
            "/Game/.../MI_New_Hor_04"). Used to find and replace the header
            FString at offset 0x24.
        package_name_new: the new PackageName FString to write.
        name_table_patches: list of (old_substring, new_substring) tuples. For
            each name table entry, if the entry contains old_substring the
            entry is replaced (substring substitution). Length differences are
            handled — entry sizes and downstream offsets are recomputed.

    Returns the rebuilt bytes. FName hashes for any patched entry are zeroed
    (UE re-hashes from the string at load time and avoids hash collisions
    between assets cloned from the same template).

    The rebuild mirrors the pattern in clone_texture_3digit:
      - Copy bytes 0x00..0x20 (uasset header up to PackageName length).
      - Write new PackageName length + new PackageName bytes.
      - Copy bytes from old fse to old name_table start.
      - Walk the name table, applying patches per entry, recomputing lengths.
      - Copy everything after the name table verbatim.
      - Update offset fields in the header (name_offset, plus the standard
        export/import-related offsets) by the cumulative byte shift.
    """
    data = bytes(data)
    pkg_len_old = struct.unpack_from('<i', data, 0x20)[0]
    fse = 0x24 + pkg_len_old
    name_count = struct.unpack_from('<i', data, fse + 4)[0]
    name_offset = struct.unpack_from('<i', data, fse + 8)[0]

    # --- Rebuild header up through PackageName ---
    new_data = bytearray(data[:0x20])
    new_pkg = package_name_new.encode("utf-8") + b"\x00"
    new_data += struct.pack('<i', len(new_pkg))
    new_data += new_pkg
    new_fse = len(new_data)
    new_data += data[fse:name_offset]
    new_name_offset = len(new_data)

    # --- Walk name table, apply patches ---
    pos = name_offset
    for _ in range(name_count):
        if pos + 4 > len(data):
            break
        slen = struct.unpack_from('<i', data, pos)[0]
        if slen <= 0 or slen > 500:
            break
        s_bytes = data[pos + 4:pos + 4 + slen]   # includes null terminator
        hash_val = struct.unpack_from('<I', data, pos + 4 + slen)[0]
        s_str = s_bytes[:-1].decode('utf-8', 'replace')

        patched = False
        for old_s, new_s in name_table_patches:
            if old_s and old_s in s_str:
                s_str = s_str.replace(old_s, new_s)
                patched = True
        if patched:
            s_bytes = s_str.encode("utf-8") + b"\x00"
            hash_val = 0  # invalidate hash so UE re-hashes from string

        new_data += struct.pack('<i', len(s_bytes))
        new_data += s_bytes
        new_data += struct.pack('<I', hash_val)
        pos += 4 + slen + 4

    # --- Copy everything after the name table verbatim ---
    new_data += data[pos:]
    total_shift = len(new_data) - len(data)

    # --- Fix offset fields in the export header section ---
    # name_offset (relative offset 8 from fse) is already implicit in our
    # rebuild — set it explicitly from the new layout.
    struct.pack_into('<i', new_data, new_fse + 8, new_name_offset)
    # The other offset fields point at locations AFTER the name table.
    # Each needs to be bumped by total_shift (provided it was non-zero).
    for rel in [16, 32, 40, 44, 136, 160, 176]:
        abs_pos = new_fse + rel
        if abs_pos + 4 > len(new_data):
            continue
        old_val = struct.unpack_from('<i', new_data, abs_pos)[0]
        if old_val > 0:
            struct.pack_into('<i', new_data, abs_pos, old_val + total_shift)

    # Patch serial_offset in the export entry (export_off + 36 holds an
    # int64 that points at the end of the file).
    new_export_off = struct.unpack_from('<i', new_data, new_fse + 32)[0]
    new_export_count = struct.unpack_from('<i', new_data, new_fse + 28)[0]
    if new_export_off + 44 <= len(new_data):
        struct.pack_into('<q', new_data, new_export_off + 36, len(new_data))

    # Multi-export assets (Blueprints): Export[1..N].SerialOffset also point
    # into the merged uasset+uexp stream and must each be shifted by
    # total_shift, otherwise UE seeks to a stale position in the merged
    # stream and crashes in FAsyncLoadingThread.
    if new_export_count > 1 and total_shift != 0:
        orig_export_off = struct.unpack_from('<i', data, fse + 32)[0]
        e0_size = struct.unpack_from('<q', data, orig_export_off + 28)[0]
        e0_off  = struct.unpack_from('<q', data, orig_export_off + 36)[0]
        expected_e1_off = e0_off + e0_size
        detected_esz = None
        for candidate in (96, 104, 100, 88, 112):
            probe = orig_export_off + candidate + 36
            if probe + 8 > len(data):
                continue
            if struct.unpack_from('<q', data, probe)[0] == expected_e1_off:
                detected_esz = candidate
                break
        if detected_esz is not None:
            for i in range(1, new_export_count):
                base_new = new_export_off + i * detected_esz
                if base_new + 36 + 8 > len(new_data):
                    break
                old_v = struct.unpack_from('<q', new_data, base_new + 36)[0]
                if old_v > 0:
                    struct.pack_into('<q', new_data, base_new + 36,
                                     old_v + total_shift)

    return bytes(new_data)


def _t_new_donor_for(genre_code):
    """Return the (donor_code, donor_num) pair to clone from for T_New textures.

    Donor is always Horror_01 — Hor has base_new=4 and is guaranteed extractable
    from the base game pak. clone_texture_3digit handles cross-genre name
    patches automatically (T_New_Hor → T_New_<code>, T_Bkg_Hor → T_Bkg_<code>).
    """
    return ("Hor", 1)


def _uasset_packagename(uasset_path):
    """Read the PackageName FString from a uasset header, or None on error.
    Used to validate that a cached donor file is the correct 2-digit T_New
    asset (vs a buggy T_Bkg/3-digit clone left over from older tool runs)."""
    try:
        with open(uasset_path, "rb") as f:
            head = f.read(256)
        pkg_len = struct.unpack_from('<i', head, 0x20)[0]
        if pkg_len <= 0 or pkg_len > 200 or 0x24 + pkg_len > len(head):
            return None
        return head[0x24:0x24 + pkg_len - 1].decode('utf-8', 'replace')
    except Exception:
        return None


def prepare_nr_donor_in_cache(pak_cache, genre_code, tex_num):
    """Materialize T_New_<code>_<NN:03d>.uasset/uexp/ubulk inside the PakCache
    base_dir so subsequent inject_texture / get_base_files calls see it.

    Uses 3-digit format (T_New_Hor_005, not T_New_Hor_05). This matches the
    convention the tool already uses for custom T_Bkg slots (T_Bkg_Hor_001
    etc. in custom_slots.json). UE treats 3-digit names as custom assets and
    resolves them via pak filename lookup, so they don't need to be in the
    AssetRegistry — which only ships the 20 base-game T_New entries.

    Donor is always T_New_Hor_01 from the base pak (cross-genre clone).
    clone_texture_3digit handles the full asset rebuild — package name,
    name table entries, and offset fields all get updated for the length
    change (T_New_Hor_01 → T_New_<code>_<NN:03d>, +1 to +N bytes).

    Validates an existing cache file's PackageName before skipping — old
    caches from pre-fix tool versions contain 2-digit names which need to
    be re-cloned as 3-digit. Returns True on success.
    """
    target_name = f"T_New_{genre_code}_{tex_num:03d}"
    target_folder = f"T_Bkg_{genre_code}"
    target_dir = os.path.join(pak_cache._base_dir, target_folder)
    target_uasset = os.path.join(target_dir, f"{target_name}.uasset")
    expected_pkg_suffix = f"/{target_name}"
    cached_pkg = _uasset_packagename(target_uasset)
    pkg_ok = (cached_pkg is not None
              and cached_pkg.endswith(expected_pkg_suffix))
    sidecars_ok = all(
        os.path.exists(os.path.join(target_dir, f"{target_name}.{ext}"))
        for ext in ("uexp", "ubulk"))
    if pkg_ok and sidecars_ok:
        return True
    if cached_pkg is not None and not pkg_ok:
        print(f"[NR] Cache invalid for {target_name} (was {cached_pkg!r}) — re-cloning")

    donor_code, donor_num = _t_new_donor_for(genre_code)
    donor_name = f"T_New_{donor_code}_{donor_num:02d}"
    donor_folder = f"T_Bkg_{donor_code}"
    donor_dir = os.path.join(pak_cache._base_dir, donor_folder)
    os.makedirs(donor_dir, exist_ok=True)
    donor_pak_path = (f"RetroRewind/Content/VideoStore/asset/prop/vhs"
                      f"/Background/{donor_folder}/{donor_name}")
    for ext in ("uasset", "uexp", "ubulk"):
        if not os.path.exists(os.path.join(donor_dir, f"{donor_name}.{ext}")):
            subprocess.run(
                [pak_cache.repak_path, "unpack",
                 "-o", pak_cache._extract_dir, "-f",
                 "-i", f"{donor_pak_path}.{ext}",
                 pak_cache.pak_path],
                capture_output=True, text=True, timeout=30,
            )
    for ext in ("uasset", "uexp", "ubulk"):
        if not os.path.exists(os.path.join(donor_dir, f"{donor_name}.{ext}")):
            print(f"[NR] prepare_nr_donor_in_cache: donor {donor_name}.{ext} "
                  f"unavailable for {target_name}")
            return False

    os.makedirs(target_dir, exist_ok=True)
    # uexp + ubulk: copy verbatim from donor. Pixel data is name-independent.
    for ext in ("uexp", "ubulk"):
        shutil.copy2(os.path.join(donor_dir, f"{donor_name}.{ext}"),
                     os.path.join(target_dir, f"{target_name}.{ext}"))
    # uasset: full asset rebuild via clone_texture_3digit (handles length
    # change from 2-digit donor to 3-digit target, fixes all header offsets,
    # zeroes patched FName hashes).
    with open(os.path.join(donor_dir, f"{donor_name}.uasset"), "rb") as f:
        donor_ua = f.read()
    cloned_ua = clone_texture_3digit(donor_ua, donor_code, donor_num,
                                     genre_code, tex_num)
    with open(os.path.join(target_dir, f"{target_name}.uasset"), "wb") as f:
        f.write(cloned_ua)
    print(f"[NR] Cached donor {target_name} ← {donor_name}"
          + (" (cross-genre)" if donor_code != genre_code else ""))
    return True


def prepare_nr_legacy_2digit_in_cache(pak_cache, genre_code, tex_num):
    """Extract base game's 2-digit T_New_<code>_<NN:02d> into pak_cache._base_dir.

    Used by the v1.8.2.1 legacy co-inject pass so pre-v1.8.2 saves continue
    to display custom NR covers. The 2-digit slot exists in the base game
    pak with the correct PackageName — extract as-is, no cloning needed.
    Idempotent — skips if all 3 files are already cached.

    Returns True iff all 3 files (uasset/uexp/ubulk) end up in cache.
    """
    if tex_num < 1 or tex_num > 99:
        return False
    target_name   = f"T_New_{genre_code}_{tex_num:02d}"
    target_folder = f"T_Bkg_{genre_code}"
    target_dir    = os.path.join(pak_cache._base_dir, target_folder)
    base_pak_path = (f"RetroRewind/Content/VideoStore/asset/prop/vhs"
                     f"/Background/{target_folder}/{target_name}")

    if all(os.path.exists(os.path.join(target_dir, f"{target_name}.{ext}"))
           for ext in ("uasset", "uexp", "ubulk")):
        return True

    os.makedirs(target_dir, exist_ok=True)
    for ext in ("uasset", "uexp", "ubulk"):
        if os.path.exists(os.path.join(target_dir, f"{target_name}.{ext}")):
            continue
        subprocess.run(
            [pak_cache.repak_path, "unpack",
             "-o", pak_cache._extract_dir, "-f",
             "-i", f"{base_pak_path}.{ext}",
             pak_cache.pak_path],
            capture_output=True, text=True, timeout=30,
        )
    return all(os.path.exists(os.path.join(target_dir, f"{target_name}.{ext}"))
               for ext in ("uasset", "uexp", "ubulk"))


def _patch_legacy_2digit_uasset(data, src_code, src_num, dst_code, dst_num):
    """Length-preserving FName patch on a T_New uasset: rename
    T_New_<src_code>_<src_num:02d> → T_New_<dst_code>_<dst_num:02d>.

    Donor and target short names are both 12 chars (e.g. T_New_Hor_01),
    folders are both 9 chars (T_Bkg_<3char>) — so all SerialOffsets and
    serial_size fields stay valid and the file is the same length. We just
    patch in place and zero the FName hashes on touched entries (UE accepts
    zero hashes). Export stored_number stays 0 (literal-style FName for
    slot<100, identical between donor and target).
    """
    import struct as _s
    data = bytearray(data)
    src_short  = f"T_New_{src_code}_{src_num:02d}"
    dst_short  = f"T_New_{dst_code}_{dst_num:02d}"
    src_folder = f"T_Bkg_{src_code}"
    dst_folder = f"T_Bkg_{dst_code}"
    if len(src_short) != len(dst_short) or len(src_folder) != len(dst_folder):
        raise ValueError(
            f"length-preserving rename requires equal lengths: "
            f"{src_short!r}->{dst_short!r}, {src_folder!r}->{dst_folder!r}")

    src_short_b  = src_short.encode()
    dst_short_b  = dst_short.encode()
    src_folder_b = src_folder.encode()
    dst_folder_b = dst_folder.encode()

    # Patch PackageName FString (len-prefixed, NUL-terminated, at 0x24)
    pkg_len = _s.unpack_from('<i', data, 0x20)[0]
    pkg_str = bytes(data[0x24:0x24 + pkg_len - 1])
    new_pkg = (pkg_str.replace(src_short_b, dst_short_b)
                       .replace(src_folder_b, dst_folder_b))
    if len(new_pkg) != len(pkg_str):
        raise ValueError("PackageName length changed unexpectedly")
    data[0x24:0x24 + len(new_pkg)] = new_pkg

    # Patch name table entries
    fse = 0x24 + pkg_len
    name_count  = _s.unpack_from('<i', data, fse + 4)[0]
    name_offset = _s.unpack_from('<i', data, fse + 8)[0]

    pos = name_offset
    for _ in range(name_count):
        if pos + 4 > len(data): break
        slen = _s.unpack_from('<i', data, pos)[0]
        if slen <= 0 or slen > 500: break
        s_bytes = bytes(data[pos+4:pos+4+slen])
        new_bytes = (s_bytes.replace(src_short_b, dst_short_b)
                            .replace(src_folder_b, dst_folder_b))
        if new_bytes != s_bytes:
            if len(new_bytes) != len(s_bytes):
                raise ValueError(
                    f"name table entry length changed: {s_bytes!r} -> {new_bytes!r}")
            data[pos+4:pos+4+slen] = new_bytes
            _s.pack_into('<I', data, pos+4+slen, 0)  # zero FName hash
        pos += 4 + slen + 4

    return bytes(data)


def prepare_nr_legacy_2digit_clone_in_cache(pak_cache, genre_code, tex_num):
    """Synthesize T_New_<code>_<NN:02d> in the cache by length-preserving
    clone of T_New_Hor_01.

    Used by the v1.8.2.2 legacy co-inject pass for NRs whose tex_num exceeds
    the base game's per-genre 2-digit slot count (e.g. Sci-Fi has only
    T_New_Sci_01..04 in base; a v1.8.1 user with hand-edited tex_num=5
    references T_New_Sci_05 which exists nowhere → blank shelf VHS in old
    saves). v1.8.2.1's co-inject silently skipped these via a gate; v1.8.2.2
    drops the gate and routes here.

    Donor T_New_Hor_01 is 12 chars; every 2-digit target T_New_<3char>_<2d>
    is also 12 chars, and folders T_Bkg_<3char> are all 9 chars — so the
    rename is length-preserving. No SerialOffset shifting, no header
    rewrites. inject_texture writes the user's cover into the cached
    uasset/uexp/ubulk afterward, same path the in-range co-inject uses.

    Returns True on success.
    """
    if tex_num < 1 or tex_num > 99:
        return False
    target_name   = f"T_New_{genre_code}_{tex_num:02d}"
    target_folder = f"T_Bkg_{genre_code}"
    target_dir    = os.path.join(pak_cache._base_dir, target_folder)
    target_uasset = os.path.join(target_dir, f"{target_name}.uasset")

    expected_pkg_suffix = f"/{target_name}"
    cached_pkg = _uasset_packagename(target_uasset)
    pkg_ok = (cached_pkg is not None
              and cached_pkg.endswith(expected_pkg_suffix))
    sidecars_ok = all(
        os.path.exists(os.path.join(target_dir, f"{target_name}.{ext}"))
        for ext in ("uexp", "ubulk"))
    if pkg_ok and sidecars_ok:
        return True
    if cached_pkg is not None and not pkg_ok:
        print(f"[NR-Legacy] Cache invalid for {target_name} (was {cached_pkg!r}) — re-cloning")

    donor_code   = "Hor"
    donor_num    = 1
    donor_name   = f"T_New_{donor_code}_{donor_num:02d}"
    donor_folder = f"T_Bkg_{donor_code}"
    donor_dir    = os.path.join(pak_cache._base_dir, donor_folder)
    os.makedirs(donor_dir, exist_ok=True)
    donor_pak_path = (f"RetroRewind/Content/VideoStore/asset/prop/vhs"
                      f"/Background/{donor_folder}/{donor_name}")
    for ext in ("uasset", "uexp", "ubulk"):
        if not os.path.exists(os.path.join(donor_dir, f"{donor_name}.{ext}")):
            subprocess.run(
                [pak_cache.repak_path, "unpack",
                 "-o", pak_cache._extract_dir, "-f",
                 "-i", f"{donor_pak_path}.{ext}",
                 pak_cache.pak_path],
                capture_output=True, text=True, timeout=30,
            )
    for ext in ("uasset", "uexp", "ubulk"):
        if not os.path.exists(os.path.join(donor_dir, f"{donor_name}.{ext}")):
            print(f"[NR-Legacy] donor {donor_name}.{ext} unavailable for {target_name}")
            return False

    os.makedirs(target_dir, exist_ok=True)
    # uexp + ubulk: pixel data is name-independent, copy verbatim
    for ext in ("uexp", "ubulk"):
        shutil.copy2(os.path.join(donor_dir, f"{donor_name}.{ext}"),
                     os.path.join(target_dir, f"{target_name}.{ext}"))
    # uasset: in-place length-preserving rename
    with open(os.path.join(donor_dir, f"{donor_name}.uasset"), "rb") as f:
        donor_ua = f.read()
    try:
        cloned_ua = _patch_legacy_2digit_uasset(
            donor_ua, donor_code, donor_num, genre_code, tex_num)
    except ValueError as e:
        print(f"[NR-Legacy] uasset patch failed for {target_name}: {e}")
        return False
    with open(os.path.join(target_dir, f"{target_name}.uasset"), "wb") as f:
        f.write(cloned_ua)
    print(f"[NR-Legacy] Cached clone {target_name} ← {donor_name}")
    return True


def ensure_nr_texture(pak_cache, genre_code, tex_num, work_dir):
    """Guarantee that T_New_{code}_{NN:03d} uasset/uexp/ubulk exist in work_dir.

    Called for every NR slot at build time. Idempotent — does nothing if
    the user uploaded a custom image (inject_texture already wrote the
    files). For slots without a custom image, copies the cached 3-digit
    donor files into work_dir so the slot at least shows the base donor
    cover instead of being missing.
    """
    name = f"T_New_{genre_code}_{tex_num:03d}"
    folder = f"T_Bkg_{genre_code}"
    dest = os.path.join(work_dir, "RetroRewind", "Content", "VideoStore",
                        "asset", "prop", "vhs", "Background", folder)
    if all(os.path.exists(os.path.join(dest, f"{name}.{ext}"))
           for ext in ("uasset", "uexp", "ubulk")):
        return True

    if not prepare_nr_donor_in_cache(pak_cache, genre_code, tex_num):
        return False

    src_dir = os.path.join(pak_cache._base_dir, folder)
    os.makedirs(dest, exist_ok=True)
    for ext in ("uasset", "uexp", "ubulk"):
        src = os.path.join(src_dir, f"{name}.{ext}")
        if not os.path.exists(src):
            print(f"[NR] ensure_nr_texture: cached {name}.{ext} missing")
            return False
        shutil.copy2(src, os.path.join(dest, f"{name}.{ext}"))
    print(f"[NR] Wrote {name} to work_dir (no user image, used cached donor)")
    return True


def remove_nr_slot(idx):
    """Remove NR slot by index."""
    if 0 <= idx < len(NR_SLOT_DATA):
        removed = NR_SLOT_DATA.pop(idx)
        save_nr_slots()
        print(f"[NR] Removed '{removed['title']}' ({removed['bkg_tex']})")
        return True
    return False



def rebuild_texture_list():
    """Rebuild ALL_TEXTURES in-place (call after modifying slot data)."""
    global ALL_TEXTURES
    ALL_TEXTURES = build_texture_list()


# ============================================================
# MOVIE DATABASE - texture slot -> movie title mapping
# Parsed from the mod's DataTable JSON files
# Genres not yet parsed show as unknown
# ============================================================

TEXTURE_MOVIES = {
    # Action - one primary title per slot (x76 majority mapping)
    "T_Bkg_Act_01": {"movies": ["Die Hard"], "new_release": False, "sku": None},
    "T_Bkg_Act_02": {"movies": ["Die Hard"], "new_release": False, "sku": None},
    "T_Bkg_Act_03": {"movies": ["Batman & Robin"], "new_release": False, "sku": None},
    "T_Bkg_Act_04": {"movies": ["Steel"], "new_release": False, "sku": None},
    "T_Bkg_Act_05": {"movies": ["The Rock"], "new_release": False, "sku": None},
    "T_Bkg_Act_06": {"movies": ["Captain America"], "new_release": False, "sku": None},
    "T_Bkg_Act_07": {"movies": ["Commando"], "new_release": False, "sku": None},
    "T_Bkg_Act_08": {"movies": ["Pulp Fiction"], "new_release": False, "sku": None},
    "T_Bkg_Act_09": {"movies": ["Con Air"], "new_release": False, "sku": None},
    "T_Bkg_Act_10": {"movies": ["Face/Off"], "new_release": False, "sku": None},
    "T_Bkg_Act_11": {"movies": ["The Matrix"], "new_release": False, "sku": None},
    "T_Bkg_Act_12": {"movies": ["Jaws: The Revenge"], "new_release": False, "sku": None},
    "T_Bkg_Act_13": {"movies": ["Jaws: The Revenge"], "new_release": False, "sku": None},
    "T_Bkg_Act_14": {"movies": ["Terminator 2: Judgment Day"], "new_release": False, "sku": None},
    "T_Bkg_Act_15": {"movies": ["True Lies"], "new_release": False, "sku": None},
    # Horror - one primary title per slot
    "T_Bkg_Hor_01": {"movies": ["The Sixth Sense"], "new_release": False, "sku": None},
    "T_Bkg_Hor_02": {"movies": ["Lawnmower Man 2: Beyond Cyberspace"], "new_release": False, "sku": None},
    "T_Bkg_Hor_03": {"movies": ["Troll 2"], "new_release": False, "sku": None},
    "T_Bkg_Hor_04": {"movies": ["Bram Stoker's Dracula"], "new_release": False, "sku": None},
    "T_Bkg_Hor_05": {"movies": ["Hellraiser"], "new_release": False, "sku": None},
    "T_Bkg_Hor_06": {"movies": ["Carnosaur"], "new_release": False, "sku": None},
    "T_Bkg_Hor_07": {"movies": ["Misery"], "new_release": False, "sku": None},
    "T_Bkg_Hor_08": {"movies": ["Scream"], "new_release": False, "sku": None},
    "T_Bkg_Hor_09": {"movies": ["Army of Darkness"], "new_release": False, "sku": None},
    "T_Bkg_Hor_10": {"movies": ["The Silence of the Lambs"], "new_release": False, "sku": None},
    "T_Bkg_Hor_11": {"movies": ["I Know What You Did Last Summer"], "new_release": False, "sku": None},
    "T_Bkg_Hor_12": {"movies": ["Soultaker"], "new_release": False, "sku": None},
    "T_Bkg_Hor_13": {"movies": ["A Nightmare on Elm Street"], "new_release": False, "sku": None},
    "T_Bkg_Hor_14": {"movies": ["Friday the 13th"], "new_release": False, "sku": None},
    "T_Bkg_Hor_15": {"movies": ["Jason Goes to Hell: The Final Friday"], "new_release": False, "sku": None},
    "T_Bkg_Hor_16": {"movies": ["Scream 2"], "new_release": False, "sku": None},
    "T_Bkg_Hor_17": {"movies": ["Event Horizon"], "new_release": False, "sku": None},
    "T_Bkg_Hor_18": {"movies": ["Cube"], "new_release": False, "sku": None},
    "T_Bkg_Hor_19": {"movies": ["The Shining"], "new_release": False, "sku": None},
    "T_Bkg_Hor_20": {"movies": ["The Blair Witch Project"], "new_release": False, "sku": None},
    "T_Bkg_Hor_21": {"movies": ["Interview with the Vampire"], "new_release": False, "sku": None},
    "T_Bkg_Hor_22": {"movies": ["From Dusk till Dawn"], "new_release": False, "sku": None},
    # New Releases (all have unique single titles)
    "T_New_Act_01": {"movies": ["Air Force One"], "new_release": True, "sku": 22664},
    "T_New_Act_02": {"movies": ["Tomorrow Never Dies"], "new_release": True, "sku": 19654},
    "T_New_Act_03": {"movies": ["GoldenEye"], "new_release": True, "sku": 13334},
    "T_New_Com_01": {"movies": ["Happy Gilmore"], "new_release": True, "sku": 27593},
    "T_New_Dra_01": {"movies": ["Fear and Loathing in Las Vegas"], "new_release": True, "sku": 22834},
    "T_New_Dra_02": {"movies": ["Meet Joe Black"], "new_release": True, "sku": 19759},
    "T_New_Dra_03": {"movies": ["Robin Hood: Prince of Thieves"], "new_release": True, "sku": 10693},
    "T_New_Fan_01": {"movies": ["Addams Family Values"], "new_release": True, "sku": 45691},
    "T_New_Fan_02": {"movies": ["Death Becomes Her"], "new_release": True, "sku": 16064},
    "T_New_Hor_01": {"movies": ["Stir of Echoes"], "new_release": True, "sku": 21599},
    "T_New_Hor_02": {"movies": ["Jacob's Ladder"], "new_release": True, "sku": 14442},
    "T_New_Hor_03": {"movies": ["The Faculty"], "new_release": True, "sku": 48927},
    "T_New_Hor_04": {"movies": ["The Craft"], "new_release": True, "sku": 12286},
    "T_New_Kid_01": {"movies": ["Babe"], "new_release": True, "sku": 23336},
    "T_New_Pol_01": {"movies": ["The Lost World: Jurassic Park"], "new_release": True, "sku": 18610},
    "T_New_Sci_01": {"movies": ["Armageddon"], "new_release": True, "sku": 48825},
    "T_New_Sci_02": {"movies": ["Star Wars: Episode I The Phantom Menace"], "new_release": True, "sku": 26621},
    "T_New_Sci_03": {"movies": ["The Puppet Masters"], "new_release": True, "sku": 22211},
    "T_New_Sci_04": {"movies": ["Alien Resurrection"], "new_release": True, "sku": 27356},
    "T_New_Xma_01": {"movies": ["The Long Kiss Goodnight"], "new_release": True, "sku": 23771},
}


# ============================================================
# DATATABLE MAPPING - which DataTable file each genre uses
# ============================================================

GENRE_DATATABLE = {
    "Action":           "Action",
    "Adult":            "Adult",
    "Adventure":        "Adventure",
    "Comedy":           "Comedy",
    "Drama":            "Drama",
    "Fantasy":          "Fantasy",
    "Horror":           "Horror",
    "Kids":             "Kid",
    "Police":           "Police",
    "Romance":          "Romance",
    "Sci-Fi":           "Sci-Fi",
    "Western":          "Western",
    "Xmas":             "Xmas",
    "Thriller":         "Thriller",
    "Music":            "Music",
    "History":          "History",
    "Documentary":      "Documentary",
    "Sports":           "Sports",
}
# New release textures all use this DataTable
NEW_RELEASE_DATATABLE = "NewRelease_Details_-_Data"

DATATABLE_PATH = "RetroRewind/Content/VideoStore/core/blueprint/data"

# New custom genres do not have base-game DataTable/background assets.
# These maps select real base-game donors for structural cloning only.
# GENRE_DATATABLE still maps each genre to its own output table name.
DT_TEMPLATE_FOR_GENRE = {
    "Thriller": "Action",
    "Music": "Action",
    "History": "Action",
    "Documentary": "Action",
    "Sports": "Action",
    "Adventure": "Action",
}

# Movie DataTable genre byte values. Keys are DataTable names, not UI labels.
# These bytes are what the in-game info panel reads for the genre badge.
FILM_DATATABLE_GENRE_BYTE = {
    "Action": 0x01,
    "Adventure": 0x02,
    "Comedy": 0x03,
    "Drama": 0x04,
    "Horror": 0x05,
    "Sci-Fi": 0x06,
    "Fantasy": 0x07,
    "Thriller": 0x08,
    "Music": 0x09,
    "Romance": 0x0A,
    "History": 0x0B,
    "Kid": 0x0C,
    "Documentary": 0x0D,
    "Police": 0x0E,
    "Sports": 0x0F,
    "Adult": 0x10,
    "Western": 0x11,
    "Xmas": 0x12,
}

T_BKG_TEMPLATE_FOR_CODE = {
    "Thr": "Act",
    "Mus": "Act",
    "His": "Act",
    "Doc": "Act",
    "Spo": "Act",
}

# ============================================================
# CONFIG
# ============================================================

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        # Clear paths that don't exist on this machine (e.g. from a different install)
        for key in ["texconv", "repak", "base_game_pak", "mods_folder"]:
            val = cfg.get(key, "")
            if val and not os.path.exists(val):
                cfg[key] = ""
        return cfg
    return {}

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)

def load_edited_slots():
    """Load the set of texture names edited since last successful build."""
    try:
        if os.path.exists(EDITED_SLOTS_FILE):
            with open(EDITED_SLOTS_FILE) as f:
                return set(json.load(f))
    except Exception:
        pass
    return set()

def save_edited_slots(edited: set):
    """Persist the edited-slots set to disk."""
    try:
        with open(EDITED_SLOTS_FILE, 'w') as f:
            json.dump(sorted(edited), f)
    except Exception as e:
        print(f"[edited_slots] save error: {e}")

def clear_edited_slots():
    """Clear all edited indicators — called after successful build+install."""
    try:
        if os.path.exists(EDITED_SLOTS_FILE):
            os.remove(EDITED_SLOTS_FILE)
    except Exception:
        pass
    return set()

def load_shipped_slots():
    """Load the set of texture names that have been successfully built at least once."""
    try:
        if os.path.exists(SHIPPED_SLOTS_FILE):
            with open(SHIPPED_SLOTS_FILE) as f:
                return set(json.load(f))
    except Exception:
        pass
    return set()

def save_shipped_slots(shipped: set):
    """Persist the shipped-slots set to disk."""
    try:
        with open(SHIPPED_SLOTS_FILE, 'w') as f:
            json.dump(sorted(shipped), f)
    except Exception as e:
        print(f"[shipped_slots] save error: {e}")


def load_replacements():
    if os.path.exists(REPLACE_FILE):
        with open(REPLACE_FILE) as f:
            data = json.load(f)
        # Migrate 2-digit T_Bkg keys to 3-digit
        migrated = {}
        changed = False
        for key, val in data.items():
            new_key = _remap_slot_to_3digit(key) if key.startswith("T_Bkg_") else key
            if new_key != key:
                changed = True
                print(f"[Migration] Replacement key: {key} → {new_key}")
            migrated[new_key] = val
        if changed:
            with open(REPLACE_FILE, 'w') as f:
                json.dump(migrated, f, indent=2)
        return migrated
    return {}

def migrate_nr_replacements(replacements):
    """Remove all stale T_New_* keys from replacements.
    The NR system now uses NR_{sku} keys which are stable across genre changes.
    Old T_New_* keys cannot be reliably mapped and are removed."""
    stale = [k for k in replacements if k.startswith("T_New_")]
    if stale:
        for k in stale:
            del replacements[k]
        save_replacements(replacements)
        print(f"[Migration] Removed {len(stale)} stale T_New_* key(s)")
    return replacements

def save_replacements(r):
    with open(REPLACE_FILE, 'w') as f:
        json.dump(r, f, indent=2)

def load_title_changes():
    """Load persisted title changes: {original_title: new_title}"""
    if os.path.exists(TITLES_FILE):
        with open(TITLES_FILE) as f:
            return json.load(f)
    return {}

def save_title_changes(t):
    with open(TITLES_FILE, 'w') as f:
        json.dump(t, f, indent=2)

# ── Sort preferences (per-tab) ────────────────────────────────────────
# Stored as a dict: { "<tab name>": "<sort_key>", ... }
# Tab name is either a genre name (e.g. "Action", "Horror") or the
# special key "New Releases" for the NR tab. Each tab keeps its own
# preference independently.
# sort_key values: "name_asc", "name_desc",
#                  "created_asc", "created_desc",
#                  "edited_asc", "edited_desc"
# Default for any tab with no saved entry: "created_asc" (oldest first).
def load_sort_prefs() -> dict:
    """Load per-tab sort preferences. Returns {} on missing/corrupted file."""
    try:
        if os.path.exists(SORT_PREFS_FILE):
            with open(SORT_PREFS_FILE) as f:
                d = json.load(f)
                if isinstance(d, dict):
                    return d
    except Exception:
        pass
    return {}


def save_sort_prefs(prefs: dict):
    """Persist per-tab sort preferences to disk."""
    try:
        with open(SORT_PREFS_FILE, 'w') as f:
            json.dump(prefs, f, indent=2)
    except Exception as e:
        print(f"[sort_prefs] save error: {e}")


# ── Timestamp helpers ─────────────────────────────────────────────────
# Custom slots may carry two optional ISO-8601 string fields:
#   created_at     — set ONCE on creation, never updated
#   last_edited_at — updated when the user finishes editing a slot
# Slots created before this feature shipped have neither field; sort
# logic falls back to slot-number order for those.
def _now_iso() -> str:
    """Return the current local time as an ISO-8601 string (no microseconds)."""
    import datetime as _dt
    return _dt.datetime.now().replace(microsecond=0).isoformat()


# =============================================================================
# MOVIE LIST SORT FEATURE  (added v1.8.2)
# =============================================================================
#
# Per-tab sort controls in the GENRE SHELF header. Six modes:
#   Name ▴/▾, Created at ▴/▾, Last edited ▴/▾
#
# Default for every tab: "Created at ▴" (oldest first). This preserves the
# legacy slot-creation-order behavior for users with existing libraries —
# slots without timestamps fall back to slot-number ordering, so the list
# looks identical to before until users start adding/editing.
#
# Visibility rules:
#   - Visible on every single-genre tab AND on the "New Releases" tab
#   - Hidden only on the "All Movies" tab (multi-genre overview)
#   - Sort dropdown popup positions itself directly below the trigger button
#
# Files / persistence:
#   sort_preferences.json   — per-tab saved sort key, e.g.
#                             { "Action": "name_asc",
#                               "Horror": "edited_desc",
#                               "New Releases": "created_desc" }
#
# Per-slot timestamp fields (optional, in both custom_slots.json and
# nr_custom_slots.json):
#   created_at:     ISO-8601 string, set ONCE on slot creation, never updated
#   last_edited_at: ISO-8601 string, written when the user navigates away
#                   from a slot they edited (selection change, tab switch,
#                   Build). "Edits" that count: title, cover image, rating,
#                   rarity, layout, standee type. Mere viewing does NOT
#                   update it.
#
# Sort behavior:
#   - "New Movie" placeholder titles are NOT special-cased — they sort with
#     everyone else, alphabetically by title (so they land in the N-block on
#     name sort) or by their actual timestamps on date sorts.
#   - Date sorts split entries into two ranks: timestamped first, no-timestamp
#     fallback second. Direction (▴/▾) flips ordering within each rank but
#     does NOT cause no-timestamp entries to overtake timestamped ones.
#   - For NR slots without timestamps, the fallback ordering uses
#     (genre tab position, tex_num) — so NRs sort by their genre tab order
#     first, then by their texture slot number within each genre.
# =============================================================================

# ── Sort option metadata ──────────────────────────────────────────────
# Each option has:
#   key:       stored in sort_preferences.json
#   label:     shown on the trigger button (without the arrow)
#   arrow:     "▴" (ascending) or "▾" (descending)
#   field:     the comparator dimension — "name" / "created" / "edited"
#   ascending: True if ▴ else False
SORT_OPTIONS = [
    {"key": "name_asc",     "label": "Name",          "arrow": "▴",
     "field": "name",       "ascending": True},
    {"key": "name_desc",    "label": "Name",          "arrow": "▾",
     "field": "name",       "ascending": False},
    {"key": "created_asc",  "label": "Created at",    "arrow": "▴",
     "field": "created",    "ascending": True},
    {"key": "created_desc", "label": "Created at",    "arrow": "▾",
     "field": "created",    "ascending": False},
    {"key": "edited_asc",   "label": "Last edited",   "arrow": "▴",
     "field": "edited",     "ascending": True},
    {"key": "edited_desc",  "label": "Last edited",   "arrow": "▾",
     "field": "edited",     "ascending": False},
]

DEFAULT_SORT_KEY = "created_asc"   # oldest first — preserves legacy order


def _get_sort_option(key: str) -> dict:
    """Return the SORT_OPTIONS entry for a key, falling back to default."""
    for opt in SORT_OPTIONS:
        if opt["key"] == key:
            return opt
    return next(o for o in SORT_OPTIONS if o["key"] == DEFAULT_SORT_KEY)


# Fast slot lookup cache. The old path repeatedly scanned every DataTable slot
# while filtering, searching, sorting, and drawing rows. This cache rebuilds
# when a slot-list length or list identity changes, while still returning
# the live slot dicts so title/rating edits are visible immediately.
_SLOT_INDEX_CACHE = {"sig": None, "by_genre_bkg": {}}


def invalidate_slot_lookup_cache():
    """Force the slot lookup cache to rebuild on the next request."""
    _SLOT_INDEX_CACHE["sig"] = None
    _SLOT_INDEX_CACHE["by_genre_bkg"] = {}


def _slot_index_signature():
    parts = []
    seen_dt = set()
    for genre, dt in GENRE_DATATABLE.items():
        if dt in seen_dt:
            continue
        seen_dt.add(dt)
        slots = CLEAN_DT_SLOT_DATA.get(dt, [])
        parts.append((dt, id(slots), len(slots)))
    parts.append(("NR", id(NR_SLOT_DATA), len(NR_SLOT_DATA)))
    return tuple(parts)


def _slot_lookup():
    sig = _slot_index_signature()
    if _SLOT_INDEX_CACHE["sig"] == sig:
        return _SLOT_INDEX_CACHE["by_genre_bkg"]

    by_genre_bkg = {}
    for genre, dt in GENRE_DATATABLE.items():
        slots = CLEAN_DT_SLOT_DATA.get(dt, [])
        for idx, slot in enumerate(slots):
            bkg = slot.get("bkg_tex")
            if bkg:
                by_genre_bkg[(genre, bkg)] = (slot, idx)

    _SLOT_INDEX_CACHE["sig"] = sig
    _SLOT_INDEX_CACHE["by_genre_bkg"] = by_genre_bkg
    return by_genre_bkg


def _slot_for_texture(texture: dict):
    """Resolve a shelf texture dict to its underlying CLEAN_DT_SLOT_DATA entry.

    Returns (slot_dict, slot_index) — slot_index is 0-based within its DT.
    Uses the module-level slot lookup cache so shelf rebuilds remain fast even
    with thousands of custom movies.
    """
    if not texture:
        return None, -1
    key = (texture.get("genre", ""), texture.get("name", ""))
    return _slot_lookup().get(key, (None, -1))


def _sort_textures(textures: list, sort_key: str) -> list:
    """Sort a list of texture dicts (genre slots) according to sort_key.

    Rules:
      - All entries sort uniformly by the chosen field. Movies whose title
        is still the default "New Movie" are NOT special-cased — they sort
        with everyone else (alphabetically by title, or by their timestamps
        / slot index in the date sort modes).
      - For date sorts, slots without the relevant timestamp (legacy data
        from before this feature) fall back to slot-number ordering, after
        the timestamped entries — preserving original list order for
        libraries that pre-date this feature.
      - Direction (▴/▾) flips order WITHIN each rank, but does NOT put
        no-timestamp entries before timestamped ones.
    """
    opt = _get_sort_option(sort_key)
    field = opt["field"]
    asc = opt["ascending"]

    # Pre-resolve each texture to its slot data and timestamps.
    resolved = []
    for t in textures:
        slot, idx = _slot_for_texture(t)
        if slot is None:
            # Defensive fallback for textures with no matching slot data.
            resolved.append({
                "tex": t, "idx": idx, "title": "",
                "ts_c": None, "ts_e": None,
            })
            continue
        resolved.append({
            "tex":   t,
            "idx":   idx,
            "title": slot.get("pn_name", "") or "",
            "ts_c":  slot.get("created_at") or None,
            "ts_e":  slot.get("last_edited_at") or None,
        })

    if field == "name":
        # Stable secondary key: slot idx so identical titles keep insert order.
        resolved.sort(key=lambda r: (r["title"].lower(), r["idx"]),
                      reverse=not asc)
        return [r["tex"] for r in resolved]

    # Date sorts: split has-ts vs no-ts BEFORE applying direction so the
    # "has-ts always before no-ts" invariant holds in both directions.
    ts_field = "ts_c" if field == "created" else "ts_e"
    with_ts = [r for r in resolved if r[ts_field]]
    no_ts   = [r for r in resolved if not r[ts_field]]
    # ISO-8601 sorts lexicographically same as chronologically.
    with_ts.sort(key=lambda r: (r[ts_field], r["idx"]), reverse=not asc)
    no_ts.sort(key=lambda r: r["idx"], reverse=not asc)
    return [r["tex"] for r in (with_ts + no_ts)]


def _sort_nr_slots(nr_slots: list, sort_key: str) -> list:
    """Sort an enumerated list of NR slots according to sort_key.

    Input format: list of (original_index, nr_slot_dict) tuples — the
    original index is preserved through sorting because the NR rendering
    code uses it as the click-target identity in NR_SLOT_DATA.

    Rules mirror _sort_textures, with one specific fallback:
      - For date sorts on slots WITHOUT the relevant timestamp, fall back
        to (genre_position_in_GENRES, tex_num) — i.e. follow the genre tab
        order, then numeric NR slot within each genre. e.g. with
        Action=0, Comedy=3, Drama=4 in GENRES order:
          [Dra_01, Com_01, Hor_02, Dra_02, Com_02]   ascending becomes
          [Com_01, Com_02, Dra_01, Dra_02, Hor_02]
    """
    opt = _get_sort_option(sort_key)
    field = opt["field"]
    asc = opt["ascending"]

    # Build a stable genre-rank map matching the GENRES dict insertion order.
    # GENRES is the authoritative tab order (see _build_ui's genres_display).
    _genre_rank = {g: i for i, g in enumerate(GENRES.keys())}

    def _genre_pos(nr):
        return _genre_rank.get(nr.get("genre", ""), 999)

    def _tex_num(nr):
        return nr.get("tex_num", 0) or 0

    if field == "name":
        # Sort by NR title (user-visible name).
        result = sorted(
            nr_slots,
            key=lambda kv: ((kv[1].get("title", "") or "").lower(),
                            _genre_pos(kv[1]), _tex_num(kv[1])),
            reverse=not asc,
        )
        return result

    # Date sorts.
    ts_field = "created_at" if field == "created" else "last_edited_at"
    with_ts = [(i, n) for (i, n) in nr_slots if n.get(ts_field)]
    no_ts   = [(i, n) for (i, n) in nr_slots if not n.get(ts_field)]
    # ISO-8601 sorts lexicographically same as chronologically.
    with_ts.sort(key=lambda kv: (kv[1][ts_field],
                                 _genre_pos(kv[1]), _tex_num(kv[1])),
                 reverse=not asc)
    # Fallback for legacy NRs: alphabetical by genre tab order, then tex_num.
    no_ts.sort(key=lambda kv: (_genre_pos(kv[1]), _tex_num(kv[1])),
               reverse=not asc)
    return with_ts + no_ts


def load_shipped_slots():
    """Load the set of texture names that have been successfully built at least once."""
    try:
        if os.path.exists(SHIPPED_SLOTS_FILE):
            with open(SHIPPED_SLOTS_FILE) as f:
                return set(json.load(f))
    except Exception:
        pass
    return set()

def save_shipped_slots(shipped: set):
    """Persist the shipped-slots set to disk."""
    try:
        with open(SHIPPED_SLOTS_FILE, 'w') as f:
            json.dump(sorted(shipped), f)
    except Exception as e:
        print(f"[shipped_slots] save error: {e}")


def load_replacements():
    if os.path.exists(REPLACE_FILE):
        with open(REPLACE_FILE) as f:
            data = json.load(f)
        # Migrate 2-digit T_Bkg keys to 3-digit
        migrated = {}
        changed = False
        for key, val in data.items():
            new_key = _remap_slot_to_3digit(key) if key.startswith("T_Bkg_") else key
            if new_key != key:
                changed = True
                print(f"[Migration] Replacement key: {key} → {new_key}")
            migrated[new_key] = val
        if changed:
            with open(REPLACE_FILE, 'w') as f:
                json.dump(migrated, f, indent=2)
        return migrated
    return {}

def migrate_nr_replacements(replacements):
    """Remove all stale T_New_* keys from replacements.
    The NR system now uses NR_{sku} keys which are stable across genre changes.
    Old T_New_* keys cannot be reliably mapped and are removed."""
    stale = [k for k in replacements if k.startswith("T_New_")]
    if stale:
        for k in stale:
            del replacements[k]
        save_replacements(replacements)
        print(f"[Migration] Removed {len(stale)} stale T_New_* key(s)")
    return replacements

def save_replacements(r):
    with open(REPLACE_FILE, 'w') as f:
        json.dump(r, f, indent=2)

def load_title_changes():
    """Load persisted title changes: {original_title: new_title}"""
    if os.path.exists(TITLES_FILE):
        with open(TITLES_FILE) as f:
            return json.load(f)
    return {}

def save_title_changes(t):
    with open(TITLES_FILE, 'w') as f:
        json.dump(t, f, indent=2)

# ============================================================
# DXT1 DECODER - reads ubulk directly, no FModel needed
# ============================================================

def decode_dxt1_block(block_bytes):
    c0   = struct.unpack_from('<H', block_bytes, 0)[0]
    c1   = struct.unpack_from('<H', block_bytes, 2)[0]
    bits = struct.unpack_from('<I', block_bytes, 4)[0]

    def rgb565(c):
        return (((c>>11)&0x1f)*255//31, ((c>>5)&0x3f)*255//63, (c&0x1f)*255//31)

    c0r, c1r = rgb565(c0), rgb565(c1)
    if c0 > c1:
        cols = [c0r, c1r,
                tuple((2*a+b)//3 for a,b in zip(c0r,c1r)),
                tuple((a+2*b)//3 for a,b in zip(c0r,c1r))]
    else:
        cols = [c0r, c1r,
                tuple((a+b)//2 for a,b in zip(c0r,c1r)),
                (0,0,0)]

    return [cols[(bits >> (2*(row*4+col))) & 0x3]
            for row in range(4) for col in range(4)]

def decode_dxt1(data, width, height):
    img     = Image.new('RGB', (width, height))
    pixels  = img.load()
    bx_cnt  = width  // 4
    by_cnt  = height // 4
    offset  = 0
    for by in range(by_cnt):
        for bx in range(bx_cnt):
            block = data[offset:offset+8]
            offset += 8
            bp = decode_dxt1_block(block)
            for row in range(4):
                for col in range(4):
                    px, py = bx*4+col, by*4+row
                    if px < width and py < height:
                        pixels[px, py] = bp[row*4+col]
    return img

def ubulk_to_image(ubulk_data, width=TEX_WIDTH, height=TEX_HEIGHT):
    """Decode raw DXT1 ubulk data to a PIL Image."""
    mip_size = (width//4) * (height//4) * 8
    return decode_dxt1(ubulk_data[:mip_size], width, height)


# ============================================================
# TEXTURE SLOT CLONING
# Clones a T_Bkg uasset from src_slot_num → dst_slot_num.
# 
# CRITICAL — UE5 FName encoding for VHS background textures:
#   Slots 01..09 (leading zero): FName stored LITERALLY as "T_Bkg_Hor_0N"
#   Slots 10..78 (no leading zero): FName stored as base="T_Bkg_Hor_" + stored_number=(N+1)
#     because UE5 adds 1 to the display number before serializing.
#     e.g. "T_Bkg_Hor_22" → stored_number=23=0x17000000 LE
#          "T_Bkg_Hor_23" → stored_number=24=0x18000000 LE
#
# Cloning _01 → _23 produces a uasset with the WRONG FName encoding (literal "T_Bkg_Hor_23"
# instead of number-suffix form), so the game can't match it to its asset registry entry
# and ignores the file entirely — it falls back to whatever it had before.
#
# CORRECT approach:
#   - For slots 10+: clone from the immediately preceding slot (same encoding family).
#     Patch the path FString digits AND the FName stored_number fields.
#   - For slots 01-09: clone _01 and do a literal byte replacement (original behaviour).
# ============================================================

def clone_texture_3digit(src_data, src_code, src_num, dst_code, dst_num):
    """
    Clone a T_Bkg/T_New uasset to a new slot number.

    Two FName encoding styles based on destination slot number:
    - Slots 1-99: LITERAL name in name table (e.g. 'T_Bkg_Dra_001'),
      stored_number=0. Matches base game style for slots 01-09.
    - Slots 100+: BASE name in name table (e.g. 'T_Bkg_Dra'),
      stored_number=N+1. Matches base game style for slots 10+.

    The PackageName FString always contains the full literal path and
    may change length. The file is rebuilt section-by-section when needed.
    """
    import struct as _s

    data = bytearray(src_data)
    prefix = "T_New" if b"T_New_" in data[:0x90] else "T_Bkg"
    src_folder = f"T_Bkg_{src_code}"
    dst_folder = f"T_Bkg_{dst_code}"

    old_short = (f"{prefix}_{src_code}_{src_num:02d}" if src_num < 100
                 else f"{prefix}_{src_code}_{src_num}")
    new_short = (f"{prefix}_{dst_code}_{dst_num:03d}" if dst_num < 100
                 else f"{prefix}_{dst_code}_{dst_num}")

    old_path = (f"/Game/VideoStore/asset/prop/vhs/Background"
                f"/{src_folder}/{old_short}")
    new_path = (f"/Game/VideoStore/asset/prop/vhs/Background"
                f"/{dst_folder}/{new_short}")

    use_literal = (dst_num < 100)

    # --- Parse structure ---
    pkg_len = _s.unpack_from('<i', data, 0x20)[0]
    fse = 0x24 + pkg_len
    name_count = _s.unpack_from('<i', data, fse + 4)[0]
    name_offset = _s.unpack_from('<i', data, fse + 8)[0]

    pos = name_offset
    names = []
    for _ in range(name_count):
        if pos + 4 > len(data): break
        slen = _s.unpack_from('<i', data, pos)[0]
        if slen <= 0 or slen > 500: break
        names.append(data[pos+4:pos+4+slen-1].decode('utf-8', 'replace'))
        pos += 4 + slen + 4

    src_base = f"{prefix}_{src_code}"
    dst_base = f"{prefix}_{dst_code}"

    # Determine target name table entries
    if use_literal:
        # Full literal name in name table, stored_number = 0
        dst_path_entry = f"/Game/VideoStore/asset/prop/vhs/Background/{dst_folder}/{new_short}"
        dst_short_entry = new_short
        dst_stored_number = 0
    else:
        # Base name in name table, stored_number = N+1
        dst_path_entry = f"/Game/VideoStore/asset/prop/vhs/Background/{dst_folder}/{dst_base}"
        dst_short_entry = dst_base
        dst_stored_number = dst_num + 1

    # --- Rebuild file ---
    new_data = bytearray(data[:0x20])

    # PackageName FString (always full literal path)
    new_pkg = new_path.encode() + b'\x00'
    new_data += _s.pack('<i', len(new_pkg))
    new_data += new_pkg
    new_fse = len(new_data)

    # Header fields
    new_data += data[fse:name_offset]
    new_name_offset = len(new_data)

    # Name table
    nt_pos = name_offset
    for i in range(name_count):
        slen = _s.unpack_from('<i', data, nt_pos)[0]
        s_bytes = data[nt_pos+4:nt_pos+4+slen]
        hash_val = _s.unpack_from('<I', data, nt_pos+4+slen)[0]
        s_str = s_bytes[:-1].decode('utf-8', 'replace')

        # Patch path entry
        if s_str.startswith("/Game/VideoStore/asset/prop/vhs/Background/") and (src_base in s_str or old_short in s_str):
            s_bytes = dst_path_entry.encode() + b'\x00'
            hash_val = 0
        # Patch short name entry
        elif s_str == src_base or s_str == old_short:
            s_bytes = dst_short_entry.encode() + b'\x00'
            hash_val = 0

        new_data += _s.pack('<i', len(s_bytes))
        new_data += s_bytes
        new_data += _s.pack('<I', hash_val)
        nt_pos += 4 + slen + 4

    # Everything after name table
    new_data += data[nt_pos:]

    total_shift = len(new_data) - len(data)

    # Fix offset fields
    _s.pack_into('<i', new_data, new_fse + 8, new_name_offset)
    for rel in [16, 32, 40, 44, 136, 160, 176]:
        abs_pos = new_fse + rel
        if abs_pos + 4 > len(new_data): continue
        old_val = _s.unpack_from('<i', new_data, abs_pos)[0]
        if old_val > 0:
            _s.pack_into('<i', new_data, abs_pos, old_val + total_shift)

    # Fix export entry
    new_export_off = _s.unpack_from('<i', new_data, new_fse + 32)[0]
    if new_export_off + 44 <= len(new_data):
        # Set stored_number
        _s.pack_into('<I', new_data, new_export_off + 20, dst_stored_number)
        # Fix serial_offset
        _s.pack_into('<q', new_data, new_export_off + 36, len(new_data))

    # For base+stored_number style: also patch any other FName pairs
    # (e.g. in import table) that reference the old stored_number
    if not use_literal:
        dst_base_idx = None
        # Re-parse names to find the base name index
        p2 = _s.unpack_from('<i', new_data, new_fse + 8)[0]
        for i in range(name_count):
            if p2 + 4 > len(new_data): break
            sl = _s.unpack_from('<i', new_data, p2)[0]
            if sl <= 0 or sl > 500: break
            s = new_data[p2+4:p2+4+sl-1].decode('utf-8', 'replace')
            if s == dst_base:
                dst_base_idx = i
                break
            p2 += 4 + sl + 4

        if dst_base_idx is not None:
            src_stored = src_num + 1
            src_pair = _s.pack('<II', dst_base_idx, src_stored)
            dst_pair = _s.pack('<II', dst_base_idx, dst_stored_number)
            p = 0
            while True:
                p = bytes(new_data).find(src_pair, p)
                if p < 0: break
                new_data[p:p+8] = dst_pair
                p += 8

    return bytes(new_data)




def patch_datatable_uasset_identity(data, old_name, new_name):
    """Patch a copied DataTable .uasset so its internal package/export identity matches the new DataTable name."""
    import struct as _s

    old_path = f"/Game/VideoStore/core/blueprint/data/{old_name}"
    new_path = f"/Game/VideoStore/core/blueprint/data/{new_name}"

    src = bytearray(data)
    if len(src) < 0x40:
        return bytes(src)

    old_pkg_len = _s.unpack_from("<i", src, 0x20)[0]
    if not (1 < old_pkg_len < 512) or 0x24 + old_pkg_len > len(src):
        return bytes(src)

    old_fse = 0x24 + old_pkg_len
    if old_fse + 36 > len(src):
        return bytes(src)

    name_count = _s.unpack_from("<i", src, old_fse + 4)[0]
    name_offset = _s.unpack_from("<i", src, old_fse + 8)[0]
    if not (0 <= name_count < 10000 and 0 < name_offset < len(src)):
        return bytes(src)

    out = bytearray(src[:0x20])
    new_pkg = new_path.encode("utf-8") + b"\x00"
    out += _s.pack("<i", len(new_pkg))
    out += new_pkg
    new_fse = len(out)

    out += src[old_fse:name_offset]
    new_name_offset = len(out)

    p = name_offset
    new_name_idx = None
    for i in range(name_count):
        if p + 8 > len(src):
            break
        slen = _s.unpack_from("<i", src, p)[0]
        if not (1 <= slen <= 1000) or p + 4 + slen + 4 > len(src):
            break
        raw = bytes(src[p + 4:p + 4 + slen])
        hash_val = _s.unpack_from("<I", src, p + 4 + slen)[0]
        try:
            text = raw[:-1].decode("utf-8") if raw.endswith(b"\x00") else raw.decode("utf-8")
        except Exception:
            text = ""

        patched = text
        if text == old_name:
            patched = new_name
        elif text == old_path:
            patched = new_path
        elif text.endswith("/" + old_name):
            patched = text[: -len(old_name)] + new_name

        if patched != text:
            raw = patched.encode("utf-8") + b"\x00"
            hash_val = 0
        if patched == new_name and new_name_idx is None:
            new_name_idx = i

        out += _s.pack("<i", len(raw))
        out += raw
        out += _s.pack("<I", hash_val)
        p += 4 + slen + 4

    out += src[p:]
    total_shift = len(out) - len(src)

    def _shift_i32(rel):
        pos = new_fse + rel
        if pos + 4 <= len(out):
            val = _s.unpack_from("<i", out, pos)[0]
            if val > 0:
                _s.pack_into("<i", out, pos, val + total_shift)

    _s.pack_into("<i", out, new_fse + 8, new_name_offset)
    for rel in (16, 32, 40, 44, 136, 160, 176):
        _shift_i32(rel)

    try:
        export_off = _s.unpack_from("<i", out, new_fse + 32)[0]
        if new_name_idx is not None and 0 < export_off < len(out) - 24:
            _s.pack_into("<I", out, export_off + 16, new_name_idx)
            _s.pack_into("<I", out, export_off + 20, 0)
    except Exception:
        pass

    return bytes(out)


class CleanDataTableBuilder:
    """
    Builds a clean DataTable uasset+uexp pair for a single genre.

    Usage:
        builder = CleanDataTableBuilder(pak_cache, "Horror")
        ua, ue = builder.build(slot_data, title_overrides)
        # slot_data: list of dicts with keys:
        #   bkg_tex, pn_name, ls, lsc, sku, ntu
        # title_overrides: {original_title: new_title}
    """

    # Row key number that must be non-zero (original game value)
    RK_NUM = 83892096   # 0x05001780

    # SubjectImages used per slot (T_Sub_01..T_Sub_77 = 77 variants).
    # Each slot gets all 77 rows. T_Sub_01 is the first row of every slot,
    # but only slot 1's T_Sub_01 row uses the base-game sentinel title.
    SUB_IMAGES = [f"T_Sub_{i:02d}" for i in range(1, 78)]

    def __init__(self, pak_cache, genre_dt_name):
        self.pak_cache  = pak_cache
        self.dt_name    = genre_dt_name
        self._uasset    = None   # bytearray — loaded once
        self._name_table = []
        self._serial_off = None  # byte offset of serial_size int64 in uasset
        self._templates  = {}    # si_name -> 72-byte row template
        self._used_horror_template = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_base_loaded(self):
        """
        Extract and cache the structural template uasset + uexp for this genre.

        Normal genres extract their own base-game DataTable. New custom genres
        extract a real donor DataTable, then patch the .uasset identity so the
        output package/export is the new genre name.
        """
        if self._uasset is not None:
            return True

        UE4_MAGIC = 0x9E2A83C1

        def try_extract(pak_path, extract_root, label):
            """Extract template uasset+uexp; return (ua_bytes, ue_bytes) or None."""
            template_name = DT_TEMPLATE_FOR_GENRE.get(self.dt_name, self.dt_name)
            dest = os.path.join(extract_root, "RetroRewind", "Content",
                                "VideoStore", "core", "blueprint", "data")
            os.makedirs(dest, exist_ok=True)

            ua_path = os.path.join(dest, f"{template_name}.uasset")
            ue_path = os.path.join(dest, f"{template_name}.uexp")

            for path in (ua_path, ue_path):
                if os.path.exists(path):
                    os.remove(path)

            for ext in ("uasset", "uexp"):
                subprocess.run(
                    [self.pak_cache.repak_path, "unpack",
                     "-o", extract_root, "-f",
                     "-i", f"{DATATABLE_PATH}/{template_name}.{ext}",
                     pak_path],
                    capture_output=True, text=True, timeout=30
                )

            if not os.path.exists(ua_path) or not os.path.exists(ue_path):
                return None

            with open(ua_path, "rb") as f:
                ua = bytearray(f.read())
            with open(ue_path, "rb") as f:
                ue = bytearray(f.read())

            if len(ua) < 4 or struct.unpack_from("<I", ua, 0)[0] != UE4_MAGIC:
                print(f"[CleanDT] {self.dt_name} from {label}: not UE4 binary — skipping")
                return None

            try:
                plen = struct.unpack_from("<i", ua, 0x20)[0]
                pname = ua[0x24:0x24 + plen].decode("utf-8", errors="replace").rstrip("\x00")
                expected_pkg = f"/Game/VideoStore/core/blueprint/data/{template_name}"
                template_part = template_name.replace("-", "")
                if not (10 < plen < 120 and (expected_pkg == pname or template_name in pname or template_part in pname)):
                    print(f"[CleanDT] {self.dt_name} from {label}: "
                          f"PackageName '{pname}' doesn't match template '{template_name}' — skipping")
                    return None

                fse = 0x24 + plen
                name_off = struct.unpack_from("<i", ua, fse + 8)[0]
                export_off = struct.unpack_from("<i", ua, fse + 32)[0]
                if not (0 < name_off < export_off < len(ua)):
                    print(f"[CleanDT] {self.dt_name} from {label}: "
                          f"header sanity fail (name_off={name_off}, export_off={export_off}, "
                          f"file_size={len(ua)}) — skipping")
                    return None
            except Exception:
                return None

            if template_name != self.dt_name:
                ua = bytearray(patch_datatable_uasset_identity(
                    bytes(ua),
                    old_name=template_name,
                    new_name=self.dt_name,
                ))
                print(f"[CleanDT] {self.dt_name}: cloned template {template_name} from {label}")
            else:
                print(f"[CleanDT] {self.dt_name}: loaded template from {label}")

            print(f"[CleanDT] {self.dt_name}: template size {len(ua)} bytes uasset, {len(ue)} bytes uexp")
            return bytes(ua), bytes(ue)

        base_extract = os.path.join(self.pak_cache._extract_dir, "_genre_templates", "base")
        result = try_extract(self.pak_cache.pak_path, base_extract, "base game")

        if result is None:
            print(f"[CleanDT] Could not load template for {self.dt_name} from any pak")
            return False

        ua_bytes, ue_bytes = result
        self._uasset = ua_bytes
        self._uexp_orig = ue_bytes
        self._used_horror_template = False
        self._parse_name_table()
        self._find_serial_offset()
        self._detect_uexp_layout()
        self._build_si_templates()
        return True

    def _parse_name_table(self):
        ua = self._uasset
        # Header fields are SEQUENTIAL after the package name FString.
        # MUST use name_count from header — NOT scan to export_off.
        # Adventure (8271-byte uasset) has 160 bytes between name table end and
        # export table. Those bytes contain import/depends data that happens to
        # look like 10 valid name entries. Using export_off as stop caused 10
        # phantom entries, making working_names have 398 entries while the actual
        # name table had 388. Titles added at index 391+ caused FName index OOB
        # which the engine resolved to 0xFFFFFFFF → TArray resize crash.
        fse         = 0x24 + struct.unpack_from("<i", ua, 0x20)[0]
        name_count  = struct.unpack_from("<i", ua, fse + 4)[0]
        name_offset = struct.unpack_from("<i", ua, fse + 8)[0]
        # Walk exactly name_count entries — authoritative count from header.
        names = []
        i = name_offset
        for _ in range(name_count):
            if i + 4 > len(ua):
                break
            length = struct.unpack_from("<i", ua, i)[0]
            if 1 <= length <= 300:
                end = i + 4 + length
                if end + 4 <= len(ua):
                    raw = bytes(ua[i+4:end])
                    if raw[-1:] == b"\x00":
                        try:
                            text = raw[:-1].decode("utf-8")
                            names.append(text)
                            i = end + 4
                            continue
                        except Exception:
                            pass
            # Malformed entry — stop early
            break
        self._name_table = names

    def _find_serial_offset(self):
        ua = self._uasset
        # original serial_size = len(uexp) - 4
        orig_serial = len(self._uexp_orig) - 4
        for i in range(len(ua) - 8):
            if struct.unpack_from("<q", ua, i)[0] == orig_serial:
                self._serial_off = i
                return
        print(f"[CleanDT] WARNING: serial_size not found in {self.dt_name}.uasset")

    def _detect_uexp_layout(self):
        """
        Detect ROW_START and row-count field offset from the uexp.

        Two uexp header formats exist in this game:
          22-byte header (ROW_START=0x16): row count uint16 at 0x0e
          26-byte header (ROW_START=0x1A): row count uint16 at 0x12
        The 26-byte variant has 4 extra bytes at offset 0, which shifts all
        subsequent fields by 4 including the row count field.

        We locate the first T_Sub FString (always at row offset +20) to find
        ROW_START, then derive row_count_off deterministically from ROW_START.
        """
        ue = self._uexp_orig
        TSUB_ROW_OFF = 20   # T_Sub FString is always at byte 20 within a row

        # Find first T_Sub_ occurrence after the very start
        pos = ue.find(b"T_Sub_", 4)
        if pos < 0:
            print(f"[CleanDT] {self.dt_name}: no T_Sub found in uexp — using default ROW_START=0x16")
            self._row_start      = 0x16
            self._row_count_off  = 0x0e
            self._detected_rk_num = self.RK_NUM
            return

        row_start = pos - TSUB_ROW_OFF
        if row_start < 0:
            row_start = 0x16
        self._row_start = row_start

        # row_count_off is deterministic from ROW_START:
        #   22-byte header (0x16) → row count at 0x0e
        #   26-byte header (0x1A) → row count at 0x12 (shifted by 4 extra header bytes)
        self._row_count_off = 0x12 if row_start == 0x1A else 0x0e

        # Detect the actual RK_NUM from the first row (varies by genre asset).
        # e.g. Horror/Xmas/Comedy = 0x05001780, Western = 0x05201780, Police = 0x04001780
        if row_start + 8 <= len(ue):
            detected_rk = struct.unpack_from("<I", ue, row_start + 4)[0]
            if detected_rk != 0:
                self._detected_rk_num = detected_rk
        else:
            self._detected_rk_num = self.RK_NUM

        # Detect actual row_size and per_slot by walking the first few rows
        rk = self._detected_rk_num
        p = row_start
        prev_bkg = None
        row_sizes = []
        per_slot = 77  # default
        for _ in range(200):
            if p + 40 > len(ue): break
            if struct.unpack_from("<I", ue, p+4)[0] != rk: break
            p2 = p + 16
            si_l = struct.unpack_from("<i", ue, p2)[0]
            if not (0 < si_l <= 20): break
            p2 += 4 + si_l
            bi_l = struct.unpack_from("<i", ue, p2)[0]
            if not (0 < bi_l <= 20): break
            bkg = ue[p2+4:p2+4+bi_l-1].decode("utf-8","replace")
            if prev_bkg is not None and bkg != prev_bkg:
                per_slot = len(row_sizes)
                break
            prev_bkg = bkg
            p2 += 4 + bi_l + 8 + 2 + 12
            found = False
            for skip in range(0, 8):
                test = p2 + skip
                if test + 8 > len(ue): break
                if struct.unpack_from("<I", ue, test+4)[0] == rk:
                    row_sizes.append(test - p)
                    p = test; found = True; break
            if not found: break
        self._row_size  = row_sizes[0] if row_sizes else 72
        self._per_slot  = per_slot

        print(f"[CleanDT] {self.dt_name}: ROW_START=0x{row_start:X}, "
              f"row_count_off=0x{self._row_count_off:X}, RK_NUM=0x{self._detected_rk_num:08X}, "
              f"row_size={self._row_size}, per_slot={self._per_slot}")

    def _build_si_templates(self):
        """
        Build one 72-byte row template per T_Sub_XX (T_Sub_01..T_Sub_77).

        Previously this read templates from an external uexp which had
        real T_Sub_02..77 rows. Now we synthesize all 77 templates from scratch
        using the known fixed row layout, so no mod pak is required.

        The synthesized template uses:
          - Genre byte and SubjectPlacement byte read from the first parseable
            row in the base game uexp (any genre — these are fixed per genre).
          - All other variable fields (title, bkg_tex, ls, lsc, sku) are filled
            in by build() per slot, so placeholder values are fine here.
        """
        ue = self._uexp_orig
        ROW_SIZE = 72
        if not hasattr(self, '_row_start'):
            self._detect_uexp_layout()
        ROW_START = self._row_start

        # Try to read genre_byte and placement_byte from the first base game row.
        # Row layout offsets relative to row start:
        #   [16] SI FString len (int32), [20] SI string
        #   [29] BI FString len (int32), [33] BI string
        #   After BI string: SubjectName FName (8), genre_byte (1), placement (1)
        genre_byte  = 4   # Horror default (NewEnumerator4)
        placement   = 1   # SubjectPlacement::NewEnumerator1
        total_rows  = (len(ue) - ROW_START - 8) // ROW_SIZE
        for rn in range(min(total_rows, 30)):
            off    = ROW_START + rn * ROW_SIZE
            si_len = struct.unpack_from("<i", ue, off + 16)[0]
            if not (7 <= si_len <= 12):
                continue
            bi_off = off + 20 + si_len
            if bi_off + 4 > len(ue):
                continue
            bi_len = struct.unpack_from("<i", ue, bi_off)[0]
            if not (8 <= bi_len <= 20):
                continue
            after_bi = bi_off + 4 + bi_len
            if after_bi + 10 > len(ue):
                continue
            genre_byte = ue[after_bi + 8]
            placement  = ue[after_bi + 9]
            self._genre_byte = genre_byte
            self._placement  = placement
            print(f"[CleanDT] {self.dt_name}: genre_byte=0x{genre_byte:02X} "
                  f"placement=0x{placement:02X} (from base game row {rn})")
            break

        # Override the donor row's genre byte with the actual output DataTable genre.
        # New genres are cloned from Action/Drama donors, so without this they display as the donor genre in-game.
        forced_genre_byte = FILM_DATATABLE_GENRE_BYTE.get(self.dt_name)
        if forced_genre_byte is not None and forced_genre_byte != genre_byte:
            print(f"[CleanDT] {self.dt_name}: forcing genre_byte 0x{genre_byte:02X} -> 0x{forced_genre_byte:02X}")
            genre_byte = forced_genre_byte
            self._genre_byte = genre_byte
        else:
            self._genre_byte = genre_byte

        # Synthesize one template per T_Sub_01..T_Sub_77.
        # The 72-byte row structure (fixed-length because SI is always "T_Sub_XX\0" = 9 bytes
        # and BI is always "T_Bkg_XXX_YY\0" = 13 bytes):
        #   [0:8]   RowKey FName        — filled by build()
        #   [8:16]  ProductName FName   — filled by build()
        #   [16:20] SI FString len = 9
        #   [20:29] SI string "T_Sub_XX\0"
        #   [29:33] BI FString len = 13
        #   [33:46] BI string "T_Bkg_XXX_YY\0"  — filled by build()
        #   [46:54] SubjectName FName   — filled by build()
        #   [54]    genre_byte
        #   [55]    placement
        #   [56:60] LayoutStyle int32   — filled by build()
        #   [60:64] LayoutStyleColor int32 — filled by build()
        #   [64:68] SKU int32           — filled by build()
        #   [68:70] NextRowKeyIdx uint16 — filled by build()
        #   [70:72] padding 0x0000
        for i in range(1, 78):
            si_name  = f"T_Sub_{i:02d}"
            si_bytes = si_name.encode("utf-8") + b"\x00"  # 9 bytes
            tmpl = bytearray(72)
            # SI FString
            struct.pack_into("<i", tmpl, 16, 9)
            tmpl[20:29] = si_bytes
            # BI FString length placeholder (13) and placeholder name (overwritten by build)
            struct.pack_into("<i", tmpl, 29, 13)
            tmpl[33:46] = b"T_Bkg_Hor_01\x00"  # placeholder; build() overwrites
            # genre and placement
            tmpl[54] = genre_byte
            tmpl[55] = placement
            self._templates[si_name] = bytes(tmpl)

        print(f"[CleanDT] {self.dt_name}: synthesized {len(self._templates)} SI templates")

    def _name_idx(self, text):
        """Return name table index for text, or 0 if not found."""
        try:
            return self._name_table.index(text)
        except ValueError:
            return 0

    def read_slot_data(self):
        """
        Read the existing DataTable rows and return a slot_data list
        (one entry per unique BKG texture) in the same format as HORROR_SLOT_DATA.

        This is used to auto-populate CLEAN_DT_SLOT_DATA for genres other than Horror
        so they can use the clean rebuild path without hardcoded slot data.

        Returns list of dicts: {bkg_tex, pn_name, ls, lsc, sku, ntu}
        or None if the DataTable could not be loaded.
        """
        if not self._ensure_base_loaded():
            return None

        ue   = self._uexp_orig
        ua   = self._uasset
        ROW_START = getattr(self, '_row_start', 0x16)

        # Walk rows one by one, parsing variable-length fields.
        # Row layout:
        #   [0:8]   RowKey FName (8 bytes)
        #   [8:16]  ProductName FName (8 bytes)
        #   [16:20] SubjectImage FString length (int32)
        #   [20:20+si_len] SubjectImage string (si_len bytes incl null)
        #   [20+si_len:24+si_len] BackgroundImage FString length (int32)
        #   [24+si_len:24+si_len+bi_len] BackgroundImage string (bi_len bytes incl null)
        #   ... SubjectName FName, Genre, SubjectPlacement, LS, LSC, SKU, NextKey, pad

        slots     = []
        seen_bkg  = set()
        pos       = ROW_START
        max_pos   = len(ue) - 8  # leave room for footer

        while pos < max_pos:
            # Safety: need at least 20 bytes to read up to si_len
            if pos + 20 > len(ue):
                break

            pn_idx  = struct.unpack_from("<i", ue, pos + 8)[0]
            si_flen = struct.unpack_from("<i", ue, pos + 16)[0]

            # Validate SI FString length: must be 7..12 for T_Sub_01..T_Sub_99
            if not (7 <= si_flen <= 12):
                break

            bi_off  = pos + 20 + si_flen
            if bi_off + 4 > len(ue):
                break

            bi_flen = struct.unpack_from("<i", ue, bi_off)[0]

            # Validate BI FString length: T_Bkg_XXX_NN = 12+1=13, T_Bkg_XXX_N = 11+1=12
            if not (8 <= bi_flen <= 20):
                break

            bkg_start = bi_off + 4
            bkg_end   = bkg_start + bi_flen
            if bkg_end > len(ue):
                break

            bkg = ue[bkg_start:bkg_end - 1].decode("utf-8", errors="replace")

            # Validate: BKG must start with T_Bkg_
            if not bkg.startswith("T_Bkg_"):
                break

            si_str = ue[pos + 20:pos + 20 + si_flen - 1].decode("utf-8", errors="replace")

            # Skip T_Sub_01 bridge/sentinel rows
            if si_str != "T_Sub_01" and bkg not in seen_bkg:
                # Fields after BKG string:
                # [bkg_end:bkg_end+8]  SubjectName FName
                # [bkg_end+8]          Genre byte
                # [bkg_end+9]          SubjectPlacement byte
                # [bkg_end+10:+14]     LayoutStyle int32
                # [bkg_end+14:+18]     LayoutStyleColor int32
                # [bkg_end+18:+22]     SKU int32
                sn_off = bkg_end
                if sn_off + 24 <= len(ue):
                    # AI NOTE (v1.8.1, April 2026): schema-aware offsets.
                    #   V1 (row_size 71, Western/Adventure): no Placement, no ColorPalette
                    #     LS@+9, LSC@+13, SKU@+17
                    #   V2 (row_size 72, standard): Placement yes, no ColorPalette
                    #     LS@+10, LSC@+14, SKU@+18
                    #   V3 (row_size 73, Police): Placement yes, ColorPalette yes
                    #     LS@+10, LSC@+14, SKU@+19 (shifted by ColorPalette byte at +18)
                    if not hasattr(self, '_row_size'):
                        self._detect_uexp_layout()
                    if self._row_size == 71:
                        _ls_o, _lsc_o, _sku_o = 9, 13, 17
                    elif self._row_size == 73:
                        _ls_o, _lsc_o, _sku_o = 10, 14, 19
                    else:
                        _ls_o, _lsc_o, _sku_o = 10, 14, 18
                    ls  = struct.unpack_from("<i", ue, sn_off + _ls_o)[0]
                    lsc = struct.unpack_from("<i", ue, sn_off + _lsc_o)[0]
                    sku = struct.unpack_from("<i", ue, sn_off + _sku_o)[0]
                    pn  = (self._name_table[pn_idx]
                           if 0 <= pn_idx < len(self._name_table) else "")
                    slots.append({
                        "bkg_tex": bkg,
                        "pn_name": pn,
                        "ls":      FIXED_REGULAR_LAYOUT,
                        "lsc":     lsc,
                        "sku":     sku,
                        "ntu":     False,
                    })
                    seen_bkg.add(bkg)

            # Advance to next row: row size = 8+8+4+si_flen+4+bi_flen+8+2+10+4+2
            # = 26 + si_flen + bi_flen + (fields after bkg)
            # fields after bkg: SubjectName(8) + Genre(1) + Placement(1) + LS(4) + LSC(4) + SKU(4) + NextKey(2) + pad(2) = 26
            if not hasattr(self, '_row_size'):
                self._detect_uexp_layout()
            pos += self._row_size

        print(f"[CleanDT] read_slot_data: {self.dt_name} → {len(slots)} slots")
        return slots if slots else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _encode_fname_entry(self, text):
        """Encode a string as a UE4 name table FName entry."""
        encoded = text.encode("utf-8") + b"\x00"
        return struct.pack("<i", len(encoded)) + encoded + b"\x00\x00\x00\x00"

    def _extend_name_table(self, extra_titles):
        """
        Return (new_uasset_bytes, extended_name_table, title->idx map)
        for titles not already in the name table.
        Inserts new FName entries at export_offset and updates all
        header fields that shift as a result.
        """
        ua = bytearray(self._uasset)
        names = list(self._name_table)

        # Header fields are sequential after the FString — compute relative offsets.
        # These relative offsets (from fse) are FIXED regardless of genre name length,
        # so they work correctly for Drama (fse=0x4F), Horror (fse=0x50), etc.
        fse        = 0x24 + struct.unpack_from("<i", ua, 0x20)[0]
        off_nc     = fse + 4    # NameCount (int32)
        off_nc2    = fse + 88   # second NameCount copy
        off_expoff = fse + 32   # ExportOffset ← THE CRITICAL FIELD
        export_off = struct.unpack_from("<i", ua, off_expoff)[0]

        # All fse-relative offsets of int32 file-offset fields that need += shift.
        # Verified against base game Horror header (fse=0x50):
        #   fse+8=NameOffset, fse+16,32,40,44,136,160 = various file offsets.
        # 0x1C (TotalHeaderSize) is at an absolute position before fse.
        FSE_OFFSET_FIELDS = [8, 16, 32, 40, 44, 136, 160]

        # Find which titles need adding
        to_add = [t for t in extra_titles if t not in names]
        if not to_add:
            return bytes(ua), names, {}

        # Build new name entries
        new_entries = bytearray()
        new_indices = {}
        for title in to_add:
            new_indices[title] = len(names)
            names.append(title)
            new_entries += self._encode_fname_entry(title)

        shift    = len(new_entries)
        old_size = len(ua)

        # Find the actual end of the name table by walking all existing entries.
        # We must insert HERE, not at export_off — there is a gap between name
        # table end and export_off containing import/depends/preload data that
        # must not be displaced into the middle of the name table.
        name_off = struct.unpack_from("<i", ua, fse + 8)[0]
        old_count = struct.unpack_from("<i", ua, off_nc)[0]
        pos = name_off
        for _ in range(old_count):
            if pos + 4 > len(ua): break
            slen = struct.unpack_from("<i", ua, pos)[0]
            if slen <= 0 or slen > 500: break
            pos += 4 + slen + 4
        insert_off = pos   # actual end of name table

        # Insert new name entries right after the last existing name entry.
        new_ua = bytearray(ua[:insert_off]) + new_entries + bytearray(ua[insert_off:])

        # Update TotalHeaderSize at absolute position 0x1C
        old_total = struct.unpack_from("<i", ua, 0x1c)[0]
        struct.pack_into("<i", new_ua, 0x1c, old_total + shift)

        # Update NameCount at both copies (counts, not offsets — no shift to value)
        new_count = old_count + len(to_add)
        for cnt_off in [off_nc, off_nc2]:
            if cnt_off + 4 <= len(new_ua):
                struct.pack_into("<i", new_ua, cnt_off, new_count)

        # Shift all file-offset fields that pointed at or after the insertion point.
        # Use fse-relative positions to avoid alignment issues.
        for rel in FSE_OFFSET_FIELDS:
            abs_off = fse + rel
            if abs_off + 4 > len(ua):
                continue
            v = struct.unpack_from("<i", ua, abs_off)[0]
            if insert_off <= v <= old_size and abs_off + 4 <= len(new_ua):
                struct.pack_into("<i", new_ua, abs_off, v + shift)

        # Update serial_offset (int64 = old uasset size, stored in export table)
        new_size   = len(new_ua)
        scan_start = max(0, min(insert_off + shift, len(new_ua) - 8))
        for i in range(scan_start, len(new_ua) - 8, 4):
            if struct.unpack_from("<q", new_ua, i)[0] == old_size:
                struct.pack_into("<q", new_ua, i, new_size)

        print(f"[CleanDT] Added {len(to_add)} name(s) to {self.dt_name} uasset "
              f"({old_size} -> {new_size} bytes)")
        return bytes(new_ua), names, new_indices

    def build(self, slot_data, title_overrides=None, custom_only=False):
        """
        Build uasset + uexp by patching the base game uexp in-place.
        custom_only=True: skip all base-game slots, output only user-added slots.

        OUTPUT: 1 row per movie slot (changed from 77).
          The original game used 77 rows per slot (one per T_Sub subject image).
          Since we always write T_Sub_01 to every row, those 76 duplicates served
          no purpose and caused the in-game computer list to show every movie 77x.
          We now emit exactly 1 row per slot. The DataTable still loads correctly.
          Since we always change the row count vs the base game, we always use
          PLAIN_FOOTER (the TMap extra block offsets are always stale).

        SERIAL SIZE:
          new_serial = row_start + total_rows * row_size - 4
          Must match exactly — engine validates this.

        SUBJECTIMAGE — always T_Sub_01.
        """
        if not self._ensure_base_loaded():
            return None, None

        overrides    = title_overrides or {}
        ROW_SIZE     = getattr(self, "_row_size",  72)
        IN_PER_SLOT  = getattr(self, "_per_slot",  77)  # rows/slot in SOURCE uexp
        rk_exp       = getattr(self, "_detected_rk_num", self.RK_NUM)
        PLAIN_FOOTER = b"\x00\x00\x00\x00\xC1\x83\x2A\x9E"

        print(f"[CleanDT] {self.dt_name}: build() ROW_SIZE={ROW_SIZE} "
              f"IN_PER_SLOT={IN_PER_SLOT}->1 "
              f"RK_NUM=0x{rk_exp:08X} uexp_len={len(self._uexp_orig)}")

        # -- Build name table --
        # Pre-add row key strings "1".."N+99" so custom slot row keys resolve correctly.
        # Base slot keys are already in the base game name table.
        # We add conservatively up to len(slot_data)+99 to cover all possible indices.
        row_key_strs = [str(i + 1) for i in range(len(slot_data) + 99)]

        effective_titles = [overrides.get(s["pn_name"], s["pn_name"]) for s in slot_data]
        # Add "End of List" sentinel title for the sacrificial last row
        sentinel_title = "End of List"
        all_needed = list(dict.fromkeys(
            t for t in (row_key_strs + effective_titles + [sentinel_title])
            if t not in self._name_table
        ))
        if self.dt_name not in self._name_table:
            all_needed = [self.dt_name] + all_needed
        if all_needed:
            titles_only = [t for t in all_needed
                           if t != self.dt_name and t not in row_key_strs]
            if self.dt_name not in self._name_table:
                print(f"[CleanDT] Adding DataTable name \'{self.dt_name}\' to name table")
            if titles_only:
                print(f"[CleanDT] Extending name table with: {titles_only}")

        new_uasset_bytes, working_names, _ = self._extend_name_table(all_needed)

        def name_idx(text):
            try:    return working_names.index(text)
            except ValueError:
                print(f"[CleanDT] WARNING: \'{text}\' not in working_names — using 0")
                return 0

        ue     = bytearray(self._uexp_orig)
        row_s  = self._row_start
        rc_off = self._row_count_off

        # Build bkg->slot_data mapping for quick lookup
        bkg_to_slot = {s["bkg_tex"]: s for s in slot_data}

        # ------------------------------------------------------------------
        # Build ALL rows from scratch with uniform 3-digit bkg names.
        # Base game rows are NOT copied from the uexp — they are rebuilt
        # from slot_data with correct 3-digit bkg_tex references.
        # ------------------------------------------------------------------
        base_rows_out  = bytearray()
        base_slot_count = 0  # no base rows from Pass 1
        our_slots = len(slot_data)
        new_rows  = bytearray()

        # Read genre_byte and placement from base game template row
        _genre_byte = getattr(self, '_genre_byte', 0)
        _placement  = getattr(self, '_placement', 0)

        if our_slots > 0:
            # -----------------------------------------------------------------
            # Schema-aware field offsets relative to _after_bi (April 2026).
            # The three schema versions differ in which fields are serialized:
            #   V1 (row_size 71, Western/Adventure): no Placement, no ColorPalette
            #   V2 (row_size 72, standard genres): Placement, no ColorPalette
            #   V3 (row_size 73, Police): Placement, ColorPalette
            # NextRowKeyIdx is a uint32 FName index pointing to the next row's RowKey name.
            # Field count per schema (counted from _after_bi):
            #   V1: SN(8) + Genre(1) + LS(4) + LSC(4) + SKU(4) + NextKey(4) = 25 bytes
            #   V2: SN(8) + Genre(1) + Place(1) + LS(4) + LSC(4) + SKU(4) + NextKey(4) = 26
            #   V3: SN(8) + Genre(1) + Place(1) + LS(4) + LSC(4) + CP(1) + SKU(4) + NextKey(4) = 27
            # -----------------------------------------------------------------
            if ROW_SIZE == 71:
                _schema = 'V1'
                _has_place, _has_cp = False, False
                _ls_o, _lsc_o, _sku_o, _nxt_o = 9, 13, 17, 21
                _tail_size = 25
                _cp_o = None
            elif ROW_SIZE == 73:
                _schema = 'V3'
                _has_place, _has_cp = True, True
                _ls_o, _lsc_o, _cp_o, _sku_o, _nxt_o = 10, 14, 18, 19, 23
                _tail_size = 27
            else:  # 72 or any other -> treat as V2 (standard)
                _schema = 'V2'
                _has_place, _has_cp = True, False
                _ls_o, _lsc_o, _sku_o, _nxt_o = 10, 14, 18, 22
                _tail_size = 26
                _cp_o = None

            # ColorPalette value for V3: base game Police uses 0x02 or 0x03 (no clear pattern).
            # Safe default: 0 (first enum value, NewEnumerator0).
            _cp_value = 0

            print(f"[CleanDT] {self.dt_name}: using schema {_schema} "
                  f"(Placement={_has_place}, ColorPalette={_has_cp})")

            for slot_idx in range(our_slots):
                slot   = slot_data[slot_idx]
                title  = overrides.get(slot["pn_name"], slot["pn_name"])
                pn_idx = name_idx(title)
                bkg    = slot["bkg_tex"]
                bkg_b  = bkg.encode("utf-8")

                # Build row based on bkg name length
                bi_str = bkg_b + b"\x00"
                bi_len = len(bi_str)  # 13 for 2-digit, 14 for 3-digit

                # Construct row: prefix through BI, then schema-specific tail
                row = bytearray(29)  # prefix: RowKey(8) + ProductName(8) + SI_len(4) + SI_str(9)
                row[16:20] = struct.pack("<i", 9)  # SI FString length
                row[20:29] = b"T_Sub_01\x00"
                row += struct.pack("<i", bi_len)  # BI FString length
                row += bi_str                      # BI string
                _after_bi = len(row)
                row += bytearray(_tail_size)       # tail: fields from SN onward (schema-sized)
                actual_row_size = len(row)

                # Fill RowKey, ProductName, SubjectName
                struct.pack_into("<I", row, 0, name_idx(str(slot_idx + 1)))  # RowKey name_idx
                struct.pack_into("<I", row, 4, rk_exp)                       # RowKey number (RK_NUM)
                struct.pack_into("<i", row, 8,  pn_idx)                      # ProductName FName idx
                struct.pack_into("<i", row, 12, 0)                           # ProductName number
                struct.pack_into("<i", row, _after_bi, pn_idx)               # SubjectName FName idx
                struct.pack_into("<i", row, _after_bi + 4, 0)                # SubjectName number

                # Genre byte (always present at _after_bi + 8)
                row[_after_bi + 8] = _genre_byte

                # Placement byte (V2/V3 only, at _after_bi + 9)
                if _has_place:
                    row[_after_bi + 9] = _placement

                # LayoutStyle is fixed. The old per-movie/random layout choice is gone.
                _ls_val2 = FIXED_REGULAR_LAYOUT
                struct.pack_into("<i", row, _after_bi + _ls_o,  _ls_val2)    # LayoutStyle
                struct.pack_into("<i", row, _after_bi + _lsc_o, slot["lsc"]) # LayoutStyleColor

                # ColorPalette byte (V3 only)
                if _has_cp:
                    row[_after_bi + _cp_o] = _cp_value & 0xFF

                # SKU (all schemas) — no shift or conversion, plain value
                struct.pack_into("<i", row, _after_bi + _sku_o, slot["sku"])

                # NextRowKeyIdx — points to next row's RowKey name
                next_rk_idx = name_idx(str(slot_idx + 2))
                struct.pack_into("<I", row, _after_bi + _nxt_o, next_rk_idx)

                new_rows += row

            print(f"[CleanDT] Appended {our_slots} new rows for {self.dt_name}")

            # Sentinel row: the game's random picker selects from rows 0..count-2,
            # always skipping the last physical row. We append an "End of List"
            # sentinel as the sacrificial last row so all real movies are pickable.
            # The sentinel is visible in the in-game computer list but cannot be
            # clicked (it has no valid SKU), serving as a harmless end marker.
            if our_slots > 0 and new_rows:
                # Clone the last custom row as sentinel template (preserves schema layout)
                sentinel = bytearray(new_rows[-actual_row_size:])
                sentinel_idx = our_slots
                sentinel_rk_str = str(sentinel_idx + 1)
                struct.pack_into("<I", sentinel, 0, name_idx(sentinel_rk_str))
                struct.pack_into("<I", sentinel, 4, rk_exp)
                sentinel_pn_idx = name_idx("End of List")
                struct.pack_into("<i", sentinel, 8, sentinel_pn_idx)
                struct.pack_into("<i", sentinel, 12, 0)
                struct.pack_into("<i", sentinel, _after_bi, sentinel_pn_idx)
                struct.pack_into("<i", sentinel, _after_bi + 4, 0)
                struct.pack_into("<i", sentinel, _after_bi + _sku_o, 0)      # SKU = 0
                struct.pack_into("<I", sentinel, _after_bi + _nxt_o, 0)      # next = 0 (end)
                # Fix last real row to point to sentinel
                real_last_start = len(new_rows) - actual_row_size
                struct.pack_into("<I", new_rows, real_last_start + _after_bi + _nxt_o,
                                 name_idx(sentinel_rk_str))
                new_rows += sentinel
                print(f"[CleanDT] Added 'End of List' sentinel row (SKU=0, key='{sentinel_rk_str}')")

        # ------------------------------------------------------------------
        # Assemble uexp: header + new rows + PLAIN_FOOTER
        # ------------------------------------------------------------------
        # Count: all slot rows + sentinel (if added)
        _has_sentinel = (our_slots > 0 and len(new_rows) > 0)
        total_rows   = our_slots + (1 if _has_sentinel else 0)
        uexp_header  = bytearray(ue[:row_s])
        struct.pack_into("<H", uexp_header, rc_off, total_rows)

        new_uexp_ba  = uexp_header + new_rows + bytearray(PLAIN_FOOTER)
        new_uexp     = bytes(new_uexp_ba)
        new_serial   = row_s + len(new_rows) - 4

        print(f"[CleanDT] Built {self.dt_name}: {total_rows} rows (1/slot), "
              f"uexp={len(new_uexp)} bytes, serial_size={new_serial}")

        # Update serial_size and serial_offset in uasset export table
        new_uasset = bytearray(new_uasset_bytes)
        plen       = struct.unpack_from("<i", new_uasset, 0x20)[0]
        fse        = 0x24 + plen
        export_off = struct.unpack_from("<i", new_uasset, fse + 32)[0]
        serial_off = export_off + 28
        if serial_off + 16 <= len(new_uasset):
            struct.pack_into("<q", new_uasset, serial_off,     new_serial)
            struct.pack_into("<q", new_uasset, serial_off + 8, len(new_uasset))
            print(f"[CleanDT] Wrote serial_size={new_serial} at uasset 0x{serial_off:X}")
        else:
            print(f"[CleanDT] WARNING: serial_off 0x{serial_off:X} out of bounds")

        return bytes(new_uasset), bytes(new_uexp)

    def write_to_dir(self, slot_data, title_overrides, output_dir):
        """Build and write uasset+uexp to output_dir. Returns True on success."""
        ua, ue = self.build(slot_data, title_overrides)
        if ua is None:
            return False
        dest = os.path.join(output_dir, "RetroRewind", "Content",
                            "VideoStore", "core", "blueprint", "data")
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, f"{self.dt_name}.uasset"), "wb") as f:
            f.write(ua)
        with open(os.path.join(dest, f"{self.dt_name}.uexp"), "wb") as f:
            f.write(ue)
        return True


# ============================================================
# NEW RELEASE DATATABLE BUILDER
# Builds NewRelease_Details_-_Data by extracting the base game
# uasset+uexp as template and replacing all rows with custom ones.
# Row layout differs from genre DataTables:
#   - SubjectImage is "-1" (3-byte FString vs 9-byte "T_Sub_01\0")
#   - LayoutStyle = -1, LayoutStyleColor = 0
#   - NewToUnlock = true
#   - SKUs are simple 5-digit IDs (no star/rarity encoding)
# ============================================================

def build_newrelease_datatable(pak_cache, new_releases, output_dir):
    """
    Build NewRelease_Details_-_Data with ONLY custom rows (no base game rows).

    new_releases: list of dicts, each with:
      - title: str (movie name, must exist in name table or will be added)
      - genre_byte: int (genre enum byte: 1=Act,3=Com,4=Dra,5=Hor,6=Sci,7=Fan,...)
      - bkg_tex: str (e.g. "T_New_Hor_01", must be exactly 12 chars)
      - sku: int (uint16, 5-digit unique ID, e.g. 50001)

    Row layout (54 bytes, confirmed from binary analysis):
      [0:4]   RowKey FName idx (all rows use idx for "1")
      [4:8]   RK_NUM = 0x01A81780
      [8:12]  ProductName FName idx
      [12:16] ProductName FName num (0)
      [16:20] SubjectImage FString len (3)
      [20:23] "-1\0"
      [23:27] BackgroundImage FString len (13)
      [27:40] "T_New_XXX_NN\0"
      [40]    Genre enum byte
      [41:45] LayoutStyle int32 (-1 = 0xFFFFFFFF)
      [45:47] SKU uint16
      [47]    0x00
      [48]    0x00
      [49]    NewToUnlock byte (0x01 = true)
      [50:54] NextRowKey idx (uint32, linked list; last row = 0)

    serial_size = ROW_START + n_rows * 54 - 4
    Returns True on success.
    """
    DT_NAME = "NewRelease_Details_-_Data"
    # Row size is bkg_b-length-dependent. Base game rows use 13-byte bkg_b
    # ("T_New_Hor_01\0") → row_size 54. Our custom 3-digit slots use 14-byte
    # bkg_b ("T_New_Hor_001\0") → row_size 55. Since the tool ships only
    # 3-digit NRs (we don't reuse base game NR slots), all rows are 55 bytes.
    BI_LEN = 14            # length of bkg_tex FString incl null terminator (3-digit)
    BI_OFFSET = 27         # byte offset where bkg_b starts within a row
    # Field offsets after BI: genre(1) + layout(4) + sku(2) + pad(2) + ntu(1) + next(4) = 14
    GENRE_OFF = BI_OFFSET + BI_LEN          # 41
    LAYOUT_OFF = GENRE_OFF + 1              # 42 (int32 = -1)
    SKU_OFF = LAYOUT_OFF + 4                # 46 (uint16)
    NTU_OFF = SKU_OFF + 4                   # 50 (after 2-byte SKU + 2-byte padding)
    NEXT_KEY_OFF = NTU_OFF + 1              # 51 (uint32)
    ROW_SIZE = NEXT_KEY_OFF + 4             # 55
    RK_NUM = 0x01A81780
    PLAIN_FOOTER = b"\x00\x00\x00\x00\xC1\x83\x2A\x9E"
    RK_NUM = 0x01A81780
    PLAIN_FOOTER = b"\x00\x00\x00\x00\xC1\x83\x2A\x9E"

    # --- Extract base game uasset + uexp ---
    extract_dir = pak_cache._extract_dir
    base_pak = pak_cache.pak_path
    repak = pak_cache.repak_path

    for ext in ("uasset", "uexp"):
        subprocess.run(
            [repak, "unpack", "-o", extract_dir, "-f",
             "-i", f"{DATATABLE_PATH}/{DT_NAME}.{ext}", base_pak],
            capture_output=True, timeout=30
        )

    dt_dir = os.path.join(extract_dir, "RetroRewind", "Content",
                          "VideoStore", "core", "blueprint", "data")
    ua_path = os.path.join(dt_dir, f"{DT_NAME}.uasset")
    ue_path = os.path.join(dt_dir, f"{DT_NAME}.uexp")

    if not os.path.exists(ua_path) or not os.path.exists(ue_path):
        print(f"[NewRelease] FAILED: could not extract {DT_NAME} from base pak")
        return False

    ua_orig = open(ua_path, 'rb').read()
    ue_orig = open(ue_path, 'rb').read()
    print(f"[NewRelease] Loaded base {DT_NAME}: uasset={len(ua_orig)} uexp={len(ue_orig)}")

    # --- Parse name table from uasset ---
    plen = struct.unpack_from("<i", ua_orig, 0x20)[0]
    fse = 0x24 + plen
    name_count = struct.unpack_from("<i", ua_orig, fse + 4)[0]
    name_off = struct.unpack_from("<i", ua_orig, fse + 8)[0]

    names = []
    pos = name_off
    for _ in range(name_count):
        if pos + 4 > len(ua_orig): break
        slen = struct.unpack_from("<i", ua_orig, pos)[0]
        if 1 <= slen <= 300:
            end = pos + 4 + slen
            if end + 4 <= len(ua_orig):
                raw = ua_orig[pos + 4:end]
                if raw[-1:] == b"\x00":
                    try:
                        text = raw[:-1].decode("utf-8")
                        names.append(text)
                        pos = end + 4
                        continue
                    except Exception:
                        pass
        break

    print(f"[NewRelease] Name table: {len(names)} entries")
    if not names:
        print("[NewRelease] FAILED: empty name table")
        return False

    # --- Detect ROW_START from uexp ---
    ue = bytearray(ue_orig)
    row_start = None
    for scan in range(0x10, min(0x30, len(ue) - 10)):
        si_len = struct.unpack_from("<i", ue, scan + 16)[0]
        if si_len == 3 and ue[scan + 20:scan + 23] == b"-1\x00":
            row_start = scan
            break

    if row_start is None:
        print("[NewRelease] FAILED: could not detect row start")
        return False

    row_count_off = 0x12 if row_start == 0x1A else 0x0e
    orig_row_count = struct.unpack_from("<H", ue, row_count_off)[0]
    print(f"[NewRelease] ROW_START=0x{row_start:X}, orig_rows={orig_row_count}")

    # No template row needed — we build each row from scratch with the
    # 3-digit (55-byte) layout. The base game's first row uses the 13-byte
    # bkg_b (54-byte) layout, which would mis-align all our fields.

    # --- Check which titles need adding to name table ---
    new_ua = bytearray(ua_orig)
    working_names = list(names)

    titles_to_add = []
    # Ensure row key strings "1".."N" exist in name table
    for i in range(len(new_releases)):
        rk_str = str(i + 1)
        if rk_str not in working_names and rk_str not in titles_to_add:
            titles_to_add.append(rk_str)
    # Ensure custom titles exist
    for nr in new_releases:
        if nr["title"] not in working_names and nr["title"] not in titles_to_add:
            titles_to_add.append(nr["title"])

    if titles_to_add:
        new_entries = bytearray()
        for title in titles_to_add:
            working_names.append(title)
            encoded = title.encode("utf-8") + b"\x00"
            new_entries += struct.pack("<i", len(encoded))
            new_entries += encoded
            new_entries += b"\x00\x00\x00\x00"

        shift = len(new_entries)
        old_size = len(new_ua)
        old_count = name_count

        p = name_off
        for _ in range(old_count):
            if p + 4 > len(new_ua): break
            sl = struct.unpack_from("<i", new_ua, p)[0]
            if sl <= 0 or sl > 500: break
            p += 4 + sl + 4
        insert_off = p

        new_ua = bytearray(new_ua[:insert_off]) + new_entries + bytearray(new_ua[insert_off:])

        old_total = struct.unpack_from("<i", ua_orig, 0x1c)[0]
        struct.pack_into("<i", new_ua, 0x1c, old_total + shift)

        new_count = old_count + len(titles_to_add)
        for cnt_off in [fse + 4, fse + 88]:
            if cnt_off + 4 <= len(new_ua):
                struct.pack_into("<i", new_ua, cnt_off, new_count)

        FSE_OFFSET_FIELDS = [8, 16, 32, 40, 44, 136, 160]
        for rel in FSE_OFFSET_FIELDS:
            abs_off = fse + rel
            if abs_off + 4 > len(ua_orig): continue
            v = struct.unpack_from("<i", ua_orig, abs_off)[0]
            if insert_off <= v <= old_size and abs_off + 4 <= len(new_ua):
                struct.pack_into("<i", new_ua, abs_off, v + shift)

        new_size = len(new_ua)
        scan_start = max(0, min(insert_off + shift, len(new_ua) - 8))
        for si in range(scan_start, len(new_ua) - 8, 4):
            if struct.unpack_from("<q", new_ua, si)[0] == old_size:
                struct.pack_into("<q", new_ua, si, new_size)

        print(f"[NewRelease] Extended name table: {old_count} -> {new_count} "
              f"(+{len(titles_to_add)}, shift={shift})")

    # --- Build custom rows ---
    # Filter out NR slots for genres without base game T_New textures
    valid_releases = []
    for nr in new_releases:
        g = nr.get("genre", "")
        if g not in NR_GENRE_BYTE:
            print(f"[NewRelease] WARNING: Skipping NR '{nr['title']}' — "
                  f"genre '{g}' not supported (no base game T_New textures)")
            continue
        valid_releases.append(nr)
    new_releases = valid_releases
    n_rows = len(new_releases)
    new_rows = bytearray()

    for idx, nr in enumerate(new_releases):
        bkg_b = nr["bkg_tex"].encode("utf-8") + b"\x00"
        if len(bkg_b) != BI_LEN:
            print(f"[NewRelease] WARNING: row {idx} bkg_tex {nr['bkg_tex']!r} "
                  f"is {len(bkg_b)} bytes, expected {BI_LEN} (3-digit format) — skipping")
            continue
        row = bytearray(ROW_SIZE)

        # Unique RowKey: name index for string "idx+1" (e.g. "1", "2", "3")
        rk_str = str(idx + 1)
        rk_idx = working_names.index(rk_str)
        struct.pack_into("<I", row, 0, rk_idx)
        struct.pack_into("<I", row, 4, RK_NUM)

        pn_idx = working_names.index(nr["title"])
        struct.pack_into("<I", row, 8, pn_idx)
        struct.pack_into("<I", row, 12, 0)

        struct.pack_into("<i", row, 16, 3)
        row[20:23] = b"-1\x00"

        struct.pack_into("<i", row, 23, BI_LEN)
        row[BI_OFFSET:BI_OFFSET + BI_LEN] = bkg_b

        row[GENRE_OFF] = nr["genre_byte"]
        struct.pack_into("<i", row, LAYOUT_OFF, -1)
        struct.pack_into("<H", row, SKU_OFF, nr["sku"])
        # SKU+2 and SKU+3 are pad bytes (already 0 from bytearray init).
        row[NTU_OFF] = 1   # NewToUnlock = true

        # Linked list: points to NEXT row's key name index, last row = 0
        is_last = (idx == n_rows - 1)
        if is_last:
            struct.pack_into("<I", row, NEXT_KEY_OFF, 0)
        else:
            next_rk_str = str(idx + 2)
            next_rk_idx = working_names.index(next_rk_str)
            struct.pack_into("<I", row, NEXT_KEY_OFF, next_rk_idx)

        new_rows += row
        print(f"[NewRelease] Row {idx}: key='{rk_str}'(idx={rk_idx}) "
              f"'{nr['title']}' genre=0x{nr['genre_byte']:02X} "
              f"sku={nr['sku']} bkg='{nr['bkg_tex']}' "
              f"next={'0(end)' if is_last else next_rk_str}")
    # --- Assemble new uexp ---
    uexp_header = bytearray(ue[:row_start])
    struct.pack_into("<H", uexp_header, row_count_off, n_rows)
    new_uexp = bytes(uexp_header + new_rows + PLAIN_FOOTER)
    new_serial = row_start + n_rows * ROW_SIZE - 4

    print(f"[NewRelease] Built: {n_rows} rows, uexp={len(new_uexp)} bytes, "
          f"serial={new_serial}")

    # --- Patch serial_size in uasset ---
    export_off = struct.unpack_from("<i", new_ua, fse + 32)[0]
    serial_off = export_off + 28
    if serial_off + 16 <= len(new_ua):
        struct.pack_into("<q", new_ua, serial_off, new_serial)
        struct.pack_into("<q", new_ua, serial_off + 8, len(new_ua))
        print(f"[NewRelease] Patched serial_size={new_serial} at uasset 0x{serial_off:X}")

    dest = os.path.join(output_dir, "RetroRewind", "Content",
                        "VideoStore", "core", "blueprint", "data")
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, f"{DT_NAME}.uasset"), "wb") as f:
        f.write(bytes(new_ua))
    with open(os.path.join(dest, f"{DT_NAME}.uexp"), "wb") as f:
        f.write(new_uexp)
    print(f"[NewRelease] Written to {dest}")
    return True


# --- Genre code to genre folder mapping for material instance paths ---
_NR_GENRE_FOLDER = {
    "Act": "T_Bkg_Act", "Com": "T_Bkg_Com", "Dra": "T_Bkg_Dra",
    "Fan": "T_Bkg_Fan", "Hor": "T_Bkg_Hor", "Kid": "T_Bkg_Kid",
    "Pol": "T_Bkg_Pol", "Rom": "T_Bkg_Rom", "Sci": "T_Bkg_Sci",
    "Wst": "T_Bkg_Wst", "Xma": "T_Bkg_Xma", "Adu": "T_Bkg_Adu",
    "Adv": "T_Bkg_Adv", "Thr": "T_Bkg_Thr", "Mus": "T_Bkg_Mus",
    "His": "T_Bkg_His", "Doc": "T_Bkg_Doc", "Spo": "T_Bkg_Spo",
}





def create_standee_thumbnail(sku, standee_shape, output_dir, texconv):
    """Create T_Standees_Collection_{sku} thumbnail texture for in-game computer.

    Converts the embedded standee preview image to 512x512 DXT5 and writes
    the thumbnail texture (uasset + uexp) to the mod pak.

    Args:
        sku: int, 5-digit SKU
        standee_shape: "A", "B", or "C"
        output_dir: build work directory
        texconv: path to texconv.exe
    Returns True on success.
    """
    sku_str = str(sku)
    if len(sku_str) != 5:
        print(f"[Thumb] ERROR: SKU {sku} is not 5 digits")
        return False

    # --- Decode the template uasset ---
    ua_template = zlib.decompress(base64.b64decode(_THUMB_TEX_UASSET_TEMPLATE_B64Z))
    ue_header = zlib.decompress(base64.b64decode(_THUMB_TEX_UEXP_HEADER_B64Z))

    # --- Patch uasset: SKU in package path + FName number ---
    ua = bytearray(ua_template)
    old_sku = str(_THUMB_TEX_TEMPLATE_SKU).encode('ascii')
    new_sku = sku_str.encode('ascii')
    ua = bytearray(bytes(ua).replace(old_sku, new_sku))

    # Patch FName number field
    old_fnum = struct.pack('<I', _THUMB_TEX_TEMPLATE_FNAME_NUM)
    new_fnum = struct.pack('<I', sku + 1)
    fnum_off = _THUMB_TEX_FNAME_NUM_OFFSET
    if ua[fnum_off:fnum_off+4] == old_fnum:
        ua[fnum_off:fnum_off+4] = new_fnum
    else:
        # Fallback: search and replace
        ua = bytearray(bytes(ua).replace(old_fnum, new_fnum, 1))

    # --- Decode the standee preview image ---
    fullres_b64z = {
        "A": _STANDEE_FULLRES_A_B64Z,
        "B": _STANDEE_FULLRES_B_B64Z,
        "C": _STANDEE_FULLRES_C_B64Z,
    }
    img_data = zlib.decompress(base64.b64decode(fullres_b64z[standee_shape]))

    # --- Convert to DXT5 via texconv ---
    with tempfile.TemporaryDirectory() as tmp:
        png_path = os.path.join(tmp, "standee.png")
        # Decode JPEG to PNG for texconv
        pil_img = Image.open(io.BytesIO(img_data)).convert("RGBA")
        pil_img.save(png_path)

        r = subprocess.run(
            [texconv, '-f', 'DXT5', '-w', '512', '-h', '512',
             '-m', '1', '-srgb', '-o', tmp, '-y', png_path],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            print(f"[Thumb] texconv failed: {r.stderr}")
            return False

        dds_path = os.path.join(tmp, "standee.dds")
        if not os.path.exists(dds_path):
            print("[Thumb] texconv produced no DDS")
            return False

        with open(dds_path, 'rb') as f:
            dds_data = f.read()

        # Strip DDS header (128 bytes standard)
        dds_header_size = 128
        if len(dds_data) > 148:
            fourcc = dds_data[84:88]
            if fourcc == b'DX10':
                dds_header_size = 148
        pixel_data = dds_data[dds_header_size:]

        expected = _THUMB_TEX_PIXEL_SIZE
        print(f"[Thumb] DDS: {len(dds_data)} bytes, header={dds_header_size}, "
              f"pixel_data={len(pixel_data)}, expected={expected}")
        if len(pixel_data) < expected:
            pixel_data += b'\x00' * (expected - len(pixel_data))
        elif len(pixel_data) > expected:
            pixel_data = pixel_data[:expected]

    # --- Assemble uexp ---
    new_uexp = ue_header + pixel_data + _THUMB_TEX_TRAILING

    # --- Write to pak path ---
    dest = os.path.join(output_dir, "RetroRewind", "Content",
                        "VideoStore", "asset", "prop", "Standees", "Thumbnail")
    os.makedirs(dest, exist_ok=True)
    out_name = f"T_Standees_Collection_{sku_str}"
    with open(os.path.join(dest, f"{out_name}.uasset"), 'wb') as f:
        f.write(bytes(ua))
    with open(os.path.join(dest, f"{out_name}.uexp"), 'wb') as f:
        f.write(new_uexp)

    print(f"[Thumb] Created {out_name} (shape={standee_shape}, "
          f"uexp={len(new_uexp)} bytes)")
    return True

def create_mi_for_nr(genre_code, tex_num, standee_shape, output_dir):
    """Create a 3-digit Material Instance for a New Release standee.

    Patches the embedded MI template (MI_New_Hor_04, Standee A) to produce
    MI_New_{genre_code}_{tex_num:03d} with the correct T_Standee_{shape}_01_ao.

    The slot number patch (Hor_04 → <code>_<NN:03d>) changes string length
    from 6 to 7 chars, which shifts all name-table offsets in the uasset.
    _rebuild_uasset_with_name_patches handles the full asset rebuild and
    zeroes FName hashes for patched entries.

    Args:
        genre_code: 3-char code e.g. "Hor", "Act", "Fan"
        tex_num: int, slot number e.g. 1, 4, 25
        standee_shape: "A", "B", or "C"
        output_dir: build work directory

    Returns True on success.
    """
    # Build patch list.
    # MI's name table has these slot/shape/genre-bearing FName entries:
    #   - "MI_New_Hor_04" + "/Game/.../T_Bkg_Hor/MI_New_Hor_04"
    #   - "T_New_Hor_04" + "/Game/.../T_Bkg_Hor/T_New_Hor_04"
    #   - "T_Standee_A_01_ao" + "/Game/.../T_Standee_A_01/T_Standee_A_01_ao"
    # The patches use longer (3-digit) replacements where applicable.
    src_gn = f"{_MI_TEMPLATE_GENRE}_{_MI_TEMPLATE_NUM:02d}"   # "Hor_04"
    dst_gn = f"{genre_code}_{tex_num:03d}"                    # "Hor_005"
    src_folder = f"T_Bkg_{_MI_TEMPLATE_GENRE}"
    dst_folder = f"T_Bkg_{genre_code}"
    # AO file name only (not the folder). Base game keeps all shape AOs in
    # the shared folder T_Standee_A_01/ — patching "T_Standee_A" wholesale
    # would also rewrite the folder path to T_Standee_B_01/ for shape B,
    # where the AO doesn't exist, causing UE to fall back to a default
    # material and the standee renders with a dark band where the AO
    # should be sampled.
    src_ao = f"T_Standee_{_MI_TEMPLATE_SHAPE}_01_ao"
    dst_ao = f"T_Standee_{standee_shape}_01_ao"
    patches = []
    if src_gn != dst_gn:
        patches.append((src_gn, dst_gn))
    if src_folder != dst_folder:
        patches.append((src_folder, dst_folder))
    if src_ao != dst_ao:
        patches.append((src_ao, dst_ao))

    src_pkg = f"/Game/VideoStore/asset/prop/vhs/Background/{src_folder}/MI_New_{src_gn}"
    dst_pkg = f"/Game/VideoStore/asset/prop/vhs/Background/{dst_folder}/MI_New_{dst_gn}"

    try:
        new_uasset = _rebuild_uasset_with_name_patches(
            _MI_UASSET_TEMPLATE, src_pkg, dst_pkg, patches)
    except Exception as e:
        print(f"[MI] ERROR rebuilding uasset: {e}")
        return False

    mi_name = f"MI_New_{genre_code}_{tex_num:03d}"
    folder_name = f"T_Bkg_{genre_code}"
    dest = os.path.join(output_dir, "RetroRewind", "Content",
                        "VideoStore", "asset", "prop", "vhs",
                        "Background", folder_name)
    os.makedirs(dest, exist_ok=True)

    with open(os.path.join(dest, f"{mi_name}.uasset"), 'wb') as f:
        f.write(new_uasset)
    with open(os.path.join(dest, f"{mi_name}.uexp"), 'wb') as f:
        f.write(_MI_UEXP_TEMPLATE)

    print(f"[MI] Created {mi_name} (shape={standee_shape}) "
          f"[uasset={len(new_uasset)} bytes, +{len(new_uasset)-len(_MI_UASSET_TEMPLATE)} vs template]")
    return True

def clone_standee_blueprint(pak_cache, sku, standee_shape, genre_code, tex_num, output_dir):
    """
    Clone Standees_Collection_{sku} blueprint from base game template.

    sku: int (5-digit, e.g. 50001)
    standee_shape: str, one of "A", "B", "C"
    genre_code: str, 3-char code like "Hor", "Act", "Dra"
    tex_num: int, texture number (e.g. 1 for T_New_Hor_01)
    output_dir: build work directory

    Clones Standees_Collection_10693 (Standee B, MI_New_Dra_03) and replaces:
      - SKU "10693" → str(sku) (must be 5 digits)
      - Mesh "LA_Standee_B_01" → "LA_Standee_{shape}_01"
      - Material "MI_New_Dra_03" → "MI_New_{genre_code}_{tex_num:02d}"
      - Material path folder "T_Bkg_Dra" → "T_Bkg_{genre_code}"

    All replacements are same-length → no offset adjustments needed.
    Returns True on success.
    """
    sku_str = str(sku)
    if len(sku_str) != 5:
        print(f"[Standee] ERROR: SKU {sku} is not 5 digits")
        return False

    # --- Extract template blueprint from base game ---
    extract_dir = pak_cache._extract_dir
    base_pak = pak_cache.pak_path
    repak = pak_cache.repak_path
    tmpl_name = "Standees_Collection_10693"
    tmpl_path = f"RetroRewind/Content/VideoStore/asset/prop/Standees/mesh/{tmpl_name}"

    for ext in ("uasset", "uexp"):
        subprocess.run(
            [repak, "unpack", "-o", extract_dir, "-f",
             "-i", f"{tmpl_path}.{ext}", base_pak],
            capture_output=True, timeout=30
        )

    tmpl_dir = os.path.join(extract_dir, "RetroRewind", "Content",
                            "VideoStore", "asset", "prop", "Standees", "mesh")
    ua_path = os.path.join(tmpl_dir, f"{tmpl_name}.uasset")
    ue_path = os.path.join(tmpl_dir, f"{tmpl_name}.uexp")

    if not os.path.exists(ua_path) or not os.path.exists(ue_path):
        print(f"[Standee] FAILED: could not extract {tmpl_name} from base pak")
        return False

    ua = bytearray(open(ua_path, 'rb').read())
    ue = bytearray(open(ue_path, 'rb').read())

    # Patch FName number for T_Standees_Collection thumbnail reference.
    # The template uexp has FName number 10694 (displays as _10693) at two locations.
    # Replace with new_sku + 1 so the standee references our custom thumbnail.
    old_fname_num = struct.pack('<I', 10694)  # template SKU 10693 + 1
    new_fname_num = struct.pack('<I', int(sku_str) + 1)
    n_fname = bytes(ue).count(old_fname_num)
    ue = bytearray(bytes(ue).replace(old_fname_num, new_fname_num))
    if n_fname:
        print(f"[Standee]   Thumbnail FName: 10694→{int(sku_str)+1} ({n_fname}x in uexp)")

    # --- Build patches for full asset rebuild ---
    # SKU and mesh shape are length-preserving (5-digit SKU, 1-char shape).
    # Material reference Dra_03 → <code>_<NN:03d> grows by 1 byte (length
    # change), so we use the generic full-asset rebuild helper rather than
    # a length-preserving in-place replace.
    src_sku = "10693"
    src_mesh_shape = "LA_Standee_B"
    dst_mesh_shape = f"LA_Standee_{standee_shape}"
    src_mat = "MI_New_Dra_03"
    dst_mat = f"MI_New_{genre_code}_{tex_num:03d}"
    src_mat_folder = "T_Bkg_Dra"
    dst_mat_folder = f"T_Bkg_{genre_code}"

    patches = []
    if src_sku != sku_str:
        patches.append((src_sku, sku_str))
    if src_mesh_shape != dst_mesh_shape:
        patches.append((src_mesh_shape, dst_mesh_shape))
    if src_mat != dst_mat:
        patches.append((src_mat, dst_mat))
    if src_mat_folder != dst_mat_folder:
        patches.append((src_mat_folder, dst_mat_folder))

    # PackageName: Standees_Collection_10693 → Standees_Collection_<sku>
    src_pkg = f"/Game/VideoStore/asset/prop/Standees/mesh/{tmpl_name}"
    dst_pkg = f"/Game/VideoStore/asset/prop/Standees/mesh/Standees_Collection_{sku_str}"

    try:
        ua_bytes = _rebuild_uasset_with_name_patches(bytes(ua), src_pkg, dst_pkg, patches)
    except Exception as e:
        print(f"[Standee] ERROR rebuilding uasset: {e}")
        return False

    print(f"[Standee] Cloned {tmpl_name} → Standees_Collection_{sku_str}")
    print(f"[Standee]   SKU: 10693→{sku_str}, mesh: B→{standee_shape}, "
          f"mat: Dra_03→{genre_code}_{tex_num:03d}, folder: Dra→{genre_code} "
          f"[uasset={len(ua_bytes)} bytes, +{len(ua_bytes)-len(ua)} vs template]")

    # --- Write cloned blueprint ---
    dest = os.path.join(output_dir, "RetroRewind", "Content",
                        "VideoStore", "asset", "prop", "Standees", "mesh")
    os.makedirs(dest, exist_ok=True)
    out_name = f"Standees_Collection_{sku_str}"
    with open(os.path.join(dest, f"{out_name}.uasset"), "wb") as f:
        f.write(ua_bytes)
    with open(os.path.join(dest, f"{out_name}.uexp"), "wb") as f:
        f.write(ue)
    print(f"[Standee] Written to {dest}")
    return True


# ============================================================
# DATATABLE MANAGER
# Reads movie titles from binary DataTable files and patches
# them for title editing.  Also drives CleanDataTableBuilder
# for genres that have confirmed slot data.
# ============================================================

# Slot data for Horror — confirmed working in-game.
# Each entry: bkg_tex, pn_name (original), ls, lsc, sku, ntu
# ============================================================
# SLOT DATA — confirmed from base game analysis.
# One entry per background texture slot.
# Gaps in slot numbers (e.g. Act_02 missing) are where the base game left
# the original game's procedurally-generated slot unreplaced.
# ============================================================

HORROR_SLOT_DATA = [
    {"bkg_tex":"T_Bkg_Hor_01","pn_name":"The Sixth Sense",                    "ls":7,  "lsc":4,  "sku":5304473,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_02","pn_name":"Lawnmower Man 2: Beyond Cyberspace", "ls":1,  "lsc":1,  "sku":5120914,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_03","pn_name":"Troll 2",                            "ls":11, "lsc":6,  "sku":5122534,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_04","pn_name":"Bram Stoker's Dracula",              "ls":18, "lsc":9,  "sku":5031851,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_05","pn_name":"Hellraiser",                         "ls":18, "lsc":9,  "sku":5002652,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_06","pn_name":"Carnosaur",                          "ls":9,  "lsc":5,  "sku":5271261,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_07","pn_name":"Misery",                             "ls":14, "lsc":7,  "sku":5232953,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_08","pn_name":"Scream",                             "ls":7,  "lsc":4,  "sku":5064364,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_09","pn_name":"Army of Darkness",                  "ls":17, "lsc":9,  "sku":5131964,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_10","pn_name":"The Silence of the Lambs",          "ls":16, "lsc":8,  "sku":5202393,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_11","pn_name":"I Know What You Did Last Summer",   "ls":7,  "lsc":4,  "sku":5021633,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_12","pn_name":"Soultaker",                         "ls":8,  "lsc":4,  "sku":5042434,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_13","pn_name":"A Nightmare on Elm Street",         "ls":10, "lsc":5,  "sku":5071665,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_14","pn_name":"Friday the 13th",                   "ls":4,  "lsc":2,  "sku":5002054,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_15","pn_name":"Jason Goes to Hell: The Final Friday","ls":20,"lsc":10,"sku":5503322,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_16","pn_name":"Scream 2",                           "ls":20, "lsc":10, "sku":5153233,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_17","pn_name":"Event Horizon",                     "ls":10, "lsc":5,  "sku":5013953,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_18","pn_name":"Cube",                              "ls":10, "lsc":5,  "sku":5111463,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_19","pn_name":"The Shining",                       "ls":2,  "lsc":1,  "sku":5011774,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_20","pn_name":"The Blair Witch Project",           "ls":10, "lsc":5,  "sku":5012862,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_21","pn_name":"Interview with the Vampire",        "ls":7,  "lsc":4,  "sku":5150673,"ntu":False},
    {"bkg_tex":"T_Bkg_Hor_22","pn_name":"From Dusk till Dawn",               "ls":10, "lsc":5,  "sku":5092742,"ntu":False},
]

ACTION_SLOT_DATA = [
    {"bkg_tex":"T_Bkg_Act_01","pn_name":"Die Hard",                          "ls":18, "lsc":9,  "sku":1002172, "ntu":False},
    {"bkg_tex":"T_Bkg_Act_02","pn_name":"Unknown Action Film",               "ls":3, "lsc":2,  "sku":3002002,   "ntu":False},
    {"bkg_tex":"T_Bkg_Act_03","pn_name":"Batman & Robin",                    "ls":2,  "lsc":1,  "sku":1151414, "ntu":False},
    {"bkg_tex":"T_Bkg_Act_04","pn_name":"Steel",                             "ls":14, "lsc":7,  "sku":1513911, "ntu":False},
    {"bkg_tex":"T_Bkg_Act_05","pn_name":"The Rock",                          "ls":16, "lsc":8,  "sku":1142444, "ntu":False},
    {"bkg_tex":"T_Bkg_Act_06","pn_name":"Captain America",                   "ls":18, "lsc":9,  "sku":1112711, "ntu":False},
    {"bkg_tex":"T_Bkg_Act_07","pn_name":"Commando",                          "ls":16, "lsc":8,  "sku":1104053, "ntu":False},
    {"bkg_tex":"T_Bkg_Act_08","pn_name":"Pulp Fiction",                      "ls":1,  "lsc":1,  "sku":1532392, "ntu":False},
    {"bkg_tex":"T_Bkg_Act_09","pn_name":"Con Air",                           "ls":9,  "lsc":5,  "sku":1063463, "ntu":False},
    {"bkg_tex":"T_Bkg_Act_10","pn_name":"Face/Off",                          "ls":2,  "lsc":1,  "sku":1020844, "ntu":False},
    {"bkg_tex":"T_Bkg_Act_11","pn_name":"The Matrix",                        "ls":11, "lsc":6,  "sku":1480694, "ntu":False},
    {"bkg_tex":"T_Bkg_Act_12","pn_name":"Jaws: The Revenge",                 "ls":11, "lsc":6,  "sku":1173612, "ntu":False},
    {"bkg_tex":"T_Bkg_Act_13","pn_name":"Unknown Action Film 2",             "ls":8, "lsc":4,  "sku":3013002,   "ntu":False},
    {"bkg_tex":"T_Bkg_Act_14","pn_name":"Terminator 2: Judgment Day",        "ls":11, "lsc":6,  "sku":1031694, "ntu":False},
    {"bkg_tex":"T_Bkg_Act_15","pn_name":"True Lies",                         "ls":4,  "lsc":2,  "sku":1042654, "ntu":False},
]

ADULT_SLOT_DATA = [
    {"bkg_tex":"T_Bkg_Adu_01","pn_name":"Bitter Moon",                       "ls":17, "lsc":9,  "sku":69093242, "ntu":False},
    {"bkg_tex":"T_Bkg_Adu_02","pn_name":"Unknown Adult Film",                        "ls":10, "lsc":5,  "sku":69002002,   "ntu":False},
    {"bkg_tex":"T_Bkg_Adu_03","pn_name":"Color of Night",                    "ls":7,  "lsc":4,  "sku":69392924, "ntu":False},
    {"bkg_tex":"T_Bkg_Adu_04","pn_name":"Sliver",                            "ls":12, "lsc":6,  "sku":69443424, "ntu":False},
    {"bkg_tex":"T_Bkg_Adu_05","pn_name":"Showgirls",                         "ls":18, "lsc":9,  "sku":69391632, "ntu":False},
    {"bkg_tex":"T_Bkg_Adu_06","pn_name":"Unknown Adult Film 2",                        "ls":10, "lsc":5,  "sku":69006002,   "ntu":False},
    {"bkg_tex":"T_Bkg_Adu_07","pn_name":"Fatal Attraction",                  "ls":6,  "lsc":3,  "sku":69032044, "ntu":False},
    {"bkg_tex":"T_Bkg_Adu_08","pn_name":"Basic Instinct",                    "ls":6,  "lsc":3,  "sku":69132163, "ntu":False},
    {"bkg_tex":"T_Bkg_Adu_09","pn_name":"Disclosure",                        "ls":6,  "lsc":3,  "sku":69263234, "ntu":False},
    {"bkg_tex":"T_Bkg_Adu_10","pn_name":"Blue Velvet",                       "ls":15, "lsc":8,  "sku":69223507, "ntu":False},
    {"bkg_tex":"T_Bkg_Adu_11","pn_name":"Unknown Adult Film 3",                        "ls":10, "lsc":5,  "sku":69011002,   "ntu":False},
    {"bkg_tex":"T_Bkg_Adu_12","pn_name":"Wild Things",                       "ls":6,  "lsc":3,  "sku":69250743, "ntu":False},
    {"bkg_tex":"T_Bkg_Adu_13","pn_name":"Bound",                             "ls":11, "lsc":6,  "sku":69162153, "ntu":False},
    {"bkg_tex":"T_Bkg_Adu_14","pn_name":"Crash",                             "ls":8,  "lsc":4,  "sku":69192838, "ntu":False},
    {"bkg_tex":"T_Bkg_Adu_15","pn_name":"The Lover",                         "ls":9,  "lsc":5,  "sku":69221162, "ntu":False},
    {"bkg_tex":"T_Bkg_Adu_16","pn_name":"Unknown Adult Film 4",                        "ls":10, "lsc":5,  "sku":69016002,   "ntu":False},
    {"bkg_tex":"T_Bkg_Adu_17","pn_name":"Eyes Wide Shut",                    "ls":19, "lsc":10, "sku":69282182, "ntu":False},
    {"bkg_tex":"T_Bkg_Adu_18","pn_name":"Striptease",                        "ls":10, "lsc":5,  "sku":69111225, "ntu":False},
]

ADVENTURE_SLOT_DATA = [
    {"bkg_tex":"T_Bkg_Adv_01","pn_name":"Conan the Barbarian",               "ls":5,  "lsc":3,  "sku":361001,   "ntu":False},
    {"bkg_tex":"T_Bkg_Adv_02","pn_name":"Mortal Kombat: Annihilation",       "ls":5,  "lsc":3,  "sku":451643,   "ntu":False},
    {"bkg_tex":"T_Bkg_Adv_03","pn_name":"Seven",                             "ls":10, "lsc":5,  "sku":291408,   "ntu":False},
]

COMEDY_SLOT_DATA = [
    {"bkg_tex":"T_Bkg_Com_01","pn_name":"Office Space",                      "ls":13, "lsc":7,  "sku":3942283,  "ntu":False},
    {"bkg_tex":"T_Bkg_Com_02","pn_name":"Baby Geniuses",                     "ls":10, "lsc":5,  "sku":3903012,  "ntu":False},
    {"bkg_tex":"T_Bkg_Com_03","pn_name":"Ace Ventura: Pet Detective",        "ls":1,  "lsc":1,  "sku":3002663,  "ntu":False},
    {"bkg_tex":"T_Bkg_Com_04","pn_name":"Ghostbusters",                      "ls":9,  "lsc":5,  "sku":3023482,  "ntu":False},
    {"bkg_tex":"T_Bkg_Com_05","pn_name":"Santa with Muscles",                "ls":20, "lsc":10, "sku":3723612,  "ntu":False},
    {"bkg_tex":"T_Bkg_Com_06","pn_name":"Hobgoblins",                        "ls":2,  "lsc":1,  "sku":3201304,  "ntu":False},
    {"bkg_tex":"T_Bkg_Com_07","pn_name":"Dumb and Dumber",                   "ls":13, "lsc":7,  "sku":3151763,  "ntu":False},
    {"bkg_tex":"T_Bkg_Com_08","pn_name":"The Big Lebowski",                  "ls":18, "lsc":9,  "sku":3002072,  "ntu":False},
    {"bkg_tex":"T_Bkg_Com_09","pn_name":"Groundhog Day",                     "ls":6,  "lsc":3,  "sku":3191781,  "ntu":False},
    {"bkg_tex":"T_Bkg_Com_10","pn_name":"Liar Liar",                         "ls":18, "lsc":9,  "sku":3114244,  "ntu":False},
    {"bkg_tex":"T_Bkg_Com_11","pn_name":"There's Something About Mary",      "ls":20, "lsc":10, "sku":3020844,  "ntu":False},
    {"bkg_tex":"T_Bkg_Com_12","pn_name":"3 Ninjas: High Noon at Mega Mountain","ls":7,"lsc":4,  "sku":3983912,  "ntu":False},
]

DRAMA_SLOT_DATA = [
    {"bkg_tex":"T_Bkg_Dra_01","pn_name":"The Legend of the Titanic",         "ls":19, "lsc":10, "sku":4241901,  "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_02","pn_name":"Extra Terrestrial Visitors",        "ls":2,  "lsc":1,  "sku":4062904,  "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_03","pn_name":"The Rainmaker",                     "ls":8,  "lsc":4,  "sku":4041674,  "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_04","pn_name":"Abraxas, Guardian of the Universe", "ls":5,  "lsc":3,  "sku":4800712,  "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_05","pn_name":"The Age of Innocence",              "ls":18, "lsc":9,  "sku":4053594,  "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_06","pn_name":"Rain Man",                          "ls":5,  "lsc":3,  "sku":4052275,  "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_07","pn_name":"The Shawshank Redemption",          "ls":13, "lsc":7,  "sku":4452894,  "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_08","pn_name":"Fight Club",                        "ls":6,  "lsc":3,  "sku":4113194,  "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_09","pn_name":"The Cider House Rules",             "ls":20, "lsc":10, "sku":4312044,  "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_10","pn_name":"The English Patient",               "ls":7,  "lsc":4,  "sku":4072495,  "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_11","pn_name":"The Green Mile",                    "ls":9,  "lsc":5,  "sku":4033294,  "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_12","pn_name":"Unknown Drama Film",                        "ls":10, "lsc":5,  "sku":4012002,   "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_13","pn_name":"Terms of Endearment",               "ls":12, "lsc":6,  "sku":4031154,  "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_14","pn_name":"Corrupt",                           "ls":19, "lsc":10, "sku":4681848,  "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_15","pn_name":"Lorenzo's Oil",                     "ls":15, "lsc":8,  "sku":4422264,  "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_16","pn_name":"Unknown Drama Film 2",                        "ls":10, "lsc":5,  "sku":4016002,   "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_17","pn_name":"10 Things I Hate About You",        "ls":17, "lsc":9,  "sku":4073442,  "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_18","pn_name":"Notting Hill",                      "ls":3,  "lsc":2,  "sku":4050662,  "ntu":False},
    {"bkg_tex":"T_Bkg_Dra_19","pn_name":"Forrest Gump",                      "ls":15, "lsc":8,  "sku":4161891,  "ntu":False},
]

FANTASY_SLOT_DATA = [
    {"bkg_tex":"T_Bkg_Fan_01","pn_name":"The Addams Family",                 "ls":4,  "lsc":2,  "sku":7042353,  "ntu":False},
    {"bkg_tex":"T_Bkg_Fan_02","pn_name":"Matilda",                           "ls":5,  "lsc":3,  "sku":7051853,  "ntu":False},
    {"bkg_tex":"T_Bkg_Fan_03","pn_name":"Hook",                              "ls":14, "lsc":7,  "sku":7024045,  "ntu":False},
    {"bkg_tex":"T_Bkg_Fan_04","pn_name":"The Mask",                          "ls":5,  "lsc":3,  "sku":7121751,  "ntu":False},
    {"bkg_tex":"T_Bkg_Fan_05","pn_name":"Hercules in New York",              "ls":14, "lsc":7,  "sku":7961512,  "ntu":False},
    {"bkg_tex":"T_Bkg_Fan_06","pn_name":"Highlander II: The Quickening",     "ls":19, "lsc":10, "sku":7513413,  "ntu":False},
    {"bkg_tex":"T_Bkg_Fan_07","pn_name":"Edward Scissorhands",               "ls":4,  "lsc":2,  "sku":7193273,  "ntu":False},
    {"bkg_tex":"T_Bkg_Fan_08","pn_name":"Being John Malkovich",              "ls":15, "lsc":8,  "sku":7102781,  "ntu":False},
    {"bkg_tex":"T_Bkg_Fan_09","pn_name":"Hocus Pocus",                       "ls":2,  "lsc":1,  "sku":7062654,  "ntu":False},
    {"bkg_tex":"T_Bkg_Fan_10","pn_name":"Star Wars: Episode VI Return of the Jedi","ls":6,"lsc":3,"sku":7092774,"ntu":False},
    {"bkg_tex":"T_Bkg_Fan_11","pn_name":"Sleepy Hollow",                     "ls":16, "lsc":8,  "sku":7001443,  "ntu":False},
]

KID_SLOT_DATA = [
    {"bkg_tex":"T_Bkg_Kid_01","pn_name":"A Bug's Life",                      "ls":5,  "lsc":3,  "sku":12053562, "ntu":False},
    {"bkg_tex":"T_Bkg_Kid_02","pn_name":"Supergirl",                         "ls":11, "lsc":6,  "sku":12051313, "ntu":False},
    {"bkg_tex":"T_Bkg_Kid_03","pn_name":"Stuart Little",                     "ls":13, "lsc":7,  "sku":12803232, "ntu":False},
    {"bkg_tex":"T_Bkg_Kid_04","pn_name":"Mrs. Doubtfire",                    "ls":10, "lsc":5,  "sku":12024854, "ntu":False},
    {"bkg_tex":"T_Bkg_Kid_05","pn_name":"Super Mario Bros.",                 "ls":15, "lsc":8,  "sku":12723621, "ntu":False},
    {"bkg_tex":"T_Bkg_Kid_06","pn_name":"Toy Story",                         "ls":9,  "lsc":5,  "sku":12001273, "ntu":False},
    {"bkg_tex":"T_Bkg_Kid_07","pn_name":"Casper",                            "ls":17, "lsc":9,  "sku":12231934, "ntu":False},
    {"bkg_tex":"T_Bkg_Kid_08","pn_name":"Look Who's Talking Now",            "ls":5,  "lsc":3,  "sku":12403613, "ntu":False},
    {"bkg_tex":"T_Bkg_Kid_09","pn_name":"E.T. the Extra-Terrestrial",        "ls":1,  "lsc":1,  "sku":12044274, "ntu":False},
    {"bkg_tex":"T_Bkg_Kid_10","pn_name":"Space Jam",                         "ls":9,  "lsc":5,  "sku":12463751, "ntu":False},
    {"bkg_tex":"T_Bkg_Kid_11","pn_name":"Aladdin",                           "ls":10, "lsc":5,  "sku":12063273, "ntu":False},
]

# AI NOTE (v1.8.1, April 2026): slot["sku"] stores the SKU value directly (no shift/conversion).
# For Police and all other genres, build() writes slot["sku"] at the schema's SKU offset.
# These default SKUs are carried over from the v1.8.0 encoding (high-byte = slot_idx+1,
# e.g. 0x0100005D) which produced valid positive int32 values. They remain functional under
# the new layout but were generated with a different formula than generate_sku() now uses.
# If you re-roll a Police SKU via the UI, it will use prefix=8 via generate_sku(), which
# gives cleaner values in the 8.0M..8.2M range. Existing defaults kept as-is to avoid
# shifting SKUs for existing user saves.
# Default rating: 4.5★ Common (last2=93).
POLICE_SLOT_DATA = [
    {"bkg_tex":"T_Bkg_Pol_01","pn_name":"Striking Distance",         "ls":3  ,"lsc":2  ,"sku":16777293, "ntu":False},
    {"bkg_tex":"T_Bkg_Pol_02","pn_name":"The Bone Collector",        "ls":15 ,"lsc":8  ,"sku":33554493, "ntu":False},
    {"bkg_tex":"T_Bkg_Pol_03","pn_name":"The Negotiator",            "ls":17 ,"lsc":9  ,"sku":50331693, "ntu":False},
    {"bkg_tex":"T_Bkg_Pol_04","pn_name":"Internal Affairs",          "ls":16 ,"lsc":8  ,"sku":67108893, "ntu":False},
    {"bkg_tex":"T_Bkg_Pol_05","pn_name":"Cop Land",                  "ls":14 ,"lsc":7  ,"sku":83886093, "ntu":False},
    {"bkg_tex":"T_Bkg_Pol_06","pn_name":"Ricochet",                  "ls":18 ,"lsc":9  ,"sku":100663393, "ntu":False},
    {"bkg_tex":"T_Bkg_Pol_07","pn_name":"Kiss the Girls",            "ls":16 ,"lsc":8  ,"sku":117440593, "ntu":False},
    {"bkg_tex":"T_Bkg_Pol_08","pn_name":"Rush Hour",                 "ls":15 ,"lsc":8  ,"sku":134217793, "ntu":False},
    {"bkg_tex":"T_Bkg_Pol_09","pn_name":"The Usual Suspects",        "ls":12 ,"lsc":6  ,"sku":150994993, "ntu":False},
    {"bkg_tex":"T_Bkg_Pol_10","pn_name":"Beverly Hills Cop",         "ls":10 ,"lsc":5  ,"sku":167772193, "ntu":False},
    {"bkg_tex":"T_Bkg_Pol_11","pn_name":"Maniac Cop 2",              "ls":18 ,"lsc":9  ,"sku":184549393, "ntu":False},
    {"bkg_tex":"T_Bkg_Pol_12","pn_name":"The Untouchables",          "ls":17 ,"lsc":9  ,"sku":201326593, "ntu":False},
    {"bkg_tex":"T_Bkg_Pol_13","pn_name":"Independence Day",          "ls":17 ,"lsc":9  ,"sku":218103893, "ntu":False},
]

# AI NOTE (April 2026): Romance SKUs were previously generated with prefix=10 (Fantasy's prefix).
# This caused SKU collisions in-game (same SKU → game picks wrong genre's movie).
# Fixed: use prefix=9 (Romance) with the confirmed single-band generate_sku approach.
ROMANCE_SLOT_DATA = [
    {"bkg_tex":"T_Bkg_Rom_01","pn_name":"Jerry Maguire",                     "ls":9,  "lsc":5,  "sku":90010093, "ntu":False},
    {"bkg_tex":"T_Bkg_Rom_02","pn_name":"Titanic",                           "ls":13, "lsc":7,  "sku":90020093, "ntu":False},
    {"bkg_tex":"T_Bkg_Rom_03","pn_name":"When Harry Met Sally...",           "ls":19, "lsc":10, "sku":90030093, "ntu":False},
    {"bkg_tex":"T_Bkg_Rom_04","pn_name":"Prem Aggan",                        "ls":4,  "lsc":2,  "sku":90040093, "ntu":False},
    {"bkg_tex":"T_Bkg_Rom_05","pn_name":"Before Sunrise",                    "ls":3,  "lsc":2,  "sku":90050093, "ntu":False},
    {"bkg_tex":"T_Bkg_Rom_06","pn_name":"Pretty Woman",                      "ls":1,  "lsc":1,  "sku":90060093, "ntu":False},
    {"bkg_tex":"T_Bkg_Rom_07","pn_name":"Space Mutiny",                      "ls":18, "lsc":9,  "sku":90070093, "ntu":False},
    {"bkg_tex":"T_Bkg_Rom_08","pn_name":"Romeo + Juliet",                    "ls":5,  "lsc":3,  "sku":90080093, "ntu":False},
    {"bkg_tex":"T_Bkg_Rom_09","pn_name":"American Ninja 5",                  "ls":5,  "lsc":3,  "sku":90090093, "ntu":False},
    {"bkg_tex":"T_Bkg_Rom_10","pn_name":"As Good as It Gets",                "ls":14, "lsc":7,  "sku":90100093, "ntu":False},
    {"bkg_tex":"T_Bkg_Rom_11","pn_name":"Dirty Dancing",                     "ls":18, "lsc":9,  "sku":90110093, "ntu":False},
    {"bkg_tex":"T_Bkg_Rom_12","pn_name":"Ghost",                             "ls":17, "lsc":9,  "sku":90120093, "ntu":False},
    {"bkg_tex":"T_Bkg_Rom_13","pn_name":"You've Got Mail",                   "ls":16, "lsc":8,  "sku":90130093, "ntu":False},
    {"bkg_tex":"T_Bkg_Rom_14","pn_name":"Shakespeare in Love",               "ls":8,  "lsc":4,  "sku":90140093, "ntu":False},
]

SCIFI_SLOT_DATA = [
    {"bkg_tex":"T_Bkg_Sci_01","pn_name":"Men in Black",                      "ls":20, "lsc":10, "sku":6041744,  "ntu":False},
    {"bkg_tex":"T_Bkg_Sci_02","pn_name":"Bicentennial Man",                  "ls":18, "lsc":9,  "sku":6043844,  "ntu":False},
    {"bkg_tex":"T_Bkg_Sci_03","pn_name":"Jurassic Park",                     "ls":9,  "lsc":5,  "sku":6004174,  "ntu":False},
    {"bkg_tex":"T_Bkg_Sci_04","pn_name":"12 Monkeys",                        "ls":16, "lsc":8,  "sku":6463284,  "ntu":False},
    {"bkg_tex":"T_Bkg_Sci_05","pn_name":"Johnny Mnemonic",                   "ls":9,  "lsc":5,  "sku":6312833,  "ntu":False},
    {"bkg_tex":"T_Bkg_Sci_06","pn_name":"Blade Runner",                      "ls":6,  "lsc":3,  "sku":6052882,  "ntu":False},
    {"bkg_tex":"T_Bkg_Sci_07","pn_name":"Lost in Space",                     "ls":4,  "lsc":2,  "sku":6631423,  "ntu":False},
    {"bkg_tex":"T_Bkg_Sci_08","pn_name":"Demolition Man",                    "ls":5,  "lsc":3,  "sku":6141853,  "ntu":False},
    {"bkg_tex":"T_Bkg_Sci_09","pn_name":"Stargate",                          "ls":11, "lsc":6,  "sku":6022251,  "ntu":False},
    {"bkg_tex":"T_Bkg_Sci_10","pn_name":"Soldier",                           "ls":13, "lsc":7,  "sku":6102931,  "ntu":False},
    {"bkg_tex":"T_Bkg_Sci_11","pn_name":"Escape from New York",              "ls":10, "lsc":5,  "sku":6152043,  "ntu":False},
    {"bkg_tex":"T_Bkg_Sci_12","pn_name":"Galaxy Quest",                      "ls":10, "lsc":5,  "sku":6250592,  "ntu":False},
    {"bkg_tex":"T_Bkg_Sci_13","pn_name":"Starship Troopers",                 "ls":14, "lsc":7,  "sku":6013843,  "ntu":False},
    {"bkg_tex":"T_Bkg_Sci_14","pn_name":"Strange Days",                      "ls":15, "lsc":8,  "sku":6001661,  "ntu":False},
    {"bkg_tex":"T_Bkg_Sci_15","pn_name":"eXistenZ",                          "ls":7,  "lsc":4,  "sku":6032752,  "ntu":False},
    {"bkg_tex":"T_Bkg_Sci_16","pn_name":"Deep Impact",                       "ls":1,  "lsc":1,  "sku":6222433,  "ntu":False},
    {"bkg_tex":"T_Bkg_Sci_17","pn_name":"Superman IV: The Quest for Peace",  "ls":14, "lsc":7,  "sku":6262215,  "ntu":False},
    {"bkg_tex":"T_Bkg_Sci_18","pn_name":"The Arrival",                       "ls":10, "lsc":5,  "sku":6012733,  "ntu":False},
]
WESTERN_SLOT_DATA = [
    {"bkg_tex":"T_Bkg_Wst_01","pn_name":"Unknown Western Film",    "ls":10, "lsc":5,  "sku":15001002,  "ntu":False},
    {"bkg_tex":"T_Bkg_Wst_02","pn_name":"Unknown Western Film 2",  "ls":10, "lsc":5,  "sku":15002002,  "ntu":False},
    {"bkg_tex":"T_Bkg_Wst_03","pn_name":"Maverick",                          "ls":11, "lsc":6,  "sku":17013554, "ntu":False},
    {"bkg_tex":"T_Bkg_Wst_04","pn_name":"Unforgiven",                        "ls":20, "lsc":10, "sku":17082209, "ntu":False},
    {"bkg_tex":"T_Bkg_Wst_05","pn_name":"Gone with the West",                "ls":20, "lsc":10, "sku":17343512, "ntu":False},
    {"bkg_tex":"T_Bkg_Wst_06","pn_name":"The Outlaw Josey Wales",            "ls":17, "lsc":9,  "sku":17022694, "ntu":False},
    {"bkg_tex":"T_Bkg_Wst_07","pn_name":"Wyatt Earp",                        "ls":20, "lsc":10, "sku":17002243, "ntu":False},
    {"bkg_tex":"T_Bkg_Wst_08","pn_name":"Young Guns II",                     "ls":13, "lsc":7,  "sku":17002062, "ntu":False},
    {"bkg_tex":"T_Bkg_Wst_09","pn_name":"Unknown Western Film 3",                        "ls":10, "lsc":5,  "sku":15009002,   "ntu":False},
    {"bkg_tex":"T_Bkg_Wst_10","pn_name":"Geronimo: An American Legend",      "ls":20, "lsc":10, "sku":17102933, "ntu":False},
]

XMAS_SLOT_DATA = [
    {"bkg_tex":"T_Bkg_Xma_01","pn_name":"A Christmas Carol",                 "ls":12, "lsc":6,  "sku":18752811, "ntu":False},
    {"bkg_tex":"T_Bkg_Xma_02","pn_name":"Silent Night, Deadly Night Part 2", "ls":17, "lsc":9,  "sku":18191413, "ntu":False},
    {"bkg_tex":"T_Bkg_Xma_03","pn_name":"Batman Returns",                    "ls":15, "lsc":8,  "sku":18044863, "ntu":False},
    {"bkg_tex":"T_Bkg_Xma_04","pn_name":"Home Alone 3",                      "ls":6,  "lsc":3,  "sku":18023334, "ntu":False},
    {"bkg_tex":"T_Bkg_Xma_05","pn_name":"Home Alone 2: Lost in New York",    "ls":17, "lsc":9,  "sku":18002744, "ntu":False},
    {"bkg_tex":"T_Bkg_Xma_06","pn_name":"The Nightmare Before Christmas",    "ls":9,  "lsc":5,  "sku":18031883, "ntu":False},
    {"bkg_tex":"T_Bkg_Xma_07","pn_name":"Home Alone",                        "ls":17, "lsc":9,  "sku":18051684, "ntu":False},
    {"bkg_tex":"T_Bkg_Xma_08","pn_name":"While You Were Sleeping",           "ls":6,  "lsc":3,  "sku":18053154, "ntu":False},
    {"bkg_tex":"T_Bkg_Xma_09","pn_name":"The Santa Clause",                  "ls":2,  "lsc":1,  "sku":18063652, "ntu":False},
    {"bkg_tex":"T_Bkg_Xma_10","pn_name":"The Muppet Christmas Carol",        "ls":13, "lsc":7,  "sku":18020682, "ntu":False},
    {"bkg_tex":"T_Bkg_Xma_11","pn_name":"Unknown Christmas Film",                        "ls":10, "lsc":5,  "sku":18011002,   "ntu":False},
    {"bkg_tex":"T_Bkg_Xma_12","pn_name":"National Lampoon's Christmas Vacation","ls":4,"lsc":2, "sku":18004173, "ntu":False},
]

THRILLER_SLOT_DATA = []
MUSIC_SLOT_DATA = []
HISTORY_SLOT_DATA = []
DOCUMENTARY_SLOT_DATA = []
SPORTS_SLOT_DATA = []

# Map dt_name -> slot data
CLEAN_DT_SLOT_DATA = {
    "Action":           ACTION_SLOT_DATA,
    "Adult":            ADULT_SLOT_DATA,
    "Adventure":        ADVENTURE_SLOT_DATA,
    "Comedy":           COMEDY_SLOT_DATA,
    "Drama":            DRAMA_SLOT_DATA,
    "Fantasy":          FANTASY_SLOT_DATA,
    "Horror":           HORROR_SLOT_DATA,
    "Kid":              KID_SLOT_DATA,
    "Police":           POLICE_SLOT_DATA,
    "Romance":          ROMANCE_SLOT_DATA,
    "Sci-Fi":           SCIFI_SLOT_DATA,
    "Western":          WESTERN_SLOT_DATA,
    "Xmas":             XMAS_SLOT_DATA,
    "Thriller":         THRILLER_SLOT_DATA,
    "Music":            MUSIC_SLOT_DATA,
    "History":          HISTORY_SLOT_DATA,
    "Documentary":      DOCUMENTARY_SLOT_DATA,
    "Sports":           SPORTS_SLOT_DATA,
}

# ── 3-digit slot remapping ──────────────────────────────────────────────
# Remap all base game 2-digit bkg_tex names to 3-digit (01→100, 02→101, etc.)
# This ensures ALL DataTable rows use uniform 13-char bkg names (73-byte rows).
def _remap_slot_to_3digit(bkg_tex):
    """Migrate old 2-digit custom slot names to 3-digit zero-padded.
    E.g. T_Bkg_Dra_20 → T_Bkg_Dra_020."""
    parts = bkg_tex.split('_')
    if len(parts) >= 4:
        try:
            num = int(parts[3])
            if num < 100 and len(parts[3]) < 3:
                parts[3] = f"{num:03d}"
                return '_'.join(parts)
        except ValueError:
            pass
    return bkg_tex

# Base game slots keep their original 2-digit bkg_tex names (e.g. T_Bkg_Dra_01).
# They are never written to the DataTable — only custom slots are.
# Custom slots get 3-digit names (001-999) assigned by add_movie_slot().
# No remapping of base game slots is needed or desired.

# Now that CLEAN_DT_SLOT_DATA is defined, build the texture list
rebuild_texture_list()


def save_custom_slots():
    """Persist all custom (non-original) slots to custom_slots.json,
    and all edits to base-game slots to base_slot_edits.json."""
    data = {}
    for dt_name, slot_list in CLEAN_DT_SLOT_DATA.items():
        # Find the base count for this genre
        genre = next((g for g, d in GENRE_DATATABLE.items() if d == dt_name), None)
        base_count = GENRES[genre]["bkg"] if genre else 0
        extra = slot_list[base_count:]
        if extra:
            data[dt_name] = extra
    with open(CUSTOM_SLOTS_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"[CustomSlots] Saved {sum(len(v) for v in data.values())} custom slot(s)")
    save_base_edits()


def load_custom_slots():
    """Load persisted custom slots into CLEAN_DT_SLOT_DATA on startup."""
    if not os.path.exists(CUSTOM_SLOTS_FILE):
        return
    with open(CUSTOM_SLOTS_FILE) as f:
        data = json.load(f)
    migrated = False
    for dt_name, extra_slots in data.items():
        if dt_name in CLEAN_DT_SLOT_DATA:
            existing_tex = {s["bkg_tex"] for s in CLEAN_DT_SLOT_DATA[dt_name]}
            genre_key  = next((g for g, d in GENRE_DATATABLE.items() if d == dt_name), None)
            base_count = GENRES[genre_key]["bkg"] if genre_key else 0
            custom_seq = 0  # counts custom slots for this genre in order
            for slot in extra_slots:
                # Migrate 2-digit bkg_tex to 3-digit (e.g. T_Bkg_Hor_23 → T_Bkg_Hor_122)
                old_bkg = slot["bkg_tex"]
                new_bkg = _remap_slot_to_3digit(old_bkg)
                if old_bkg != new_bkg:
                    slot["bkg_tex"] = new_bkg
                    migrated = True
                    print(f"[CustomSlots] Migrated bkg_tex: {old_bkg} → {new_bkg}")
                if slot["bkg_tex"] not in existing_tex:
                    custom_seq += 1
                    # Migrate old-style slots that used T_Sub_02 as dedicated SI
                    old_si = slot.get("sub_tex", "")
                    if not old_si or old_si == TSUB_CUSTOM or \
                       not old_si.startswith("T_Sub_") or \
                       int(old_si.replace("T_Sub_","")) < TSUB_CUSTOM_BASE:
                        new_si = get_custom_slot_si(custom_seq)
                        slot["sub_tex"] = new_si
                        migrated = True
                        print(f"[CustomSlots] Migrated {slot['bkg_tex']}: "
                              f"sub_tex {old_si!r} -> {new_si}")
                    CLEAN_DT_SLOT_DATA[dt_name].append(slot)
                    existing_tex.add(slot["bkg_tex"])
    if migrated:
        save_custom_slots()
    rebuild_texture_list()
    total = sum(len(v) for v in data.values())
    print(f"[CustomSlots] Loaded {total} custom slot(s)")


def add_movie_slot(genre, title, ls=FIXED_REGULAR_LAYOUT, lsc=4, sku=None, last2=93, rarity="Common"):
    """
    Add a new movie slot to a genre's slot data at runtime.
    Persists to custom_slots.json so it survives tool restarts.

    Custom slots use a dedicated T_Sub_78+ as SubjectImage so the
    T_Bkg_Hor_XX background cover art shows through fully.

    The base game AssetRegistry.bin pre-registers T_Bkg_Hor_23..78 (Horror),
    so custom Horror slots up to slot 78 work without any registry patching.

    Returns the new slot's bkg_tex name e.g. "T_Bkg_Hor_23", or None on error.
    """
    title_err = _movie_title_error(title)
    if title_err:
        print(f"[AddSlot] ERROR: {title_err}")
        return None

    dt_name = GENRE_DATATABLE.get(genre)
    if dt_name not in CLEAN_DT_SLOT_DATA:
        print(f"[AddSlot] Genre '{genre}' not in CLEAN_DT_SLOT_DATA")
        return None

    genre_info  = GENRES[genre]
    code        = genre_info["code"]
    slot_data   = CLEAN_DT_SLOT_DATA[dt_name]

    # Find the lowest unused slot number starting from 1.
    # Slots use zero-padded 3-digit names for <100 (e.g. T_Bkg_Dra_001).
    # The texture uasset uses literal FName style (stored_number=0),
    # matching how the base game handles slots 01-09.
    prefix = f"T_Bkg_{code}_"
    base_count = genre_info["bkg"]
    existing_nums = set()
    for s in slot_data[base_count:]:
        bkg = s.get("bkg_tex", "")
        if bkg.startswith(prefix):
            try:
                existing_nums.add(int(bkg[len(prefix):]))
            except ValueError:
                pass
    new_idx = 1
    while new_idx in existing_nums:
        new_idx += 1

    bkg_tex = (f"T_Bkg_{code}_{new_idx:03d}" if new_idx < 100
               else f"T_Bkg_{code}_{new_idx}")

    # Enforce the pre-registered slot cap (if known for this genre)
    bkg_max = genre_info.get("bkg_max")
    if bkg_max and new_idx > bkg_max:
        print(f"[AddSlot] ERROR: {genre} exceeds max slot {bkg_max} "
              f"(max = {bkg_max})")
        return None

    # Count existing custom slots for this genre to assign the right T_Sub number
    base_count   = genre_info["bkg"]
    custom_count = len(slot_data) - base_count  # how many custom slots exist already
    dedicated_si = get_custom_slot_si(custom_count + 1)  # 1-based: first = T_Sub_78

    if sku is None:
        # Build used_skus BEFORE appending so the new SKU is guaranteed unique
        # against everything currently in memory (including any slots just added
        # in the same session that haven't been saved yet).
        used = _all_used_skus()
        sku = generate_sku(genre, new_idx, last2=last2, rarity=rarity, used_skus=used)

    new_slot = {
        "bkg_tex": bkg_tex,
        "sub_tex": dedicated_si,   # unique T_Sub_78+ for this custom slot
        "pn_name": title,
        "ls":      ls,
        "lsc":     lsc,
        "sku":     sku,
        "ntu":     False,
    }
    slot_data.append(new_slot)
    save_custom_slots()
    rebuild_texture_list()
    print(f"[AddSlot] Added '{title}' as {bkg_tex} (SI={dedicated_si}) to {genre} "
          f"[slot {new_idx}/{bkg_max or '?'}]")
    return bkg_tex


def remove_last_movie_slot(genre):
    """Remove the last added (custom) slot from a genre."""
    dt_name    = GENRE_DATATABLE.get(genre)
    base_count = GENRES[genre]["bkg"]
    if dt_name not in CLEAN_DT_SLOT_DATA:
        return False
    slot_data = CLEAN_DT_SLOT_DATA[dt_name]
    if len(slot_data) <= base_count:
        return False
    slot_data.pop()
    save_custom_slots()
    rebuild_texture_list()
    return True

# Load any previously saved custom slots on startup
load_custom_slots()
load_nr_slots()


def save_base_edits():
    """
    Persist edits to base-game slots (SKU and pn_name changes) to base_slot_edits.json.

    Format: { "dt_name": { "bkg_tex": { "sku": int, "pn_name": str }, ... }, ... }

    Only slots that differ from their hardcoded defaults are stored, keeping the file
    small and the defaults self-documenting in the source.
    """
    # We need the original defaults to detect changes.
    # Re-import defaults by re-reading the module-level *_SLOT_DATA lists at their
    # original values — but since they're mutated in place we track changes by
    # comparing against a snapshot taken at load time (_BASE_SLOT_DEFAULTS).
    data = {}
    for dt_name, slot_list in CLEAN_DT_SLOT_DATA.items():
        genre = next((g for g, d in GENRE_DATATABLE.items() if d == dt_name), None)
        base_count = GENRES[genre]["bkg"] if genre else 0
        defaults   = _BASE_SLOT_DEFAULTS.get(dt_name, {})
        for slot in slot_list[:base_count]:
            bkg = slot["bkg_tex"]
            default = defaults.get(bkg, {})
            entry = {}
            if slot.get("sku")     != default.get("sku"):     entry["sku"]     = slot["sku"]
            if slot.get("pn_name") != default.get("pn_name"): entry["pn_name"] = slot["pn_name"]
            if slot.get("ls")      != default.get("ls"):      entry["ls"]      = slot["ls"]
            if slot.get("lsc")     != default.get("lsc"):     entry["lsc"]     = slot["lsc"]
            if entry:
                data.setdefault(dt_name, {})[bkg] = entry
    with open(BASE_EDITS_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    total = sum(len(v) for v in data.values())
    print(f"[BaseEdits] Saved {total} base-slot edit(s)")


def load_base_edits():
    """Apply persisted base-slot edits from base_slot_edits.json into CLEAN_DT_SLOT_DATA."""
    if not os.path.exists(BASE_EDITS_FILE):
        return
    with open(BASE_EDITS_FILE) as f:
        data = json.load(f)
    # Migrate 2-digit keys to 3-digit
    migrated_data = {}
    _changed = False
    for dt_name, edits in data.items():
        new_edits = {}
        for bkg_key, edit_val in edits.items():
            new_key = _remap_slot_to_3digit(bkg_key)
            if new_key != bkg_key:
                _changed = True
            new_edits[new_key] = edit_val
        migrated_data[dt_name] = new_edits
    if _changed:
        with open(BASE_EDITS_FILE, 'w') as f:
            json.dump(migrated_data, f, indent=2)
        print("[BaseEdits] Migrated 2-digit keys to 3-digit")
        data = migrated_data
    total = 0
    for dt_name, edits in data.items():
        slot_list = CLEAN_DT_SLOT_DATA.get(dt_name, [])
        for slot in slot_list:
            bkg = slot["bkg_tex"]
            if bkg in edits:
                edit = edits[bkg]
                if "sku"     in edit: slot["sku"]     = edit["sku"]
                if "pn_name" in edit: slot["pn_name"] = edit["pn_name"]
                if "ls"      in edit: slot["ls"]      = edit["ls"]
                if "lsc"     in edit: slot["lsc"]     = edit["lsc"]
                total += 1
    print(f"[BaseEdits] Applied {total} base-slot edit(s)")


# Snapshot the original default values BEFORE load_base_edits mutates them.
# This is used by save_base_edits to detect which slots actually changed.
_BASE_SLOT_DEFAULTS = {
    dt_name: {slot["bkg_tex"]: {"sku": slot["sku"], "pn_name": slot["pn_name"],
                                "ls": slot.get("ls", 7), "lsc": slot.get("lsc", 4)}
              for slot in slot_list}
    for dt_name, slot_list in CLEAN_DT_SLOT_DATA.items()
}

load_base_edits()


class DataTableManager:
    """
    Reads and patches movie titles in UE4 DataTable binary files.
    Also builds clean DataTables via CleanDataTableBuilder for
    genres listed in CLEAN_DT_SLOT_DATA.
    Persists changes to title_changes.json between sessions.
    """

    def __init__(self, pak_cache, saved_changes=None):
        self.pak_cache      = pak_cache
        self._binary_cache  = {}    # dt_name -> bytearray
        self._modified_dts  = set()
        self._titles_cache  = {}    # texture_name -> [entry_dicts]
        self._saved_changes = saved_changes or {}  # {original: new}
        self._clean_builders = {}   # dt_name -> CleanDataTableBuilder

    # ----------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------

    def _get_dt_name(self, texture):
        name = texture["name"]
        if name.startswith("T_New_"):
            return NEW_RELEASE_DATATABLE
        return GENRE_DATATABLE.get(texture["genre"])

    def _ensure_loaded(self, dt_name):
        """Extract DataTable uasset from pak and replay any saved changes."""
        if dt_name in self._binary_cache:
            return True

        extract_dir = self.pak_cache._extract_dir
        dest        = os.path.join(extract_dir, "RetroRewind", "Content",
                                   "VideoStore", "core", "blueprint", "data")
        uasset_path = os.path.join(dest, f"{dt_name}.uasset")

        if not os.path.exists(uasset_path):
            os.makedirs(dest, exist_ok=True)
            for ext in ["uasset", "uexp"]:
                pak_entry = f"{DATATABLE_PATH}/{dt_name}.{ext}"
                subprocess.run(
                    [self.pak_cache.repak_path, "unpack",
                     "-o", extract_dir, "-f",
                     "-i", pak_entry,
                     self.pak_cache.pak_path],
                    capture_output=True, timeout=30
                )

        if not os.path.exists(uasset_path):
            print(f"[DataTable] Could not extract {dt_name}.uasset")
            return False

        with open(uasset_path, 'rb') as f:
            self._binary_cache[dt_name] = bytearray(f.read())

        # Replay any saved title changes for this DataTable
        replayed = 0
        for original, new_title in self._saved_changes.items():
            offset = self._find_fstring(self._binary_cache[dt_name], original)
            if offset >= 0:
                self._patch_in_place(dt_name, offset, new_title)
                replayed += 1
            else:
                # Maybe was already patched from a previous session -
                # try finding the new title to confirm it's there
                if self._find_fstring(self._binary_cache[dt_name], new_title) >= 0:
                    pass  # already applied
        
        if replayed:
            self._modified_dts.add(dt_name)
            print(f"[DataTable] Loaded {dt_name}, replayed {replayed} saved change(s)")
        else:
            print(f"[DataTable] Loaded {dt_name} ({len(self._binary_cache[dt_name])} bytes)")
        return True

    def _find_fstring(self, data, text):
        """Find FString offset for text. Returns -1 if not found."""
        needle = text.encode('utf-8')
        pos = 0
        while True:
            p = data.find(needle, pos)
            if p < 0:
                return -1
            if p >= 4:
                prefix = struct.unpack_from('<i', data, p-4)[0]
                if prefix == len(needle) + 1:
                    return p - 4
            pos = p + 1

    def _find_fstring_padded(self, data, text):
        """Find FString even if it was padded with trailing spaces."""
        # Try exact match first
        offset = self._find_fstring(data, text)
        if offset >= 0:
            return offset
        # Try padded versions (up to 50 spaces of padding)
        for pad in range(1, 51):
            padded = text + ' ' * pad
            offset = self._find_fstring(data, padded)
            if offset >= 0:
                return offset
        return -1

    def _patch_in_place(self, dt_name, offset, new_title):
        """Patch string in binary, padding with spaces to match length."""
        data        = self._binary_cache[dt_name]
        old_len     = struct.unpack_from('<i', data, offset)[0]
        old_str_len = old_len - 1
        new_encoded = new_title.encode('utf-8')
        if len(new_encoded) > old_str_len:
            return False
        padded = new_encoded + b' ' * (old_str_len - len(new_encoded))
        start  = offset + 4
        for i, b in enumerate(padded):
            data[start + i] = b
        return True

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    def get_current_title(self, original_title):
        """Return the current (possibly renamed) version of a title."""
        return self._saved_changes.get(original_title, original_title).rstrip()

    def get_titles_for_texture(self, texture):
        """
        Returns list of dicts: {title, original_title, dt_name, offset, max_len}
        For clean-DT genres (Horror), reads from CLEAN_DT_SLOT_DATA.
        For others, reads from the binary DataTable.
        """
        name    = texture["name"]
        dt_name = self._get_dt_name(texture)

        if name in self._titles_cache:
            return self._titles_cache[name]

        # --- Clean DT path: title comes from slot data ---
        if dt_name in CLEAN_DT_SLOT_DATA:
            slot_data = CLEAN_DT_SLOT_DATA[dt_name]
            for slot in slot_data:
                if slot["bkg_tex"] == name:
                    original = slot["pn_name"]
                    current  = self._saved_changes.get(original, original)
                    # max_len: clean DataTables can add new UE name-table entries
                    result = [{
                        "title":          current.rstrip(),
                        "original_title": original,
                        "dt_name":        dt_name,
                        "offset":         -1,   # not a binary offset
                        "max_len":        MOVIE_TITLE_MAX_BYTES,
                    }]
                    self._titles_cache[name] = result
                    return result
            self._titles_cache[name] = []
            return []

        # --- Legacy binary path ---
        if not dt_name or not self._ensure_loaded(dt_name):
            return []

        data  = self._binary_cache[dt_name]
        info  = TEXTURE_MOVIES.get(name, {})
        hints = info.get("movies", [])[:1]

        results = []
        for original in hints:
            current = self._saved_changes.get(original, original)
            offset  = self._find_fstring_padded(data, current)
            if offset < 0:
                offset = self._find_fstring_padded(data, original)
            if offset >= 0:
                max_len      = struct.unpack_from("<i", data, offset)[0] - 1
                actual_bytes = bytes(data[offset+4:offset+4+max_len])
                actual_text  = actual_bytes.decode("utf-8", errors="replace").rstrip()
                results.append({
                    "title":          actual_text,
                    "original_title": original,
                    "dt_name":        dt_name,
                    "offset":         offset,
                    "max_len":        max_len,
                })
            else:
                print(f"[DataTable] Not found: '{current}' in {dt_name}")

        self._titles_cache[name] = results
        return results

    def patch_title(self, dt_name, offset, original_title, new_title, app_title_changes,
                    bkg_tex=None):
        """
        Patch title.  For clean-DT genres (offset=-1), just saves the override.
        For legacy genres, patches the binary in-place.
        bkg_tex: if provided, used to identify the specific slot (avoids
                 matching by title which breaks when multiple slots share a name).
        Returns (True, None) or (False, error_msg).
        """
        new_title_stripped = new_title.strip()
        if not new_title_stripped:
            return False, "Title cannot be empty."

        # --- Clean DT path ---
        if offset == -1:
            title_err = _movie_title_error(new_title_stripped)
            if title_err:
                return False, title_err

            # Is this a custom slot (not in original base count)?
            # If so, update pn_name directly in slot data + custom_slots.json
            is_custom = False
            slots = CLEAN_DT_SLOT_DATA.get(dt_name, [])
            genre = next((g for g, d in GENRE_DATATABLE.items() if d == dt_name), None)
            base_count = GENRES[genre]["bkg"] if genre else 0
            for idx, slot in enumerate(slots):
                # Match by bkg_tex (unique) when available, fall back to pn_name
                if bkg_tex and slot.get("bkg_tex") == bkg_tex and idx >= base_count:
                    slot["pn_name"] = new_title_stripped
                    is_custom = True
                    break
                elif not bkg_tex and slot["pn_name"] == original_title and idx >= base_count:
                    slot["pn_name"] = new_title_stripped
                    is_custom = True
                    break

            if is_custom:
                # Also update _saved_changes so get_current_title reflects it
                # Remove old key if present, no override needed since pn_name is updated
                self._saved_changes.pop(original_title, None)
                app_title_changes.pop(original_title, None)
                save_title_changes(app_title_changes)
                save_custom_slots()
            else:
                # Original base slot: store as override
                app_title_changes[original_title] = new_title_stripped
                save_title_changes(app_title_changes)
                self._saved_changes[original_title] = new_title_stripped

            self._modified_dts.add(dt_name)
            self._titles_cache = {}
            print(f"[DataTable] Clean title: '{original_title}' -> '{new_title_stripped}' "
                  f"({'custom slot' if is_custom else 'override'})")
            return True, None

        # --- Legacy binary path ---
        if dt_name not in self._binary_cache:
            return False, "DataTable not loaded"

        data        = self._binary_cache[dt_name]
        old_len     = struct.unpack_from("<i", data, offset)[0]
        old_str_len = old_len - 1

        new_encoded = new_title_stripped.encode("utf-8")
        if len(new_encoded) > old_str_len:
            return False, (
                f"Title too long! Max {old_str_len} chars, "
                f"got {len(new_encoded)}. Please shorten by "
                f"{len(new_encoded) - old_str_len} character(s)."
            )

        if not self._patch_in_place(dt_name, offset, new_title_stripped):
            return False, "Patch failed"

        self._modified_dts.add(dt_name)
        app_title_changes[original_title] = new_title_stripped
        save_title_changes(app_title_changes)
        self._titles_cache = {}
        print(f"[DataTable] Patched '{original_title}' -> '{new_title_stripped}' in {dt_name}")
        return True, None

    def get_modified_datatables(self):
        # A DataTable is "modified" if it has in-place patches OR
        # if it has a CleanDataTableBuilder entry (always regenerated cleanly)
        modified = {k: v for k, v in self._binary_cache.items()
                    if k in self._modified_dts}
        for dt_name in CLEAN_DT_SLOT_DATA:
            # Skip hidden genres (Adventure) — building an empty DataTable
            # that overrides the base game's version can crash the async loader.
            genre_key = next((g for g, info in GENRES.items()
                              if GENRE_DATATABLE.get(g) == dt_name), None)
            if genre_key in HIDDEN_GENRES:
                continue
            if dt_name not in modified:
                modified[dt_name] = None  # signals clean build needed
        return modified

    def save_datatable(self, dt_name, output_dir):
        """
        Save a DataTable to output_dir.
        For genres with CLEAN_DT_SLOT_DATA, use CleanDataTableBuilder
        so titles, linked-list pointers and row structure are all correct.
        For other genres, fall back to the in-place patched binary.
        """
        dest = os.path.join(output_dir, "RetroRewind", "Content",
                            "VideoStore", "core", "blueprint", "data")
        os.makedirs(dest, exist_ok=True)

        # --- Clean build path ---
        if dt_name in CLEAN_DT_SLOT_DATA:
            builder = self._clean_builders.get(dt_name)
            if builder is None:
                builder = CleanDataTableBuilder(self.pak_cache, dt_name)
                self._clean_builders[dt_name] = builder

            slot_data = CLEAN_DT_SLOT_DATA[dt_name]
            if CUSTOM_ONLY_MODE:
                # Pass only user-added slots (index >= base_count)
                genre_key  = next((g for g, d in GENRE_DATATABLE.items()
                                   if d == dt_name), None)
                base_count = GENRES[genre_key]["bkg"] if genre_key else 0
                slot_data  = slot_data[base_count:]
            ua, ue = builder.build(slot_data, self._saved_changes,
                                          custom_only=CUSTOM_ONLY_MODE)
            if ua is None:
                print(f"[DataTable] CleanDT build failed for {dt_name}")
                return False

            uasset_path = os.path.join(dest, f"{dt_name}.uasset")
            with open(uasset_path, "wb") as f:
                f.write(ua)
            with open(os.path.join(dest, f"{dt_name}.uexp"), "wb") as f:
                f.write(ue)
            print(f"[DataTable] Clean build: {dt_name} ({len(ue)} bytes uexp)")
            return True

        # --- Legacy in-place patch path ---
        if dt_name not in self._binary_cache:
            return False

        uasset_out = os.path.join(dest, f"{dt_name}.uasset")
        with open(uasset_out, "wb") as f:
            f.write(self._binary_cache[dt_name])
        print(f"[DataTable] Wrote {dt_name}.uasset ({len(self._binary_cache[dt_name])} bytes)")

        src_uexp = os.path.join(
            self.pak_cache._extract_dir,
            "RetroRewind", "Content", "VideoStore",
            "core", "blueprint", "data", f"{dt_name}.uexp"
        )
        if not os.path.exists(src_uexp):
            pak_entry = f"{DATATABLE_PATH}/{dt_name}.uexp"
            subprocess.run(
                [self.pak_cache.repak_path, "unpack",
                 "-o", self.pak_cache._extract_dir, "-f",
                 "-i", pak_entry,
                 self.pak_cache.pak_path],
                capture_output=True, timeout=30
            )

        if os.path.exists(src_uexp):
            shutil.copy2(src_uexp, os.path.join(dest, f"{dt_name}.uexp"))
            return True
        print(f"[DataTable] WARNING: {dt_name}.uexp not found!")
        return False



# ============================================================
# PAK READER - extract ubulk from pak without full unpack
# ============================================================

class PakCache:
    """
    Extracts files from the base game pak on demand for previews and DataTable sources.
    No mod pak required — the tool is fully independent.
    Files are extracted once and cached; subsequent runs use the cached files.
    """
    def __init__(self, pak_path, repak_path, base_game_pak=None):
        # pak_path IS the base game pak. base_game_pak param kept for compat but unused.
        self.pak_path      = pak_path
        self.repak_path    = repak_path
        self.base_game_pak = pak_path   # always the same — no mod pak
        self._cache        = {}   # name -> PIL Image
        self._extract_dir  = os.path.join(OUTPUT_DIR, "_pak_cache")
        self._base_extract = self._extract_dir  # unified cache — only one pak
        self._unpacked     = False
        self._base_dir     = os.path.join(
            self._extract_dir,
            "RetroRewind", "Content", "VideoStore",
            "asset", "prop", "vhs", "Background"
        )
        self._base_game_dir = self._base_dir  # same directory — one pak

        # Cache invalidation by tool version. Bugs in asset-rebuild logic
        # (clone_texture_3digit, _rebuild_uasset_with_name_patches, …) can
        # leave broken files in the cache that survive across runs because
        # the cache only checks for file existence, not correctness. When
        # a new tool version ships a fix, force re-extraction.
        self._stamp_path = os.path.join(self._extract_dir, ".tool_version")
        self._invalidate_stale_cache()

    def _invalidate_stale_cache(self):
        stamp = None
        if os.path.exists(self._stamp_path):
            try:
                with open(self._stamp_path) as f:
                    stamp = f.read().strip()
            except Exception:
                stamp = None
        if stamp == TOOL_VERSION:
            return
        if os.path.exists(self._extract_dir) and os.listdir(self._extract_dir):
            print(f"[PakCache] Cache stamp {stamp!r} != {TOOL_VERSION!r} — wiping to rebuild")
            try:
                shutil.rmtree(self._extract_dir)
            except Exception as e:
                print(f"[PakCache] Could not wipe stale cache: {e}")
                return
        os.makedirs(self._extract_dir, exist_ok=True)
        try:
            with open(self._stamp_path, "w") as f:
                f.write(TOOL_VERSION)
        except Exception as e:
            print(f"[PakCache] Could not write version stamp: {e}")
            
    def _ensure_unpacked(self):
        """Unpack the whole pak if not done yet."""
        if self._unpacked:
            return True
        # Already unpacked from a previous run
        if os.path.exists(self._base_dir):
            self._unpacked = True
            return True
        # Unpack now
        os.makedirs(self._extract_dir, exist_ok=True)
        try:
            print(f"[PakCache] Unpacking: {self.pak_path}")
            print(f"[PakCache] Into: {self._extract_dir}")

            # Clear any partial previous extraction
            if os.path.exists(self._extract_dir):
                shutil.rmtree(self._extract_dir)
            # Pre-create the output folder - repak needs it to exist
            os.makedirs(self._extract_dir, exist_ok=True)

            result = subprocess.run(
                [self.repak_path, "unpack",
                 "-o", self._extract_dir,
                 "-f",
                 self.pak_path],
                capture_output=True, text=True, timeout=120
            )
            print(f"[PakCache] repak stdout: {result.stdout[:300]}")
            print(f"[PakCache] repak stderr: {result.stderr[:300]}")
            print(f"[PakCache] base_dir exists: {os.path.exists(self._base_dir)}")

            # repak may return access denied at end (trying to rename pak)
            # but files are still extracted - check if base dir exists
            if os.path.exists(self._base_dir):
                self._unpacked = True
                return True
            return False
        except Exception as e:
            print(f"[PakCache] Exception: {e}")
            return False

    def get_thumbnail(self, texture):
        """Return a small PIL Image (tile-sized) for a texture, or None.
        Reads from the in-memory thumbnail cache populated by preload_all_thumbnails().
        Falls back to None if not yet loaded."""
        key = "__thumb_" + texture["name"]
        return self._cache.get(key)

    def preload_all_thumbnails(self, all_textures, thumb_w=72, thumb_h=90,
                                progress_cb=None):
        """
        Batch-extract and decode thumbnails for all T_Bkg textures.

        Strategy:
          1. One repak call with 'Background/' filter extracts ALL ubulk files at once.
          2. Decode each ubulk (mip0, 1024×2048 DXT1) and thumbnail() to thumb_w×thumb_h.
          3. Cache under '__thumb_{name}'.

        progress_cb(done, total) called after each texture decoded.
        Returns number of thumbnails successfully loaded.
        """
        from concurrent.futures import ThreadPoolExecutor
        import time

        # Ensure Background/ folder is extracted (one repak call for all)
        if not os.path.exists(self._base_dir):
            os.makedirs(self._extract_dir, exist_ok=True)
            print("[PakCache] Batch-extracting Background/ for thumbnails...")
            t0 = time.time()
            result = subprocess.run(
                [self.repak_path, "unpack",
                 "-o", self._extract_dir,
                 "-f", "-i", "RetroRewind/Content/VideoStore/asset/prop/vhs/Background/",
                 self.pak_path],
                capture_output=True, text=True, timeout=120
            )
            print(f"[PakCache] Batch extract done in {time.time()-t0:.1f}s")
            if result.returncode != 0 and not os.path.exists(self._base_dir):
                print(f"[PakCache] Batch extract failed: {result.stderr[:200]}")
                return 0

        # Collect textures that need thumbnails (not already cached)
        to_load = [t for t in all_textures
                   if "__thumb_" + t["name"] not in self._cache]

        total   = len(to_load)
        done    = 0
        lock    = __import__('threading').Lock()

        def _decode_one(texture):
            nonlocal done
            name   = texture["name"]
            folder = texture["folder"]
            key    = "__thumb_" + name

            ubulk_path = os.path.join(self._base_dir, folder, f"{name}.ubulk")
            if not os.path.exists(ubulk_path):
                # Try per-file extract as fallback
                pak_entry = (f"RetroRewind/Content/VideoStore/asset/prop/vhs"
                             f"/Background/{folder}/{name}.ubulk")
                subprocess.run(
                    [self.repak_path, "unpack",
                     "-o", self._extract_dir,
                     "-f", "-i", pak_entry,
                     self.pak_path],
                    capture_output=True, text=True, timeout=30)

            if not os.path.exists(ubulk_path):
                with lock:
                    done += 1
                    if progress_cb:
                        progress_cb(done, total)
                return

            try:
                with open(ubulk_path, "rb") as f:
                    data = f.read(1_048_576)   # only mip0 = 1MB
                img = ubulk_to_image(data)
                thumb = img.resize((thumb_w, thumb_h), Image.BILINEAR)
                with lock:
                    self._cache[key] = thumb
                    done += 1
                    if progress_cb:
                        progress_cb(done, total)
            except Exception as e:
                print(f"[PakCache] Thumbnail error {name}: {e}")
                with lock:
                    done += 1
                    if progress_cb:
                        progress_cb(done, total)

        # Decode in parallel threads (I/O + CPU bound)
        with ThreadPoolExecutor(max_workers=4) as pool:
            pool.map(_decode_one, to_load)

        loaded = sum(1 for t in all_textures
                     if "__thumb_" + t["name"] in self._cache)
        print(f"[PakCache] Thumbnails loaded: {loaded}/{total}")
        return loaded

    def get_preview(self, texture):
        """Return decoded PIL Image for texture, or None."""
        name   = texture["name"]
        folder = texture["folder"]

        if name in self._cache:
            return self._cache[name]

        # Check if already extracted (from previous full unpack)
        ubulk_path = os.path.join(self._base_dir, folder, f"{name}.ubulk")

        # If not extracted yet, extract just this one file
        if not os.path.exists(ubulk_path):
            os.makedirs(os.path.dirname(ubulk_path), exist_ok=True)
            pak_entry = (f"RetroRewind/Content/VideoStore/asset/prop/vhs"
                        f"/Background/{folder}/{name}.ubulk")
            subprocess.run(
                [self.repak_path, "unpack",
                 "-o", self._extract_dir,
                 "-f",
                 "-i", pak_entry,
                 self.pak_path],
                capture_output=True, text=True, timeout=30
            )
        if not os.path.exists(ubulk_path):
            return None

        try:
            with open(ubulk_path, "rb") as f:
                data = f.read()
            img = ubulk_to_image(data)
            self._cache[name] = img
            print(f"[PakCache] Decoded preview for {name}")
            return img
        except Exception as e:
            print(f"[PakCache] Decode error for {name}: {e}")
            return None

    def get_layout_texture(self, n: int, variant: str = "bc"):
        """
        Extract and return the fixed regular T_Layout_01_{variant} cropped to 1024×2048.

        Uses a persistent PNG cache alongside the tool for instant startup.
        First run: extracts from pak, decodes DXT1, saves PNG.
        Subsequent runs: loads PNG directly (no pak/DXT1 needed).

        variant: "bc" (base colour / visual frame) or "msk" (mask: black=frame, white=bg).
        Returns RGBA PIL Image or None if extraction fails.
        """
        n = FIXED_REGULAR_LAYOUT
        cache_key = f"__layout_{n}_{variant}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        name = f"T_Layout_{n:02d}_{variant}"

        # Check persistent PNG cache first (instant load)
        png_cache_dir = os.path.join(SCRIPT_DIR, LAYOUT_CACHE_DIRNAME)
        png_path = os.path.join(png_cache_dir, f"{name}.png")
        if os.path.exists(png_path):
            try:
                img = Image.open(png_path).convert("RGBA")
                self._cache[cache_key] = img
                return img
            except Exception:
                pass  # Fall through to extraction

        # Extract from pak and decode DXT1
        layout_dir = os.path.join(
            self._extract_dir,
            "RetroRewind", "Content", "VideoStore",
            "asset", "prop", "vhs", "Layout"
        )
        ubulk_path = os.path.join(layout_dir, f"{name}.ubulk")

        if not os.path.exists(ubulk_path):
            os.makedirs(layout_dir, exist_ok=True)
            pak_entry = (f"RetroRewind/Content/VideoStore/asset/prop/vhs"
                         f"/Layout/{name}.ubulk")
            subprocess.run(
                [self.repak_path, "unpack",
                 "-o", self._extract_dir,
                 "-f", "-i", pak_entry,
                 self.pak_path],
                capture_output=True, text=True, timeout=30
            )

        if not os.path.exists(ubulk_path):
            print(f"[PakCache] Layout texture not found: {name}.ubulk")
            return None

        try:
            with open(ubulk_path, "rb") as f:
                data = f.read()
            # mip0 at 2048×2048 DXT1 = (2048/4)*(2048/4)*8 = 2,097,152 bytes
            MIP0 = 2_097_152
            img = decode_dxt1(data[:MIP0], 2048, 2048)
            # Regular movie background corresponds to the LEFT half of the layout.
            img_cropped = img.crop((0, 0, 1024, 2048))
            img_rgba = img_cropped.convert("RGBA")
            self._cache[cache_key] = img_rgba
            # Save to persistent PNG cache for instant future loads
            os.makedirs(png_cache_dir, exist_ok=True)
            img_rgba.save(png_path, "PNG")
            print(f"[PakCache] Loaded layout texture {name} → cropped to {img_cropped.size} (cached PNG)")
            return img_rgba
        except Exception as e:
            print(f"[PakCache] Layout decode error {name}: {e}")
            return None


    def get_layout_texture_full(self, n: int, variant: str = "bc"):
        """
        Return the fixed regular T_Layout_01_{variant} as full 2048x2048 RGBA.
        Uses persistent PNG cache for instant startup.
        """
        n = FIXED_REGULAR_LAYOUT
        cache_key = f"__layout_full_{n}_{variant}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        name = f"T_Layout_{n:02d}_{variant}"

        # Check persistent PNG cache first
        png_cache_dir = os.path.join(SCRIPT_DIR, LAYOUT_CACHE_DIRNAME)
        png_path = os.path.join(png_cache_dir, f"{name}_full.png")
        if os.path.exists(png_path):
            try:
                img = Image.open(png_path).convert("RGBA")
                self._cache[cache_key] = img
                return img
            except Exception:
                pass

        # Ensure ubulk is extracted (get_layout_texture handles extraction)
        self.get_layout_texture(n, variant)
        ubulk_path = os.path.join(
            self._extract_dir, "RetroRewind", "Content", "VideoStore",
            "asset", "prop", "vhs", "Layout", f"{name}.ubulk")
        if not os.path.exists(ubulk_path):
            return None
        try:
            with open(ubulk_path, "rb") as f:
                data = f.read()
            MIP0 = 2_097_152
            img = decode_dxt1(data[:MIP0], 2048, 2048)
            img_rgba = img.convert("RGBA")
            self._cache[cache_key] = img_rgba
            # Save to persistent cache
            os.makedirs(png_cache_dir, exist_ok=True)
            img_rgba.save(png_path, "PNG")
            return img_rgba
        except Exception as e:
            print(f"[PakCache] Layout full decode error {name}: {e}")
            return None

    def get_base_files(self, texture):
        """
        Return path to folder with uasset/uexp/ubulk for this texture.

        All files are extracted from the base game pak. For new slots beyond
        the base game cap, the preceding slot is cloned and patched.
        """
        name   = texture["name"]
        folder = texture["folder"]
        dest   = os.path.join(self._base_dir, folder)

        # T_New textures are staged by prepare_nr_donor_in_cache in the build
        # pre-pass: it clones T_New_Hor_01 → T_New_<code>_<NN:03d> with the
        # correct PackageName via clone_texture_3digit. The custom-slot path
        # below uses T_Bkg conventions (base_count=GENRES[g]["bkg"], clone
        # source f"T_Bkg_{code}_{NN:02d}") and would overwrite the staged
        # T_New file with one whose PackageName ends in T_Bkg_<code>_<NN>
        # — exactly the FAsyncLoadingThread crash signature. Trust the pre-pass.
        if name.startswith("T_New_"):
            os.makedirs(dest, exist_ok=True)
            return dest

        # Parse genre code and slot number from name (e.g. T_Bkg_Hor_23 -> Hor, 23)
        parts    = name.split('_')   # ['T', 'Bkg', 'Hor', '23']
        code     = parts[2] if len(parts) >= 4 else None
        slot_num = int(parts[3]) if len(parts) >= 4 else 1

        # Determine if this slot is beyond the base game count
        genre_key = next((g for g, info in GENRES.items()
                          if info["code"] == code), None) if code else None
        base_count = GENRES[genre_key]["bkg"] if genre_key else 99

        # Extract all 3 files for this texture if missing
        is_custom_slot = (len(parts) >= 4 and len(parts[3]) >= 3)
        missing = [e for e in ['uasset', 'uexp', 'ubulk']
                   if not os.path.exists(os.path.join(dest, f"{name}.{e}"))]
        # Custom slots: always re-clone uasset (FName stored_number must be correct)
        if is_custom_slot and 'uasset' not in missing:
            missing.append('uasset')
        if missing:
            os.makedirs(dest, exist_ok=True)
            base_path = (f"RetroRewind/Content/VideoStore/asset/prop/vhs"
                        f"/Background/{folder}/{name}")

            # Custom slots use 3-digit names (e.g. T_Bkg_Dra_001), base game
            # uses 2-digit (e.g. T_Bkg_Dra_01). Even if the slot number is <= base_count,
            # a 3-digit name means it's a custom slot needing cloning.
            is_base_game_file = (slot_num <= base_count and len(parts[3]) <= 2)

            if is_base_game_file:
                # Base game slot — extract directly from pak
                truly_missing = []
                for ext in missing:
                    subprocess.run(
                        [self.repak_path, "unpack",
                         "-o", self._extract_dir, "-f",
                         "-i", f"{base_path}.{ext}",
                         self.pak_path],
                        capture_output=True, text=True, timeout=30
                    )
                    if not os.path.exists(os.path.join(dest, f"{name}.{ext}")):
                        truly_missing.append(ext)
                if truly_missing:
                    for ext in truly_missing:
                        print(f"[PakCache] ERROR: Could not extract {name}.{ext} from base game pak")
                    # For T_New textures from genres without base game T_New files,
                    # extract a donor genre's T_New_01 as template for cross-genre cloning.
                    if truly_missing and name.startswith("T_New_") and code:
                        donor_code = "Hor"  # Horror always has T_New textures
                        if code != donor_code:
                            donor_name = f"T_New_{donor_code}_01"
                            # T_New files live in the T_Bkg_XXX folder alongside backgrounds
                            donor_folder = f"T_Bkg_{donor_code}"
                            donor_dest = os.path.join(self._base_dir, donor_folder)
                            os.makedirs(donor_dest, exist_ok=True)
                            donor_pak = (f"RetroRewind/Content/VideoStore/asset/prop/vhs"
                                        f"/Background/{donor_folder}/{donor_name}")
                            for ext in ['uasset', 'uexp', 'ubulk']:
                                if not os.path.exists(os.path.join(donor_dest, f"{donor_name}.{ext}")):
                                    subprocess.run(
                                        [self.repak_path, "unpack",
                                         "-o", self._extract_dir, "-f",
                                         "-i", f"{donor_pak}.{ext}",
                                         self.pak_path],
                                        capture_output=True, text=True, timeout=30
                                    )
                            # Verify extraction worked
                            _extracted = [ext for ext in ['uasset', 'uexp', 'ubulk']
                                          if os.path.exists(os.path.join(donor_dest, f"{donor_name}.{ext}"))]
                            print(f"[PakCache] Donor extraction: {donor_name} in {donor_folder}/ "
                                  f"-> {len(_extracted)}/3 files found")
            else:
                # Custom slot beyond base game — clone from last base game slot.
                # Always clone from the SAME base game source (never chain N-1→N)
                # to avoid cumulative FString corruption.
                donor_code = T_BKG_TEMPLATE_FOR_CODE.get(code, code)
                donor_genre_key = next((g for g, info in GENRES.items()
                                        if info["code"] == donor_code), None)
                donor_base_count = GENRES[donor_genre_key]["bkg"] if donor_genre_key else base_count
                if donor_base_count <= 0:
                    donor_base_count = 1

                clone_src_num = donor_base_count
                clone_src_name = f"T_Bkg_{donor_code}_{clone_src_num:02d}"
                donor_folder = f"T_Bkg_{donor_code}"
                src_folder = os.path.join(self._base_dir, donor_folder)

                # Ensure clone source is extracted from pak (once per donor genre)
                src_uasset = os.path.join(src_folder, f"{clone_src_name}.uasset")
                if not os.path.exists(src_uasset):
                    clone_pak_path = (
                        f"RetroRewind/Content/VideoStore/asset/prop/vhs"
                        f"/Background/{donor_folder}/{clone_src_name}"
                    )
                    os.makedirs(src_folder, exist_ok=True)
                    for ext in ['uasset', 'uexp', 'ubulk']:
                        subprocess.run(
                            [self.repak_path, "unpack",
                             "-o", self._extract_dir, "-f",
                             "-i", f"{clone_pak_path}.{ext}",
                             self.pak_path],
                            capture_output=True, text=True, timeout=30
                        )

                for ext in missing:
                    src_file = os.path.join(src_folder, f"{clone_src_name}.{ext}")
                    dst_file = os.path.join(dest, f"{name}.{ext}")
                    if not os.path.exists(src_file):
                        print(f"[PakCache] ERROR: Clone source {clone_src_name}.{ext} not available")
                        continue
                    if ext == 'uasset':
                        with open(src_file, 'rb') as f:
                            src_data = f.read()
                        cloned = clone_texture_3digit(
                            src_data, donor_code, clone_src_num, code, slot_num)
                        with open(dst_file, 'wb') as f:
                            f.write(cloned)
                    else:
                        shutil.copy2(src_file, dst_file)
                if not hasattr(self, '_clone_counts'):
                    self._clone_counts = {}
                key = f"{code}:{clone_src_num}"
                self._clone_counts[key] = self._clone_counts.get(key, 0) + 1
        return dest


# ============================================================
# INJECTION
# ============================================================

def prepare_image(input_path, output_path, offset_x=0, offset_y=0, zoom=1.0):
    """Fit image onto 1024x2048 canvas centered — covers the full canvas.
    Image is scaled to cover the entire canvas (no black gaps at zoom=1.0)."""
    img        = Image.open(input_path).convert('RGB')
    # Scale to COVER the full canvas (same as fullcanvas variant)
    base_scale = max(TEX_WIDTH / img.width, TEX_HEIGHT / img.height)
    scale      = base_scale * zoom
    nw, nh     = int(img.width * scale), int(img.height * scale)
    r          = img.resize((nw, nh), Image.LANCZOS)
    c          = Image.new('RGB', (TEX_WIDTH, TEX_HEIGHT), (0, 0, 0))
    px         = (TEX_WIDTH - nw) // 2 + offset_x
    py         = (TEX_HEIGHT - nh) // 2 + offset_y
    c.paste(r, (px, py))
    c.save(output_path, 'PNG')
    c.save(output_path, 'PNG')


def prepare_image_fullcanvas(input_path, output_path, offset_x=0, offset_y=0, zoom=1.0):
    """Fit image onto 1024x2048 canvas CENTERED — no VHS safe-area offset.
    Used for New Release T_New textures where the standee shows the full canvas."""
    img        = Image.open(input_path).convert('RGB')
    # Scale to COVER the full canvas (overflow is cropped, no black gaps)
    cover_scale = max(TEX_WIDTH / img.width, TEX_HEIGHT / img.height)
    scale      = cover_scale * zoom
    nw, nh     = int(img.width * scale), int(img.height * scale)
    r          = img.resize((nw, nh), Image.LANCZOS)
    c          = Image.new('RGB', (TEX_WIDTH, TEX_HEIGHT), (0, 0, 0))
    # Center the image (may overflow canvas edges — that's fine, crop happens naturally)
    px         = (TEX_WIDTH - nw) // 2 + offset_x
    py         = (TEX_HEIGHT - nh) // 2 + offset_y
    c.paste(r, (px, py))
    c.save(output_path, 'PNG')


def inject_texture(texture, entry, work_dir, texconv, base_dir):
    """Inject user image into T_Bkg texture (ubulk replacement).

    For T_Bkg textures the ubulk size is always exactly
    TEX_WIDTH × TEX_HEIGHT × 0.5 bytes (DXT1) = 1,048,576 bytes.
    We use this constant so the build works even when the base ubulk
    couldn't be extracted (e.g. when cloning from the base game pak fails).
    """
    png_path   = entry["path"] if isinstance(entry, dict) else entry
    folder, name = texture["folder"], texture["name"]
    orig_ubulk = os.path.join(base_dir, f"{name}.ubulk")

    # Expected ubulk size: DXT1 = 4 bytes per 4×4 block (8 bytes per block / 2 pixels/byte)
    # = width/4 * height/4 * 8 bytes
    BKG_UBULK_SIZE = (TEX_WIDTH // 4) * (TEX_HEIGHT // 4) * 8   # = 1,048,576
    TNEW_UBULK_SIZE = sum(
        ((TEX_WIDTH >> m) // 4) * ((TEX_HEIGHT >> m) // 4) * 8
        for m in range(5)
    )  # = 1,396,736
    is_new_release = name.startswith("T_New_")
    # Both T_Bkg and T_New use 5 mip levels in the ubulk.
    # _TBKG_UEXP_TEMPLATE describes 5 external mips (1,396,736 bytes total).
    expected = TNEW_UBULK_SIZE
    if os.path.exists(orig_ubulk):
        with open(orig_ubulk, 'rb') as f:
            actual = len(f.read())
        if actual != expected:
            print(f"[Inject] NOTE: {name}.ubulk is {actual} bytes, expected {expected}")
    else:
        print(f"[Inject] {name}.ubulk not in cache, using size {expected}")

    dest = os.path.join(work_dir, "RetroRewind", "Content", "VideoStore",
                        "asset", "prop", "vhs", "Background", folder)
    os.makedirs(dest, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        resized  = os.path.join(tmp, f"{name}.png")
        offset_x = entry.get("offset_x", 0) if isinstance(entry, dict) else 0
        offset_y = entry.get("offset_y", 0) if isinstance(entry, dict) else 0
        zoom     = entry.get("zoom", 1.0)    if isinstance(entry, dict) else 1.0
        if name.startswith("T_New_"):
            prepare_image_fullcanvas(png_path, resized, offset_x, offset_y, zoom)
        else:
            prepare_image(png_path, resized, offset_x, offset_y, zoom)

        texconv_args = [texconv, '-f', 'DXT1', '-w', str(TEX_WIDTH), '-h', str(TEX_HEIGHT),
             '-if', 'LINEAR', '-srgb', '-o', tmp, '-y', resized]
        # texconv generates all mip levels by default (no -m flag).
        # Mips 0-4 go to ubulk, mips 5-11 replace inline data in uexp.
        r = subprocess.run(texconv_args, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"texconv failed: {r.stderr}")

        dds = os.path.join(tmp, f"{name}.dds")
        if not os.path.exists(dds):
            raise FileNotFoundError("texconv produced no DDS file")

        with open(dds, 'rb') as f:
            dds_data = f.read()

        # DDS header: first 4 bytes = "DDS " magic, then DWORD dwSize at offset 4
        # Standard DDS header = 128 bytes (magic + 124-byte header)
        # DX10 extended header = 128 + 20 = 148 bytes (has DX10 fourcc at offset 84)
        # Check for DX10 extended header by looking at the pixel format fourcc
        dds_header_size = 128
        if len(dds_data) > 148:
            fourcc = dds_data[84:88]
            if fourcc == b'DX10':
                dds_header_size = 148
                print(f"[Inject] DX10 extended DDS header detected (148 bytes)")

        raw = dds_data[dds_header_size:]

        ubulk = bytearray(raw)
        if len(ubulk) < expected:
            ubulk.extend(b'\x00' * (expected - len(ubulk)))
        elif len(ubulk) > expected:
            ubulk = ubulk[:expected]

        # Copy uasset and uexp.  For new slots the cloned files may be in
        # base_dir already (from get_base_files).  If not, fall back to the
        # mod pak cache version with the correct slot cloned on the fly.
        parts        = name.split('_')          # ['T', 'Bkg', 'Hor', '23']
        dst_slot_num = int(parts[3]) if len(parts) >= 4 else 1
        genre_code_fb = parts[2] if len(parts) >= 4 else None
        genre_key_fb = next((g for g, info in GENRES.items()
                             if info["code"] == genre_code_fb), None) if genre_code_fb else None
        base_count_fb = GENRES[genre_key_fb]["bkg"] if genre_key_fb else 99
        # Clone source: always the last base game slot (never chain)
        src_slot_num_fb = min(dst_slot_num - 1, base_count_fb) if dst_slot_num > 1 else 1
        base_name_fb = "_".join(parts[:3]) + f"_{src_slot_num_fb:02d}"  # e.g. T_Bkg_Hor_22
        folder_name  = texture["folder"]                                  # e.g. T_Bkg_Hor

        # uexp: use base game uexp, then replace inline mip data with our custom mips.
        uexp_dst = os.path.join(dest, f"{name}.uexp")
        uexp_src = os.path.join(base_dir, f"{name}.uexp")
        if os.path.exists(uexp_src):
            uexp_data = bytearray(open(uexp_src, 'rb').read())
        else:
            uexp_data = bytearray(_TBKG_UEXP_TEMPLATE)

        # Replace inline mip pixel data (mips 5-11) with our texconv output.
        # texconv DDS has mips concatenated: mip0 at offset 0, mip1 at 1048576, etc.
        # Mips 5-11 start at offset 1396736 in the DDS pixel data.
        if len(uexp_data) == 1702:
            dds_mip_offset = TNEW_UBULK_SIZE  # = 1,396,736 (end of mips 0-4)
            for mip_level, uexp_off, mip_size in _UEXP_INLINE_MIP_MAP:
                src_start = dds_mip_offset
                src_end = src_start + mip_size
                if src_end <= len(raw):
                    uexp_data[uexp_off:uexp_off + mip_size] = raw[src_start:src_end]
                dds_mip_offset += mip_size

        with open(uexp_dst, 'wb') as f:
            f.write(bytes(uexp_data))

        uasset_dst = os.path.join(dest, f"{name}.uasset")
        uasset_src = os.path.join(base_dir, f"{name}.uasset")
        if os.path.exists(uasset_src):
            shutil.copy2(uasset_src, uasset_dst)
        else:
            # Clone from preceding slot — search backwards to find a valid source
            fb_found = False

            for cand_num in range(dst_slot_num - 1, 0, -1):
                cand_name_fb = "_".join(parts[:3]) + f"_{cand_num:02d}" if cand_num < 100 else "_".join(parts[:3]) + f"_{cand_num}"
                fb_candidates = [
                    os.path.join(base_dir, f"{cand_name_fb}.uasset"),
                    os.path.join(os.path.dirname(base_dir), folder_name, f"{cand_name_fb}.uasset"),
                    os.path.join(os.path.dirname(os.path.dirname(base_dir)), folder_name, f"{cand_name_fb}.uasset"),
                ]
                for fb_src in fb_candidates:
                    if os.path.exists(fb_src):
                        with open(fb_src, 'rb') as f:
                            fb_data = f.read()
                        cloned = clone_texture_3digit(
                            fb_data, genre_code_fb, cand_num,
                            genre_code_fb, dst_slot_num)
                        with open(uasset_dst, 'wb') as f:
                            f.write(cloned)
                        print(f"[Inject] Cloned {cand_name_fb}.uasset -> {name}.uasset")
                        fb_found = True
                        break
                if fb_found or cand_num <= base_count_fb:
                    break
            # Fallback: clone from the base game's last slot for this genre
            if not fb_found and name.startswith("T_Bkg_"):
                fb_base_num = base_count_fb
                fb_base_name = "_".join(parts[:3]) + f"_{fb_base_num:02d}"
                fb_base_candidates = [
                    os.path.join(base_dir, f"{fb_base_name}.uasset"),
                    os.path.join(os.path.dirname(base_dir), folder_name, f"{fb_base_name}.uasset"),
                    os.path.join(os.path.dirname(os.path.dirname(base_dir)), folder_name, f"{fb_base_name}.uasset"),
                ]
                for fb_src in fb_base_candidates:
                    if os.path.exists(fb_src):
                        with open(fb_src, 'rb') as f:
                            fb_data = f.read()
                        cloned = clone_texture_3digit(
                            fb_data, genre_code_fb, fb_base_num,
                            genre_code_fb, dst_slot_num)
                        with open(uasset_dst, 'wb') as f:
                            f.write(cloned)
                        print(f"[Inject] Cloned {fb_base_name}.uasset -> {name}.uasset (base game fallback)")
                        fb_found = True
                        break
            if not fb_found:
                # Cross-genre clone: for genres without base game T_New textures
                # (e.g. Romance, Western, Adult), clone from a donor genre.
                if name.startswith("T_New_"):
                    donor_genre = "Hor"  # Horror always has T_New textures
                    donor_name = f"T_New_{donor_genre}_01"
                    # T_New files live in T_Bkg_XXX folders alongside background textures
                    _cache_root = os.path.dirname(base_dir)  # .../Background/ level
                    donor_candidates = [
                        os.path.join(base_dir, f"{donor_name}.uasset"),
                        os.path.join(_cache_root, f"T_Bkg_{donor_genre}", f"{donor_name}.uasset"),
                    ]
                    # Walk the cache root as last resort
                    if os.path.isdir(_cache_root):
                        for _root, _dirs, _files in os.walk(_cache_root):
                            if f"{donor_name}.uasset" in _files:
                                donor_candidates.append(
                                    os.path.join(_root, f"{donor_name}.uasset"))
                    for fb_src in donor_candidates:
                        if os.path.exists(fb_src):
                            with open(fb_src, 'rb') as f:
                                donor_data = bytearray(f.read())
                            # Patch genre code: replace donor genre code with target
                            target_code = parts[2]  # e.g. "Rom"
                            donor_bytes = donor_genre.encode()
                            target_bytes = target_code.encode()
                            if len(donor_bytes) == len(target_bytes):
                                # Replace T_New genre code in all FStrings
                                donor_full = f"T_New_{donor_genre}".encode()
                                target_full = f"T_New_{target_code}".encode()
                                donor_data = bytearray(
                                    bytes(donor_data).replace(donor_full, target_full))
                                # Replace T_Bkg folder references in internal paths
                                # (e.g. /Game/.../T_Bkg_Hor/T_New_Rom_01 → .../T_Bkg_Rom/...)
                                donor_folder = f"T_Bkg_{donor_genre}".encode()
                                target_folder = f"T_Bkg_{target_code}".encode()
                                donor_data = bytearray(
                                    bytes(donor_data).replace(donor_folder, target_folder))
                                # Patch slot number via FString replacement
                                src_sn = 1
                                if src_sn != dst_slot_num:
                                    src_slot_str = f"_{src_sn:02d}".encode() + b"\x00"
                                    dst_slot_str = f"_{dst_slot_num:02d}".encode() + b"\x00"
                                    if len(src_slot_str) == len(dst_slot_str):
                                        donor_data = bytearray(
                                            bytes(donor_data).replace(
                                                src_slot_str, dst_slot_str))
                            with open(uasset_dst, 'wb') as f:
                                f.write(bytes(donor_data))
                            print(f"[Inject] Cross-genre cloned {donor_name}.uasset "
                                  f"-> {name}.uasset (donor={donor_genre})")
                            fb_found = True
                            break
                if not fb_found:
                    print(f"[Inject] WARNING: could not find uasset for {name}")

        with open(os.path.join(dest, f"{name}.ubulk"), 'wb') as f:
            f.write(ubulk)





# ─────────────────────────────────────────────────────────────
# PALETTE & FONT
# ─────────────────────────────────────────────────────────────

# =============================================================================
# RETRO REWIND VHS TOOL — DESIGN SYSTEM  (Performance Edition, April 2026)
# =============================================================================
# Rule: every color, size, and font in the UI must reference a token below.
# Never use raw hex strings in widget code.
#
# PHILOSOPHY
#   Flat · Sharp · Fast · Terminal-inspired
#   No shadows, no blur, no gradients, no rounded corners (max 2px if needed)
#   One color = one meaning. If you break that the UI becomes unreadable.
#
# BACKGROUNDS
#   DS["bg"]          #050505   App window background (pure black-ish)
#   DS["panel"]       #0B0F14   Panels, sidebars, cards
#   DS["surface"]     #101722   Inputs, inner surfaces, wells
#   DS["divider"]     #1C1C1C   Section separators (hairlines)
#   DS["border"]      #333333   Default 1px borders
#
# ACCENTS  (one color = one role — never cross-use)
#   DS["cyan"]        #00F5FF   Active · Selected · CTA · Progress · OK
#   DS["pink"]        #FF0055   Edit · Custom · Error · Upload · Warning
#   DS["gold"]        #FFD84A   Rarity · Highlights · Star ratings
#   DS["disabled"]    #5A5A5A   Disabled controls
#
# TEXT
#   DS["text"]        #F2F5F7   Primary text (titles, labels, buttons)
#   DS["text2"]       #A8B0B8   Secondary (descriptions, path hints)
#   DS["text3"]       #6A7A7A   Muted (inactive, logs, placeholders)
#   DS["text_inv"]    #050505   On cyan buttons (inverse)
#
# SEMANTIC ALIASES
#   DS["success"]     #00F5FF   (= cyan) success / installed / ready
#   DS["error"]       #FF0055   (= pink) error / delete / missing
#   DS["warn"]        #FFD84A   (= gold) warning
#
# SPACING  (4px grid)
#   SP[1]=4  SP[2]=8  SP[3]=12  SP[4]=16  SP[6]=24
#
# TYPOGRAPHY
#   Font stack: Consolas → Courier New
#   Sizes: FS["app"]=15  FS["sec"]=12  FS["body"]=11  FS["meta"]=9
#   Bold only for hierarchy (section headers, button labels, app title)
#   UPPERCASE only for: headers · labels · system states
#
# COMPONENT RULES (quick ref)
#   Primary button:   bg=DS["cyan"]  fg=DS["text_inv"]  — ONE per screen
#   Secondary button: bg=transparent  border=DS["border"]  fg=DS["text"]
#   Danger button:    border=DS["pink"]  fg=DS["pink"]  no fill
#   Input default:    bg=DS["surface"]  border=DS["border"]
#   Input active:     border=DS["cyan"]
#   Input error:      border=DS["pink"]
#   List row idle:    bg=DS["panel"]  left-stripe=DS["border"]  4px wide
#   List row custom:  left-stripe=DS["pink"]
#   List row selected:border=DS["cyan"]  1px
# =============================================================================

DS = {
    # Backgrounds
    "bg":        "#050505",
    "panel":     "#0B0F14",
    "surface":   "#101722",
    "divider":   "#1C1C1C",
    "border":    "#333333",
    # Accents
    "cyan":      "#00F5FF",
    "pink":      "#FF0055",
    "gold":      "#FFD84A",
    "disabled":  "#5A5A5A",
    # Text
    "text":      "#F2F5F7",
    "text2":     "#A8B0B8",
    "text3":     "#6A7A7A",
    "text_inv":  "#050505",
    # Semantic shortcuts
    "success":   "#00F5FF",
    "error":     "#FF0055",
    "warn":      "#FFD84A",
}

# Spacing grid (4px base)
SP = {1: 4, 2: 8, 3: 12, 4: 16, 6: 24}

# Layout thumbnail images (base64 PNG, 46×76px)
LAYOUT_THUMB_B64 = {}  # Generated at runtime from cached layout PNGs

# Font sizes — scale based on screen height (1080p baseline)
def _compute_scale_factor():
    """Compute UI scale factor based on screen resolution."""
    try:
        import tkinter as _tk
        _r = _tk.Tk()
        _r.withdraw()
        sh = _r.winfo_screenheight()
        # Account for Windows DPI scaling
        try:
            dpi = _r.winfo_fpixels('1i')
            dpi_scale = dpi / 72.0  # standard is 72 dpi
        except Exception:
            dpi_scale = 1.0
        _r.destroy()
        raw_scale = sh / 1080.0
        return max(0.9, raw_scale)  # never below 0.9
    except Exception:
        return 1.0

SCALE_FACTOR = _compute_scale_factor()

def _scaled_font_size(base_size, minimum=10):
    """Scale a font size from 1080p baseline, with a floor."""
    return max(minimum, round(base_size * SCALE_FACTOR))

# Font size tiers (at 1080p baseline)
_FS_BASE = {"app": 15, "sec": 14, "body": 13, "meta": 11}
FS = {k: _scaled_font_size(v, minimum=10) for k, v in _FS_BASE.items()}

# Keep legacy C dict as alias so old code still runs
# TODO: migrate all C["..."] references to DS["..."] over time
C = {
    "bg":        DS["panel"],
    "bg2":       DS["panel"],
    "card":      DS["panel"],
    "input_bg":  DS["surface"],
    "border":    DS["border"],
    "sel_bg":    DS["surface"],
    "cyan":      DS["cyan"],
    "pink":      DS["pink"],
    "yellow":    DS["gold"],
    "purple":    DS["disabled"],
    "green":     DS["success"],
    "red":       DS["error"],
    "orange":    DS["warn"],
    "text":      DS["text"],
    "text_dim":  DS["text2"],
    "text_hi":   DS["text"],
    "build_btn": DS["cyan"],
    "upload_btn":DS["pink"],
    "star_on":   DS["gold"],
    "star_off":  DS["border"],
}

VCR_FONT_NAME = "VCR OSD Mono"

# Primary font helper — Consolas → Courier New (no VCR in Performance Edition
# but we keep trying VCR so it still works if the user has the file)
def _f(size, bold=False):
    """Return font tuple for the design system font stack."""
    weight = "bold" if bold else "normal"
    import tkinter.font as tkfont
    try:
        fams = tkfont.families()
        for fam in ("Consolas", "Cascadia Code", VCR_FONT_NAME, "Courier New"):
            if fam in fams:
                return (fam, size, weight)
    except Exception:
        pass
    return ("Courier New", size, weight)

# Legacy alias so all existing _vcr() calls keep working
def _vcr(size, bold=False):
    return _f(size, bold)

def _try_load_vcr_font():
    """Load VCR_OSD_MONO.ttf from script directory on Windows."""
    import sys, os
    script_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    font_path  = os.path.join(script_dir, "VCR_OSD_MONO.ttf")
    if not os.path.exists(font_path):
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.gdi32.AddFontResourceExW(font_path, 0x10, 0)
            return True
    except Exception as e:
        print(f"[Font] VCR OSD Mono load error: {e}")
    return False



class LoadingScreen:
    """
    Minimal terminal-style loading screen (Performance Edition).

    Layout  600×380, bg.base background, 1px DS["border"] outer border.
    Log area: tk.Text widget, state=DISABLED, updated by appending lines.
    Progress: 12-segment Canvas bar (DS["cyan"] filled, DS["border"] empty).
    Blink:  the current (most-recently-filled) segment pulses cyan/dark.
    Status: single muted line at bottom-left (italic feel via spacing).
    No decorative frames, no glow borders, no nested padding.
    """

    SEG_COUNT = 12

    def __init__(self, root):
        self.root = root
        self._win = tk.Toplevel(root)
        self._win.title("RETRO REWIND VHS TOOL")
        self._win.configure(bg=DS["bg"])
        self._win.resizable(False, False)
        self._win.overrideredirect(False)
        self._win.focus_force(); self.lift() if hasattr(self, "lift") else None

        W, H = 600, 380
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        self._win.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")

        self._total_steps  = 2   # config(1) + slots(1)
        self._done         = 0
        self._blink_state  = True
        self._blink_job    = None
        self._current_seg  = -1  # index of the segment currently blinking

        self._build()

    def _build(self):
        W = 600
        BG = DS["bg"]

        # ── Outer 1px border ──────────────────────────────────────
        border_frame = tk.Frame(self._win, bg=DS["border"], padx=1, pady=1)
        border_frame.pack(fill=tk.BOTH, expand=True)
        inner = tk.Frame(border_frame, bg=BG)
        inner.pack(fill=tk.BOTH, expand=True)

        # ── Header ────────────────────────────────────────────────
        hdr = tk.Frame(inner, bg=BG)
        hdr.pack(fill=tk.X, padx=SP[4], pady=(SP[4], SP[2]))
        tk.Label(hdr,
                 text=f"RETRO REWIND MOVIE WORKSHOP  {TOOL_VERSION}",
                 font=_f(FS["app"], bold=True),
                 fg=DS["text"], bg=BG, anchor=tk.W).pack(side=tk.LEFT)

        # ── 1px divider ───────────────────────────────────────────
        tk.Frame(inner, bg=DS["border"], height=1).pack(fill=tk.X, padx=SP[4])

        # ── Log text area (1px border, DS["surface"] bg) ──────────
        log_border = tk.Frame(inner, bg=DS["border"], padx=1, pady=1)
        log_border.pack(fill=tk.BOTH, expand=True, padx=SP[4], pady=SP[3])

        self._log_text_widget = tk.Text(
            log_border,
            bg=DS["surface"], fg=DS["text3"],
            font=_f(FS["body"]),
            relief=tk.FLAT, bd=0,
            state=tk.DISABLED,
            cursor="arrow",
            padx=SP[3], pady=SP[2],
            wrap=tk.NONE,
            height=7,
            selectbackground=DS["surface"],
        )
        self._log_text_widget.pack(fill=tk.BOTH, expand=True)

        # Tag styles
        self._log_text_widget.tag_config("done",  foreground=DS["text"])
        self._log_text_widget.tag_config("ok",    foreground=DS["cyan"])
        self._log_text_widget.tag_config("error", foreground=DS["pink"])
        self._log_text_widget.tag_config("muted", foreground=DS["text3"])

        # ── 1px divider ───────────────────────────────────────────
        tk.Frame(inner, bg=DS["border"], height=1).pack(fill=tk.X, padx=SP[4])

        # ── Progress bar (Canvas) ─────────────────────────────────
        bar_wrap = tk.Frame(inner, bg=BG)
        bar_wrap.pack(fill=tk.X, padx=SP[4], pady=(SP[3], 0))

        self._bar_canvas = tk.Canvas(bar_wrap, bg=BG, bd=0,
                                      highlightthickness=0, height=40)
        self._bar_canvas.pack(fill=tk.X)
        self._bar_canvas.bind("<Configure>", lambda e: self._redraw_bar())

        # ── Bottom status line ────────────────────────────────────
        bot = tk.Frame(inner, bg=BG)
        bot.pack(fill=tk.X, padx=SP[4], pady=(0, SP[3]))
        self._status_var = tk.StringVar(value="Initializing VCR Head...")
        tk.Label(bot, textvariable=self._status_var,
                 font=_f(FS["meta"]),
                 fg=DS["text3"], bg=BG, anchor=tk.W).pack(side=tk.LEFT)

        self._pct_var = tk.StringVar(value="0%")
        tk.Label(bot, textvariable=self._pct_var,
                 font=_f(FS["meta"]),
                 fg=DS["text2"], bg=BG, anchor=tk.E).pack(side=tk.RIGHT)

    # ── Log helpers ───────────────────────────────────────────────
    def _append_log(self, text, tag="muted"):
        w = self._log_text_widget
        w.config(state=tk.NORMAL)
        if w.index("end-1c") != "1.0":
            w.insert(tk.END, "\n")
        w.insert(tk.END, text, tag)
        w.see(tk.END)
        w.config(state=tk.DISABLED)
        self._win.update()

    def _update_last_log(self, text, tag="done"):
        """Replace the last line of the log."""
        w = self._log_text_widget
        w.config(state=tk.NORMAL)
        w.delete("end-1l", "end")
        w.insert(tk.END, text, tag)
        w.see(tk.END)
        w.config(state=tk.DISABLED)
        self._win.update()

    # ── Progress bar ─────────────────────────────────────────────
    def _redraw_bar(self, blink_on=True):
        c = self._bar_canvas
        c.delete("all")
        w = c.winfo_width()
        if w < 10:
            return
        h     = 40
        n     = self.SEG_COUNT
        gap   = 4
        total_gaps = gap * (n - 1)
        seg_w = (w - total_gaps) // n

        for i in range(n):
            x1 = i * (seg_w + gap)
            x2 = x1 + seg_w
            y1, y2 = 8, h - 8

            if i < self._done:
                # Filled — check if this is the blinking "active" segment
                if i == self._done - 1 and self._done < self._total_steps:
                    fill = DS["cyan"] if blink_on else DS["panel"]
                else:
                    fill = DS["cyan"]
                c.create_rectangle(x1, y1, x2, y2,
                                   fill=fill, outline=DS["border"], width=1)
            else:
                c.create_rectangle(x1, y1, x2, y2,
                                   fill="", outline=DS["border"], width=1)

        # Percentage
        pct = int(self._done / self._total_steps * 100)
        self._pct_var.set(f"{pct}%")

    def _start_blink(self):
        """Blink the leading filled segment until loading finishes."""
        if self._blink_job:
            self._win.after_cancel(self._blink_job)
        self._blink_state = True
        self._do_blink()

    def _do_blink(self):
        if not self._win.winfo_exists():
            return
        self._blink_state = not self._blink_state
        self._redraw_bar(blink_on=self._blink_state)
        self._blink_job = self._win.after(500, self._do_blink)

    def _stop_blink(self):
        if self._blink_job:
            self._win.after_cancel(self._blink_job)
            self._blink_job = None
        self._redraw_bar(blink_on=True)

    def _advance(self, status_text):
        self._done += 1
        pct = int(self._done / self._total_steps * 100)
        self._status_var.set(status_text)
        self._redraw_bar()
        self._start_blink()
        self._win.update()

    # ── Step callbacks ────────────────────────────────────────────
    def step_config(self, ok=True):
        tag = "ok" if ok else "error"
        label = "[DONE]" if ok else "[FAILED]"
        self._append_log(f"> PARSING CONFIG...  {label}", tag)
        self._advance("Config loaded")

    def step_slots(self, count):
        tag = "ok" if count >= 0 else "error"
        self._append_log(
            f"> LOADING PERSISTED SLOT DATA...  [{count} FOUND]", tag)
        self._advance(f"{count} custom slots loaded")


    def finish(self):
        self._stop_blink()
        self._append_log("> SYSTEM READY.", "ok")
        self._done = self._total_steps
        self._redraw_bar(blink_on=True)
        self._status_var.set("Ready.")
        self._win.update()
        self._win.after(700, self._win.destroy)

    def close(self):
        self._stop_blink()
        try:
            self._win.destroy()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# SETUP DIALOG  (Membership Card style)
# ─────────────────────────────────────────────────────────────
class SetupDialog(tk.Toplevel):
    """
    First-time setup dialog styled as a "Video Store Membership" card.
    Redesigned: merged game sections, auto-detect tools, status summary.
    """
    TEXCONV_URL = "https://github.com/microsoft/DirectXTex/releases/latest"
    REPAK_URL   = "https://github.com/trumank/repak/releases/latest"

    def __init__(self, parent, config, on_complete):
        super().__init__(parent)
        self.config      = config.copy()
        self.on_complete = on_complete
        self._vars       = {}
        self._status     = {}  # key -> (led_label, status_label)
        self.title("VHS Membership Setup")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.focus_force()
        self.lift()

        W, H = 680, 720
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")

        self._build()
        self._auto_detect_tools()
        self._auto_detect_game()
        self._update_all_status()

    def _build(self):
        outer = tk.Frame(self, bg=C["cyan"], padx=2, pady=2)
        outer.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)
        card = tk.Frame(outer, bg=C["card"])
        card.pack(fill=tk.BOTH, expand=True)
        self._card = card

        # Header
        tk.Label(card, text="\U0001f3ac  VHS MEMBERSHIP SETUP",
                 font=_vcr(14, bold=True), fg=C["pink"], bg=C["card"],
                 pady=14).pack(fill=tk.X)
        tk.Label(card,
                 text="Configure the tool once — settings are saved automatically.\n"
                      "Need help? Check the Nexus Mods page for instructions.",
                 font=_vcr(10), fg=C["text_dim"], bg=C["card"],
                 justify=tk.CENTER).pack(pady=(0, 8))

        # ── Status summary bar ──
        summary = tk.Frame(card, bg=C["card"])
        summary.pack(pady=(0, 8))
        self._summary_leds = {}
        for key, label in [("tools", "Modding Tools"), ("game", "Game Folder")]:
            f = tk.Frame(summary, bg=C["card"])
            f.pack(side=tk.LEFT, padx=12)
            led = tk.Label(f, text="○", font=_vcr(11), fg=C["text_dim"], bg=C["card"])
            led.pack(side=tk.LEFT)
            tk.Label(f, text=f"  {label}", font=_vcr(9), fg=C["text_dim"],
                     bg=C["card"]).pack(side=tk.LEFT)
            self._summary_leds[key] = led

        ttk.Separator(card).pack(fill=tk.X, padx=16, pady=(0, 8))

        content = tk.Frame(card, bg=C["card"], padx=20)
        content.pack(fill=tk.BOTH, expand=True)

        # ════════════════════════════════════════════════════════
        # SECTION 1: MODDING TOOLS
        # ════════════════════════════════════════════════════════
        sec1 = tk.Frame(content, bg=C["card"], highlightthickness=1,
                        highlightbackground=C["border"])
        sec1.pack(fill=tk.X, pady=6)
        inner1 = tk.Frame(sec1, bg=C["card"], padx=12, pady=8)
        inner1.pack(fill=tk.X)

        tk.Label(inner1, text="\U0001f527  MODDING TOOLS",
                 font=_vcr(11, bold=True), fg=C["text_hi"],
                 bg=C["card"], anchor=tk.W).pack(fill=tk.X)
        tk.Label(inner1,
                 text="Required for building and installing your mod to the game.\n"
                      "These should already be included in your download — paths are auto-detected.",
                 font=_vcr(9), fg=C["text_dim"], bg=C["card"],
                 anchor=tk.W, wraplength=580, justify=tk.LEFT).pack(fill=tk.X, pady=(2, 6))

        # Download + auto-detect buttons
        btn_row1 = tk.Frame(inner1, bg=C["card"])
        btn_row1.pack(fill=tk.X, pady=(0, 6))
        tk.Button(btn_row1, text="⬇ texconv.exe (GitHub)",
                  command=lambda: __import__("webbrowser").open(SetupDialog.TEXCONV_URL),
                  bg=C["sel_bg"], fg=C["cyan"], relief=tk.FLAT,
                  font=_vcr(9), cursor="hand2", padx=8, pady=2
                  ).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(btn_row1, text="⬇ repak.exe (GitHub)",
                  command=lambda: __import__("webbrowser").open(SetupDialog.REPAK_URL),
                  bg=C["sel_bg"], fg=C["cyan"], relief=tk.FLAT,
                  font=_vcr(9), cursor="hand2", padx=8, pady=2
                  ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(btn_row1, text="↻ Auto-Detect",
                  command=lambda: (self._auto_detect_tools(), self._update_all_status()),
                  bg=C["border"], fg=C["text"], relief=tk.FLAT,
                  font=_vcr(9), cursor="hand2", padx=8, pady=2
                  ).pack(side=tk.LEFT)

        # texconv row
        for key, label in [("texconv", "texconv.exe"), ("repak", "repak.exe")]:
            row = tk.Frame(inner1, bg=C["card"])
            row.pack(fill=tk.X, pady=2)
            led = tk.Label(row, text="●", font=_vcr(10), fg=C["text_dim"],
                           bg=C["card"], width=2)
            led.pack(side=tk.LEFT)
            tk.Label(row, text=label, font=_vcr(9), fg=C["text_dim"],
                     bg=C["card"], width=12, anchor=tk.W).pack(side=tk.LEFT)
            var = tk.StringVar(value=self.config.get(key, ""))
            self._vars[key] = var
            e = tk.Entry(row, textvariable=var, font=_vcr(9),
                         bg=C["input_bg"], fg=C["text"], insertbackground=C["text"],
                         relief=tk.FLAT, width=40)
            e.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
            var.trace_add("write", lambda *a, k=key: self._update_all_status())
            status = tk.Label(row, text="", font=_vcr(8), fg=C["text_dim"],
                              bg=C["card"], width=10, anchor=tk.W)
            status.pack(side=tk.LEFT, padx=(2, 0))
            self._status[key] = (led, status)
            tk.Button(row, text="Browse", command=lambda k=key, v=var: self._browse_file(k, v),
                      bg=C["border"], fg=C["text"], relief=tk.FLAT,
                      font=_vcr(9), cursor="hand2", padx=6, pady=1
                      ).pack(side=tk.LEFT)

        # Tools detection status message — at bottom of section with full width
        self._tools_detect_msg = tk.Label(inner1, text="", font=_vcr(8),
                                           fg=C["text_dim"], bg=C["card"],
                                           anchor=tk.W)
        self._tools_detect_msg.pack(fill=tk.X, pady=(4, 0))
        # Helper hint when tools are missing
        self._tools_hint = tk.Label(inner1, text="", font=_vcr(8),
                                     fg=C["red"], bg=C["card"],
                                     anchor=tk.W, wraplength=580, justify=tk.LEFT)
        self._tools_hint.pack(fill=tk.X)

        # ════════════════════════════════════════════════════════
        # SECTION 2: CONNECT GAME (merged pak + mods)
        # ════════════════════════════════════════════════════════
        sec2 = tk.Frame(content, bg=C["card"], highlightthickness=1,
                        highlightbackground=C["border"])
        sec2.pack(fill=tk.X, pady=6)
        inner2 = tk.Frame(sec2, bg=C["card"], padx=12, pady=8)
        inner2.pack(fill=tk.X)

        tk.Label(inner2, text="\U0001f3ae  CONNECT GAME",
                 font=_vcr(11, bold=True), fg=C["text_hi"],
                 bg=C["card"], anchor=tk.W).pack(fill=tk.X)
        tk.Label(inner2,
                 text="Locating your Retro Rewind installation.",
                 font=_vcr(9), fg=C["text_dim"], bg=C["card"],
                 anchor=tk.W).pack(fill=tk.X, pady=(2, 6))

        # Auto-detect + browse buttons
        btn_row2 = tk.Frame(inner2, bg=C["card"])
        btn_row2.pack(fill=tk.X, pady=(0, 6))
        tk.Button(btn_row2, text="↻ Auto-Detect",
                  command=lambda: (self._auto_detect_game(), self._update_all_status()),
                  bg=C["border"], fg=C["text"], relief=tk.FLAT,
                  font=_vcr(9), cursor="hand2", padx=8, pady=2
                  ).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(btn_row2, text="Browse Game Folder",
                  command=self._browse_game_folder,
                  bg=C["border"], fg=C["text"], relief=tk.FLAT,
                  font=_vcr(9), cursor="hand2", padx=8, pady=2
                  ).pack(side=tk.LEFT)
        self._game_detect_msg = tk.Label(btn_row2, text="", font=_vcr(8),
                                          fg=C["text_dim"], bg=C["card"])
        self._game_detect_msg.pack(side=tk.LEFT, padx=8)

        # Status rows (read-only display)
        # Game pak file row
        pak_row = tk.Frame(inner2, bg=C["card"])
        pak_row.pack(fill=tk.X, pady=2)
        pak_led = tk.Label(pak_row, text="●", font=_vcr(10), fg=C["text_dim"],
                       bg=C["card"], width=2)
        pak_led.pack(side=tk.LEFT)
        tk.Label(pak_row, text="Game pak file", font=_vcr(9), fg=C["text_dim"],
                 bg=C["card"], width=14, anchor=tk.W).pack(side=tk.LEFT)
        pak_var = tk.StringVar(value=self.config.get("base_game_pak", ""))
        self._vars["base_game_pak"] = pak_var
        tk.Entry(pak_row, textvariable=pak_var, font=_vcr(9),
                 bg=C["card"], fg=C["text_dim"],
                 readonlybackground=C["card"],
                 relief=tk.FLAT, width=40, state="readonly"
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        pak_status = tk.Label(pak_row, text="", font=_vcr(8), fg=C["text_dim"],
                          bg=C["card"], width=10, anchor=tk.W)
        pak_status.pack(side=tk.LEFT, padx=(2, 0))
        self._status["base_game_pak"] = (pak_led, pak_status)

        # Pak hint — directly under pak row
        self._pak_hint = tk.Label(inner2, text="",
                 font=_vcr(8), fg=C["text_dim"], bg=C["card"],
                 anchor=tk.W, wraplength=580, justify=tk.LEFT,
                 padx=22)
        self._pak_hint.pack(fill=tk.X)

        # Mods folder row
        mods_row = tk.Frame(inner2, bg=C["card"])
        mods_row.pack(fill=tk.X, pady=2)
        mods_led = tk.Label(mods_row, text="●", font=_vcr(10), fg=C["text_dim"],
                       bg=C["card"], width=2)
        mods_led.pack(side=tk.LEFT)
        tk.Label(mods_row, text="Mods folder", font=_vcr(9), fg=C["text_dim"],
                 bg=C["card"], width=14, anchor=tk.W).pack(side=tk.LEFT)
        mods_var = tk.StringVar(value=self.config.get("mods_folder", ""))
        self._vars["mods_folder"] = mods_var
        tk.Entry(mods_row, textvariable=mods_var, font=_vcr(9),
                 bg=C["card"], fg=C["text_dim"],
                 readonlybackground=C["card"],
                 relief=tk.FLAT, width=40, state="readonly"
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        mods_status = tk.Label(mods_row, text="", font=_vcr(8), fg=C["text_dim"],
                          bg=C["card"], width=10, anchor=tk.W)
        mods_status.pack(side=tk.LEFT, padx=(2, 0))
        self._status["mods_folder"] = (mods_led, mods_status)

        # Mods folder hint
        self._mods_hint = tk.Label(inner2, text="", font=_vcr(8),
                                    fg=C["red"], bg=C["card"],
                                    anchor=tk.W, wraplength=580, justify=tk.LEFT,
                                    padx=22)
        self._mods_hint.pack(fill=tk.X)

        # ════════════════════════════════════════════════════════
        # FOOTER
        # ════════════════════════════════════════════════════════
        ttk.Separator(card).pack(fill=tk.X, padx=16, pady=(12, 0))

        foot = tk.Frame(card, bg=C["card"], padx=20)
        foot.pack(fill=tk.X, pady=(6, 0))
        self._dev_mode_var = tk.BooleanVar(value=self.config.get("dev_mode", False))
        tk.Checkbutton(foot, text="Enable Dev Mode",
                       variable=self._dev_mode_var,
                       font=_vcr(8), fg=C["text_dim"], bg=C["card"],
                       selectcolor=C["card"], activebackground=C["card"],
                       activeforeground=C["text_dim"],
                       command=lambda: self.config.__setitem__("dev_mode",
                                                                self._dev_mode_var.get())
                       ).pack(side=tk.LEFT)

        self._save_btn = tk.Button(card,
                  text="✅  SETUP COMPLETE — GO TO MAIN MENU",
                  command=self._save,
                  bg=C["border"], fg=C["text_dim"],
                  font=_vcr(13, bold=True), relief=tk.FLAT,
                  pady=14, state=tk.DISABLED)
        self._save_btn.pack(fill=tk.X, padx=20, pady=(10, 16))

    def _update_all_status(self):
        """Update all LEDs, status labels, hints, summary bar, and save button."""
        for key in ["texconv", "repak", "base_game_pak", "mods_folder"]:
            val = self._vars.get(key, tk.StringVar()).get()
            led, status_lbl = self._status.get(key, (None, None))
            if not led:
                continue
            if val and os.path.exists(val):
                led.config(fg=C["green"])
                status_lbl.config(text="Found", fg=C["green"])
            elif val:
                led.config(fg=C["red"])
                status_lbl.config(text="Not found", fg=C["red"])
            else:
                led.config(fg=C["red"])
                status_lbl.config(text="Missing", fg=C["red"])

        # Special: mods_folder shows "Created" if we just made it
        mods_val = self._vars.get("mods_folder", tk.StringVar()).get()
        if mods_val and os.path.isdir(mods_val):
            led, status_lbl = self._status.get("mods_folder", (None, None))
            if led and status_lbl:
                if getattr(self, "_mods_created", False):
                    status_lbl.config(text="Created", fg=C["cyan"])
                    led.config(fg=C["green"])
                else:
                    status_lbl.config(text="Found", fg=C["green"])

        # --- Contextual helper hints ---

        # Tools hint
        tc_ok = os.path.exists(self._vars.get("texconv", tk.StringVar()).get())
        rp_ok = os.path.exists(self._vars.get("repak", tk.StringVar()).get())
        tools_ok = tc_ok and rp_ok
        if hasattr(self, "_tools_hint"):
            if not tools_ok:
                self._tools_hint.config(
                    text="Download the missing tools using the GitHub links above, "
                         "then use Browse to select them.",
                    fg=C["red"])
            else:
                self._tools_hint.config(text="")

        # Pak file hint
        pak_val = self._vars.get("base_game_pak", tk.StringVar()).get()
        pak_ok = pak_val and os.path.exists(pak_val)
        if hasattr(self, "_pak_hint"):
            if not pak_ok:
                # Check if the game folder was found (mods or pak path has content)
                game_folder_found = bool(mods_val)
                if game_folder_found:
                    self._pak_hint.config(
                        text="Is Retro Rewind fully installed via Steam? "
                             "Try verifying game files in Steam.",
                        fg=C["red"])
                else:
                    self._pak_hint.config(
                        text="Expected at: [Steam]\\steamapps\\common\\RetroRewind\\"
                             "RetroRewind\\Content\\Paks\\RetroRewind-Windows.pak",
                        fg=C["text_dim"])
            else:
                self._pak_hint.config(text="")

        # Mods folder hint
        mods_ok = mods_val and os.path.isdir(mods_val)
        if hasattr(self, "_mods_hint"):
            if not mods_ok and not pak_ok:
                self._mods_hint.config(
                    text="Locate the game folder first — the mods folder "
                         "will be created automatically.",
                    fg=C["red"])
            elif not mods_ok:
                self._mods_hint.config(
                    text="Could not create the mods folder. Check that the game "
                         "folder has write permissions.",
                    fg=C["red"])
            else:
                self._mods_hint.config(text="")

        # Summary bar
        game_ok = pak_ok and mods_ok
        self._summary_leds["tools"].config(
            text="●",
            fg=C["green"] if tools_ok else C["red"])
        self._summary_leds["game"].config(
            text="●",
            fg=C["green"] if game_ok else C["red"])

        # Save button — show exactly what's missing when disabled
        if tools_ok and game_ok:
            self._save_btn.config(
                text="✅  SETUP COMPLETE — GO TO MAIN MENU",
                state=tk.NORMAL, bg=C["green"], fg=C["bg"],
                cursor="hand2")
        else:
            missing = []
            for key, label in [("texconv", "texconv.exe"),
                               ("repak", "repak.exe"),
                               ("base_game_pak", "game pak file"),
                               ("mods_folder", "mods folder")]:
                val = self._vars.get(key, tk.StringVar()).get()
                if not val or not os.path.exists(val):
                    missing.append(label)
            hint = "Missing: " + ", ".join(missing)
            self._save_btn.config(
                text=f"⚠  {hint}",
                state=tk.DISABLED, bg=C["border"], fg=C["red"],
                cursor="")

    def _auto_detect_tools(self):
        """Check for texconv.exe and repak.exe next to the tool. 3-scenario feedback."""
        # Snapshot state before detection
        was_ok = {key: os.path.exists(self._vars.get(key, tk.StringVar()).get())
                  for key in ["texconv", "repak"]}

        newly_found = []
        still_missing = []
        for key, filename in [("texconv", "texconv.exe"), ("repak", "repak.exe")]:
            current = self._vars.get(key, tk.StringVar()).get()
            if current and os.path.exists(current):
                continue  # already good
            # Check multiple locations: next to exe, and in tools/ subfolder
            found = False
            for search_dir in [SCRIPT_DIR, os.path.join(SCRIPT_DIR, "tools")]:
                local_path = os.path.join(search_dir, filename)
                if os.path.exists(local_path):
                    self._vars[key].set(local_path)
                    newly_found.append(filename)
                    found = True
                    break
            if not found:
                still_missing.append(filename)

        # Cancel any pending fade timer
        if hasattr(self, "_tools_msg_fade_id") and self._tools_msg_fade_id:
            self.after_cancel(self._tools_msg_fade_id)
            self._tools_msg_fade_id = None

        if newly_found:
            # Scenario 1: something changed
            self._tools_detect_msg.config(
                text="Auto-detected successfully", fg=C["cyan"])
            self._tools_msg_fade_id = self.after(3000,
                lambda: self._tools_detect_msg.config(text=""))
        elif not still_missing:
            # Scenario 2: nothing changed, already configured
            self._tools_detect_msg.config(
                text="Already configured — nothing to update", fg=C["cyan"])
            self._tools_msg_fade_id = self.after(3000,
                lambda: self._tools_detect_msg.config(text=""))
        else:
            # Scenario 3: still can't find — persistent message
            if getattr(self, "_tools_detect_failed_before", False):
                # Re-pressed — flash the message
                self._tools_detect_msg.config(fg=C["red"])
                self.after(150, lambda: self._tools_detect_msg.config(
                    text="Could not auto-detect — please browse manually",
                    fg="#FF6644"))
                self.after(400, lambda: self._tools_detect_msg.config(fg=C["orange"]))
            else:
                self._tools_detect_msg.config(
                    text="Could not auto-detect — please browse manually",
                    fg=C["orange"])
            self._tools_detect_failed_before = True
            self._tools_msg_fade_id = None  # persistent, don't fade

    def _auto_detect_game(self):
        """Scan Steam library folders for RetroRewind. 3-scenario feedback."""
        import platform

        # Snapshot state before detection
        pak_was_ok = os.path.exists(self._vars.get("base_game_pak", tk.StringVar()).get())
        mods_was_ok = os.path.isdir(self._vars.get("mods_folder", tk.StringVar()).get())
        was_ok = pak_was_ok and mods_was_ok

        self._game_detect_msg.config(text="Scanning...", fg=C["text_dim"])
        self.update_idletasks()

        candidates = []
        if platform.system() == "Windows":
            steam_dirs = []
            drives = [chr(d) + ":" for d in range(ord('A'), ord('Z') + 1)
                      if os.path.exists(chr(d) + ":")]
            for d in drives:
                for sub in [
                    f"{d}\\Program Files (x86)\\Steam",
                    f"{d}\\Program Files\\Steam",
                    f"{d}\\Steam",
                ]:
                    if os.path.isdir(sub):
                        steam_dirs.append(sub)

            library_paths = set()
            for steam_dir in steam_dirs:
                library_paths.add(os.path.join(steam_dir, "steamapps"))
                vdf_path = os.path.join(steam_dir, "steamapps", "libraryfolders.vdf")
                if os.path.exists(vdf_path):
                    try:
                        with open(vdf_path, 'r', encoding='utf-8') as f:
                            for line in f:
                                line = line.strip()
                                if '"path"' in line:
                                    parts = line.split('"')
                                    if len(parts) >= 4:
                                        lib_path = parts[3].replace("\\\\", "\\")
                                        sa = os.path.join(lib_path, "steamapps")
                                        if os.path.isdir(sa):
                                            library_paths.add(sa)
                    except Exception:
                        pass

            for d in drives:
                for sub in [f"{d}\\SteamLibrary\\steamapps"]:
                    if os.path.isdir(sub):
                        library_paths.add(sub)

            for sa in library_paths:
                rr = os.path.join(sa, "common", "RetroRewind")
                if os.path.isdir(rr):
                    candidates.append(rr)

        elif platform.system() == "Darwin":
            home = os.path.expanduser("~")
            rr = os.path.join(home, "Library/Application Support/Steam/steamapps/common/RetroRewind")
            if os.path.isdir(rr):
                candidates.append(rr)
        else:
            home = os.path.expanduser("~")
            for sub in [".steam/steam", ".local/share/Steam"]:
                rr = os.path.join(home, sub, "steamapps/common/RetroRewind")
                if os.path.isdir(rr):
                    candidates.append(rr)

        found_now = False
        if candidates:
            self._set_game_folder(candidates[0])
            pak_ok = os.path.exists(self._vars.get("base_game_pak", tk.StringVar()).get())
            mods_ok = os.path.isdir(self._vars.get("mods_folder", tk.StringVar()).get())
            found_now = pak_ok and mods_ok

        # Cancel pending fade
        if hasattr(self, "_game_msg_fade_id") and self._game_msg_fade_id:
            self.after_cancel(self._game_msg_fade_id)
            self._game_msg_fade_id = None

        if found_now and not was_ok:
            # Scenario 1: something changed
            self._game_detect_msg.config(
                text="Auto-detected successfully", fg=C["cyan"])
            self._game_msg_fade_id = self.after(3000,
                lambda: self._game_detect_msg.config(text=""))
            self._game_detect_failed_before = False
        elif found_now and was_ok:
            # Scenario 2: already configured
            self._game_detect_msg.config(
                text="Already configured — nothing to update", fg=C["cyan"])
            self._game_msg_fade_id = self.after(3000,
                lambda: self._game_detect_msg.config(text=""))
        else:
            # Scenario 3: still can't find — persistent
            if getattr(self, "_game_detect_failed_before", False):
                self._game_detect_msg.config(fg=C["red"])
                self.after(150, lambda: self._game_detect_msg.config(
                    text="Could not auto-detect — please browse manually",
                    fg="#FF6644"))
                self.after(400, lambda: self._game_detect_msg.config(fg=C["orange"]))
            else:
                self._game_detect_msg.config(
                    text="Could not auto-detect — please browse manually",
                    fg=C["orange"])
            self._game_detect_failed_before = True
            self._game_msg_fade_id = None

    def _set_game_folder(self, game_root):
        """Set all game paths from a root game folder."""
        pak = os.path.join(game_root, "RetroRewind", "Content", "Paks", "RetroRewind-Windows.pak")
        mods = os.path.join(game_root, "RetroRewind", "Content", "Paks", "~mods")

        if os.path.exists(pak):
            self._vars["base_game_pak"].set(pak)
        else:
            self._vars["base_game_pak"].set("")

        self._mods_created = False
        if os.path.isdir(mods):
            self._vars["mods_folder"].set(mods)
        else:
            try:
                os.makedirs(mods, exist_ok=True)
                self._vars["mods_folder"].set(mods)
                self._mods_created = True
            except Exception:
                self._vars["mods_folder"].set("")

        self._update_all_status()

    def _browse_game_folder(self):
        """Let user browse to the RetroRewind game folder."""
        path = filedialog.askdirectory(title="Select RetroRewind game folder")
        if path:
            # Check if user selected the root or a subfolder
            # Try to find RetroRewind-Windows.pak relative to selection
            for candidate in [
                path,
                os.path.dirname(path),  # user might have selected Content or Paks
                os.path.join(path, ".."),
            ]:
                pak = os.path.join(candidate, "RetroRewind", "Content", "Paks", "RetroRewind-Windows.pak")
                if os.path.exists(pak):
                    self._set_game_folder(candidate)
                    self._game_detect_msg.config(text="Game found!", fg=C["green"])
                    return
            # Direct pak check if user browsed deeper
            if os.path.exists(os.path.join(path, "RetroRewind-Windows.pak")):
                # User selected the Paks folder
                game_root = os.path.dirname(os.path.dirname(os.path.dirname(path)))
                self._set_game_folder(game_root)
                self._game_detect_msg.config(text="Game found!", fg=C["green"])
                return
            self._game_detect_msg.config(
                text="Pak file not found in selected folder", fg=C["red"])

    def _browse_file(self, key, var):
        path = filedialog.askopenfilename(
            filetypes=[("Executables", "*.exe"), ("All", "*.*")])
        if path:
            var.set(path)
            self._update_all_status()

    def _save(self):
        for key, var in self._vars.items():
            self.config[key] = var.get()
        save_config(self.config)
        self.destroy()
        self.on_complete(self.config)



# ─────────────────────────────────────────────────────────────
# MAIN APP (new VHSToolApp class header + __init__ + _build_ui)
# ─────────────────────────────────────────────────────────────
class VHSToolApp:
    def __init__(self, root, config):
        self.root          = root
        self.config        = config
        self.replacements    = load_replacements()
        self.title_changes   = load_title_changes()
        # Migrate old T_New_* replacement keys to NR_{sku} format
        self.replacements = migrate_nr_replacements(self.replacements)
        self.selected        = None
        self.preview_photo   = None
        self._raw_img        = None
        self._base_img       = None
        self.pak_cache       = PakCache(
            config.get("base_game_pak", ""), config.get("repak", "")
        )
        self.dt_manager      = DataTableManager(self.pak_cache, self.title_changes)
        self._preview_thread = None
        self._title_entries  = []
        self._layout_preview = tk.IntVar(value=0)
        self._layout_offsets = {n: dict(LAYOUT_OFFSETS[n]) for n in range(1, 6)}
        self._layout_cal_mode = False
        self._lay_drag_start  = None
        self._layout_photo    = None   # keep reference

        # Star rating click state
        self._star_btns = []
        # Inline title var (trace wired after _build_ui to avoid premature callbacks)
        self._inline_title_var = tk.StringVar()
        self._inline_title_dirty = False
        self._show_overlay_labels = False
        # Edited-since-last-build tracking (persisted to disk)
        self._edited_slots = load_edited_slots()
        self._shipped_slots = load_shipped_slots()
        # Self-heal orphan keys left over from pre-v1.8.2.2 deletes (or external
        # JSON edits): replacements/shipped/edited could reference textures
        # that no longer exist in ALL_TEXTURES or NR_SLOT_DATA. Without this,
        # stale keys linger forever (shipped/edited are append-only at build),
        # causing build counter mismatches and ghost badges.
        self._prune_orphan_tracking()
        # Per-tab sort preferences (default: created_asc / oldest first).
        # Keys are tab names: genre names like "Action", or "New Releases".
        self._sort_prefs = load_sort_prefs()
        self._sort_dropdown_open = False  # popup state flag

        # Bulk move mode: lets users select several custom movies from one genre
        # and move them together without switching away from the source tab.
        self._bulk_mode = False
        self._bulk_selected = set()       # bkg_tex names
        self._bulk_source_genre = None

        self.root.title(f"Retro Rewind Movie Workshop  {TOOL_VERSION}")
        self.root.minsize(900, 600)
        # Scale default window size
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        def_w = min(int(1320 * SCALE_FACTOR), sw - 100)
        def_h = min(int(820 * SCALE_FACTOR), sh - 80)
        self.root.geometry(f"{def_w}x{def_h}")
        self.root.configure(bg=C["bg"])
        self.root.resizable(True, True)

        self._apply_ttk_style()
        self._build_ui()
        self._populate_shelf()
        self._preload_layouts()
        # Background preload the fixed regular layout — starts immediately,
        # completes before the user typically opens the overlay.
        self._start_layout_preload()
        self._startup_issues = []  # set by _show_startup_warning if needed

    def _show_ship_blocked_banner(self):
        """Show/re-show the warning banner when user clicks disabled Ship button."""
        issues = getattr(self, '_startup_issues', [])
        names = [i[2] for i in issues] if issues else ["Unknown issue"]
        msg = f"⚠  {', '.join(names)} not found — Ship to Store is disabled."

        # Remove existing banner if present
        if hasattr(self, '_warning_banner') and self._warning_banner:
            try:
                self._warning_banner.place_forget()
            except Exception:
                pass

        banner = tk.Frame(self.root, bg="#332B00", highlightthickness=1,
                          highlightbackground="#554400")
        banner.place(relx=0.5, rely=1.0, anchor=tk.S, height=32)
        self._warning_banner = banner

        tk.Label(banner, text=msg, font=_vcr(9), fg="#FFAA00",
                 bg="#332B00", padx=12).pack(side=tk.LEFT)

        fix_btn = tk.Label(banner, text="Fix in Setup →", font=_vcr(9, bold=True),
                           fg=C["cyan"], bg="#332B00", cursor="hand2", padx=8)
        fix_btn.pack(side=tk.LEFT)
        fix_btn.bind("<Button-1>", lambda e: self._open_setup())

        close_btn = tk.Label(banner, text="  ✕", font=_vcr(10),
                             fg="#FFAA00", bg="#332B00", cursor="hand2", padx=8)
        close_btn.pack(side=tk.LEFT)
        close_btn.bind("<Button-1>", lambda e: banner.place_forget())

        # Flash the banner briefly to draw attention
        def _flash(n=3):
            if n <= 0:
                banner.config(bg="#332B00")
                return
            banner.config(bg="#553300" if n % 2 else "#332B00")
            self.root.after(120, lambda: _flash(n - 1))
        _flash()

    def _show_startup_warning(self, issues):
        """Show an amber warning banner for non-critical issues (Level 2)."""
        self._startup_issues = issues
        names = [i[2] for i in issues]
        msg = f"⚠  {', '.join(names)} not found — Ship to Store is disabled."

        banner = tk.Frame(self.root, bg="#332B00", highlightthickness=1,
                          highlightbackground="#554400")
        banner.place(relx=0.5, rely=1.0, anchor=tk.S, height=32)
        self._warning_banner = banner

        tk.Label(banner, text=msg, font=_vcr(9), fg="#FFAA00",
                 bg="#332B00", padx=12).pack(side=tk.LEFT)

        fix_btn = tk.Label(banner, text="Fix in Setup →", font=_vcr(9, bold=True),
                           fg=C["cyan"], bg="#332B00", cursor="hand2", padx=8)
        fix_btn.pack(side=tk.LEFT)
        fix_btn.bind("<Button-1>", lambda e: self._open_setup())

        close_btn = tk.Label(banner, text="  ✕", font=_vcr(10),
                             fg="#FFAA00", bg="#332B00", cursor="hand2", padx=8)
        close_btn.pack(side=tk.LEFT)
        close_btn.bind("<Button-1>", lambda e: banner.place_forget())

        # Disable Ship to Store — clicking shows the warning banner
        if hasattr(self, '_ship_canvas'):
            self._ship_canvas.unbind("<Button-1>")
            self._draw_ship_btn(label="⚠  Setup required")
            self._ship_canvas.bind("<Button-1>", lambda e: self._show_ship_blocked_banner())

    def _show_critical_warning(self, issues):
        """Show a blocking dialog for critical issues (Level 3) inside the app."""
        self._startup_issues = issues
        names = [i[2] for i in issues]

        # Also disable Ship to Store — clicking shows the warning banner
        if hasattr(self, '_ship_canvas'):
            self._ship_canvas.unbind("<Button-1>")
            self._draw_ship_btn(label="⚠  Setup required")
            self._ship_canvas.bind("<Button-1>", lambda e: self._show_ship_blocked_banner())

        dlg = tk.Toplevel(self.root)
        dlg.title("Configuration Issue")
        dlg.configure(bg=C["bg"])
        dlg.resizable(False, False)
        dlg.focus_force()
        dlg.lift()
        dlg.transient(self.root)

        # Center on parent
        dlg.withdraw()

        pak_path = self.config.get("base_game_pak", "(not set)")

        tk.Label(dlg, text="⚠  Game pak file not found",
                 font=_vcr(13, bold=True), fg=C["red"], bg=C["bg"]
                 ).pack(padx=24, pady=(20, 8))

        if pak_path and pak_path != "(not set)":
            tk.Label(dlg, text=f"The game file at:\n{pak_path}\ncould not be found.",
                     font=_vcr(9), fg=C["text"], bg=C["bg"],
                     justify=tk.CENTER, wraplength=460).pack(padx=24, pady=(0, 8))
        else:
            tk.Label(dlg, text="No game pak file configured.",
                     font=_vcr(9), fg=C["text"], bg=C["bg"],
                     justify=tk.CENTER).pack(padx=24, pady=(0, 8))

        tk.Label(dlg, text="This usually means Retro Rewind was moved,\n"
                           "uninstalled, or updated by Steam.",
                 font=_vcr(9), fg=C["text_dim"], bg=C["bg"],
                 justify=tk.CENTER).pack(padx=24, pady=(0, 12))

        # Status row
        status_f = tk.Frame(dlg, bg=C["bg"])
        status_f.pack(padx=30, fill=tk.X, pady=(0, 4))
        _crit_led = tk.Label(status_f, text="●", font=_vcr(10), fg=C["red"], bg=C["bg"])
        _crit_led.pack(side=tk.LEFT)
        tk.Label(status_f, text="  Game pak file", font=_vcr(9), fg=C["text_dim"],
                 bg=C["bg"]).pack(side=tk.LEFT)
        _crit_status = tk.Label(status_f, text="Missing", font=_vcr(8),
                                 fg=C["red"], bg=C["bg"])
        _crit_status.pack(side=tk.RIGHT)

        _crit_msg = tk.Label(dlg, text="", font=_vcr(8),
                              fg=C["text_dim"], bg=C["bg"])
        _crit_msg.pack(padx=30, fill=tk.X)

        def _try_auto_detect():
            import platform
            _crit_msg.config(text="Scanning...", fg=C["text_dim"])
            dlg.update_idletasks()
            candidates = []
            if platform.system() == "Windows":
                drives = [chr(d) + ":" for d in range(ord('A'), ord('Z') + 1)
                          if os.path.exists(chr(d) + ":")]
                library_paths = set()
                for d in drives:
                    for sub in [f"{d}\\Program Files (x86)\\Steam",
                                f"{d}\\Program Files\\Steam",
                                f"{d}\\Steam"]:
                        if os.path.isdir(sub):
                            library_paths.add(os.path.join(sub, "steamapps"))
                            vdf = os.path.join(sub, "steamapps", "libraryfolders.vdf")
                            if os.path.exists(vdf):
                                try:
                                    with open(vdf, 'r', encoding='utf-8') as f:
                                        for line in f:
                                            if '"path"' in line.strip():
                                                parts = line.strip().split('"')
                                                if len(parts) >= 4:
                                                    lp = parts[3].replace("\\\\", "\\")
                                                    sa = os.path.join(lp, "steamapps")
                                                    if os.path.isdir(sa):
                                                        library_paths.add(sa)
                                except Exception:
                                    pass
                    for sub in [f"{d}\\SteamLibrary\\steamapps"]:
                        if os.path.isdir(sub):
                            library_paths.add(sub)
                for sa in library_paths:
                    rr = os.path.join(sa, "common", "RetroRewind")
                    if os.path.isdir(rr):
                        candidates.append(rr)
            for rr in candidates:
                pak = os.path.join(rr, "RetroRewind", "Content", "Paks",
                                   "RetroRewind-Windows.pak")
                if os.path.exists(pak):
                    _on_fixed(pak, rr)
                    return
            _crit_msg.config(text="Could not auto-detect — please browse manually",
                             fg=C["orange"])

        def _browse():
            path = filedialog.askdirectory(title="Select RetroRewind game folder")
            if path:
                for cand in [path, os.path.dirname(path)]:
                    pak = os.path.join(cand, "RetroRewind", "Content", "Paks",
                                       "RetroRewind-Windows.pak")
                    if os.path.exists(pak):
                        _on_fixed(pak, cand)
                        return
                _crit_msg.config(text="Pak file not found in selected folder", fg=C["red"])

        def _on_fixed(pak, game_root):
            self.config["base_game_pak"] = pak
            mods = os.path.join(os.path.dirname(pak), "~mods")
            os.makedirs(mods, exist_ok=True)
            self.config["mods_folder"] = mods
            save_config(self.config)
            # Update PakCache with new paths
            self.pak_cache = PakCache(pak, self.config.get("repak", ""))
            self.dt_manager = DataTableManager(self.pak_cache, self.title_changes)
            _crit_led.config(fg=C["green"])
            _crit_status.config(text="Found", fg=C["green"])
            _crit_msg.config(text="Game found!", fg=C["green"])
            self._startup_issues = []
            _continue_btn.config(state=tk.NORMAL, bg=C["green"], fg=C["bg"],
                                 cursor="hand2")

        def _open_setup():
            dlg.destroy()
            self._open_setup()

        btn_f = tk.Frame(dlg, bg=C["bg"])
        btn_f.pack(pady=(12, 6))
        tk.Button(btn_f, text="↻ Auto-Detect", command=_try_auto_detect,
                  bg=C["border"], fg=C["text"], relief=tk.FLAT,
                  font=_vcr(10), cursor="hand2", padx=12, pady=4
                  ).pack(side=tk.LEFT, padx=4)
        tk.Button(btn_f, text="Browse", command=_browse,
                  bg=C["border"], fg=C["text"], relief=tk.FLAT,
                  font=_vcr(10), cursor="hand2", padx=12, pady=4
                  ).pack(side=tk.LEFT, padx=4)
        tk.Button(btn_f, text="Open Setup", command=_open_setup,
                  bg=C["border"], fg=C["text"], relief=tk.FLAT,
                  font=_vcr(10), cursor="hand2", padx=12, pady=4
                  ).pack(side=tk.LEFT, padx=4)

        _continue_btn = tk.Button(dlg, text="✅  Continue",
                  command=dlg.destroy,
                  bg=C["border"], fg=C["text_dim"], relief=tk.FLAT,
                  font=_vcr(11, bold=True), padx=16, pady=6,
                  state=tk.DISABLED)
        _continue_btn.pack(pady=(4, 14))

        # Center on parent
        dlg.update_idletasks()
        dw = dlg.winfo_reqwidth()
        dh = dlg.winfo_reqheight()
        px = self.root.winfo_x() + (self.root.winfo_width() - dw) // 2
        py = self.root.winfo_y() + (self.root.winfo_height() - dh) // 2
        dlg.geometry(f"+{max(0, px)}+{max(0, py)}")
        dlg.deiconify()

    # ── TTK Style ─────────────────────────────────────────────
    def _apply_ttk_style(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure("TCombobox",
                    fieldbackground=C["input_bg"], background=C["input_bg"],
                    foreground=C["text"], selectbackground=C["pink"],
                    selectforeground=C["text_hi"])
        s.configure("TSeparator", background=C["border"])
        s.configure("Horizontal.TScale",
                    background=C["card"], troughcolor=C["border"],
                    sliderlength=28)
        s.configure("Horizontal.TProgressbar",
                    background=C["cyan"], troughcolor=C["border"])

    def _prune_orphan_tracking(self):
        """Remove stale keys from replacements/shipped/edited that no longer
        match a current texture or NR slot.

        Pre-v1.8.2.2 the delete handlers only cleaned `replacements`; shipped
        and edited sets were append-only at build time. Old saves carry the
        leak forward — call this once at startup to heal it. Also catches
        replacements drift from external JSON edits.
        """
        valid_tex = {t["name"] for t in ALL_TEXTURES}
        valid_nr  = {f"NR_{nr['sku']}" for nr in NR_SLOT_DATA}
        valid    = valid_tex | valid_nr

        repl_orphans = [k for k in self.replacements if k not in valid]
        if repl_orphans:
            for k in repl_orphans:
                del self.replacements[k]
            save_replacements(self.replacements)
            print(f"[Heal] Pruned {len(repl_orphans)} orphan replacement(s): "
                  f"{repl_orphans[:5]}{'…' if len(repl_orphans) > 5 else ''}")

        ship_orphans = self._shipped_slots - valid
        if ship_orphans:
            self._shipped_slots -= ship_orphans
            save_shipped_slots(self._shipped_slots)
            print(f"[Heal] Pruned {len(ship_orphans)} orphan shipped key(s): "
                  f"{sorted(ship_orphans)[:5]}{'…' if len(ship_orphans) > 5 else ''}")

        edit_orphans = self._edited_slots - valid
        if edit_orphans:
            self._edited_slots -= edit_orphans
            save_edited_slots(self._edited_slots)
            print(f"[Heal] Pruned {len(edit_orphans)} orphan edited key(s): "
                  f"{sorted(edit_orphans)[:5]}{'…' if len(edit_orphans) > 5 else ''}")

    # ── Layout preload ─────────────────────────────────────────
    def _mark_edited(self, name):
        """Mark a texture slot as edited since last build, persist to disk.

        Also flags the slot as having pending edits whose `last_edited_at`
        timestamp will be committed when the user moves away (selecting a
        different slot, switching genre, or clicking Build). This avoids
        a torrent of timestamp updates on every small canvas nudge or
        keystroke — we just commit once when the user is "done."

        Triggers a shelf re-populate so the "Edited" badge appears
        immediately on the affected row. Without this, callers that
        change non-image fields (rating, rarity, rotation) would only
        see the badge on the next natural refresh — which used to mean
        the next image upload elsewhere.
        """
        if name and name not in self._edited_slots:
            self._edited_slots.add(name)
            save_edited_slots(self._edited_slots)
        # Track that this slot has pending changes whose timestamp hasn't
        # been written yet. _flush_pending_edit_timestamp() commits later.
        if name:
            if not hasattr(self, "_pending_edit_slots"):
                self._pending_edit_slots = set()
            self._pending_edit_slots.add(name)
        if hasattr(self, "_refresh_shelf_keep_scroll"):
            self._refresh_shelf_keep_scroll()

    def _flush_pending_edit_timestamp(self):
        """Commit `last_edited_at` for any slots flagged via _mark_edited().

        Called when the user moves away from an edited slot — e.g. selects
        another slot, switches genre tab, or clicks Build. Writes the
        current time to each pending slot's `last_edited_at` field and
        persists. Single batched save per flush.
        """
        pending = getattr(self, "_pending_edit_slots", None)
        if not pending:
            return
        ts = _now_iso()
        # Find each slot by its texture name (== bkg_tex) across all DTs
        # plus NR slots, set last_edited_at, and persist once at the end.
        any_changed = False
        for tex_name in list(pending):
            # Genre slots
            for dt_name, slots in CLEAN_DT_SLOT_DATA.items():
                for s in slots:
                    if s.get("bkg_tex") == tex_name:
                        s["last_edited_at"] = ts
                        any_changed = True
                        break
            # NR slots — bkg_tex is shared so disambiguate by stable_key match.
            # But since _mark_edited is fed bkg_tex names from the shelf and NRs
            # also have bkg_tex, we'd hit the wrong slot. NRs get marked via a
            # separate path (NR-edits set the flag with the NR's stable key, not
            # bkg_tex). For now, also scan NR_SLOT_DATA by bkg_tex match if the
            # slot exists there too. NR-specific timestamping is handled when an
            # NR is the selected slot during a flush — see _flush_pending_nr().
        if any_changed:
            try:
                save_custom_slots()
            except Exception as e:
                print(f"[timestamp] save_custom_slots error: {e}")
        pending.clear()

    def _flush_pending_nr_timestamp(self, nr_idx):
        """Commit last_edited_at for a single NR slot if it was marked dirty.

        NR slots are tracked separately because their identity is the index +
        SKU, not a unique bkg_tex (multiple NRs can share bkg_tex). Called
        from NR-side selection-change and tab-switch hooks.
        """
        flag_attr = "_pending_nr_edit_idx"
        if not hasattr(self, flag_attr):
            return
        pending_idx = getattr(self, flag_attr)
        if pending_idx is None or not (0 <= pending_idx < len(NR_SLOT_DATA)):
            setattr(self, flag_attr, None)
            return
        NR_SLOT_DATA[pending_idx]["last_edited_at"] = _now_iso()
        try:
            save_nr_slots()
        except Exception as e:
            print(f"[timestamp] save_nr_slots error: {e}")
        setattr(self, flag_attr, None)

    def _mark_nr_edited(self, nr_idx):
        """Flag an NR slot as having pending edits (for last_edited_at)."""
        if 0 <= nr_idx < len(NR_SLOT_DATA):
            self._pending_nr_edit_idx = nr_idx

    def _preload_layouts(self):
        """No-op — layouts are now preloaded in background thread."""
        pass

    def _start_layout_preload(self):
        """Preload the single fixed regular layout in the background."""
        png_cache_dir = os.path.join(SCRIPT_DIR, LAYOUT_CACHE_DIRNAME)
        all_cached = (
            os.path.exists(os.path.join(png_cache_dir, "T_Layout_01_bc.png"))
            and os.path.exists(os.path.join(png_cache_dir, "T_Layout_01_bc_full.png"))
        )
        if all_cached:
            try:
                self.pak_cache.get_layout_texture(FIXED_REGULAR_LAYOUT, "bc")
                self.pak_cache.get_layout_texture_full(FIXED_REGULAR_LAYOUT, "bc")
            except Exception:
                pass
            self._hide_preload_bar()
            print("[PakCache] Fixed regular layout loaded from PNG cache (instant)")
            return

        self._preload_done = 0
        self._preload_total = 1
        self._update_preload_bar(0)

        def _worker():
            try:
                self.pak_cache.get_layout_texture_full(FIXED_REGULAR_LAYOUT, "bc")
                self.pak_cache.get_layout_texture(FIXED_REGULAR_LAYOUT, "bc")
            except Exception:
                pass
            self.root.after(0, lambda: self._update_preload_bar(1.0))
            self.root.after(800, self._hide_preload_bar)

        import threading
        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def _update_preload_bar(self, fraction):
        """Draw thin cyan progress line at top of window."""
        if not hasattr(self, "_preload_bar_canvas"):
            return
        c = self._preload_bar_canvas
        c.delete("all")
        if fraction <= 0:
            return
        w = c.winfo_width() or self.root.winfo_width()
        fill_w = int(w * min(fraction, 1.0))
        c.create_rectangle(0, 0, fill_w, 2, fill=DS["cyan"], outline="")

    def _hide_preload_bar(self):
        """Remove the progress bar after preload completes."""
        if hasattr(self, "_preload_bar_canvas"):
            self._preload_bar_canvas.pack_forget()

    # ─────────────────────────────────────────────────────────
    # BUILD UI
    # ─────────────────────────────────────────────────────────
    def _build_ui(self):
        BG, CARD, CYAN, PINK = C["bg"], C["card"], C["cyan"], C["pink"]

        # ── Thin preload progress bar (top of window, disappears when done) ──
        self._preload_bar_canvas = tk.Canvas(self.root, height=2,
            bg=DS["bg"], bd=0, highlightthickness=0)
        self._preload_bar_canvas.pack(fill=tk.X, side=tk.TOP)
        self._preload_bar_progress = 0  # 0.0 to 1.0

        # ── TOP BAR ───────────────────────────────────────────
        topbar = tk.Frame(self.root, bg=C["bg2"], pady=0)
        topbar.pack(fill=tk.X)

        # Logo left
        logo_f = tk.Frame(topbar, bg=C["bg2"])
        logo_f.pack(side=tk.LEFT, padx=(16,0), pady=8)
        tk.Label(logo_f, text="📼", font=("Segoe UI Emoji", 18),
                 bg=C["bg2"], fg=PINK).pack(side=tk.LEFT)
        tk.Label(logo_f, text=" RETRO REWIND",
                 font=_vcr(13, bold=True), fg=PINK,
                 bg=C["bg2"]).pack(side=tk.LEFT)
        tk.Label(logo_f, text=" VHS",
                 font=_vcr(13, bold=True), fg=CYAN,
                 bg=C["bg2"]).pack(side=tk.LEFT)

        # Right controls
        right_bar = tk.Frame(topbar, bg=C["bg2"])
        right_bar.pack(side=tk.RIGHT, padx=16, pady=6)

        self._stats_var = tk.StringVar(value="Shelves: 0 filled")
        tk.Label(right_bar, textvariable=self._stats_var,
                 font=_vcr(10), fg=C["text_dim"], bg=C["bg2"]
                 ).pack(side=tk.LEFT, padx=(0,14))

        self._search_var = tk.StringVar()
        self._search_after_id = None
        def _search_changed(*_a):
            if self._search_after_id is not None:
                try:
                    self.root.after_cancel(self._search_after_id)
                except Exception:
                    pass
            def _do_search():
                self._search_after_id = None
                self._populate_shelf()
            self._search_after_id = self.root.after(120, _do_search)
        self._search_var.trace_add('write', _search_changed)
        search_f = tk.Frame(right_bar, bg=C["input_bg"],
                            highlightthickness=1,
                            highlightbackground=C["border"])
        search_f.pack(side=tk.LEFT, padx=(0,10))
        tk.Label(search_f, text="🔍", bg=C["input_bg"],
                 fg=C["text_dim"], font=("Segoe UI Emoji",10)).pack(side=tk.LEFT, padx=(6,2))
        tk.Entry(search_f, textvariable=self._search_var,
                 bg=C["input_bg"], fg=C["text"],
                 insertbackground=C["cyan"], font=_vcr(11),
                 relief=tk.FLAT, width=18).pack(side=tk.LEFT, padx=(0,6), ipady=4)

        tk.Button(right_bar, text="⚙  Setup",
                  command=self._open_setup,
                  bg=C["border"], fg=C["text"], relief=tk.FLAT,
                  font=_vcr(10), cursor="hand2", padx=8, pady=4
                  ).pack(side=tk.LEFT)
        self._dev_btn = tk.Button(right_bar, text="🧪 Dev",
                  command=self._show_dev_dialog,
                  bg=C["border"], fg=C["text_dim"], relief=tk.FLAT,
                  font=_vcr(10), cursor="hand2", padx=8, pady=4)
        if self.config.get("dev_mode", False):
            self._dev_btn.pack(side=tk.LEFT, padx=(4, 0))

        # ── GENRE TABS ────────────────────────────────────────
        self._genre_var = tk.StringVar(value="All Movies")
        self._filter_var = tk.StringVar(value="All")      # internal only
        self._list_status_var = tk.StringVar(value="")    # compat

        tabs_outer = tk.Frame(self.root, bg=DS["bg"])
        tabs_outer.pack(fill=tk.X)
        tk.Frame(tabs_outer, bg=DS["border"], height=1).pack(fill=tk.X)

        # Scrollable tab bar with arrow buttons
        tabs_scroll_frame = tk.Frame(tabs_outer, bg=DS["bg"])
        tabs_scroll_frame.pack(fill=tk.X, side=tk.TOP)

        self._tab_left_btn = tk.Label(tabs_scroll_frame, text="‹",
            font=_f(FS["sec"], bold=True), fg=DS["text3"], bg=DS["bg"],
            cursor="hand2", padx=2)
        self._tab_left_btn.pack(side=tk.LEFT, fill=tk.Y)
        self._tab_left_btn.bind("<Button-1>", lambda e: self._scroll_tabs(-1))

        # Canvas for horizontal scrolling
        self._tabs_canvas = tk.Canvas(tabs_scroll_frame, bg=DS["bg"],
            bd=0, highlightthickness=0)
        self._tabs_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._tab_right_btn = tk.Label(tabs_scroll_frame, text="›",
            font=_f(FS["sec"], bold=True), fg=DS["text3"], bg=DS["bg"],
            cursor="hand2", padx=2)
        self._tab_right_btn.pack(side=tk.LEFT, fill=tk.Y)
        self._tab_right_btn.bind("<Button-1>", lambda e: self._scroll_tabs(1))

        self._tabs_row = tk.Frame(self._tabs_canvas, bg=DS["bg"])
        self._tabs_canvas.create_window((0, 0), window=self._tabs_row, anchor=tk.NW)
        def _on_tabs_row_configure(e):
            bbox = self._tabs_canvas.bbox("all")
            if bbox:
                self._tabs_canvas.configure(scrollregion=bbox)
                # Set canvas height to match content
                content_h = bbox[3] - bbox[1]
                self._tabs_canvas.configure(height=content_h)
            self._update_tab_arrows()
        self._tabs_row.bind("<Configure>", _on_tabs_row_configure)
        self._tabs_canvas.bind("<Configure>", lambda e: self._update_tab_arrows())

        # Mousewheel scroll on tab bar
        def _tab_wheel(event):
            if event.delta:
                direction = -1 if event.delta > 0 else 1
            elif event.num == 4:
                direction = -1
            elif event.num == 5:
                direction = 1
            else:
                return
            self._scroll_tabs(direction)
        for w in [self._tabs_canvas, tabs_scroll_frame]:
            w.bind("<MouseWheel>", _tab_wheel)
            w.bind("<Button-4>", _tab_wheel)
            w.bind("<Button-5>", _tab_wheel)
        self._tab_wheel_fn = _tab_wheel

        self._tab_underline = tk.Frame(tabs_outer, bg=DS["cyan"], height=2)

        tk.Frame(tabs_outer, bg=DS["border"], height=1).pack(fill=tk.X)

        genres_display = ["All Movies"] + [g for g in GENRES if g not in HIDDEN_GENRES] + ["New Releases"]
        self._tab_btns   = {}   # genre -> outer clickable Frame
        self._tab_labels = {}   # genre -> name Label (for font/color update)
        self._tab_badges = {}   # genre -> badge Label (for count update)

        for idx, g in enumerate(genres_display):
            # Vertical 1px divider between "All Movies" and first genre tab
            if idx == 1:
                tk.Frame(self._tabs_row, bg=DS["border"],
                         width=1).pack(side=tk.LEFT, fill=tk.Y, pady=6)

            # Outer tab frame — acts as the clickable hit target
            tab_f = tk.Frame(self._tabs_row, bg=DS["bg"], cursor="hand2")
            tab_f.pack(side=tk.LEFT, padx=0)

            # Inner padding frame
            inner = tk.Frame(tab_f, bg=DS["bg"])
            inner.pack(padx=SP[3], pady=(SP[2], SP[2]))

            # Genre name label
            name_lbl = tk.Label(inner, text=g,
                                font=_f(FS["body"]), fg=DS["text3"],
                                bg=DS["bg"], cursor="hand2")
            name_lbl.pack(side=tk.LEFT)
            self._tab_labels[g] = name_lbl

            # Count badge — small pill: border-colored bg, muted text
            badge_frame = tk.Frame(inner, bg=DS["border"])
            badge_frame.pack(side=tk.LEFT, padx=(SP[1], 0))
            badge_lbl = tk.Label(badge_frame, text="0",
                                 font=_f(FS["meta"]), fg=DS["text3"],
                                 bg=DS["border"],
                                 padx=SP[1], pady=0)
            badge_lbl.pack()
            self._tab_badges[g] = badge_lbl

            # Click binding on every child widget
            cmd = lambda genre=g: self._select_genre(genre)
            for w in (tab_f, inner, name_lbl, badge_frame, badge_lbl):
                w.bind("<Button-1>", lambda e, fn=cmd: fn())
                w.bind("<MouseWheel>", _tab_wheel)
                w.bind("<Button-4>", _tab_wheel)
                w.bind("<Button-5>", _tab_wheel)

            self._tab_btns[g] = tab_f

        self._update_tab_colors("All Movies")

        # ── MAIN CONTENT AREA ─────────────────────────────────
        main = tk.Frame(self.root, bg=C["bg"])
        main.pack(fill=tk.BOTH, expand=True, padx=0)
        # Panel sizing: left=fixed, center=grows, right=fixed
        LEFT_PANEL_W = max(220, min(320, int(280 * SCALE_FACTOR)))
        RIGHT_PANEL_W = max(260, min(340, int(300 * SCALE_FACTOR)))
        main.columnconfigure(0, minsize=LEFT_PANEL_W, weight=0)
        main.columnconfigure(1, weight=1, minsize=350)
        main.columnconfigure(2, minsize=RIGHT_PANEL_W, weight=0)
        main.rowconfigure(0, weight=1, minsize=400)

        # ── LEFT: SHELF ───────────────────────────────────────
        shelf_outer = tk.Frame(main, bg=C["card"],
                               highlightthickness=1,
                               highlightbackground=C["border"])
        shelf_outer.grid(row=0, column=0, sticky="nsew", padx=(8,4), pady=8)

        shelf_header = tk.Frame(shelf_outer, bg=C["card"])
        shelf_header.pack(fill=tk.X, padx=10, pady=(8,4))
        tk.Label(shelf_header, text="GENRE SHELF",
                 font=_vcr(11, bold=True), fg=CYAN, bg=C["card"]
                 ).pack(side=tk.LEFT)

        # ── Sort control (right side of GENRE SHELF header) ──
        # Visible on every tab EXCEPT "All Movies" (the multi-genre view
        # doesn't sort). For all other tabs — single genre tabs and the
        # "New Releases" tab — the sort control is shown and the chosen
        # sort is persisted independently per tab. See _select_genre and
        # _show_sort_control for the visibility logic. The trigger button
        # shows the current sort selection (label + arrow); clicking it
        # opens a Toplevel dropdown popup. Sort state is persisted per-tab
        # to sort_preferences.json — see SORT_OPTIONS / _on_sort_selected.
        self._sort_control_frame = tk.Frame(shelf_header, bg=C["card"])
        self._sort_control_frame.pack(side=tk.RIGHT)

        sort_lbl = tk.Label(self._sort_control_frame, text="Sort by",
                            font=_vcr(9), fg=DS["text3"], bg=C["card"])
        sort_lbl.pack(side=tk.LEFT, padx=(0, 6))

        # Cyan text-button styled like a clickable label. Initial label is
        # the default sort ("Created at ▴"); _refresh_sort_btn_label updates
        # it whenever the active genre's stored preference changes.
        self._sort_btn = tk.Label(
            self._sort_control_frame, text="Created at  ▴",
            font=_vcr(9, bold=True), fg=DS["cyan"], bg=C["card"],
            cursor="hand2", padx=6, pady=2,
        )
        self._sort_btn.pack(side=tk.LEFT)

        # Bind the click handler to every visible widget in the control —
        # the cyan label, the parent frame, and the "Sort by" prefix label.
        # Single-widget binding sometimes misses on certain cursor positions
        # in Tkinter; binding the whole area makes the trigger reliable.
        def _sort_click_handler(_e=None):
            self._toggle_sort_dropdown()
        for w in (self._sort_btn, self._sort_control_frame, sort_lbl):
            w.bind("<Button-1>", _sort_click_handler)

        # Bulk Move control — shown on regular single-genre tabs only.
        self._bulk_btn = tk.Label(
            shelf_header, text="Bulk Move", font=_vcr(9, bold=True),
            fg=DS["text3"], bg=C["card"], cursor="hand2", padx=6, pady=2)
        self._bulk_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self._bulk_btn.bind("<Button-1>", lambda e: self._toggle_bulk_mode())

        self._bulk_count_var = tk.StringVar(value="Bulk mode: 0 selected")
        self._bulk_action_bar = tk.Frame(shelf_outer, bg="#071a1a",
                                         highlightthickness=1,
                                         highlightbackground=DS["cyan"])
        tk.Label(self._bulk_action_bar, textvariable=self._bulk_count_var,
                 font=_vcr(8), fg=DS["cyan"], bg="#071a1a",
                 anchor=tk.W).pack(side=tk.LEFT, padx=(8, 4), pady=5, fill=tk.X, expand=True)

        self._bulk_select_all_btn = tk.Label(self._bulk_action_bar, text="Select all shown",
                 font=_vcr(8, bold=True), fg=DS["text_inv"], bg=DS["cyan"],
                 padx=6, pady=3, cursor="hand2")
        self._bulk_select_all_btn.pack(side=tk.LEFT, padx=(3, 3), pady=4)
        self._bulk_select_all_btn.bind("<Button-1>", lambda e: self._bulk_select_all_filtered())

        self._bulk_clear_btn = tk.Label(self._bulk_action_bar, text="Clear",
                 font=_vcr(8), fg=DS["text3"], bg=DS["border"],
                 padx=6, pady=3, cursor="hand2")
        self._bulk_clear_btn.pack(side=tk.LEFT, padx=(0, 3), pady=4)
        self._bulk_clear_btn.bind("<Button-1>", lambda e: self._bulk_clear_selection())

        self._bulk_move_btn = tk.Label(self._bulk_action_bar, text="Move selected…",
                 font=_vcr(8, bold=True), fg=DS["bg"], bg=DS["cyan"],
                 padx=6, pady=3, cursor="hand2")
        self._bulk_move_btn.pack(side=tk.LEFT, padx=(0, 3), pady=4)
        self._bulk_move_btn.bind("<Button-1>", lambda e: self._open_bulk_move_picker())

        self._bulk_done_btn = tk.Label(self._bulk_action_bar, text="Done",
                 font=_vcr(8), fg=DS["text3"], bg=DS["border"],
                 padx=6, pady=3, cursor="hand2")
        self._bulk_done_btn.pack(side=tk.LEFT, padx=(0, 8), pady=4)
        self._bulk_done_btn.bind("<Button-1>", lambda e: self._toggle_bulk_mode(False))
        self._bulk_action_bar.pack_forget()

        # Canvas + scrollbar for tile gallery
        shelf_canvas_outer = tk.Frame(shelf_outer, bg=C["card"])
        self._shelf_canvas_outer = shelf_canvas_outer
        shelf_canvas_outer.pack(fill=tk.BOTH, expand=True)

        shelf_vsb = tk.Scrollbar(shelf_canvas_outer, orient=tk.VERTICAL,
                                  bg=C["border"], troughcolor=C["bg"])
        shelf_vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._shelf_canvas = tk.Canvas(shelf_canvas_outer,
                                        bg=C["bg"], bd=0,
                                        highlightthickness=0,
                                        yscrollcommand=shelf_vsb.set)
        self._shelf_canvas.pack(fill=tk.BOTH, expand=True)
        shelf_vsb.config(command=self._shelf_canvas.yview)

        # Sticky genre header (pinned above scroll area, visible in All Movies)
        self._sticky_genre_hdr = tk.Frame(shelf_canvas_outer, bg="#1a1a1a")
        self._sticky_genre_accent = tk.Frame(self._sticky_genre_hdr, bg=C["border"], width=4)
        self._sticky_genre_accent.pack(side=tk.LEFT, fill=tk.Y)
        self._sticky_genre_inner = tk.Frame(self._sticky_genre_hdr, bg="#1a1a1a")
        self._sticky_genre_inner.pack(side=tk.LEFT, fill=tk.X, expand=True,
                                       padx=(8, SP[3]), pady=SP[2])
        self._sticky_genre_label = tk.Label(self._sticky_genre_inner, text="",
            font=_f(FS["body"], bold=True), fg=C["text"], bg="#1a1a1a")
        self._sticky_genre_label.pack(side=tk.LEFT)
        self._sticky_genre_count = tk.Frame(self._sticky_genre_inner, bg=C["border"],
                                             padx=6, pady=1)
        self._sticky_genre_count.pack(side=tk.LEFT, padx=(8,0))
        self._sticky_genre_count_lbl = tk.Label(self._sticky_genre_count, text="0",
            font=_f(FS["meta"]), fg=C["text_dim"], bg=C["border"])
        self._sticky_genre_count_lbl.pack()
        self._sticky_genre_chevron = tk.Label(self._sticky_genre_inner, text="▾",
            font=_f(FS["body"]), fg=C["text_dim"], bg="#1a1a1a")
        self._sticky_genre_chevron.pack(side=tk.RIGHT)
        # Hidden by default — shown only in All Movies tab
        self._sticky_genre_hdr.pack_forget()

        self._shelf_frame = tk.Frame(self._shelf_canvas, bg=C["bg"])
        self._shelf_canvas_window = self._shelf_canvas.create_window(
            (0, 0), window=self._shelf_frame, anchor=tk.NW)
        self._shelf_frame.bind("<Configure>",
            lambda e: self._shelf_canvas.configure(
                scrollregion=self._shelf_canvas.bbox("all")))
        self._shelf_canvas.bind("<Configure>",
            lambda e: self._shelf_canvas.itemconfig(
                self._shelf_canvas_window, width=e.width))
        # ── Smooth-momentum scroll ────────────────────────────
        # velocity: fractional canvas units per frame (decays each frame)
        # DECAY:    multiplier per 16ms frame  (~0.88 → stops in ~300ms)
        # THRESH:   stop when |velocity| < this
        # Smooth eased scrolling — ~2.5 rows per tick, ease-out animation
        ROW_H       = 32        # approximate row height
        SCROLL_PX   = ROW_H * 2.5  # ~80px per tick
        EASE_FACTOR = 0.35      # remaining_distance * factor each frame
        EASE_MS     = 12        # frame interval
        EASE_THRESH = 1.0       # stop when remaining < this (pixels)
        _remaining  = [0.0]     # remaining scroll distance in pixels
        _ease_job   = [None]

        def _clamp_shelf_scroll():
            """Prevent scrolling past content or when content fits in view."""
            bbox = self._shelf_canvas.bbox("all")
            if not bbox:
                return
            content_h = bbox[3] - bbox[1]
            canvas_h = self._shelf_canvas.winfo_height()
            if content_h <= canvas_h:
                self._shelf_canvas.yview_moveto(0)
                _remaining[0] = 0.0
                return
            top, bottom = self._shelf_canvas.yview()
            if top < 0:
                self._shelf_canvas.yview_moveto(0)
                _remaining[0] = 0.0
            elif bottom >= 1.0:
                self._shelf_canvas.yview_moveto(max(0, 1.0 - (bottom - top)))
                _remaining[0] = 0.0

        def _ease_tick():
            """Animate one frame of eased scrolling."""
            step = _remaining[0] * EASE_FACTOR
            if abs(_remaining[0]) < EASE_THRESH:
                _remaining[0] = 0.0
                _ease_job[0] = None
                return
            # Convert pixel step to fraction of scrollregion
            bbox = self._shelf_canvas.bbox("all")
            if not bbox:
                _remaining[0] = 0.0
                _ease_job[0] = None
                return
            content_h = max(1, bbox[3] - bbox[1])
            frac = step / content_h
            top = self._shelf_canvas.yview()[0]
            self._shelf_canvas.yview_moveto(top + frac)
            _remaining[0] -= step
            _clamp_shelf_scroll()
            if hasattr(self, '_update_sticky_header'):
                self._update_sticky_header()
            if abs(_remaining[0]) >= EASE_THRESH:
                _ease_job[0] = self.root.after(EASE_MS, _ease_tick)
            else:
                _remaining[0] = 0.0
                _ease_job[0] = None

        def _shelf_scroll(event):
            # Determine direction
            if event.delta:
                direction = -1 if event.delta > 0 else 1
            elif event.num == 4:
                direction = -1
            elif event.num == 5:
                direction = 1
            else:
                return
            # Accumulate: add to remaining distance if animation in progress
            _remaining[0] += direction * SCROLL_PX
            # Start easing if not already running
            if not _ease_job[0]:
                _ease_job[0] = self.root.after(EASE_MS, _ease_tick)

        # Use root-level bind_all while mouse is inside the shelf area.
        # This is the only reliable way to catch scroll events over every pixel
        # (canvas window child frames, gaps between rows, header labels, etc.).
        def _shelf_enter(e):
            self.root.bind_all("<MouseWheel>", _shelf_scroll)
            self.root.bind_all("<Button-4>",   _shelf_scroll)
            self.root.bind_all("<Button-5>",   _shelf_scroll)
        def _shelf_leave(e):
            self.root.unbind_all("<MouseWheel>")
            self.root.unbind_all("<Button-4>")
            self.root.unbind_all("<Button-5>")
        self._shelf_canvas.bind("<Enter>", _shelf_enter)
        self._shelf_canvas.bind("<Leave>", _shelf_leave)

        # Sticky header scroll tracking
        def _update_sticky_header(*args):
            if self._genre_var.get() != "All Movies":
                if self._sticky_genre_hdr.winfo_manager():
                    self._sticky_genre_hdr.pack_forget()
                return
            if not hasattr(self, '_genre_header_widgets') or not self._genre_header_widgets:
                if self._sticky_genre_hdr.winfo_manager():
                    self._sticky_genre_hdr.pack_forget()
                return

            canvas_top_y = self._shelf_canvas.canvasy(0)

            # Find which genre header is at or just above the visible top
            current_genre = None
            first_header = self._genre_header_widgets[0]
            first_y = first_header.winfo_y()

            # Only show sticky if the first header has scrolled out of view
            if canvas_top_y <= first_y + 5:
                # First header still visible — hide sticky
                if self._sticky_genre_hdr.winfo_manager():
                    self._sticky_genre_hdr.pack_forget()
                return

            for hdr_f in self._genre_header_widgets:
                try:
                    hdr_y = hdr_f.winfo_y()
                    hdr_h = hdr_f.winfo_height()
                    if hdr_y + hdr_h <= canvas_top_y + 5:
                        current_genre = hdr_f
                    else:
                        break
                except Exception:
                    pass

            if current_genre:
                g = current_genre._genre_name
                ac = current_genre._accent_color
                tc = current_genre._text_color
                cnt = current_genre._count
                self._sticky_genre_accent.config(bg=ac)
                self._sticky_genre_label.config(text=g.upper(), fg=tc)
                self._sticky_genre_count_lbl.config(text=str(cnt))
                chev = "▸" if current_genre._collapsed else "▾"
                self._sticky_genre_chevron.config(text=chev)
                if not self._sticky_genre_hdr.winfo_manager():
                    self._sticky_genre_hdr.pack(fill=tk.X, before=self._shelf_canvas)
                def _toggle_sticky(e=None, genre_key=g):
                    self._genre_collapsed[genre_key] = not self._genre_collapsed.get(genre_key, False)
                    self._populate_shelf()
                for w in [self._sticky_genre_hdr, self._sticky_genre_inner,
                          self._sticky_genre_label, self._sticky_genre_chevron,
                          self._sticky_genre_accent]:
                    w.config(cursor="hand2")
                    w.bind("<Button-1>", _toggle_sticky)
            elif self._sticky_genre_hdr.winfo_manager():
                self._sticky_genre_hdr.pack_forget()

        def _on_shelf_configure(e):
            self._shelf_canvas.itemconfig(self._shelf_canvas_window, width=e.width)
            _update_sticky_header()
        self._shelf_canvas.bind("<Configure>", _on_shelf_configure)
        # Also update on scrollbar drag (yscrollcommand fires on any scroll change)
        def _on_shelf_yscroll(*args):
            shelf_vsb.set(*args)
            _update_sticky_header()
        self._shelf_canvas.config(yscrollcommand=_on_shelf_yscroll)
        self._update_sticky_header = _update_sticky_header
        self._shelf_frame.bind("<Enter>",  _shelf_enter)
        self._shelf_frame.bind("<Leave>",  _shelf_leave)
        self._shelf_leave_fn_real = _shelf_leave
        # Store so child widgets can also trigger enter/leave
        self._shelf_scroll_fn  = _shelf_scroll
        self._shelf_enter_fn   = _shelf_enter
        self._shelf_leave_fn   = _shelf_leave

        # ── Sticky "Add Movie to Genre" button ───────────────
        # Always visible at bottom of left panel, outside the scroll area.
        # Hidden when "All Movies" is selected (each genre has its own inline button there).
        self._sticky_add_frame = tk.Frame(shelf_outer, bg=DS["bg"],
                                           highlightthickness=1,
                                           highlightbackground=DS["border"])
        self._sticky_add_frame.pack(fill=tk.X, padx=6, pady=(0, 6))

        self._sticky_add_canvas = tk.Canvas(
            self._sticky_add_frame, bg=DS["bg"], height=34,
            bd=0, highlightthickness=0, cursor="hand2")
        self._sticky_add_canvas.pack(fill=tk.X)

        def _draw_sticky_add(hover=False):
            c = self._sticky_add_canvas
            c.delete("all")
            w = c.winfo_width() or 200
            h = 34
            genre = self._genre_var.get()
            if genre == "All Movies":
                return
            # Eye-catching cyan style
            bg_fill = DS["cyan"] if hover else "#0A2A2A"
            border_c = DS["cyan"]
            txt_color = DS["bg"] if hover else DS["cyan"]
            c.create_rectangle(0, 0, w, h, fill=bg_fill, outline=border_c, width=1)
            if genre == "New Releases":
                label = "+ Add New Release"
            else:
                label = f"+ Add movie to {genre}"
            c.create_text(w // 2, h // 2, text=label,
                          font=_f(FS["body"], bold=True), fill=txt_color, anchor=tk.CENTER)

        self._sticky_add_canvas.bind("<Configure>", lambda e: _draw_sticky_add())
        self._sticky_add_canvas.bind("<Enter>",     lambda e: _draw_sticky_add(hover=True))
        self._sticky_add_canvas.bind("<Leave>",     lambda e: _draw_sticky_add(hover=False))
        def _on_sticky_add_click(e):
            genre = self._genre_var.get()
            if genre == "New Releases":
                self._add_new_release()
            else:
                self._add_movie_to_genre(genre)
        self._sticky_add_canvas.bind("<Button-1>", _on_sticky_add_click)
        self._draw_sticky_add = _draw_sticky_add

        # ── CENTER: PREVIEW ───────────────────────────────────
        preview_outer = tk.Frame(main, bg=DS["panel"],
                                  highlightthickness=1,
                                  highlightbackground=DS["border"])
        preview_outer.grid(row=0, column=1, sticky="nsew", padx=4, pady=8)
        # Row 0 = tab bar, Row 1 = canvas (fills all space), rows 2+ = controls
        preview_outer.rowconfigure(1, weight=1)
        preview_outer.columnconfigure(0, weight=1)

        # ── Viewport tab bar (above canvas) ──────────────────
        self._vp_tab_bar = tk.Frame(preview_outer, bg=DS["panel"])
        self._vp_tab_bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(6, 0))
        # Inner frame for centering
        self._vp_tab_inner = tk.Frame(self._vp_tab_bar, bg=DS["panel"])
        self._vp_tab_inner.pack(anchor=tk.CENTER)

        # Replace Image button (always visible, centered)
        self._vp_replace_btn = tk.Button(self._vp_tab_inner,
            text="🖼  Replace Image", font=_f(FS["body"]),
            bg=DS["border"], fg=DS["text"], relief=tk.FLAT,
            cursor="hand2", padx=12, pady=3,
            command=self._upload)
        self._vp_replace_btn.pack(side=tk.LEFT, padx=(0, 6))

        # NR Preview Mode toggle (hidden by default, shown for NR)
        self._vp_nr_toggle_frame = tk.Frame(self._vp_tab_inner, bg=DS["panel"])
        tk.Label(self._vp_nr_toggle_frame, text="PREVIEW:",
                 font=_f(FS["meta"], bold=True), fg=DS["text3"],
                 bg=DS["panel"]).pack(side=tk.LEFT, padx=(0, 4))
        self._vp_vhs_btn = tk.Button(self._vp_nr_toggle_frame, text="VHS Tape",
            font=_vcr(9), bg=DS["cyan"], fg=DS["text_inv"],
            relief=tk.FLAT, cursor="hand2", padx=8, pady=2,
            command=lambda: self._set_nr_view_mode("VHS"))
        self._vp_vhs_btn.pack(side=tk.LEFT, padx=(0, 2))
        self._vp_standee_btn = tk.Button(self._vp_nr_toggle_frame, text="Standee",
            font=_vcr(9), bg=DS["surface"], fg="#AABBCC",
            relief=tk.FLAT, cursor="hand2", padx=8, pady=2,
            command=lambda: self._set_nr_view_mode("Standee"))
        self._vp_standee_btn.pack(side=tk.LEFT)
        # Hidden by default — shown only for NR
        # self._vp_nr_toggle_frame.pack(side=tk.LEFT, padx=(8, 0))

        # Canvas — takes all vertical space
        self.canvas = tk.Canvas(preview_outer,
                                 bg=DS["bg"], bd=0,
                                 highlightthickness=1,
                                 highlightbackground=DS["border"],
                                 cursor="fleur")
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=10, pady=(4, 2))
        def _on_canvas_resize(e):
            # Clear immediately to avoid stale hatch artefacts, then redraw
            self.canvas.delete("all")
            self.root.after(50, self._draw_preview)
        self.canvas.bind("<Configure>", _on_canvas_resize)
        self.canvas.bind("<ButtonPress-1>",   self._drag_start)
        self.canvas.bind("<B1-Motion>",        self._drag_move)
        self.canvas.bind("<ButtonRelease-1>",  self._drag_end)
        # Mouse wheel → viewport zoom (scales entire preview rect, not image)
        # Zoom state for throttling and debounce
        self._zoom_debounce_id = None
        self._zoom_quality = "hq"
        self._zoom_base_photo = None  # PhotoImage at the zoom level when zooming started
        self._zoom_base_vz = 1.0     # viewport zoom when _zoom_base_photo was captured

        def _canvas_wheel(event):
            # Only allow zoom when a movie is selected and has an image
            if not self.selected:
                return
            if self.selected["name"] not in self.replacements and self._raw_img is None:
                return
            if event.delta:
                step = 0.1 if event.delta > 0 else -0.1
            elif event.num == 4:
                step = 0.1
            elif event.num == 5:
                step = -0.1
            else:
                return

            old_vz  = self._viewport_zoom
            new_vz  = max(0.25, min(4.0, round(old_vz + step, 2)))
            if new_vz == old_vz:
                return

            # Zoom toward mouse position
            cw_ = self.canvas.winfo_width()
            ch_ = self.canvas.winfo_height()
            if cw_ < 10 or ch_ < 10:
                self._viewport_zoom = new_vz
                self._render_preview()
                return
            base_dh = min(ch_ - 10, (cw_ - 10) * 2)
            base_dw = base_dh // 2
            if base_dw <= 0:
                return

            mx = event.x - cw_ // 2
            my = event.y - ch_ // 2
            px = self._viewport_pan_x
            py = self._viewport_pan_y
            old_dw = base_dw * old_vz
            old_dh = base_dh * old_vz
            rel_x = (mx - px) / old_dw if old_dw else 0
            rel_y = (my - py) / old_dh if old_dh else 0
            new_dw = base_dw * new_vz
            new_dh = base_dh * new_vz
            self._viewport_pan_x = int(mx - rel_x * new_dw)
            self._viewport_pan_y = int(my - rel_y * new_dh)
            self._viewport_zoom = new_vz
            if new_vz == 1.0:
                self._viewport_pan_x = 0
                self._viewport_pan_y = 0

            # Fast zoom: capture current preview photo on first scroll,
            # then just use PIL to resize that cached image to the new zoom.
            # This avoids the full render pipeline during active scrolling.
            if self._zoom_base_photo is None and self.preview_photo is not None:
                # Capture the current rendered image as our zoom base
                try:
                    # Convert PhotoImage back to PIL for fast rescaling
                    w = self.preview_photo.width()
                    h = self.preview_photo.height()
                    self._zoom_base_pil = Image.new('RGB', (w, h))
                    # PhotoImage doesn't have a direct to-PIL method, so
                    # instead just use the current full composite if available
                    if getattr(self, '_full_comp', None) is not None:
                        self._zoom_base_pil = self._full_comp.copy()
                        self._zoom_base_vz = old_vz
                        self._zoom_base_photo = True  # flag that we have a base
                except Exception:
                    pass

            if self._zoom_base_photo and getattr(self, '_zoom_base_pil', None) is not None:
                # Scale the cached base to approximate the new zoom level
                ratio = new_vz / self._zoom_base_vz
                base_pil = self._zoom_base_pil
                new_w = max(1, int(base_pil.width * ratio))
                new_h = max(1, int(base_pil.height * ratio))
                # Cap size to prevent memory issues
                if new_w > 4000 or new_h > 8000:
                    cap = min(4000 / new_w, 8000 / new_h)
                    new_w = int(new_w * cap)
                    new_h = int(new_h * cap)
                # NEAREST is instant regardless of size
                zoomed = base_pil.resize((new_w, new_h), Image.NEAREST)
                # Crop to visible area
                new_dx = (cw_ - new_w) // 2 + self._viewport_pan_x
                new_dy = (ch_ - new_h) // 2 + self._viewport_pan_y
                vis_x = max(0, -new_dx)
                vis_y = max(0, -new_dy)
                vis_w = min(new_w - vis_x, cw_ - max(0, new_dx))
                vis_h = min(new_h - vis_y, ch_ - max(0, new_dy))
                if vis_w > 0 and vis_h > 0:
                    cropped = zoomed.crop((vis_x, vis_y, vis_x + vis_w, vis_y + vis_h))
                    self._zoom_fast_photo = ImageTk.PhotoImage(cropped)
                    self.canvas.delete("all")
                    self.canvas.create_image(max(0, new_dx), max(0, new_dy),
                                             anchor=tk.NW, image=self._zoom_fast_photo)

            # Debounce: after 150ms of no zoom, render at full quality
            if self._zoom_debounce_id is not None:
                self.root.after_cancel(self._zoom_debounce_id)
            self._zoom_debounce_id = self.root.after(150, self._zoom_settle)
        self.canvas.bind("<MouseWheel>", _canvas_wheel)
        self.canvas.bind("<Button-4>",   _canvas_wheel)
        self.canvas.bind("<Button-5>",   _canvas_wheel)
        # Use bind_all while mouse is over canvas — overrides any shelf bind_all
        def _canvas_enter(e):
            self.root.bind_all("<MouseWheel>", _canvas_wheel)
            self.root.bind_all("<Button-4>",   _canvas_wheel)
            self.root.bind_all("<Button-5>",   _canvas_wheel)
        def _canvas_leave(e):
            self.root.unbind_all("<MouseWheel>")
            self.root.unbind_all("<Button-4>")
            self.root.unbind_all("<Button-5>")
        self.canvas.bind("<Enter>", _canvas_enter)
        self.canvas.bind("<Leave>", _canvas_leave)
        # Middle mouse button → pan viewport
        self.canvas.bind("<ButtonPress-2>",   self._pan_start)
        self.canvas.bind("<B2-Motion>",        self._pan_move)
        self.canvas.bind("<ButtonRelease-2>",  self._pan_end)
        self._drag_start_x = 0; self._drag_start_y = 0
        self._drag_orig_x  = 0; self._drag_orig_y  = 0
        self._dragging = False
        self._auto_fit = False  # True when fit-to-canvas is active; auto-refits on layout change
        # Overlay label state (fades after 3s)
        self._overlay_label_job = None
        # Drag performance cache
        self._drag_photo   = None
        self._drag_dw      = 0
        self._drag_dh      = 0
        self._drag_dx      = 0
        self._drag_dy      = 0
        # Viewport zoom + pan (mouse wheel = zoom, middle-drag = pan)
        self._viewport_zoom  = 1.0
        self._viewport_pan_x = 0    # canvas pixel offset
        self._viewport_pan_y = 0
        self._nr_view_mode = tk.StringVar(value="VHS")  # "VHS" or "Standee"
        self._pan_dragging   = False
        self._pan_start_x    = 0
        self._pan_start_y    = 0
        self._pan_orig_x     = 0
        self._pan_orig_y     = 0

        # Info row — dimmer, belongs to the canvas not the controls
        self._info_var = tk.StringVar(value="Select a movie to get started")
        self._info_row = info_row = tk.Frame(preview_outer, bg=DS["panel"])
        info_row.grid(row=2, column=0, sticky="ew", padx=10, pady=(2, 0))
        tk.Label(info_row, textvariable=self._info_var,
                 font=_f(FS["meta"]), fg=DS["text3"],
                 bg=DS["panel"], anchor=tk.W).pack(side=tk.LEFT)

        # Thin divider separates canvas info from controls
        self._info_divider = tk.Frame(preview_outer, bg=DS["divider"], height=1)
        self._info_divider.grid(row=3, column=0, sticky="ew", padx=10, pady=(4, 0))

        # Controls: two rows for clarity at all resolutions
        self._ctrl_f = ctrl_f = tk.Frame(preview_outer, bg=DS["panel"])
        ctrl_f.grid(row=4, column=0, sticky="ew", padx=10, pady=(4, 4))

        # Row 1: zoom slider (full width)
        zoom_row = tk.Frame(ctrl_f, bg=DS["panel"])
        zoom_row.pack(fill=tk.X)

        tk.Button(zoom_row, text="−",
                  font=_f(FS["meta"], bold=True),
                  fg=DS["text"], bg=DS["border"], relief=tk.FLAT,
                  cursor="hand2", padx=6, pady=0, height=1,
                  command=lambda: self._zoom_step(-0.05)
                  ).pack(side=tk.LEFT, padx=(0, 2))

        slider_f = tk.Frame(zoom_row, bg=DS["panel"])
        slider_f.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=0)
        self.zoom_var = tk.DoubleVar(value=1.0)
        self.zoom_slider = ttk.Scale(slider_f, from_=0.5, to=3.0,
                                      variable=self.zoom_var,
                                      orient=tk.HORIZONTAL,
                                      command=self._on_zoom)
        self.zoom_slider.pack(fill=tk.X, expand=True, pady=2)

        tk.Button(zoom_row, text="+",
                  font=_f(FS["meta"], bold=True),
                  fg=DS["text"], bg=DS["border"], relief=tk.FLAT,
                  cursor="hand2", padx=6, pady=0, height=1,
                  command=lambda: self._zoom_step(+0.05)
                  ).pack(side=tk.LEFT, padx=(2, 4))

        self.zoom_label = tk.Label(zoom_row, text="1.0x",
                                    font=_f(FS["meta"]), fg=DS["text"],
                                    bg=DS["panel"], width=5, anchor=tk.CENTER)
        self.zoom_label.pack(side=tk.LEFT)

        # Row 2: action buttons (equal width, shrink together)
        action_row = tk.Frame(ctrl_f, bg=DS["panel"])
        action_row.pack(fill=tk.X, pady=(2, 0))
        action_row.columnconfigure(0, weight=1, uniform="action")
        action_row.columnconfigure(1, weight=1, uniform="action")
        action_row.columnconfigure(2, weight=1, uniform="action")

        def _action_btn(parent, text, cmd):
            b = tk.Button(parent, text=text, command=cmd,
                          bg=DS["border"], fg=DS["text2"], relief=tk.FLAT,
                          font=_f(FS["meta"]), cursor="hand2",
                          padx=2, pady=2)
            return b

        self._btn_rotate = _action_btn(action_row, "↻ Rotate", self._rotate_image)
        self._btn_rotate.grid(row=0, column=0, sticky="ew", padx=(0, 2))
        self._btn_fill_canvas = _action_btn(action_row, "⬜ Fill Canvas", self._fit_full_canvas)
        self._btn_fill_canvas.grid(row=0, column=1, sticky="ew", padx=2)
        self._btn_fit_canvas = _action_btn(action_row, "⛶ Fit Visible", self._fit_to_canvas)
        self._btn_fit_canvas.grid(row=0, column=2, sticky="ew", padx=(2, 0))

        # Tooltips for action buttons
        self._attach_layout_tooltip(self._btn_fit_canvas,
            "Scale image to fill the visible tape area")
        self._attach_layout_tooltip(self._btn_fill_canvas,
            "Scale image to fill the entire canvas (1024×2048)")
        self._attach_layout_tooltip(self._btn_rotate,
            "Rotate image 90° clockwise")

        self._upload_btn_frame = None  # compat

        # ── Fixed layout overlay ────────────────────────────────
        # Regular movies use one fixed layout. The toggle is preview-only.

        lay_outer = tk.Frame(preview_outer, bg=DS["panel"])
        lay_outer.grid(row=5, column=0, sticky="ew", padx=10, pady=(0, 6))
        self._layout_section = lay_outer  # ref for show/hide in NR mode

        # NR toggle moved to viewport tab bar
        self._nr_view_toggle_frame = self._vp_nr_toggle_frame  # compat ref
# Row A — overlay toggle
        overlay_row = tk.Frame(lay_outer, bg=DS["panel"])
        overlay_row.pack(fill=tk.X, pady=(0, 4))
        tk.Label(overlay_row, text="LAYOUT OVERLAY",
                 font=_f(FS["meta"], bold=True), fg=DS["text3"],
                 bg=DS["panel"]).pack(side=tk.LEFT)

        self._layout_overlay_var = tk.BooleanVar(value=False)
        def _toggle_overlay():
            on = self._layout_overlay_var.get()
            if on and self._layout_preview.get() == 0:
                saved = self._get_saved_layout()
                self._layout_preview.set(max(1, saved))
            elif not on:
                self._layout_preview.set(0)
            _draw_overlay_toggle(on)   # redraw pill immediately
            self._render_preview()
            self._redraw_layout_cards()

        # Pill-style toggle
        TOGGLE_W, TOGGLE_H = 34, 16
        self._overlay_toggle_canvas = tk.Canvas(overlay_row,
            width=TOGGLE_W, height=TOGGLE_H,
            bg=DS["panel"], bd=0, highlightthickness=0, cursor="hand2")
        self._overlay_toggle_canvas.pack(side=tk.LEFT, padx=SP[2])
        self._overlay_toggle_canvas.bind("<Button-1>",
            lambda e: (self._layout_overlay_var.set(
                           not self._layout_overlay_var.get()),
                       _toggle_overlay()))

        def _draw_overlay_toggle(on=None):
            if on is None:
                on = self._layout_overlay_var.get()
            c = self._overlay_toggle_canvas
            c.delete("all")
            # Track: slightly lighter when on, border-color when off — no cyan
            track = DS["text3"] if on else DS["border"]
            c.create_rectangle(0, 2, TOGGLE_W, TOGGLE_H-2,
                fill=track, outline=track)
            # Thumb: white when on, dim when off
            tx = TOGGLE_W - TOGGLE_H + 2 if on else 2
            thumb_col = DS["text"] if on else DS["text3"]
            c.create_rectangle(tx, 1, tx+TOGGLE_H-2, TOGGLE_H-1,
                fill=thumb_col, outline="")
        self._draw_overlay_toggle = _draw_overlay_toggle
        _draw_overlay_toggle(False)

        tk.Label(overlay_row, text="Show on canvas",
                 font=_f(FS["meta"]), fg=DS["text3"],
                 bg=DS["panel"]).pack(side=tk.LEFT)

        # Fixed-layout info. The old five-card selector and random dice are gone because
        # all regular movie layouts now resolve to the same in-game frame.
        info_row2 = tk.Frame(lay_outer, bg=DS["panel"])
        info_row2.pack(fill=tk.X, pady=(2, 0))
        tk.Label(info_row2, text="FIXED REGULAR LAYOUT",
                 font=_f(FS["meta"], bold=True), fg=DS["text3"],
                 bg=DS["panel"]).pack(anchor=tk.W)
        tk.Label(info_row2,
                 text=f"Visible art window: {SAFE_W} × {SAFE_H} px  (x {HIDDEN_LEFT}–{TEX_WIDTH-HIDDEN_RIGHT}, y {HIDDEN_TOP}–{TEX_HEIGHT-HIDDEN_BOTTOM})",
                 font=_f(FS["meta"]), fg=DS["text3"],
                 bg=DS["panel"]).pack(anchor=tk.W, pady=(2, 0))

        self._layout_card_frames = {}
        self._layout_thumb_photos = {}
        self._layout_random_label = None
        self._layout_random_label_job = None

        # Layout change notification label (shown beneath canvas)
        self._layout_notify_label = None  # created lazily near canvas
        self._layout_notify_job = None

        self._layout_btns = {}  # kept for compat with _update_layout_btn_colors

        # Snap state
        self._snap_guide_job  = None
        self._snap_enabled    = True   # toggled by controls HUD checkbox
        self._hatch_tile_cache = None  # reset on init so opacity changes take effect
        # HUD minimized state
        self._hud_minimized   = False

        # ── RIGHT: DETAILS PANEL ──────────────────────────────
        details = tk.Frame(main, bg=C["card"],
                            highlightthickness=1,
                            highlightbackground=C["border"])
        details.grid(row=0, column=2, sticky="nsew", padx=(4,8), pady=8)
        self._details_panel = details

        # Scrollable right panel
        self._det_canvas = det_canvas = tk.Canvas(details, bg=C["card"], bd=0,
                                highlightthickness=0)
        det_vsb    = tk.Scrollbar(details, orient=tk.VERTICAL,
                                   command=det_canvas.yview)
        det_canvas.configure(yscrollcommand=det_vsb.set)
        det_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        det_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._det_inner = det_inner = tk.Frame(det_canvas, bg=C["card"])
        self._det_inner_window = det_canvas.create_window((0,0), window=det_inner, anchor=tk.NW)
        det_inner.bind("<Configure>",
            lambda e: det_canvas.configure(
                scrollregion=det_canvas.bbox("all")))
        def _det_on_configure(e):
            try:
                det_canvas.itemconfig(self._det_inner_window, width=e.width)
                det_canvas.itemconfig(self._det_empty_window, width=e.width)
                det_canvas.itemconfig(self._det_onboard_window, width=e.width)
            except Exception:
                pass
        det_canvas.bind("<Configure>", _det_on_configure)

        # Mousewheel scrolling for right panel
        def _det_scroll(event):
            if event.delta:
                det_canvas.yview_scroll(int(-event.delta / 120), "units")
            elif event.num == 4:
                det_canvas.yview_scroll(-3, "units")
            elif event.num == 5:
                det_canvas.yview_scroll(3, "units")
        def _det_enter(e):
            self.root.bind_all("<MouseWheel>", _det_scroll)
            self.root.bind_all("<Button-4>", _det_scroll)
            self.root.bind_all("<Button-5>", _det_scroll)
        def _det_leave(e):
            self.root.unbind_all("<MouseWheel>")
            self.root.unbind_all("<Button-4>")
            self.root.unbind_all("<Button-5>")
        det_canvas.bind("<Enter>", _det_enter)
        det_canvas.bind("<Leave>", _det_leave)
        det_inner.bind("<Enter>", _det_enter)
        det_inner.bind("<Leave>", _det_leave)

        # Empty state overlay (shown when no movie selected)
        self._det_empty_frame = tk.Frame(det_canvas, bg=C["card"])
        self._det_empty_window = det_canvas.create_window(
            (0, 0), window=self._det_empty_frame, anchor=tk.NW)
        tk.Label(self._det_empty_frame, text="",
                 bg=C["card"]).pack(pady=30)
        tk.Label(self._det_empty_frame, text="No movie selected",
                 font=_vcr(11), fg=C["text_dim"], bg=C["card"]
                 ).pack(padx=20)

        # Getting started guide (shown when no movies exist)
        self._det_onboard_frame = tk.Frame(det_canvas, bg=C["card"])
        self._det_onboard_window = det_canvas.create_window(
            (0, 0), window=self._det_onboard_frame, anchor=tk.NW)
        tk.Label(self._det_onboard_frame, text="",
                 bg=C["card"]).pack(pady=20)
        tk.Label(self._det_onboard_frame, text="GET STARTED",
                 font=_vcr(11, bold=True), fg=C["cyan"], bg=C["card"]
                 ).pack(padx=20, anchor=tk.W)
        tk.Label(self._det_onboard_frame, text="",
                 bg=C["card"]).pack(pady=4)
        for step, title, desc in [
            ("①", "Add a movie",
             'Click "+ Add movie to [Genre]"\nin the left panel'),
            ("②", "Set up your movie",
             "Add a title, rating and cover image"),
            ("③", "Ship to store",
             "Build your mod and install it\nto the game"),
        ]:
            sf = tk.Frame(self._det_onboard_frame, bg=C["card"])
            sf.pack(fill=tk.X, padx=20, pady=(0, 10))
            tk.Label(sf, text=step, font=_vcr(14),
                     fg=C["cyan"], bg=C["card"]).pack(side=tk.LEFT, padx=(0, 10))
            tf = tk.Frame(sf, bg=C["card"])
            tf.pack(side=tk.LEFT, fill=tk.X)
            tk.Label(tf, text=title, font=_vcr(10, bold=True),
                     fg=C["text"], bg=C["card"], anchor=tk.W).pack(fill=tk.X)
            tk.Label(tf, text=desc, font=_vcr(8),
                     fg=C["text_dim"], bg=C["card"], anchor=tk.W,
                     justify=tk.LEFT).pack(fill=tk.X)

        # Initially hide both overlays — managed by _update_viewport_state
        det_canvas.itemconfig(self._det_empty_window, state="hidden")
        det_canvas.itemconfig(self._det_onboard_window, state="hidden")

        def det_sec(text):
            tk.Label(det_inner, text=text,
                     font=_vcr(10, bold=True), fg=CYAN,
                     bg=C["card"], anchor=tk.W, padx=14, pady=4
                     ).pack(fill=tk.X, pady=(6, 0))

        def det_sep():
            ttk.Separator(det_inner).pack(fill=tk.X, padx=14, pady=4)

        # ─────────────────────────────────────────────────────────
        # MOVIE TITLE — most prominent field
        # ─────────────────────────────────────────────────────────
        tk.Label(det_inner, text="MOVIE TITLE",
                 font=_vcr(11, bold=True), fg=CYAN,
                 bg=C["card"], anchor=tk.W, padx=14
                 ).pack(fill=tk.X, pady=(8,0))
        self._inline_title_entry = tk.Entry(
            det_inner, textvariable=self._inline_title_var,
            bg=C["input_bg"], fg=C["text_hi"],
            insertbackground=C["cyan"],
            font=_vcr(13), relief=tk.FLAT)
        self._inline_title_entry.pack(fill=tk.X, padx=14, ipady=7, pady=(2,0))
        tk.Label(det_inner, text=f"Max {MOVIE_TITLE_MAX_BYTES} UTF-8 bytes",
                 font=_vcr(8), fg=C["text_dim"], bg=C["card"],
                 anchor=tk.W, padx=14).pack(fill=tk.X)
        det_sep()

        # ── STAR RATING ──────────────────────────────────────────
        self._star_rating_frame = tk.Frame(det_inner, bg=C["card"])
        self._star_rating_frame.pack(fill=tk.X)
        _star_hdr = tk.Frame(self._star_rating_frame, bg=C["card"])
        _star_hdr.pack(fill=tk.X, pady=(4, 2))
        tk.Label(_star_hdr, text="STAR RATING",
                 font=_vcr(9, bold=True), fg=C["text_dim"],
                 bg=C["card"], anchor=tk.W, padx=14).pack(side=tk.LEFT)
        # Randomize dice button — right-aligned, dimmed
        self._star_dice = tk.Label(_star_hdr, text="⚄",
                 font=_f(14), fg="#4A7A7A", bg=C["card"],
                 cursor="hand2", padx=10)
        self._star_dice.pack(side=tk.RIGHT)
        self._star_dice.bind("<Button-1>", lambda e: self._randomize_rating())
        self._star_dice.bind("<Enter>", lambda e: self._star_dice.config(fg=DS["cyan"]))
        self._star_dice.bind("<Leave>", lambda e: self._star_dice.config(fg="#4A7A7A"))
        # NR info label (hidden by default, shown when in NR mode)
        # NR info label removed — RATING/RARITY sections in NR controls explain this
        self._nr_rating_info = tk.Label(det_inner, text="", bg=C["card"])
        # Hidden by default — never shown

        # ── Pre-render star images via PIL ────────────────────────
        # Each star is 44×44px. Three variants:
        #   FULL  — entire star in SELECTED color (#F5A623)
        #   HALF  — left half SELECTED, right half EMPTY (#444444)
        #   EMPTY — entire star in EMPTY color (#444444)
        #   FULL_P / HALF_P — same but PREVIEW color (#FFD700)
        # Also a ☆ outline-star image for the zero button.
        _SW, _SH_img = 44, 44          # star cell size
        _ZW          = 44              # zero-star cell (same width for symmetry)
        _NS          = 5
        _C_S  = "#F5A623"   # SELECTED
        _C_P  = "#FFD700"   # PREVIEW
        _C_E  = "#444444"   # EMPTY
        _BG   = C["card"]   # background

        def _make_star_img(left_col, right_col, outline_only=False):
            """Render a 44×44 star PhotoImage. left/right_col are hex strings."""
            from PIL import Image as _I, ImageDraw as _ID, ImageFont as _IF
            img = _I.new("RGBA", (_SW, _SH_img), (0,0,0,0))
            d   = _ID.Draw(img)
            # Star polygon points (normalized to 0..1, then scaled)
            import math
            pts = []
            for k in range(10):
                angle = math.radians(-90 + k * 36)
                r = _SW*0.45 if k%2==0 else _SW*0.18
                pts.append((_SW/2 + r*math.cos(angle),
                             _SH_img/2 + r*math.sin(angle)))
            if outline_only:
                d.polygon(pts, outline=left_col, fill=(0,0,0,0))
            else:
                # Draw full star then mask right half with right_col
                def _hex_to_rgb(h):
                    h = h.lstrip("#")
                    return tuple(int(h[i:i+2],16) for i in (0,2,4)) + (255,)
                lc = _hex_to_rgb(left_col)
                rc = _hex_to_rgb(right_col)
                # Left half: clip x < SW//2
                # Right half: clip x >= SW//2
                # Draw left-colored full star
                img_l = _I.new("RGBA", (_SW, _SH_img), (0,0,0,0))
                _ID.Draw(img_l).polygon(pts, fill=lc)
                img_r = _I.new("RGBA", (_SW, _SH_img), (0,0,0,0))
                _ID.Draw(img_r).polygon(pts, fill=rc)
                # Combine: left half from img_l, right half from img_r
                combined = _I.new("RGBA", (_SW, _SH_img), (0,0,0,0))
                combined.paste(img_l.crop((0, 0, _SW//2, _SH_img)), (0, 0))
                combined.paste(img_r.crop((_SW//2, 0, _SW, _SH_img)), (_SW//2, 0))
                img = combined
            # Composite onto background
            bg_rgb = tuple(int(_BG.lstrip("#")[i:i+2],16) for i in (0,2,4))
            out = _I.new("RGB", (_SW, _SH_img), bg_rgb)
            out.paste(img, mask=img.split()[3])
            return ImageTk.PhotoImage(out)

        # Build image set once
        _img_full   = _make_star_img(_C_S, _C_S)   # full selected
        _img_half   = _make_star_img(_C_S, _C_E)   # half selected
        _img_empty  = _make_star_img(_C_E, _C_E)   # empty
        _img_fullP  = _make_star_img(_C_P, _C_P)   # full preview
        _img_halfP  = _make_star_img(_C_P, _C_E)   # half preview
        _img_zero_s = _make_star_img(_C_S, _C_E, outline_only=True)   # ☆ selected
        _img_zero_p = _make_star_img(_C_P, _C_E, outline_only=True)   # ☆ preview
        _img_zero_e = _make_star_img(_C_E, _C_E, outline_only=True)   # ☆ empty
        # Keep refs so GC doesn't collect
        self._star_imgs = [_img_full,_img_half,_img_empty,
                           _img_fullP,_img_halfP,
                           _img_zero_s,_img_zero_p,_img_zero_e]

        _CW = _ZW + _NS * _SW + 4
        star_cv = tk.Canvas(self._star_rating_frame, width=_CW, height=_SH_img,
                            bg=_BG, bd=0, highlightthickness=0, cursor="hand2")
        star_cv.pack(anchor=tk.W, padx=14, pady=(0,2))
        self._star_canvas = star_cv
        self._star_saved  = 4.5
        self._star_btns   = []   # compat stub
        self._star_pulse_job = None

        def _redraw(hover=None):
            star_cv.delete("all")
            sv = self._star_saved
            ih = hover is not None

            # Zero star
            if ih and hover == 0.0:
                zimg = _img_zero_p
            elif not ih and sv == 0.0:
                zimg = _img_zero_s
            else:
                zimg = _img_zero_e
            star_cv.create_image(0, 0, anchor=tk.NW, image=zimg)

            # 5 stars
            for i in range(_NS):
                x    = _ZW + i * _SW
                vf   = float(i+1)
                vh   = i + 0.5
                if ih and hover > 0.0:
                    img = _img_fullP if vf<=hover else                           _img_halfP if vh<=hover else _img_empty
                elif not ih:
                    img = _img_full if sv>=vf else                           _img_half if sv>=vh else _img_empty
                else:   # hover==0.0
                    img = _img_empty
                star_cv.create_image(x, 0, anchor=tk.NW, image=img)

        self._draw_stars = _redraw

        def _x_to_val(x):
            ix = int(x)
            if ix < _ZW:
                return 0.0
            rx  = ix - _ZW
            idx = min(rx // _SW, _NS-1)
            frc = rx % _SW
            val = idx + (0.5 if frc < _SW//2 else 1.0)
            return round(min(max(val, 0.5), 5.0) * 2) / 2

        def _do_save(val):
            self._set_stars_half(val)

        def _pulse(val, step=0):
            if step > 8:
                self._star_saved = val
                _redraw()
                return
            import math
            t = math.sin(step / 8.0 * math.pi)
            star_cv.delete("all")
            if val == 0.0:
                star_cv.create_image(0, 0, anchor=tk.NW,
                    image=_img_zero_p if t > 0.3 else _img_zero_s)
            else:
                star_cv.create_image(0, 0, anchor=tk.NW, image=_img_zero_e)
            for i in range(_NS):
                x  = _ZW + i*_SW; vf=float(i+1); vh=i+0.5
                if val >= vf:
                    img = _img_fullP if t>0.3 else _img_full
                elif val >= vh:
                    img = _img_halfP if t>0.3 else _img_half
                else:
                    img = _img_empty
                star_cv.create_image(x, 0, anchor=tk.NW, image=img)
            self._star_pulse_job = self.root.after(25, lambda: _pulse(val, step+1))
        self._pulse_stars = _pulse

        star_cv.bind("<Button-1>", lambda e: _do_save(_x_to_val(e.x)))
        star_cv.bind("<Motion>",   lambda e: (
            _redraw(_x_to_val(e.x)),
            self._draw_critic_badge_preview(_x_to_val(e.x))
                if hasattr(self,"_draw_critic_badge_preview") else None
        ))
        star_cv.bind("<Leave>",    lambda e: (
            _redraw(),
            self._draw_critic_badge(self._get_current_critic())
                if hasattr(self,"_draw_critic_badge") and
                   hasattr(self,"_get_current_critic") else None
        ))

        self.root.after(30, _redraw)


        # Star info line removed — critic badge and rarity section show this info
        self._star_label = tk.Label(self._star_rating_frame, text="",
                                     font=_vcr(9), fg=C["text_dim"],
                                     bg=C["card"], anchor=tk.W, padx=14)
        # Not packed — hidden by design
        self._star_note = tk.Label(self._star_rating_frame, text="",
                                    font=_vcr(8), fg=DS["gold"],
                                    bg=C["card"], anchor=tk.W, padx=14)
        self._star_note.pack(fill=tk.X)

        # Critic badge
        self._critic_badge = tk.Canvas(self._star_rating_frame, height=24,
                                        bg=C["card"], bd=0, highlightthickness=0)
        self._critic_badge.pack(fill=tk.X, padx=14, pady=(2,4))
        # Star/critic mapping mirrors the game's SKU % 100 score thresholds.

        def _draw_critic_badge(critic, preview=False):
            c = self._critic_badge
            c.delete("all")
            if not critic:
                return
            is_good = "Good" in str(critic)
            bg  = ("#004A10" if preview else "#00C020") if is_good else ("#4A0808" if preview else "#CC2010")
            lbl = "GOOD CRITIC" if is_good else "BAD CRITIC"
            fg  = "#888888" if preview else "white"
            c.create_rectangle(0, 2, 112, 22, fill=bg, outline=fg, width=1)
            c.create_text(56, 12, text=lbl,
                font=_vcr(8, bold=True), fill=fg, anchor=tk.CENTER)
        self._draw_critic_badge = _draw_critic_badge

        def _draw_critic_badge_preview(hover_val):
            l2 = stars_to_last2(hover_val)
            critic = critic_badge_key(critic_for_last2(l2))
            # If hovering over the saved value, show the real badge (not preview)
            is_saved = (hover_val == self._star_saved)
            _draw_critic_badge(critic, preview=not is_saved)
        self._draw_critic_badge_preview = _draw_critic_badge_preview

        def _get_current_critic():
            l2 = self._get_current_last2()
            return critic_badge_key(critic_for_last2(l2))
        self._get_current_critic = _get_current_critic

        # Collect all star-rating widgets for NR-mode hiding
        self._star_sep = ttk.Separator(self._star_rating_frame)
        self._star_sep.pack(fill=tk.X, padx=14, pady=4)

        # ── RARITY ───────────────────────────────────────────────
        # Container holds either genre controls or NR controls
        self._mode_container = tk.Frame(det_inner, bg=C["card"])
        self._mode_container.pack(fill=tk.X)
        self._genre_controls_frame = tk.Frame(self._mode_container, bg=C["card"])
        self._genre_controls_frame.pack(fill=tk.X)
        _gcf = self._genre_controls_frame
        tk.Label(_gcf, text="RARITY",
                 font=_vcr(9, bold=True), fg=C["text_dim"],
                 bg=C["card"], anchor=tk.W, padx=14).pack(fill=tk.X, pady=(4,0))

        self._rarity_var   = tk.StringVar(value="Common")
        self._old_tape_var = tk.BooleanVar(value=False)

        # ── Two equal-width canvas buttons side by side ──────────
        BTN_H = 34
        BTN_W = 110   # fixed width — both same size, no grid expansion issues

        rar_row = tk.Frame(_gcf, bg=C["card"])
        rar_row.pack(anchor=tk.W, padx=14, pady=(6,0))

        # Common — also canvas so both buttons look identical in structure
        self._rar_com_cv = tk.Canvas(rar_row, width=BTN_W, height=BTN_H,
            bg=C["card"], bd=0, highlightthickness=0, cursor="hand2")
        self._rar_com_cv.pack(side=tk.LEFT, padx=(0,4))
        self._rar_com_cv.bind("<Button-1>", lambda e: _set_rar("Common"))
        self._rar_com_cv.bind("<Configure>",
            lambda e: _draw_com_btn(self._rarity_var.get() in ("Common","Common (Old)")))

        # Keep old tk.Button ref as stub for compat
        self._rar_btn_common = tk.Button(rar_row, state=tk.DISABLED)
        self._rar_btn_common.pack_forget()

        # Limited — canvas for rainbow border
        self._rar_lim_cv = tk.Canvas(rar_row, width=BTN_W, height=BTN_H,
            bg=C["card"], bd=0, highlightthickness=0, cursor="hand2")
        self._rar_lim_cv.pack(side=tk.LEFT)
        self._rar_lim_cv.bind("<Button-1>",
            lambda e: _set_rar("Limited Edition (holo)"))

        def _draw_com_btn(active=False):
            c = self._rar_com_cv
            c.delete("all")
            w = BTN_W; h = BTN_H
            bg  = DS["cyan"]    if active else DS["surface"]
            fg  = DS["text_inv"] if active else DS["text3"]
            bdr = DS["cyan"]    if active else DS["border"]
            c.create_rectangle(0, 0, w-1, h-1, fill=bg, outline=bdr, width=1)
            c.create_text(w//2, h//2, text="Common",
                font=_vcr(10), fill=fg, anchor=tk.CENTER)

        def _draw_lim_btn(active=False):
            c = self._rar_lim_cv
            c.delete("all")
            w = BTN_W
            h = BTN_H
            if active:
                bg_col  = "#140820"
                txt_col = "#E0C0FF"
                # Rainbow border: draw as 1px lines on each edge
                # Use PIL to create a smooth gradient border image
                from PIL import Image as _PI, ImageDraw as _PD
                rim = 2  # border thickness px
                # Build gradient colours around the perimeter
                rainbow = ["#FF3030","#FF8000","#FFD700","#40DD40","#2090FF","#9040FF"]
                # Draw background rect
                c.create_rectangle(0, 0, w, h, fill=bg_col, outline="")
                # Draw rainbow border as coloured line segments on each side
                # Top edge
                seg = w // len(rainbow)
                for ki, col in enumerate(rainbow):
                    x0 = ki * seg; x1 = min((ki+1)*seg, w)
                    c.create_line(x0, 0, x1, 0, fill=col, width=rim)
                    c.create_line(x0, h-1, x1, h-1, fill=col, width=rim)
                # Left + right edges
                seg_h = h // len(rainbow)
                for ki, col in enumerate(rainbow):
                    y0 = ki*seg_h; y1 = min((ki+1)*seg_h, h)
                    c.create_line(0, y0, 0, y1, fill=col, width=rim)
                    c.create_line(w-1, y0, w-1, y1, fill=col, width=rim)
            else:
                bg_col  = DS["surface"]
                txt_col = DS["text3"]
                c.create_rectangle(0, 0, w-1, h-1,
                    fill=bg_col, outline=DS["border"], width=1)
            c.create_text(w//2, h//2, text="✦  Limited",
                font=_vcr(10, bold=True), fill=txt_col, anchor=tk.CENTER)
        self._draw_lim_btn = _draw_lim_btn
        self.root.after(30, lambda: (_draw_com_btn(True), _draw_lim_btn(False)))

        # ── Old tape toggle ───────────────────────────────────────
        old_f = tk.Frame(_gcf, bg=C["card"])
        old_f.pack(fill=tk.X, padx=14, pady=(8,0))

        self._old_cb = tk.Checkbutton(old_f,
            text=" Old tape",
            variable=self._old_tape_var,
            font=_vcr(9), fg=DS["text2"], bg=C["card"],
            selectcolor=DS["surface"],
            activebackground=C["card"],
            relief=tk.FLAT, cursor="hand2",
            command=lambda: _on_old_toggle())
        self._old_cb.pack(side=tk.LEFT)

        # Info line — only when Limited
        self._rar_note = tk.Label(_gcf, text="",
            font=_vcr(8), fg=DS["text3"], bg=C["card"],
            anchor=tk.W, padx=14)
        self._rar_note.pack(fill=tk.X, pady=(2,0))

        # Compat stub
        self._rar_btns = {
            "Common": self._rar_btn_common,
            "Limited Edition (holo)": self._rar_lim_cv,
        }

        def _on_old_toggle():
            cur = self._rarity_var.get()
            if cur == "Limited Edition (holo)":
                self._old_tape_var.set(True)
                return
            self._rarity_var.set(
                "Common (Old)" if self._old_tape_var.get() else "Common")
            self._on_rarity_change()

        def _set_rar(v):
            self._rarity_var.set(v)
            is_lim = (v == "Limited Edition (holo)")
            if is_lim:
                self._old_tape_var.set(True)
                self._old_cb.config(state=tk.DISABLED, fg=DS["text3"])
                self._rar_note.config(text="Included with Limited.")
            else:
                self._old_cb.config(state=tk.NORMAL, fg=DS["text2"])
                self._rar_note.config(text="")
            _update_rarity_ui()
            self._on_rarity_change()

        def _update_rarity_ui():
            cur    = self._rarity_var.get()
            is_lim = (cur == "Limited Edition (holo)")
            is_com = not is_lim
            _draw_com_btn(is_com)
            _draw_lim_btn(is_lim)

        self._update_rarity_buttons = _update_rarity_ui
        _update_rarity_ui()

        det_sep()

        # ── CATALOG ID ───────────────────────────────────────────
        self._catalog_id_var = tk.StringVar(value="")
        cat_f = tk.Frame(_gcf, bg=C["card"])
        cat_f.pack(fill=tk.X, padx=14, pady=(2,6))
        tk.Label(cat_f, text="CATALOG ID",
                 font=_vcr(9, bold=True), fg=C["text_dim"],
                 bg=C["card"]).pack(side=tk.LEFT, padx=(0,6))
        sku_e = tk.Entry(cat_f, textvariable=self._catalog_id_var,
                         font=_vcr(10), fg=C["yellow"],
                         bg=C["card"], relief=tk.FLAT,
                         state="readonly", readonlybackground=C["card"], width=14)
        sku_e.pack(side=tk.LEFT)
        def _copy_sku_inline():
            v = self._catalog_id_var.get()
            if v and v != "—":
                self.root.clipboard_clear()
                self.root.clipboard_append(v)
        tk.Button(cat_f, text="⎘", command=_copy_sku_inline,
                  font=_vcr(10), fg=C["text_dim"], bg=C["card"],
                  relief=tk.FLAT, cursor="hand2").pack(side=tk.LEFT, padx=(2,0))

        # ─────────────────────────────────────────────────────────
        # ── NEW RELEASE CONTROLS (hidden for genre movies) ────
        self._nr_controls_frame = tk.Frame(self._mode_container, bg=C["card"])
        # NOT packed by default — shown only when NR slot selected

        tk.Label(self._nr_controls_frame, text="STANDEE SHAPE",
                 font=_vcr(9, bold=True), fg=C["text_dim"],
                 bg=C["card"], anchor=tk.W, padx=14).pack(fill=tk.X, pady=(4,0))

        self._nr_standee_var = tk.StringVar(value="A")
        standee_row = tk.Frame(self._nr_controls_frame, bg=C["card"])
        standee_row.pack(fill=tk.X, padx=14, pady=(6,0))

        # Load standee preview images (game screenshots)
        _preview_b64 = {"A": _STANDEE_PREVIEW_A_B64,
                        "B": _STANDEE_PREVIEW_B_B64,
                        "C": _STANDEE_PREVIEW_C_B64}
        self._standee_photos = {}
        for shape in NR_STANDEE_SHAPES:
            raw = base64.b64decode(_preview_b64[shape])
            pil_img = Image.open(io.BytesIO(raw)).convert("RGB")
            self._standee_photos[shape] = ImageTk.PhotoImage(pil_img)

        CARD_W, CARD_H = 88, 196
        self._standee_btns = {}
        for shape in NR_STANDEE_SHAPES:
            card = tk.Frame(standee_row, bg=DS["border"],
                            width=CARD_W, height=CARD_H,
                            cursor="hand2", highlightthickness=2,
                            highlightbackground=DS["border"])
            card.pack(side=tk.LEFT, padx=3)
            card.pack_propagate(False)
            img_lbl = tk.Label(card, image=self._standee_photos[shape],
                               bg=C["card"], cursor="hand2")
            img_lbl.pack(fill=tk.BOTH, expand=True, padx=1, pady=(1, 0))
            lbl = tk.Label(card, text=shape, font=_vcr(10, bold=True),
                           fg=DS["text3"], bg=DS["border"], pady=1)
            lbl.pack(fill=tk.X)
            self._standee_btns[shape] = (card, img_lbl, lbl)
            for w in (card, img_lbl, lbl):
                w.bind("<Button-1>", lambda e, s=shape: self._set_standee(s))

        def _update_standee_btns():
            cur = self._nr_standee_var.get()
            for s, (card, img_lbl, lbl) in self._standee_btns.items():
                if s == cur:
                    card.config(highlightbackground=DS["cyan"])
                    lbl.config(fg=DS["cyan"])
                else:
                    card.config(highlightbackground=DS["border"])
                    lbl.config(fg=DS["text3"])
        self._update_standee_btns = _update_standee_btns

        ttk.Separator(self._nr_controls_frame).pack(fill=tk.X, padx=14, pady=6)

        tk.Label(self._nr_controls_frame, text="GENRE",
                 font=_vcr(9, bold=True), fg=C["text_dim"],
                 bg=C["card"], anchor=tk.W, padx=14).pack(fill=tk.X, pady=(0,0))

        self._nr_genre_var = tk.StringVar(value="Action")
        genre_row = tk.Frame(self._nr_controls_frame, bg=C["card"])
        genre_row.pack(fill=tk.X, padx=14, pady=(4,0))

        # Genre selector as styled buttons in a grid
        _nr_genre_btns_frame = tk.Frame(genre_row, bg=C["card"])
        _nr_genre_btns_frame.pack(fill=tk.X)
        self._nr_genre_btns = {}
        _nr_cols = 3
        for gi, gname in enumerate(NR_GENRES):
            btn = tk.Button(_nr_genre_btns_frame, text=gname,
                           font=_vcr(8), bg=DS["surface"], fg=DS["text3"],
                           activebackground=DS["surface"],
                           relief=tk.FLAT, cursor="hand2", pady=2,
                           command=lambda g=gname: self._set_nr_genre(g))
            btn.grid(row=gi // _nr_cols, column=gi % _nr_cols, sticky="ew", padx=1, pady=1)
            self._nr_genre_btns[gname] = btn
        for col in range(_nr_cols):
            _nr_genre_btns_frame.columnconfigure(col, weight=1)

        ttk.Separator(self._nr_controls_frame).pack(fill=tk.X, padx=14, pady=6)

        tk.Label(self._nr_controls_frame, text="RATING",
                 font=_vcr(9, bold=True), fg=C["text_dim"],
                 bg=C["card"], anchor=tk.W, padx=14).pack(fill=tk.X, pady=(0,0))
        tk.Label(self._nr_controls_frame,
                 text="Rating is always 5★ (set by the game).",
                 font=_vcr(8), fg=C["text_dim"], bg=C["card"],
                 anchor=tk.W, padx=14).pack(fill=tk.X, pady=(2,0))

        ttk.Separator(self._nr_controls_frame).pack(fill=tk.X, padx=14, pady=6)

        tk.Label(self._nr_controls_frame, text="RARITY",
                 font=_vcr(9, bold=True), fg=C["text_dim"],
                 bg=C["card"], anchor=tk.W, padx=14).pack(fill=tk.X, pady=(0,0))
        tk.Label(self._nr_controls_frame,
                 text="The game creates two copies: Common and Limited Edition.",
                 font=_vcr(8), fg=C["text_dim"], bg=C["card"],
                 anchor=tk.W, padx=14).pack(fill=tk.X, pady=(2,0))

        ttk.Separator(self._nr_controls_frame).pack(fill=tk.X, padx=14, pady=8)



        # SHIP TO STORE — dominant CTA
        # ─────────────────────────────────────────────────────────
        # Ship to Store — button speaks for itself, no redundant label
        self._ship_desc = tk.Label(det_inner,
                 text="Builds your mod pak and installs\nit to the game's mod folder.",
                 font=_vcr(8), fg=C["text_dim"], bg=C["card"],
                 justify=tk.LEFT, padx=14, wraplength=250)
        self._ship_desc.pack(fill=tk.X, pady=(8, 0))
        SHIP_H = 64
        ship_canvas = tk.Canvas(det_inner, height=SHIP_H, bg=C["card"],
                                 bd=0, highlightthickness=0, cursor="hand2")
        ship_canvas.pack(fill=tk.X, padx=14, pady=(6, 4))
        self._ship_label = "🚀  SHIP TO STORE"

        def _draw_ship_btn(hover=False, label=None):
            ship_canvas.delete("all")
            w = ship_canvas.winfo_width() or 220
            h = SHIP_H
            bc = C["cyan"]
            r_c=int(bc[1:3],16); g_c=int(bc[3:5],16); b_c=int(bc[5:7],16)
            bg_r=int(C["card"][1:3],16); bg_g=int(C["card"][3:5],16); bg_b=int(C["card"][5:7],16)
            def mix(c,b,t): return max(0,min(255,int(c*t+b*(1-t))))
            gf=f"#{mix(r_c,bg_r,.08):02x}{mix(g_c,bg_g,.08):02x}{mix(b_c,bg_b,.08):02x}"
            gm=f"#{mix(r_c,bg_r,.22):02x}{mix(g_c,bg_g,.22):02x}{mix(b_c,bg_b,.22):02x}"
            gn=f"#{mix(r_c,bg_r,.55):02x}{mix(g_c,bg_g,.55):02x}{mix(b_c,bg_b,.55):02x}"
            fb=C["cyan"] if hover else f"#{mix(r_c,bg_r,.18):02x}{mix(g_c,bg_g,.18):02x}{mix(b_c,bg_b,.18):02x}"
            tc=C["bg"] if hover else C["cyan"]
            ship_canvas.create_rectangle(0,0,w,h,fill=gf,outline="")
            ship_canvas.create_rectangle(2,2,w-2,h-2,fill=gm,outline="")
            ship_canvas.create_rectangle(4,4,w-4,h-4,fill=gn,outline="")
            ship_canvas.create_rectangle(6,6,w-6,h-6,fill=fb,outline="")
            ship_canvas.create_rectangle(6,6,w-6,h-6,fill="",outline=bc,width=2)
            tx=w//2; ty=h//2
            lbl=label or self._ship_label
            ship_canvas.create_text(tx+1,ty+1,text=lbl,fill=gm,font=_vcr(13,bold=True),anchor=tk.CENTER)
            ship_canvas.create_text(tx-1,ty-1,text=lbl,fill=gm,font=_vcr(13,bold=True),anchor=tk.CENTER)
            ship_canvas.create_text(tx,ty,text=lbl,fill=tc,font=_vcr(13,bold=True),anchor=tk.CENTER)

        ship_canvas.bind("<Configure>", lambda e: _draw_ship_btn())
        ship_canvas.bind("<Enter>",     lambda e: _draw_ship_btn(hover=True))
        ship_canvas.bind("<Leave>",     lambda e: _draw_ship_btn(hover=False))
        ship_canvas.bind("<Button-1>",  lambda e: self._build())
        self._draw_ship_btn = _draw_ship_btn
        self._ship_canvas = ship_canvas

        # Dev section removed from right panel — moved to top bar dialog
        self._dev_visible = tk.BooleanVar(value=False)
        self._dev_frame = tk.Frame(det_inner, bg=C["card"])  # kept for compat
        self._custom_only_var = tk.BooleanVar(value=CUSTOM_ONLY_MODE)

        # ── Delete This Movie — text-only red, demoted ──────
        self._remove_slot_btn_del = tk.Label(det_inner,
                  text="Delete This Movie",
                  font=_vcr(8), fg="#994444", bg=C["card"],
                  cursor="hand2", anchor=tk.CENTER)
        self._remove_slot_btn_del.pack(fill=tk.X, padx=14, pady=(12, 2))
        self._remove_slot_btn_del.bind("<Button-1>",
            lambda e: self._delete_selected_slot())
        self._remove_slot_btn_del.bind("<Enter>",
            lambda e: e.widget.config(fg="#FF6666"))
        self._remove_slot_btn_del.bind("<Leave>",
            lambda e: e.widget.config(fg="#994444"))
        self._remove_slot_btn_del.pack_forget()
        self._remove_slot_btn = self._remove_slot_btn_del

        # Spacer + divider
        tk.Frame(det_inner, bg=C["card"], height=20).pack(fill=tk.X)
        tk.Frame(det_inner, bg=C["border"], height=1).pack(fill=tk.X, padx=30)
        tk.Frame(det_inner, bg=C["card"], height=12).pack(fill=tk.X)

        # "clear all movies" — well separated from delete
        self._clear_all_label = tk.Label(det_inner, text="clear all movies",
                 font=_vcr(7), fg="#554444", bg=C["card"],
                 cursor="hand2", anchor=tk.CENTER)
        self._clear_all_label.pack(pady=(0, 12))
        self._clear_all_label.bind("<Button-1>",
            lambda e: self._clear_all_custom_confirm())
        self._clear_all_label.bind("<Enter>",
            lambda e: e.widget.config(fg="#FF6666"))
        self._clear_all_label.bind("<Leave>",
            lambda e: e.widget.config(fg="#554444"))

        # ── Init display state ────────────────────────────────
        # Wire title trace now that all widgets exist
        self._title_trace_id = self._inline_title_var.trace_add('write', self._on_inline_title_change)
        self._set_details_enabled(False)
        # Hide controls until a movie is selected (progressive disclosure)
        self.root.after(100, self._update_viewport_state)

    # ── Helper: genre tab colour update ───────────────────────

    def _scroll_tabs(self, direction):
        """Scroll genre tabs by one tab width."""
        self._tabs_canvas.xview_scroll(direction * 80, "units")
        self._update_tab_arrows()

    def _update_tab_arrows(self):
        """Show/hide tab scroll arrows based on overflow."""
        try:
            left, right = self._tabs_canvas.xview()
            self._tab_left_btn.config(
                fg=DS["text"] if left > 0.01 else DS["border"])
            self._tab_right_btn.config(
                fg=DS["text"] if right < 0.99 else DS["border"])
        except Exception:
            pass

    def _scroll_tab_into_view(self, genre):
        """Ensure the selected tab is visible in the scrollable area."""
        tab_f = self._tab_btns.get(genre)
        if not tab_f:
            return
        self._tabs_canvas.update_idletasks()
        tab_x = tab_f.winfo_x()
        tab_w = tab_f.winfo_width()
        canvas_w = self._tabs_canvas.winfo_width()
        bbox = self._tabs_canvas.bbox("all")
        if not bbox:
            return
        total_w = max(1, bbox[2] - bbox[0])
        # Scroll so tab is centered if possible
        target = max(0, (tab_x + tab_w // 2 - canvas_w // 2)) / total_w
        self._tabs_canvas.xview_moveto(target)
        self._update_tab_arrows()

    def _genre_movie_count(self, genre):
        """Return visible slot count, respecting CUSTOM_ONLY_MODE filter."""
        if genre == "New Releases":
            return len(NR_SLOT_DATA)
        if CUSTOM_ONLY_MODE:
            # Count only T_New + custom T_Bkg slots
            def _visible(t):
                if t["name"].startswith("T_New_"):
                    return True
                dt = GENRE_DATATABLE.get(t["genre"])
                base_count = GENRES.get(t["genre"], {}).get("bkg", 0)
                if dt:
                    slots = CLEAN_DT_SLOT_DATA.get(dt, [])
                    for i, s in enumerate(slots):
                        if s.get("bkg_tex") == t["name"]:
                            return i >= base_count
                return False
            if genre == "All Movies":
                return sum(1 for t in ALL_TEXTURES
                           if t["genre"] not in HIDDEN_GENRES and _visible(t))
            return sum(1 for t in ALL_TEXTURES
                       if t["genre"] == genre and _visible(t))
        if genre == "All Movies":
            return len(ALL_TEXTURES)
        dt = GENRE_DATATABLE.get(genre)
        if not dt:
            return 0
        return len(CLEAN_DT_SLOT_DATA.get(dt, []))

    def _update_tab_colors(self, active):
        """Update tab label/badge colors and reposition the underline bar."""
        for g in self._tab_btns:
            count   = self._genre_movie_count(g)
            is_sel  = (g == active)
            tab_f   = self._tab_btns[g]
            name_l  = self._tab_labels.get(g)
            badge_l = self._tab_badges.get(g)

            gc = GENRE_COLORS.get(g)
            if is_sel and gc:
                tab_bg = gc["bg"]
                tab_fg = gc["fg"]
            elif is_sel:
                tab_bg = DS["panel"]
                tab_fg = DS["cyan"]
            else:
                tab_bg = DS["bg"]
                tab_fg = DS["text3"]
            tab_f.config(bg=tab_bg)
            # propagate bg to inner children
            for child in tab_f.winfo_children():
                try: child.config(bg=tab_bg)
                except Exception: pass
                for sub in child.winfo_children():
                    try: sub.config(bg=tab_bg)
                    except Exception: pass

            if name_l:
                name_l.config(fg=tab_fg, bg=tab_bg,
                              font=_f(FS["body"], bold=is_sel))
            if badge_l:
                if is_sel and gc:
                    badge_bg = gc["fg"]
                    badge_fg = gc["bg"]
                elif is_sel:
                    badge_bg = DS["cyan"]
                    badge_fg = DS["text_inv"]
                else:
                    badge_bg = DS["border"]
                    badge_fg = DS["text3"]
                badge_l.config(text=str(count), fg=badge_fg, bg=badge_bg)
                badge_l.master.config(bg=badge_bg)  # badge_frame

        # Genre-colored underline bars: thin colored line under inactive genre tabs
        # Skip underline recreation if active tab hasn't changed (prevents flicker)
        if not hasattr(self, '_last_active_tab') or self._last_active_tab != active:
            self._last_active_tab = active
            for w in getattr(self, '_genre_underlines', []):
                try: w.destroy()
                except Exception: pass
            self._genre_underlines = []
            self._tabs_row.update_idletasks()
            row_x = self._tabs_row.winfo_rootx()
            for g, tab_f in self._tab_btns.items():
                gc = GENRE_COLORS.get(g)
                if not gc:
                    continue
                is_sel = (g == active)
                if is_sel:
                    continue
                try:
                    tab_x = tab_f.winfo_rootx() - row_x
                    tab_w = tab_f.winfo_width()
                    bar = tk.Frame(self._tabs_row, bg=gc["bg"], height=2)
                    bar.place(x=tab_x, rely=1.0, anchor=tk.SW,
                              width=tab_w, height=2)
                    bar.lift()
                    self._genre_underlines.append(bar)
                except Exception:
                    pass
            if hasattr(self, "_tab_underline"):
                self._tab_underline.place_forget()

    def _select_nr_slot(self, idx):
        """Select a New Release slot for editing."""
        if idx < 0 or idx >= len(NR_SLOT_DATA):
            return
        self._selected_nr_idx = idx
        nr = NR_SLOT_DATA[idx]

        # Use stable NR_{sku} key so replacements persist across genre changes.
        # The actual bkg_tex is only resolved at build time.
        self.selected = {
            "name": f"NR_{nr['sku']}",
            "genre": nr["genre"],
            "folder": f"T_Bkg_{nr['genre_code']}",
            "type": "New Release",
        }

        # Enable title entry
        self._set_details_enabled(True)
        self._update_upload_btn_state()
        # Update title field — swap trace to NR handler
        self._inline_title_var.trace_remove('write', self._title_trace_id)
        self._loaded_title = nr["title"]
        self._inline_title_var.set(nr["title"])
        self._title_trace_id = self._inline_title_var.trace_add('write', self._on_nr_title_change)
        # Update standee display
        if hasattr(self, '_nr_standee_var'):
            self._nr_standee_var.set(nr.get("standee_shape", "A"))
        if hasattr(self, '_nr_genre_var'):
            self._nr_genre_var.set(nr.get("genre", "Action"))
        if hasattr(self, '_update_nr_genre_btns'):
            self._update_nr_genre_btns()
        # Update SKU display
        if hasattr(self, '_catalog_id_var'):
            self._catalog_id_var.set(str(nr.get("sku", "—")))
        self._show_nr_panel(True)
        self._update_standee_btns()
        # Show delete button for NR slots
        if hasattr(self, '_remove_slot_btn_del'):
            self._remove_slot_btn_del.pack(fill=tk.X, padx=14, pady=(12, 2))

        # Reset viewport and trigger preview load
        self._raw_img = None
        self._base_img = None
        self._viewport_zoom = 1.0
        self._viewport_pan_x = 0
        self._viewport_pan_y = 0
        self._draw_preview()

        # Update selection highlight and show controls
        self._update_shelf_highlight()
        self._update_viewport_state()
        print(f"[NR] Selected slot {idx}: '{nr['title']}'")

    def _on_nr_title_change(self, *args):
        """Auto-save NR title with debounce."""
        if not hasattr(self, '_selected_nr_idx'):
            return
        if hasattr(self, '_nr_title_debounce') and self._nr_title_debounce:
            self.root.after_cancel(self._nr_title_debounce)
        self._nr_title_debounce = self.root.after(500, self._save_nr_title)

    def _save_nr_title(self):
        """Save the current title to the NR slot."""
        idx = getattr(self, '_selected_nr_idx', -1)
        if idx < 0 or idx >= len(NR_SLOT_DATA):
            return
        new_title = self._inline_title_var.get().strip()
        if new_title and new_title != NR_SLOT_DATA[idx]["title"]:
            NR_SLOT_DATA[idx]["title"] = new_title
            save_nr_slots()
            self._refresh_shelf_keep_scroll()

    def _add_new_release(self):
        """Add a new New Release slot with a genre picker dialog."""
        genres = NR_GENRES

        # Show every supported New Release genre at once.  The previous
        # one-column canvas could hide the last genres on smaller screens and
        # had no visible scrollbar, so the picker felt like it had missing
        # options.  A compact 3-column grid mirrors the editor panel and fits
        # all current NR genres without scrolling.
        cols = 3
        rows = (len(genres) + cols - 1) // cols
        dlg_w = 520
        dlg_h = max(260, 112 + rows * 34)

        dlg = tk.Toplevel(self.root)
        dlg.title("New Release — Choose Genre")
        dlg.configure(bg=C["bg"])
        dlg.overrideredirect(False)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.focus_force(); self.lift() if hasattr(self, "lift") else None

        # Position dialog next to the sticky add button, but clamp it onto the
        # visible screen so the bottom row never vanishes below the taskbar.
        if hasattr(self, '_sticky_add_canvas'):
            btn = self._sticky_add_canvas
            btn.update_idletasks()
            bx = btn.winfo_rootx()
            by = btn.winfo_rooty()
            sw = dlg.winfo_screenwidth()
            sh = dlg.winfo_screenheight()
            x = max(0, min(bx, sw - dlg_w - 20))
            y = max(0, min(by - dlg_h, sh - dlg_h - 60))
            dlg.geometry(f"{dlg_w}x{dlg_h}+{x}+{y}")
        else:
            dlg.geometry(f"{dlg_w}x{dlg_h}")

        tk.Label(dlg, text="Select genre:", font=_vcr(11),
                 fg=C["text"], bg=C["bg"]).pack(pady=(12, 8))

        genre_frame = tk.Frame(dlg, bg=C["bg"])
        genre_frame.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 8))
        for c in range(cols):
            genre_frame.grid_columnconfigure(c, weight=1, uniform="nr_genres")

        chosen = [None]
        for i, g in enumerate(genres):
            color = GENRE_COLORS.get(g, {})
            bg = color.get("bg", C["card"])
            fg = color.get("fg", C["text"])
            btn = tk.Button(genre_frame, text=g, font=_vcr(10),
                            bg=bg, fg=fg, activebackground=DS["cyan"],
                            activeforeground=DS["text_inv"], relief=tk.FLAT,
                            cursor="hand2", pady=5,
                            command=lambda genre=g: (chosen.__setitem__(0, genre),
                                                     dlg.destroy()))
            btn.grid(row=i // cols, column=i % cols, sticky="ew", padx=4, pady=3)

        tk.Button(dlg, text="Cancel", font=_vcr(9),
                  bg=C["border"], fg=C["text_dim"], relief=tk.FLAT,
                  command=dlg.destroy).pack(pady=(0, 10))

        dlg.grab_set()
        self.root.wait_window(dlg)
        if chosen[0]:
            slot = add_nr_slot(chosen[0])
            if slot:
                self._populate_shelf()
                self._select_nr_slot(len(NR_SLOT_DATA) - 1)

    def _on_nr_standee_change(self, *args):
        """Update standee shape for selected NR slot."""
        idx = getattr(self, '_selected_nr_idx', -1)
        if idx < 0 or idx >= len(NR_SLOT_DATA):
            return
        new_shape = self._nr_standee_var.get()
        if new_shape in NR_STANDEE_SHAPES:
            NR_SLOT_DATA[idx]["standee_shape"] = new_shape
            save_nr_slots()
            self._populate_shelf()

    def _set_nr_genre(self, genre):
        """Set genre for selected NR slot via button grid."""
        self._nr_genre_var.set(genre)
        self._update_nr_genre_btns()
        self._on_nr_genre_change()

    def _update_nr_genre_btns(self):
        """Highlight the active genre button."""
        cur = self._nr_genre_var.get()
        for gname, btn in self._nr_genre_btns.items():
            if gname == cur:
                btn.config(bg=DS["cyan"], fg=DS["text_inv"])
            else:
                btn.config(bg=DS["surface"], fg=DS["text3"])

    def _on_nr_genre_change(self, *args):
        """Update genre for selected NR slot."""
        idx = getattr(self, '_selected_nr_idx', -1)
        if idx < 0 or idx >= len(NR_SLOT_DATA):
            return
        new_genre = self._nr_genre_var.get()
        if new_genre in NR_GENRE_BYTE:
            nr = NR_SLOT_DATA[idx]
            nr["genre"] = new_genre
            nr["genre_code"] = GENRES[new_genre]["code"]
            nr["genre_byte"] = NR_GENRE_BYTE[new_genre]
            code = nr["genre_code"]
            # Find the lowest available tex_num for the new genre (gap-tolerant,
            # no base_new_count cap — slots beyond base get auto-cloned at build).
            used_nums = {s["tex_num"] for i, s in enumerate(NR_SLOT_DATA)
                         if s.get("genre") == new_genre and i != idx}
            tex_num = next(i for i in range(1, 1000) if i not in used_nums)
            nr["tex_num"] = tex_num
            nr["bkg_tex"] = f"T_New_{code}_{tex_num:03d}"
            save_nr_slots()
            self._populate_shelf()

    def _delete_nr_slot(self):
        """Delete the selected NR slot and its replacement."""
        idx = getattr(self, '_selected_nr_idx', -1)
        if idx < 0 or idx >= len(NR_SLOT_DATA):
            return
        nr = NR_SLOT_DATA[idx]
        if messagebox.askyesno("Delete New Release",
                f"Delete '{nr['title']}'?\nThis cannot be undone.",
                icon="warning", parent=self.root):
            # Remove the replacement and tracking state for this NR's stable key.
            # Shipped/edited sets are append-only at build time; without explicit
            # cleanup here a deleted NR's key would survive in memory and get
            # re-persisted on the next build (Nexus repro: deleted NR_sku
            # reappears in shipped_slots.json after rebuild).
            stable_key = f"NR_{nr['sku']}"
            if stable_key in self.replacements:
                del self.replacements[stable_key]
                save_replacements(self.replacements)
                print(f"[NR] Removed replacement for {stable_key}")
            if stable_key in self._shipped_slots:
                self._shipped_slots.discard(stable_key)
                save_shipped_slots(self._shipped_slots)
                print(f"[NR] Removed shipped tracking for {stable_key}")
            if stable_key in self._edited_slots:
                self._edited_slots.discard(stable_key)
                save_edited_slots(self._edited_slots)
            remove_nr_slot(idx)
            self._selected_nr_idx = -1
            self.selected = None
            self._raw_img = None
            self._base_img = None
            self._populate_shelf()
            self._draw_preview()
            self._update_viewport_state()
            self._refresh_stats()
            self._update_nr_tab_badge()

    def _set_nr_view_mode(self, mode):
        """Switch NR viewport between VHS and Standee view."""
        self._nr_view_mode.set(mode)
        is_vhs = (mode == "VHS")
        # Update both old and new toggle buttons
        for vhs_btn, std_btn in [(self._vp_vhs_btn, self._vp_standee_btn)]:
            vhs_btn.config(
                bg=DS["cyan"] if is_vhs else DS["surface"],
                fg=DS["text_inv"] if is_vhs else "#AABBCC")
            std_btn.config(
                bg=DS["cyan"] if not is_vhs else DS["surface"],
                fg=DS["text_inv"] if not is_vhs else "#AABBCC")
        self._render_preview()

    def _set_standee(self, shape):
        """Set standee shape for selected NR slot."""
        self._nr_standee_var.set(shape)
        self._update_standee_btns()
        idx = getattr(self, '_selected_nr_idx', -1)
        if 0 <= idx < len(NR_SLOT_DATA):
            NR_SLOT_DATA[idx]["standee_shape"] = shape
            save_nr_slots()
            # Auto-switch to Standee preview mode
            if hasattr(self, '_nr_view_mode') and self._nr_view_mode.get() != "Standee":
                self._set_nr_view_mode("Standee")
            else:
                self._render_preview()

    def _show_nr_panel(self, show=True):
        """Toggle between genre-movie controls and NR controls in right panel.
        Both frames live inside self._mode_container so pack order is preserved."""
        if show:
            self._genre_controls_frame.pack_forget()
            self._nr_controls_frame.pack(fill=tk.X, in_=self._mode_container)
            # Hide layout section, show NR view toggle
            if hasattr(self, '_layout_section'):
                self._layout_section.grid_forget()
            if hasattr(self, '_vp_nr_toggle_frame'):
                self._vp_nr_toggle_frame.pack(side=tk.LEFT, padx=(8, 0))
            # Hide star rating section, show NR info
            if hasattr(self, '_star_rating_frame'):
                self._star_rating_frame.pack_forget()
            # Also hide after delay to catch any re-packing from concurrent updates
            if hasattr(self, '_star_rating_frame'):
                self.root.after(100, lambda: (
                    self._star_rating_frame.pack_forget()
                    if getattr(self, '_selected_nr_idx', -1) >= 0 else None))
            if hasattr(self, '_nr_rating_info'):
                # Pack before mode_container so it appears right after title
                self._nr_rating_info.pack(fill=tk.X, pady=(4, 0),
                                          before=self._mode_container)
            # Reset to VHS view
            self._set_nr_view_mode("VHS")
        else:
            self._nr_controls_frame.pack_forget()
            self._genre_controls_frame.pack(fill=tk.X, in_=self._mode_container)
            # Restore layout section, hide NR view toggle
            if hasattr(self, '_layout_section'):
                self._layout_section.grid(row=5, column=0, sticky="ew", padx=10, pady=(0, 6))
            if hasattr(self, '_vp_nr_toggle_frame'):
                self._vp_nr_toggle_frame.pack_forget()
            # Restore star rating, hide NR info
            if hasattr(self, '_star_rating_frame'):
                self._star_rating_frame.pack(fill=tk.X, before=self._mode_container)
            if hasattr(self, '_nr_rating_info'):
                self._nr_rating_info.pack_forget()

    def _select_genre(self, genre):
        # Commit pending last_edited_at for the slot that's about to be deselected.
        self._flush_pending_edit_timestamp()
        self._flush_pending_nr_timestamp(getattr(self, "_selected_nr_idx", -1))
        # Close any open sort dropdown when switching tabs.
        if hasattr(self, "_close_sort_dropdown"):
            self._close_sort_dropdown()
        self._scroll_tab_into_view(genre)
        self._genre_var.set(genre)
        if getattr(self, "_bulk_mode", False):
            self._bulk_mode = False
            self._bulk_selected.clear()
            self._bulk_source_genre = None
            if hasattr(self, "_update_bulk_bar"):
                self._update_bulk_bar()
        # Clear selection when switching tabs
        self.selected = None
        self._selected_nr_idx = -1
        self._raw_img = None
        self._base_img = None
        self._set_details_enabled(False)
        self._draw_preview()
        self._update_viewport_state()
        self._update_tab_colors(genre)
        # Show sort control on single-genre tabs AND the New Releases tab.
        # Hidden only on "All Movies" because that's a multi-genre view.
        sortable_tab = (genre != "All Movies")
        if hasattr(self, "_show_sort_control"):
            self._show_sort_control(sortable_tab)
            if sortable_tab:
                self._refresh_sort_btn_label()
        self._populate_shelf()
        self._update_tab_colors(genre)
        self._update_sticky_add_btn(genre)
        if hasattr(self, "_update_bulk_bar"):
            self._update_bulk_bar()
        # Show/hide sticky genre header
        if genre != "All Movies" and hasattr(self, '_sticky_genre_hdr'):
            if self._sticky_genre_hdr.winfo_manager():
                self._sticky_genre_hdr.pack_forget()

    def _update_sticky_add_btn(self, genre=None):
        """Show/hide and redraw the sticky Add Movie button."""
        if not hasattr(self, "_sticky_add_frame"):
            return
        if genre is None:
            genre = self._genre_var.get()
        if genre == "All Movies":
            self._sticky_add_frame.pack_forget()
        else:
            # Re-pack after shelf_canvas_outer (below scrollable area)
            self._sticky_add_frame.pack(fill=tk.X, padx=6, pady=(0, 6))
            if hasattr(self, "_draw_sticky_add"):
                self._draw_sticky_add()

    # ── Layout button colours ──────────────────────────────────
    def _update_layout_btn_colors(self, active_val):
        for v, btn in self._layout_btns.items():
            if v == active_val:
                btn.config(bg=C["cyan"], fg=C["bg"])
            else:
                btn.config(bg=C["border"], fg=C["text_dim"])

    def _get_saved_layout(self):
        """Return the fixed regular layout ID."""
        return FIXED_REGULAR_LAYOUT

    def _save_layout_choice(self, val):
        """Compatibility shim: regular movies always use the fixed layout."""
        if not self.selected:
            return
        self._layout_overlay_var.set(True)
        if hasattr(self, "_draw_overlay_toggle"):
            self._draw_overlay_toggle(True)
        self._apply_ls(FIXED_REGULAR_LAYOUT)
        if self._raw_img is not None:
            self._show_layout_change_notify(FIXED_REGULAR_LAYOUT)

    def _redraw_layout_cards(self):
        """No-op: the five selectable regular layouts have been removed."""
        return

    def _show_layout_random_label(self, n):
        """No-op: random regular layout selection has been removed."""
        return

    def _show_layout_change_notify(self, n):
        """Show fixed-layout notification beneath the canvas for 4 seconds."""
        if not hasattr(self, "_layout_notify_label") or self._layout_notify_label is None:
            info_lbl = getattr(self, "_info_label", None)
            parent = info_lbl.master if info_lbl else self.canvas.master
            self._layout_notify_label = tk.Label(parent, text="",
                font=_f(FS["meta"]), fg=DS["cyan"], bg=DS["bg"])
            if info_lbl:
                self._layout_notify_label.pack(before=info_lbl, anchor=tk.W, padx=6)
            else:
                self._layout_notify_label.pack(anchor=tk.W, padx=6)
        lbl = self._layout_notify_label
        lbl.config(text="Fixed layout visible area updated. Use Fit Visible to realign if needed.",
                   fg=DS["cyan"])
        if self._layout_notify_job is not None:
            self.root.after_cancel(self._layout_notify_job)
        self._layout_notify_job = self.root.after(4000, self._fade_layout_notify)

    def _fade_layout_notify(self):
        """Fade out the layout change notification."""
        lbl = getattr(self, "_layout_notify_label", None)
        if lbl is None:
            return
        # Simple fade: dim the color then clear
        lbl.config(fg=DS["text3"])
        self._layout_notify_job = self.root.after(500, lambda: lbl.config(text=""))

    def _attach_layout_tooltip(self, widget, text):
        """Attach a tooltip to a layout widget, shown on 400ms hover."""
        tip_win = [None]
        delay_id = [None]
        def _enter(e):
            def _show():
                tw = tk.Toplevel(widget)
                tw.wm_overrideredirect(True)
                tw.wm_geometry(f"+{e.x_root+5}+{e.y_root+20}")
                tw.config(bg=DS["border"])
                tk.Label(tw, text=text, font=_f(FS["meta"]),
                         fg=DS["text"], bg=DS["panel"],
                         padx=8, pady=3).pack()
                tip_win[0] = tw
            delay_id[0] = widget.after(400, _show)
        def _leave(e):
            if delay_id[0]:
                widget.after_cancel(delay_id[0])
                delay_id[0] = None
            if tip_win[0]:
                tip_win[0].destroy()
                tip_win[0] = None
        widget.bind("<Enter>", _enter, add="+")
        widget.bind("<Leave>", _leave, add="+")

    def _set_layout(self, val):
        val = FIXED_REGULAR_LAYOUT if val >= 1 else 0
        self._layout_preview.set(val)
        self._update_layout_btn_colors(val)
        # Lazy-load fallback: if background preload hasn't finished yet
        if val >= 1:
            key = f"__layout_full_{val}_bc"
            if key not in self.pak_cache._cache:
                # Show pulsing border on the card as loading indicator
                if hasattr(self, "_layout_card_frames") and val in self._layout_card_frames:
                    self._layout_card_frames[val].config(
                        highlightbackground=DS["text3"])
                self._info_var.set("Loading fixed layout...")
                self.root.update()
                try:
                    self.pak_cache.get_layout_texture_full(val, "bc")
                except Exception:
                    pass
                self._info_var.set("")
        self._render_preview()

    # ── LS button colours ─────────────────────────────────────
    def _update_ls_btn_colors(self, active_ls):
        """No-op — regular movie layout style is fixed."""
        pass

    def _apply_ls(self, n):
        """Force the selected slot to the fixed regular layout and update preview."""
        if not self.selected:
            return
        name    = self.selected["name"]
        genre   = self.selected["genre"]
        dt_name = GENRE_DATATABLE.get(genre)
        if not dt_name:
            return
        slot = next((s for s in CLEAN_DT_SLOT_DATA.get(dt_name, [])
                     if s.get("bkg_tex") == name), None)
        if slot is None:
            return
        slot["ls"] = FIXED_REGULAR_LAYOUT
        save_custom_slots()
        self.dt_manager._clean_builders.clear()
        self._update_ls_btn_colors(FIXED_REGULAR_LAYOUT)
        self._set_layout(FIXED_REGULAR_LAYOUT)   # also update preview overlay
        # Auto-refit if fit-to-canvas was active
        if self._auto_fit:
            self._fit_to_canvas()

    # ── Dev panel toggle ──────────────────────────────────────
    def _reset_ship_label(self):
        self._ship_label = "🚀  SHIP TO STORE"
        if hasattr(self, "_draw_ship_btn"):
            self._draw_ship_btn()

    def _show_dev_dialog(self):
        """Show dev/testing dialog with useful tools."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Dev / Testing")
        dlg.configure(bg=C["bg"])
        dlg.resizable(False, False)
        dlg.focus_force()
        dlg.lift()
        dlg.geometry("380x300")

        tk.Label(dlg, text="Development Tools",
                 font=_vcr(11, bold=True), fg=C["text"],
                 bg=C["bg"]).pack(pady=(12, 8))

        tk.Button(dlg, text="🧪  Layout Test Slots (obsolete)",
                  command=lambda: (self._create_layout_test_slots(), dlg.destroy()),
                  bg=C["card"], fg=C["text"], relief=tk.FLAT,
                  font=_vcr(10), cursor="hand2", pady=6
                  ).pack(fill=tk.X, padx=20, pady=3)

        tk.Button(dlg, text="📊  Scan AssetRegistry",
                  command=lambda: (dlg.destroy(), self._scan_asset_registry()),
                  bg=C["card"], fg=C["text"], relief=tk.FLAT,
                  font=_vcr(10), cursor="hand2", pady=6
                  ).pack(fill=tk.X, padx=20, pady=3)

        tk.Button(dlg, text="🗑  Clear Output & Cache",
                  command=lambda: (self._clear_output_cache(), dlg.destroy()),
                  bg=C["card"], fg=C["text"], relief=tk.FLAT,
                  font=_vcr(10), cursor="hand2", pady=6
                  ).pack(fill=tk.X, padx=20, pady=3)

        tk.Button(dlg, text="📋  Copy Config Info to Clipboard",
                  command=lambda: (self._copy_config_info(), dlg.destroy()),
                  bg=C["card"], fg=C["text"], relief=tk.FLAT,
                  font=_vcr(10), cursor="hand2", pady=6
                  ).pack(fill=tk.X, padx=20, pady=3)

        tk.Button(dlg, text="Cancel", font=_vcr(9),
                  bg=C["border"], fg=C["text_dim"], relief=tk.FLAT,
                  command=dlg.destroy).pack(pady=8)

    def _clear_output_cache(self):
        """Clear the Output folder and pak cache."""
        import shutil
        cache_dir = os.path.join(OUTPUT_DIR, "_pak_cache")
        build_dir = os.path.join(OUTPUT_DIR, "build")
        cleared = []
        for d in [cache_dir, build_dir]:
            if os.path.isdir(d):
                shutil.rmtree(d)
                cleared.append(os.path.basename(d))
        pak = os.path.join(OUTPUT_DIR, "zzzzzz_MovieWorkshop_P.pak")
        if os.path.exists(pak):
            os.remove(pak)
            cleared.append("pak file")
        if cleared:
            messagebox.showinfo("Cache Cleared",
                f"Cleared: {', '.join(cleared)}", parent=self.root)
        else:
            messagebox.showinfo("Nothing to Clear",
                "Output folder is already empty.", parent=self.root)

    def _copy_config_info(self):
        """Copy diagnostic info to clipboard for bug reports."""
        import platform
        lines = [
            f"Tool: Retro Rewind Movie Workshop {TOOL_VERSION}",
            f"OS: {platform.system()} {platform.release()}",
            f"Python: {platform.python_version()}",
            f"",
            f"Config:",
        ]
        for key in ["texconv", "repak", "base_game_pak", "mods_folder"]:
            val = self.config.get(key, "")
            exists = "✓" if val and os.path.exists(val) else "✗"
            lines.append(f"  {key}: {exists} {val}")
        # Movie counts
        lines.append("")
        lines.append("Movies:")
        for genre, info in GENRES.items():
            if genre in HIDDEN_GENRES:
                continue
            dt = GENRE_DATATABLE.get(genre)
            if dt:
                base = info.get("bkg", 0)
                custom = max(0, len(CLEAN_DT_SLOT_DATA.get(dt, [])) - base)
                if custom > 0:
                    lines.append(f"  {genre}: {custom}")
        nr = len(NR_SLOT_DATA)
        if nr > 0:
            lines.append(f"  New Releases: {nr}")
        text = "\n".join(lines)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        messagebox.showinfo("Copied", "Diagnostic info copied to clipboard.",
                            parent=self.root)

    def _toggle_dev_panel(self):
        """Legacy compat — now opens dialog."""
        self._show_dev_dialog()




    def _has_custom_movies(self):
        """Return True if any user-created movies exist (not base game slots)."""
        for genre, info in GENRES.items():
            if genre in HIDDEN_GENRES:
                continue
            dt = GENRE_DATATABLE.get(genre)
            if dt:
                base_count = info.get("bkg", 0)
                total = len(CLEAN_DT_SLOT_DATA.get(dt, []))
                if total > base_count:
                    return True
        return bool(NR_SLOT_DATA)

    def _update_viewport_state(self):
        """Update viewport and right panel visibility based on selection state.
        Three states: no movies, movies but none selected, movie selected."""
        has_custom = self._has_custom_movies()
        has_selection = self.selected is not None or getattr(self, '_selected_nr_idx', -1) >= 0
        genre = self._genre_var.get()

        if genre == "New Releases":
            tab_has_movies = bool(NR_SLOT_DATA)
        elif genre == "All Movies":
            tab_has_movies = has_custom
        else:
            # For individual genre tabs, check custom slots only
            dt = GENRE_DATATABLE.get(genre)
            if dt:
                base_count = GENRES.get(genre, {}).get("bkg", 0)
                total = len(CLEAN_DT_SLOT_DATA.get(dt, []))
                tab_has_movies = total > base_count
            else:
                tab_has_movies = False

        # ── Viewport: tab bar, controls, info, layout ──
        vp_widgets = [
            (self._vp_tab_bar,   dict(row=0, column=0, sticky="ew", padx=10, pady=(6, 0))),
            (self._info_row,     dict(row=2, column=0, sticky="ew", padx=10, pady=(2, 0))),
            (self._info_divider, dict(row=3, column=0, sticky="ew", padx=10, pady=(4, 0))),
            (self._ctrl_f,       dict(row=4, column=0, sticky="ew", padx=10, pady=(4, 4))),
        ]
        if hasattr(self, '_layout_section'):
            # Layout section only for genre movies, not NR
            is_nr = getattr(self, '_selected_nr_idx', -1) >= 0
            if not is_nr:
                vp_widgets.append(
                    (self._layout_section, dict(row=5, column=0, sticky="ew", padx=10, pady=(0, 6))))
            else:
                self._layout_section.grid_forget()

        # Hide "Fit Visible" button for NR (it does the same as "Fill Canvas")
        if hasattr(self, '_btn_fit_canvas'):
            is_nr = getattr(self, '_selected_nr_idx', -1) >= 0
            if is_nr:
                self._btn_fit_canvas.grid_remove()
            else:
                self._btn_fit_canvas.grid(row=0, column=2, sticky="ew", padx=(2, 0))

        for widget, grid_opts in vp_widgets:
            if has_selection:
                widget.grid(**grid_opts)
            else:
                widget.grid_forget()

        # ── Right panel overlays ──
        if hasattr(self, '_det_inner'):
            if has_selection:
                # Show full content
                for w in self._det_inner.winfo_children():
                    w_name = str(w)
                    # Re-pack all children that were hidden
                    if not w.winfo_manager():
                        w.pack(fill=tk.X)
                # Simpler: just show/hide the entire inner frame
                self._det_canvas.itemconfig(self._det_inner_window, state="normal")
                self._det_canvas.itemconfig(self._det_empty_window, state="hidden")
                self._det_canvas.itemconfig(self._det_onboard_window, state="hidden")
            elif tab_has_movies:
                # Movies exist but none selected
                self._det_canvas.itemconfig(self._det_inner_window, state="hidden")
                self._det_canvas.itemconfig(self._det_onboard_window, state="hidden")
                self._det_canvas.itemconfig(self._det_empty_window, state="normal")
                try:
                    cw = self._det_canvas.winfo_width()
                    if cw > 1:
                        self._det_canvas.itemconfig(self._det_empty_window, width=cw)
                except Exception:
                    pass
            else:
                # No movies — onboarding
                self._det_canvas.itemconfig(self._det_inner_window, state="hidden")
                self._det_canvas.itemconfig(self._det_empty_window, state="hidden")
                self._det_canvas.itemconfig(self._det_onboard_window, state="normal")
                try:
                    cw = self._det_canvas.winfo_width()
                    if cw > 1:
                        self._det_canvas.itemconfig(self._det_onboard_window, width=cw)
                except Exception:
                    pass

        # ── Ship button always active when movies exist (unless setup issues) ──
        if hasattr(self, '_ship_canvas'):
            has_issues = bool(getattr(self, '_startup_issues', []))
            if has_custom and not has_issues:
                self._ship_canvas.bind("<Button-1>", lambda e: self._build())
            elif has_issues:
                self._ship_canvas.bind("<Button-1>", lambda e: self._show_ship_blocked_banner())
            else:
                self._ship_canvas.unbind("<Button-1>")

    def _set_details_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        if not hasattr(self, '_inline_title_entry'):
            return
        widgets = ([self._inline_title_entry])
        for w in widgets:
            try: w.config(state=state)
            except Exception: pass
        # Ship button always stays active (handled by _update_viewport_state)

    # ── Inline title handling ──────────────────────────────────
    def _on_inline_title_change(self, *args):
        """Auto-save title after user stops typing (500ms debounce)."""
        if not self.selected:
            return
        # Cancel any pending save
        if hasattr(self, '_title_debounce_id') and self._title_debounce_id:
            self.root.after_cancel(self._title_debounce_id)
        self._title_debounce_id = self.root.after(500, self._save_inline_title)

    def _save_inline_title(self):
        """Save the inline title to the DataTable."""
        if not self.selected:
            return
        new_title = self._inline_title_var.get().strip()
        if not new_title:
            return  # silently ignore empty (user is still typing)
        title_err = _movie_title_error(new_title)
        if title_err:
            messagebox.showwarning("Too Long", title_err)
            return

        # Skip save if title matches what was loaded (selection change, not edit)
        loaded = getattr(self, '_loaded_title', None)
        if loaded is not None and new_title == loaded:
            return

        # Use dt_manager to find and patch the title
        entries = self.dt_manager.get_titles_for_texture(self.selected)
        if entries:
            entry = entries[0]
            if new_title != entry["title"].rstrip():
                success, err = self.dt_manager.patch_title(
                    entry["dt_name"], entry["offset"],
                    entry["original_title"], new_title,
                    self.title_changes,
                    bkg_tex=self.selected["name"])
                if not success:
                    messagebox.showerror("Title Too Long", err)
                    return
        # Also update pn_name in slot dict
        name  = self.selected["name"]
        genre = self.selected["genre"]
        dt    = GENRE_DATATABLE.get(genre)
        if dt:
            for slot in CLEAN_DT_SLOT_DATA.get(dt, []):
                if slot.get("bkg_tex") == name:
                    slot["pn_name"] = new_title
                    break
        save_custom_slots()
        self._mark_edited(name)
        self.dt_manager._clean_builders.clear()
        self.dt_manager._titles_cache.clear()
        self._loaded_title = new_title  # update loaded title after save
        self._refresh_shelf_keep_scroll()

    # ── Star rating click ─────────────────────────────────────
    def _get_current_last2(self):
        """Return last2 for the selected slot, or 93 (4.5 stars default)."""
        if not self.selected:
            return 93
        name = self.selected["name"]
        dt   = GENRE_DATATABLE.get(self.selected["genre"])
        if dt:
            for s in CLEAN_DT_SLOT_DATA.get(dt, []):
                if s.get("bkg_tex") == name:
                    return s.get("sku", 0) % 100
        return 93

    def _randomize_rating(self):
        """Randomize star rating with a sweep animation."""
        if not self.selected:
            return
        import random
        valid = sorted(STAR_VALUE_TO_LAST2.keys())
        target = random.choice(valid)

        # Sweep animation: fill stars left to right over ~150ms
        sweep_steps = valid
        # Only sweep up to target
        sweep = [v for v in sweep_steps if v <= target]
        delay_per_step = max(15, 150 // max(len(sweep), 1))

        def _sweep(idx=0):
            if idx < len(sweep):
                val = sweep[idx]
                if hasattr(self, '_draw_stars'):
                    self._draw_stars(val)
                # Update critic badge during sweep
                l2 = stars_to_last2(val)
                critic = critic_badge_key(critic_for_last2(l2))
                if hasattr(self, '_draw_critic_badge'):
                    self._draw_critic_badge(critic)
                self.root.after(delay_per_step, lambda: _sweep(idx + 1))
            else:
                # Settle on target — save and pulse
                self._set_stars_half(target)

        _sweep()

    def _set_stars_half(self, val):
        """Set rating from half-star click (val = 0.0..5.0 in 0.5 steps)."""
        if not self.selected:
            return
        last2 = stars_to_last2(val)
        name  = self.selected["name"]
        genre = self.selected["genre"]
        dt    = GENRE_DATATABLE.get(genre)
        if not dt:
            return
        slots = CLEAN_DT_SLOT_DATA.get(dt, [])
        slot  = next((s for s in slots if s.get("bkg_tex") == name), None)
        if slot is None:
            return
        idx    = slots.index(slot) + 1
        rarity = self._rarity_var.get()
        new_sku = generate_sku(genre, idx, last2=last2, rarity=rarity)
        slot["sku"] = new_sku
        save_custom_slots()
        self._mark_edited(name)
        self.dt_manager._clean_builders.clear()
        # Update saved star state and animate
        self._star_saved = val
        if getattr(self, "_star_pulse_job", None):
            self.root.after_cancel(self._star_pulse_job)
            self._star_pulse_job = None
        if hasattr(self, "_pulse_stars"):
            self._pulse_stars(val)
        # Update critic badge
        if hasattr(self, "_draw_critic_badge") and hasattr(self, "_get_current_critic"):
            self._draw_critic_badge(critic_badge_key(critic_for_last2(last2)))
        self._refresh_slot_rating()
        self._update_star_display(new_sku)

    def _set_stars(self, stars_clicked):
        """Legacy integer click — delegate to half-star handler."""
        star_map = {1: 1.0, 2: 2.0, 3: 3.0, 4: 4.0, 5: 5.0}
        self._set_stars_half(star_map.get(stars_clicked, 4.5))

    def _set_stars_legacy(self, stars_clicked):
        """Map star click to last2 and regenerate SKU."""
        if not self.selected:
            return
        star_map = {1: 12, 2: 23, 3: 38, 4: 83, 5: 0}
        last2 = star_map.get(stars_clicked, 93)
        name  = self.selected["name"]
        genre = self.selected["genre"]
        dt    = GENRE_DATATABLE.get(genre)
        if not dt:
            return
        slots    = CLEAN_DT_SLOT_DATA.get(dt, [])
        slot     = next((s for s in slots if s.get("bkg_tex") == name), None)
        if slot is None:
            return
        idx      = slots.index(slot) + 1
        rarity   = self._rarity_var.get()
        new_sku  = generate_sku(genre, idx, last2=last2, rarity=rarity)
        slot["sku"] = new_sku
        save_custom_slots()
        self._mark_edited(name)
        self.dt_manager._clean_builders.clear()
        self._refresh_slot_rating()
        self._update_star_display(new_sku)

    def _update_star_display(self, sku):
        """Update half-star canvases, value label, and critic badge."""
        if not hasattr(self, "_star_canvas"):
            return
        if not sku:
            self._star_saved = 0.0
            if hasattr(self, "_draw_stars"): self._draw_stars()
            if hasattr(self, "_star_label"):
                self._star_label.config(text="")
            if hasattr(self, "_draw_critic_badge"):
                self._draw_critic_badge(None)
            return
        stars, critic, is_holo = sku_to_info(sku)
        val = round(stars * 2) / 2
        self._star_saved = val
        if hasattr(self, "_draw_stars"):
            self._draw_stars()   # no hover arg → draws saved state
        if hasattr(self, "_star_label"):
            self._star_label.config(text=sku_display(sku))
        if hasattr(self, "_draw_critic_badge"):
            self._draw_critic_badge(critic)

    # ── Rarity change ─────────────────────────────────────────
    def _on_rarity_change(self, event=None):
        if not self.selected:
            return
        name  = self.selected["name"]
        genre = self.selected["genre"]
        dt    = GENRE_DATATABLE.get(genre)
        if not dt:
            return
        slots  = CLEAN_DT_SLOT_DATA.get(dt, [])
        slot   = next((s for s in slots if s.get("bkg_tex") == name), None)
        if slot is None:
            return
        old_sku = slot.get("sku", 0)
        last2   = old_sku % 100 if old_sku else 93
        idx     = slots.index(slot) + 1
        new_sku = generate_sku(genre, idx, last2=last2, rarity=self._rarity_var.get())
        slot["sku"] = new_sku
        save_custom_slots()
        self._mark_edited(name)
        self.dt_manager._clean_builders.clear()
        self._refresh_slot_rating()
        self._update_star_display(new_sku)

    # ── Rotate 90° CW ─────────────────────────────────────────
    def _rotate_image(self):
        if not self.selected:
            return
        name  = self.selected["name"]
        entry = self.replacements.get(name)
        if not entry:
            messagebox.showinfo("No Image",
                "Upload a cover image first, then use Rotate.", parent=self.root)
            return
        path = entry["path"] if isinstance(entry, dict) else entry
        if not os.path.exists(path):
            return
        try:
            img = Image.open(path)
            img_r = img.rotate(-90, expand=True)   # 90° clockwise
            img_r.save(path)
            self._mark_edited(name)
            # Reload
            self._raw_img  = None
            self._base_img = None
            self._load_preview_bg(self.selected)
        except Exception as e:
            messagebox.showerror("Rotate Error", str(e), parent=self.root)

    # ── Open movie editor in "new" mode ───────────────────────
    def _open_movie_editor_new(self):
        """Open movie editor defaulting to 'Create new' mode."""
        # Temporarily override mode by setting selected to None
        orig = self.selected
        self.selected = None
        self._open_movie_editor()
        self.selected = orig

    # ── Delete a custom slot ──────────────────────────────────
    def _flash_shipped(self):
        """Briefly show ✓ Shipped! on the ship button then revert."""
        if not hasattr(self, "_ship_canvas"):
            return
        c = self._ship_canvas
        def _draw_shipped():
            c.delete("all")
            w = c.winfo_width() or 220
            h = 64
            bc = DS["cyan"]
            c.create_rectangle(0, 0, w, h, fill=bc, outline="")
            c.create_text(w//2, h//2 - 4, text="✓ Shipped!",
                font=_vcr(14, bold=True), fill=DS["bg"], anchor=tk.CENTER)
            c.create_text(w//2, h//2 + 12, text="Mod installed successfully",
                font=_vcr(9), fill=DS["bg"], anchor=tk.CENTER)
        _draw_shipped()
        # Revert after 2.5s — call _draw_ship_btn if accessible
        def _revert():
            c.event_generate("<Configure>")
        self.root.after(2500, _revert)

    # ── Bulk move between genres ──────────────────────────────────
    def _bulk_allowed_tab(self):
        """Bulk move is for regular single-genre tabs only."""
        genre = self._genre_var.get()
        return (genre not in ("All Movies", "New Releases")
                and genre not in HIDDEN_GENRES
                and GENRE_DATATABLE.get(genre) in CLEAN_DT_SLOT_DATA)

    def _can_bulk_move_texture(self, texture):
        """Return True if texture is a movable custom regular movie."""
        if not texture or texture.get("type") == "New Release":
            return False
        genre = texture.get("genre")
        if getattr(self, "_bulk_source_genre", None) and genre != self._bulk_source_genre:
            return False
        dt_name, slots, slot, idx, base_count = self._find_slot_record(genre, texture.get("name"))
        return bool(slot is not None and idx is not None and idx >= base_count)

    def _update_bulk_bar(self):
        """Refresh bulk mode button/bar visibility and selected count."""
        if not hasattr(self, "_bulk_btn"):
            return
        allowed = self._bulk_allowed_tab()
        if allowed:
            if not self._bulk_btn.winfo_ismapped():
                self._bulk_btn.pack(side=tk.RIGHT, padx=(0, 8))
        else:
            self._bulk_btn.pack_forget()
            if getattr(self, "_bulk_mode", False):
                self._bulk_mode = False
                self._bulk_selected.clear()
                self._bulk_source_genre = None

        active = bool(getattr(self, "_bulk_mode", False) and allowed)
        if active:
            self._bulk_btn.config(text="Bulk: ON", fg=DS["bg"], bg=DS["cyan"])
            if not self._bulk_action_bar.winfo_ismapped():
                kwargs = dict(fill=tk.X, padx=6, pady=(0, 4))
                if hasattr(self, "_shelf_canvas_outer"):
                    kwargs["before"] = self._shelf_canvas_outer
                self._bulk_action_bar.pack(**kwargs)
        else:
            self._bulk_btn.config(text="Bulk Move", fg=DS["text3"], bg=C["card"])
            if hasattr(self, "_bulk_action_bar"):
                self._bulk_action_bar.pack_forget()

        count = len(getattr(self, "_bulk_selected", set()))
        src = self._bulk_source_genre or self._genre_var.get()
        self._bulk_count_var.set(
            f"Bulk move: {count} selected from {src}"
            if active else "Bulk mode: 0 selected")
        if hasattr(self, "_bulk_move_btn"):
            if count:
                self._bulk_move_btn.config(bg=DS["cyan"], fg=DS["bg"])
            else:
                self._bulk_move_btn.config(bg=DS["border"], fg=DS["text3"])

    def _toggle_bulk_mode(self, enable=None):
        """Enter/exit bulk move mode for the current regular genre tab."""
        if enable is None:
            enable = not getattr(self, "_bulk_mode", False)
        if enable and not self._bulk_allowed_tab():
            if hasattr(self, "_info_var"):
                self._info_var.set("Bulk move is available on regular genre tabs only.")
            return
        self._bulk_mode = bool(enable)
        self._bulk_selected.clear()
        self._bulk_source_genre = self._genre_var.get() if self._bulk_mode else None
        self._update_bulk_bar()
        self._refresh_shelf_keep_scroll()

    def _bulk_toggle_texture(self, texture):
        """Toggle one movie in the bulk selection set."""
        if not self._can_bulk_move_texture(texture):
            if hasattr(self, "_info_var"):
                self._info_var.set("Only custom regular movies can be bulk-moved.")
            return
        name = texture.get("name")
        if name in self._bulk_selected:
            self._bulk_selected.remove(name)
        else:
            self._bulk_selected.add(name)
        self._update_bulk_bar()
        self._refresh_shelf_keep_scroll()

    def _bulk_clear_selection(self):
        self._bulk_selected.clear()
        self._update_bulk_bar()
        self._refresh_shelf_keep_scroll()

    def _bulk_select_all_filtered(self):
        """Select all currently filtered movies in the active source genre."""
        if not getattr(self, "_bulk_mode", False):
            return
        for tex in getattr(self, "filtered", []):
            if self._can_bulk_move_texture(tex):
                self._bulk_selected.add(tex.get("name"))
        self._update_bulk_bar()
        self._refresh_shelf_keep_scroll()

    def _open_bulk_move_picker(self):
        """Choose target genre for selected bulk-move movies."""
        if not getattr(self, "_bulk_mode", False):
            return
        if not self._bulk_selected:
            if hasattr(self, "_info_var"):
                self._info_var.set("Select one or more movies first, then choose a target genre.")
            return
        src_genre = self._bulk_source_genre or self._genre_var.get()
        genres = [g for g in GENRES
                  if g not in HIDDEN_GENRES
                  and g != src_genre
                  and GENRE_DATATABLE.get(g) in CLEAN_DT_SLOT_DATA]
        if not genres:
            return

        cols = 3
        rows = (len(genres) + cols - 1) // cols
        dlg_w = 560
        dlg_h = max(270, 150 + rows * 34)
        dlg = tk.Toplevel(self.root)
        dlg.title("Bulk Move — Choose Target Genre")
        dlg.configure(bg=C["bg"])
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.bind("<Escape>", lambda e: dlg.destroy())

        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()
        bx = self.root.winfo_rootx() + 160
        by = self.root.winfo_rooty() + 120
        x = max(0, min(bx, sw - dlg_w - 20))
        y = max(0, min(by, sh - dlg_h - 60))
        dlg.geometry(f"{dlg_w}x{dlg_h}+{x}+{y}")

        tk.Label(dlg, text="Move selected movies to:", font=_vcr(11),
                 fg=C["text"], bg=C["bg"]).pack(pady=(12, 2))
        tk.Label(dlg, text=f"{len(self._bulk_selected)} movie(s) selected from {src_genre}",
                 font=_vcr(8), fg=DS["text3"], bg=C["bg"]).pack(pady=(0, 10))

        genre_frame = tk.Frame(dlg, bg=C["bg"])
        genre_frame.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 8))
        for c in range(cols):
            genre_frame.grid_columnconfigure(c, weight=1, uniform="bulk_move_genres")

        chosen = [None]
        for i, g in enumerate(genres):
            color = GENRE_COLORS.get(g, {})
            btn = tk.Button(genre_frame, text=g, font=_vcr(10),
                            bg=color.get("bg", C["card"]),
                            fg=color.get("fg", C["text"]),
                            activebackground=DS["cyan"],
                            activeforeground=DS["text_inv"], relief=tk.FLAT,
                            cursor="hand2", pady=5,
                            command=lambda genre=g: (chosen.__setitem__(0, genre),
                                                     dlg.destroy()))
            btn.grid(row=i // cols, column=i % cols, sticky="ew", padx=4, pady=3)

        tk.Button(dlg, text="Cancel", font=_vcr(9),
                  bg=C["border"], fg=C["text_dim"], relief=tk.FLAT,
                  command=dlg.destroy).pack(pady=(0, 10))
        dlg.focus_force()
        dlg.grab_set()
        self.root.wait_window(dlg)
        if chosen[0]:
            self._bulk_move_selected_to_genre(chosen[0])

    def _bulk_move_selected_to_genre(self, target_genre):
        """Move all selected movies without switching away from the source tab."""
        selected_names = list(self._bulk_selected)
        if not selected_names:
            return
        src_genre = self._bulk_source_genre or self._genre_var.get()
        moved = []
        failed = []

        for old_name in selected_names:
            tex = next((t for t in ALL_TEXTURES
                        if t.get("name") == old_name and t.get("genre") == src_genre), None)
            if not tex:
                failed.append(old_name)
                continue
            res = self._move_movie_to_genre(
                tex, target_genre,
                switch_tab=False, select_moved=False,
                refresh=False, commit=False, show_status=False)
            if res:
                moved.append(res)
            else:
                failed.append(old_name)

        if moved:
            save_replacements(self.replacements)
            save_shipped_slots(self._shipped_slots)
            save_edited_slots(self._edited_slots)
            save_custom_slots()
            rebuild_texture_list()
            invalidate_slot_lookup_cache()
            self.dt_manager._clean_builders.clear()
            self.dt_manager._titles_cache.clear()

        self._bulk_selected.clear()
        self._bulk_mode = False
        self._bulk_source_genre = None
        self.selected = None
        self._populate_shelf()  # stay on the original/source tab
        self._draw_preview()
        self._refresh_stats()
        self._update_bulk_bar()

        if hasattr(self, "_info_var"):
            msg = f"Bulk moved {len(moved)} movie(s) from {src_genre} to {target_genre}."
            if failed:
                msg += f" {len(failed)} skipped."
            self._info_var.set(msg)
        print(f"[BulkMove] moved={len(moved)} failed={len(failed)} target={target_genre}")

    # ── Quick move between genres ─────────────────────────────────
    def _find_slot_record(self, genre, bkg_tex):
        """Return (dt_name, slots, slot, index, base_count) for a regular movie."""
        dt_name = GENRE_DATATABLE.get(genre)
        if not dt_name:
            return None, None, None, None, 0
        slots = CLEAN_DT_SLOT_DATA.get(dt_name, [])
        base_count = GENRES.get(genre, {}).get("bkg", 0)
        for idx, slot in enumerate(slots):
            if slot.get("bkg_tex") == bkg_tex:
                return dt_name, slots, slot, idx, base_count
        return dt_name, slots, None, None, base_count

    def _next_custom_bkg_for_genre(self, genre):
        """Return (slot_number, bkg_tex) for the lowest free custom slot in genre."""
        info = GENRES.get(genre, {})
        code = info.get("code")
        dt_name = GENRE_DATATABLE.get(genre)
        if not code or dt_name not in CLEAN_DT_SLOT_DATA:
            return None, None
        slots = CLEAN_DT_SLOT_DATA.get(dt_name, [])
        base_count = info.get("bkg", 0)
        prefix = f"T_Bkg_{code}_"
        used_nums = set()
        for slot in slots[base_count:]:
            bkg = slot.get("bkg_tex", "")
            if bkg.startswith(prefix):
                try:
                    used_nums.add(int(bkg[len(prefix):]))
                except ValueError:
                    pass
        slot_num = 1
        while slot_num in used_nums:
            slot_num += 1
        bkg_max = info.get("bkg_max")
        if bkg_max and slot_num > bkg_max:
            return None, None
        bkg_tex = (f"T_Bkg_{code}_{slot_num:03d}" if slot_num < 100
                   else f"T_Bkg_{code}_{slot_num}")
        return slot_num, bkg_tex

    def _open_move_genre_picker(self, texture=None, root_x=None, root_y=None):
        """Right-click quick picker: move a custom regular movie to another genre."""
        tex = texture or self.selected
        if not tex or tex.get("type") == "New Release":
            return

        # Flush a just-typed title before moving so the slot carries the newest text.
        if hasattr(self, '_title_debounce_id') and self._title_debounce_id:
            try:
                self.root.after_cancel(self._title_debounce_id)
            except Exception:
                pass
            self._title_debounce_id = None
            self._save_inline_title()

        src_genre = tex.get("genre")
        src_name = tex.get("name")
        dt_name, slots, slot, idx, base_count = self._find_slot_record(src_genre, src_name)
        if slot is None:
            messagebox.showinfo("Not Movable",
                "This movie is not in the editable movie table.", parent=self.root)
            return
        if idx is not None and idx < base_count:
            messagebox.showinfo("Protected Slot",
                "Base-game movie slots cannot be moved safely.\n\n"
                "Create a custom movie slot first, then move that custom movie between genres.",
                parent=self.root)
            return

        genres = [g for g in GENRES
                  if g not in HIDDEN_GENRES
                  and g != src_genre
                  and GENRE_DATATABLE.get(g) in CLEAN_DT_SLOT_DATA]
        if not genres:
            return

        cols = 3
        rows = (len(genres) + cols - 1) // cols
        dlg_w = 540
        dlg_h = max(260, 138 + rows * 34)

        dlg = tk.Toplevel(self.root)
        title = slot.get("pn_name") or src_name
        dlg.title("Move Movie — Choose Genre")
        dlg.configure(bg=C["bg"])
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.bind("<Escape>", lambda e: dlg.destroy())

        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()
        if root_x is None or root_y is None:
            root_x = self.root.winfo_rootx() + 140
            root_y = self.root.winfo_rooty() + 120
        x = max(0, min(int(root_x), sw - dlg_w - 20))
        y = max(0, min(int(root_y), sh - dlg_h - 60))
        dlg.geometry(f"{dlg_w}x{dlg_h}+{x}+{y}")

        tk.Label(dlg, text="Move movie to genre:", font=_vcr(11),
                 fg=C["text"], bg=C["bg"]).pack(pady=(12, 2))
        tk.Label(dlg, text=f"{title}   •   currently {src_genre}", font=_vcr(8),
                 fg=DS["text3"], bg=C["bg"]).pack(pady=(0, 10))

        genre_frame = tk.Frame(dlg, bg=C["bg"])
        genre_frame.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 8))
        for c in range(cols):
            genre_frame.grid_columnconfigure(c, weight=1, uniform="move_genres")

        chosen = [None]
        for i, g in enumerate(genres):
            color = GENRE_COLORS.get(g, {})
            bg = color.get("bg", C["card"])
            fg = color.get("fg", C["text"])
            btn = tk.Button(genre_frame, text=g, font=_vcr(10),
                            bg=bg, fg=fg, activebackground=DS["cyan"],
                            activeforeground=DS["text_inv"], relief=tk.FLAT,
                            cursor="hand2", pady=5,
                            command=lambda genre=g: (chosen.__setitem__(0, genre),
                                                     dlg.destroy()))
            btn.grid(row=i // cols, column=i % cols, sticky="ew", padx=4, pady=3)

        tk.Button(dlg, text="Cancel", font=_vcr(9),
                  bg=C["border"], fg=C["text_dim"], relief=tk.FLAT,
                  command=dlg.destroy).pack(pady=(0, 10))

        dlg.focus_force()
        dlg.grab_set()
        self.root.wait_window(dlg)
        if chosen[0]:
            self._move_movie_to_genre(tex, chosen[0])

    def _move_movie_to_genre(self, texture, target_genre,
                             switch_tab=True, select_moved=True,
                             refresh=True, commit=True, show_status=True):
        """Move a custom regular movie to another genre, preserving its settings.

        switch_tab/select_moved keep the original quick right-click behaviour.
        Bulk mode calls this with refresh=False/commit=False so many moves can be
        written in one batch without repeatedly rebuilding the shelf and JSON.
        """
        if not texture or not target_genre:
            return
        src_genre = texture.get("genre")
        old_name = texture.get("name")
        if src_genre == target_genre:
            return

        src_dt, src_slots, slot, src_idx, src_base = self._find_slot_record(src_genre, old_name)
        if slot is None:
            if show_status:
                messagebox.showerror("Move Failed", "Could not find the source movie slot.", parent=self.root)
            return
        if src_idx is not None and src_idx < src_base:
            if show_status:
                messagebox.showinfo("Protected Slot",
                    "Base-game movie slots cannot be moved safely.", parent=self.root)
            return

        target_dt = GENRE_DATATABLE.get(target_genre)
        if target_dt not in CLEAN_DT_SLOT_DATA:
            if show_status:
                messagebox.showerror("Move Failed", f"{target_genre} is not editable.", parent=self.root)
            return

        new_num, new_name = self._next_custom_bkg_for_genre(target_genre)
        if not new_name:
            if show_status:
                messagebox.showerror("Move Failed",
                    f"{target_genre} has no free custom movie slots.", parent=self.root)
            return

        old_sku = slot.get("sku", 0)
        last2 = old_sku % 100 if old_sku else STAR_OPTIONS.get("4.5 ★  Good Critic", 93)
        rarity = sku_to_rarity(old_sku) if old_sku else "Common"
        used = _all_used_skus()
        if old_sku in used:
            used.remove(old_sku)
        new_sku = generate_sku(target_genre, new_num, last2=last2,
                               rarity=rarity, used_skus=used)

        moved_slot = dict(slot)
        moved_slot["bkg_tex"] = new_name
        moved_slot["sku"] = new_sku
        moved_slot["ls"] = FIXED_REGULAR_LAYOUT
        moved_slot["last_edited_at"] = _now_iso()

        # Remove from source, append to target.  Appending keeps custom slots after
        # protected base slots, preserving the clean-DataTable save model.
        src_slots.pop(src_idx)
        CLEAN_DT_SLOT_DATA[target_dt].append(moved_slot)

        # The cover/crop settings are keyed by bkg_tex, so move the key without
        # touching the original image file path.
        if old_name in self.replacements:
            self.replacements[new_name] = self.replacements.pop(old_name)
            if commit:
                save_replacements(self.replacements)

        # A genre move changes pak paths and the DataTable row, so the new slot must
        # be built again even if the old slot had already shipped.
        if old_name in self._shipped_slots:
            self._shipped_slots.discard(old_name)
            if commit:
                save_shipped_slots(self._shipped_slots)
        if old_name in self._edited_slots:
            self._edited_slots.discard(old_name)
        self._edited_slots.add(new_name)
        if commit:
            save_edited_slots(self._edited_slots)
        if hasattr(self, "_pending_edit_slots"):
            self._pending_edit_slots.discard(old_name)
            self._pending_edit_slots.add(new_name)

        moved_title = moved_slot.get("pn_name", new_name)
        result = {
            "title": moved_title,
            "src_genre": src_genre,
            "target_genre": target_genre,
            "old_name": old_name,
            "new_name": new_name,
            "new_sku": new_sku,
        }

        if commit:
            save_custom_slots()
            rebuild_texture_list()
            invalidate_slot_lookup_cache()
            self.dt_manager._clean_builders.clear()
            self.dt_manager._titles_cache.clear()

        if refresh:
            if switch_tab:
                # Original quick-move behaviour: follow the movie to its new genre.
                self._genre_var.set(target_genre)
                self._update_tab_colors(target_genre)
                self._populate_shelf()
                new_tex = next((t for t in ALL_TEXTURES if t.get("name") == new_name), None)
                if new_tex and select_moved:
                    self._on_select_texture(new_tex)
                self._update_sticky_add_btn(target_genre)
            else:
                # Bulk/background behaviour: stay exactly where the user is.
                self._refresh_shelf_keep_scroll()
                self._update_sticky_add_btn(self._genre_var.get())
            self._refresh_stats()

        if show_status:
            move_msg = (f"Moved '{moved_title}' from {src_genre} to {target_genre} "
                        f"({new_name}, SKU {new_sku} → {sku_display(new_sku)})")
            print(f"[MoveGenre] {move_msg}")
            if hasattr(self, "_info_var"):
                self._info_var.set(move_msg)
        return result

    def _delete_selected_slot(self):
        """Route delete to the correct handler based on current selection."""
        if getattr(self, '_selected_nr_idx', -1) >= 0:
            self._delete_nr_slot()
        else:
            self._delete_custom_slot_confirm()

    def _delete_custom_slot_confirm(self):
        """Ask for confirmation before deleting."""
        if not self.selected:
            return
        name  = self.selected["name"]
        dt    = GENRE_DATATABLE.get(self.selected["genre"])
        title = "this movie"
        if dt:
            for s in CLEAN_DT_SLOT_DATA.get(dt, []):
                if s.get("bkg_tex") == name:
                    title = s.get("pn_name", name)
                    break
        ans = messagebox.askyesno(
            "Delete Movie",
            f'Delete "{title}"?\nThis cannot be undone.',
            icon="warning", parent=self.root)
        if ans:
            self._delete_custom_slot()

    def _delete_custom_slot(self):
        if not self.selected:
            return
        name  = self.selected["name"]
        genre = self.selected["genre"]
        dt    = GENRE_DATATABLE.get(genre)
        if not dt:
            return
        genre_info  = GENRES.get(genre, {})
        base_count  = genre_info.get("bkg", 0)
        slots       = CLEAN_DT_SLOT_DATA.get(dt, [])
        slot        = next((s for s in slots if s.get("bkg_tex") == name), None)
        if slot is None:
            return
        idx = slots.index(slot)
        if idx < base_count:
            messagebox.showinfo("Protected Slot",
                "Base-game movie slots cannot be deleted.\n"
                "You can clear the custom cover art with 'Remove Cover Art'.",
                parent=self.root)
            return
        slots.remove(slot)
        # Remove from texture list, replacements, and tracking state.
        # Shipped/edited sets are append-only at build time; explicit cleanup
        # here keeps deleted slots from re-persisting on the next build.
        if name in self.replacements:
            del self.replacements[name]
            save_replacements(self.replacements)
        if name in self._shipped_slots:
            self._shipped_slots.discard(name)
            save_shipped_slots(self._shipped_slots)
            print(f"[Movie] Removed shipped tracking for {name}")
        if name in self._edited_slots:
            self._edited_slots.discard(name)
            save_edited_slots(self._edited_slots)
        global ALL_TEXTURES
        ALL_TEXTURES = [t for t in ALL_TEXTURES if t["name"] != name]
        save_custom_slots()
        self.dt_manager._clean_builders.clear()
        self.dt_manager._titles_cache.clear()
        self.selected = None
        self._populate_shelf()
        self._draw_preview()

    # ── Remove all custom slots ───────────────────────────────
    def _clear_all_custom_confirm(self):
        """Ask confirmation then remove all custom movies."""
        from tkinter import messagebox as _mb
        # Count genre custom slots
        genre_count = sum(
            len([s for s in slots
                 if slots.index(s) >= (GENRES.get(
                     next((g for g,d in GENRE_DATATABLE.items() if d==dt),None),
                     {}).get("bkg",0))])
            for dt, slots in CLEAN_DT_SLOT_DATA.items()
        )
        nr_count = len(NR_SLOT_DATA)
        count = genre_count + nr_count
        if count == 0:
            _mb.showinfo("Nothing to Remove",
                "There are no custom movies to remove.", parent=self.root)
            return
        plural = "s" if count != 1 else ""
        parts = []
        if genre_count > 0:
            parts.append(f"{genre_count} genre movie{'' if genre_count == 1 else 's'}")
        if nr_count > 0:
            parts.append(f"{nr_count} new release{'' if nr_count == 1 else 's'}")
        desc = " and ".join(parts)
        ans = _mb.askyesno("Remove All Custom Movies",
            f"Remove {desc}?\nThis cannot be undone.",
            icon="warning", parent=self.root)
        if ans:
            self._clear_all_custom()

    def _clear_all_custom(self):
        # Remove all custom genre slots
        for genre, dt in GENRE_DATATABLE.items():
            base = GENRES.get(genre, {}).get("bkg", 0)
            if dt in CLEAN_DT_SLOT_DATA:
                CLEAN_DT_SLOT_DATA[dt] = CLEAN_DT_SLOT_DATA[dt][:base]
        # Remove all NR slots
        NR_SLOT_DATA.clear()
        save_nr_slots()
        # Clear all replacements (custom images)
        self.replacements.clear()
        save_replacements(self.replacements)
        # Clear shipped/edited tracking. New slots created after this point
        # reuse the same names (smallest-free tex_num picks 1 again on an
        # empty list, colliding with previously-shipped T_New_Dra_001 etc.),
        # so without clearing here the fresh entries would inherit stale
        # "shipped" / "edited" badges from the old slots.
        self._shipped_slots.clear()
        save_shipped_slots(self._shipped_slots)
        self._edited_slots = clear_edited_slots()
        # Clear cached images
        self._raw_img = None
        self._base_img = None
        # Rebuild texture list
        rebuild_texture_list()
        save_custom_slots()
        self.dt_manager._clean_builders.clear()
        self.dt_manager._titles_cache.clear()
        self.selected = None
        self._selected_nr_idx = -1
        self._populate_shelf()
        self._draw_preview()
        self._refresh_stats()
        self._update_nr_tab_badge()

    # ── Refresh stats bar ─────────────────────────────────────
    def _update_nr_tab_badge(self):
        """Update the New Releases tab badge count."""
        if "New Releases" in self._tab_badges:
            self._tab_badges["New Releases"].config(text=str(len(NR_SLOT_DATA)))

    def _refresh_stats(self):
        # Count custom movies across all genres
        custom_movies = 0
        for genre, info in GENRES.items():
            if genre in HIDDEN_GENRES:
                continue
            dt = GENRE_DATATABLE.get(genre)
            if dt:
                base_count = info.get("bkg", 0)
                total = len(CLEAN_DT_SLOT_DATA.get(dt, []))
                custom_movies += max(0, total - base_count)
        nr_count = len(NR_SLOT_DATA)
        total_movies = custom_movies + nr_count
        # 999 per genre (12 visible genres) + 999 NR
        genre_count = sum(1 for g in GENRES if g not in HIDDEN_GENRES)
        max_total = genre_count * 999 + 999
        self._stats_var.set(f"Movies: {total_movies} / {max_total}")

    # ── Sort dropdown UI (added v1.8.2) ───────────────────────
    # See the SORT FEATURE block at module top (near SORT_OPTIONS) for the
    # full feature spec. Public surface from this group:
    #
    #   _format_sort_btn_label  — label text from a sort key
    #   _refresh_sort_btn_label — update trigger button text after change
    #   _show_sort_control      — show / hide the entire sort area
    #   _toggle_sort_dropdown   — open/close the popup (bound to the click)
    #   _open_sort_dropdown     — internal: build and position the popup
    #   _close_sort_dropdown    — internal: tear down popup + bindings
    #   _on_sort_selected       — internal: handle option click
    #   _sort_animate_reorder   — brief dim + repopulate transition
    def _format_sort_btn_label(self, sort_key: str) -> str:
        """Build the trigger-button text from a sort key (e.g. 'Created at  ▴')."""
        opt = _get_sort_option(sort_key)
        return f"{opt['label']}  {opt['arrow']}"

    def _refresh_sort_btn_label(self):
        """Update the trigger button text to reflect the current tab's sort.

        Reads the saved sort key for the active tab from self._sort_prefs
        — the active tab can be a genre name or "New Releases" — and renders
        the label + arrow combo on the trigger button.
        """
        if not hasattr(self, "_sort_btn"):
            return
        genre = self._genre_var.get()
        sort_key = self._sort_prefs.get(genre, DEFAULT_SORT_KEY)
        self._sort_btn.config(text=self._format_sort_btn_label(sort_key))

    def _show_sort_control(self, visible: bool):
        """Show or hide the entire sort control (label + button).

        Hidden only on the "All Movies" tab — that's a multi-genre overview
        and doesn't have a single sort context. Shown on every single-genre
        tab and on the "New Releases" tab; each tab keeps its own sort
        preference in self._sort_prefs (persisted to sort_preferences.json).
        """
        if not hasattr(self, "_sort_control_frame"):
            return
        if visible:
            if not self._sort_control_frame.winfo_ismapped():
                self._sort_control_frame.pack(side=tk.RIGHT)
        else:
            self._sort_control_frame.pack_forget()
            self._close_sort_dropdown()

    def _toggle_sort_dropdown(self):
        """Open or close the sort dropdown popup."""
        if getattr(self, "_sort_dropdown_open", False):
            self._close_sort_dropdown()
        else:
            self._open_sort_dropdown()

    def _open_sort_dropdown(self):
        """Show a Toplevel popup styled like a native dropdown.

        Layout: positioned with its top-left aligned so the popup's right
        edge meets the trigger button's right edge, top edge sits 2px below
        the button. Six rows in three pairs (Name / Created at / Last edited)
        with thin dividers between pairs. Currently selected option is
        highlighted in cyan; hover gives a subtle brighter background.

        Implementation notes:
          - We use overrideredirect + withdraw + deiconify because Windows
            otherwise ignores the position part of geometry() when a window
            is mid-transition between decorated and undecorated state. The
            "withdraw, set geometry, deiconify" pattern shows the popup at
            the correct coordinates in one frame.
          - Click-outside-to-close is bound app-wide on root with a small
            timestamp guard so the same click that OPENED the popup doesn't
            bubble back to the handler and immediately close it.
        """
        if getattr(self, "_sort_dropdown_open", False):
            return

        # Resolve the trigger button's screen position so we can pin the popup.
        btn = self._sort_btn
        btn.update_idletasks()
        bx = btn.winfo_rootx()
        by = btn.winfo_rooty()
        bw = btn.winfo_width()
        bh = btn.winfo_height()

        # Build the popup window — withdraw it first so layout work happens
        # off-screen and the user only sees the final positioned popup.
        pop = tk.Toplevel(self.root)
        pop.withdraw()
        pop.overrideredirect(True)
        pop.transient(self.root)
        pop.configure(bg=DS["border"])     # acts as a 1px outline
        pop.attributes("-topmost", True)

        inner = tk.Frame(pop, bg=DS["panel"])
        inner.pack(padx=1, pady=1)

        genre = self._genre_var.get()
        current_key = self._sort_prefs.get(genre, DEFAULT_SORT_KEY)

        # SORT_OPTIONS is defined as (asc, desc) per field. The visual spec
        # shows descending arrow (▾) above ascending (▴) within each pair,
        # so swap the order for display.
        display_pairs = [
            (SORT_OPTIONS[1], SORT_OPTIONS[0]),  # Name        ▾, ▴
            (SORT_OPTIONS[3], SORT_OPTIONS[2]),  # Created at  ▾, ▴
            (SORT_OPTIONS[5], SORT_OPTIONS[4]),  # Last edited ▾, ▴
        ]

        def _make_row(parent, opt):
            """Build one option row with hover, click, and active highlight."""
            is_active = (opt["key"] == current_key)
            normal_bg = "#1A2230" if is_active else DS["panel"]
            hover_bg  = "#243149" if is_active else "#161D27"
            text_fg   = DS["cyan"] if is_active else DS["text"]

            row = tk.Frame(parent, bg=normal_bg, cursor="hand2")
            row.pack(fill=tk.X)

            lbl = tk.Label(row, text=opt["label"],
                           font=_vcr(10, bold=is_active),
                           fg=text_fg, bg=normal_bg, anchor="w", padx=12, pady=6)
            lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

            arr = tk.Label(row, text=opt["arrow"],
                           font=_vcr(10, bold=is_active),
                           fg=text_fg, bg=normal_bg, padx=12)
            arr.pack(side=tk.RIGHT)

            def _enter(_e=None):
                for w_ in (row, lbl, arr):
                    w_.config(bg=hover_bg)

            def _leave(_e=None):
                for w_ in (row, lbl, arr):
                    w_.config(bg=normal_bg)

            def _click(_e=None):
                self._on_sort_selected(opt["key"])

            for w_ in (row, lbl, arr):
                w_.bind("<Enter>", _enter)
                w_.bind("<Leave>", _leave)
                w_.bind("<Button-1>", _click)

        for pair_idx, (top, bottom) in enumerate(display_pairs):
            _make_row(inner, top)
            _make_row(inner, bottom)
            # Thin separator between pairs (none after the last pair)
            if pair_idx < len(display_pairs) - 1:
                tk.Frame(inner, bg=DS["divider"], height=1).pack(fill=tk.X)

        # Resolve the popup's required size (winfo_reqwidth/height work
        # before the window is mapped; winfo_width returns 1 in that state).
        pop.update_idletasks()
        pw = pop.winfo_reqwidth()
        ph = pop.winfo_reqheight()

        # Right-align: popup's right edge aligned to button's right edge,
        # top sits 2px below the button.
        x = bx + bw - pw
        y = by + bh + 2

        # Set BOTH size and position while withdrawn, then show.
        pop.geometry(f"{pw}x{ph}+{x}+{y}")
        pop.deiconify()
        pop.lift()

        self._sort_dropdown = pop
        self._sort_dropdown_open = True

        # Click-outside-to-close. We bind app-wide on root rather than
        # per-widget because we want clicks anywhere outside the popup to
        # close it. Timestamp guard skips events within 200ms of opening,
        # which prevents the original trigger click — still bubbling
        # through the queue — from being seen as an outside click.
        import time as _t
        self._sort_open_at = _t.monotonic()

        def _outside(event):
            if _t.monotonic() - self._sort_open_at < 0.2:
                return
            try:
                w_target = pop.winfo_containing(event.x_root, event.y_root)
            except Exception:
                w_target = None
            # Walk up the widget tree — if we find the popup, click was inside.
            p = w_target
            while p is not None:
                if p is pop:
                    return
                p = p.master if hasattr(p, "master") else None
            self._close_sort_dropdown()

        self._sort_outside_bind_id = self.root.bind_all(
            "<Button-1>", _outside, add="+")
        self._sort_escape_bind_id = self.root.bind_all(
            "<Escape>", lambda e: self._close_sort_dropdown(), add="+")

    def _close_sort_dropdown(self):
        """Tear down the sort dropdown popup and release its bindings.

        Safe to call even if the popup isn't currently open. Called from
        _on_sort_selected (after a choice is made), the click-outside
        handler, the Escape handler, and on tab/genre changes.
        """
        if not getattr(self, "_sort_dropdown_open", False):
            return
        # Release the app-wide button-1 / escape bindings. unbind_all is safe
        # here because no other code in this app installs app-wide handlers
        # for these events.
        try:
            if getattr(self, "_sort_outside_bind_id", None):
                self.root.unbind_all("<Button-1>")
                self._sort_outside_bind_id = None
        except Exception:
            pass
        try:
            if getattr(self, "_sort_escape_bind_id", None):
                self.root.unbind_all("<Escape>")
                self._sort_escape_bind_id = None
        except Exception:
            pass
        try:
            if getattr(self, "_sort_dropdown", None):
                self._sort_dropdown.destroy()
        except Exception:
            pass
        self._sort_dropdown = None
        self._sort_dropdown_open = False

    def _on_sort_selected(self, sort_key: str):
        """Apply a user-chosen sort option for the active tab.

        Persists the choice to sort_preferences.json (per-tab — the active
        tab can be a genre name or "New Releases"), updates the trigger
        button label, and triggers the dim/reorder animation. Does nothing
        if the user picked the option that's already active.
        """
        self._close_sort_dropdown()
        genre = self._genre_var.get()
        if self._sort_prefs.get(genre, DEFAULT_SORT_KEY) == sort_key:
            return
        self._sort_prefs[genre] = sort_key
        save_sort_prefs(self._sort_prefs)
        self._refresh_sort_btn_label()
        self._sort_animate_reorder()

    def _sort_animate_reorder(self):
        """Brief dim → repopulate → un-dim transition (~150ms total).

        Tkinter has no native alpha so we approximate a fade by briefly
        switching the shelf canvas background to a darker tone, repopulating
        the shelf with the new sort applied, then restoring the normal color.
        Timing: 80ms dim → repopulate → 70ms restore.
        """
        try:
            canvas = self._shelf_canvas
            normal_bg = canvas.cget("bg")
            dim_bg = "#0A0A0A"   # slightly darker than the normal canvas bg
            canvas.config(bg=dim_bg)
            self._shelf_frame.config(bg=dim_bg)
        except Exception:
            normal_bg = None

        def _phase2():
            # Re-render with new sort applied
            try:
                self._populate_shelf()
            except Exception as e:
                print(f"[sort] populate error: {e}")
            # Restore colors after a short delay so the dim is perceptible
            def _restore():
                try:
                    if normal_bg is not None:
                        self._shelf_canvas.config(bg=normal_bg)
                        self._shelf_frame.config(bg=normal_bg)
                except Exception:
                    pass
            self.root.after(70, _restore)

        self.root.after(80, _phase2)

    # ── Populate Shelf (tile gallery) ─────────────────────────
    def _populate_shelf(self, *args):
        self._genre_header_widgets = []
        for w in self._shelf_frame.winfo_children():
            w.destroy()

        genre  = self._genre_var.get()
        search = self._search_var.get().lower()
        filt   = self._filter_var.get()

        # Reset the render cap only when the visible query really changes. This
        # keeps "Load more" state while selection/highlight updates preserve scroll.
        query_sort = self._sort_prefs.get(genre, DEFAULT_SORT_KEY) if hasattr(self, "_sort_prefs") else DEFAULT_SORT_KEY
        query_key = (genre, search, filt, query_sort, len(ALL_TEXTURES), len(NR_SLOT_DATA))
        if getattr(self, "_last_shelf_query", None) != query_key:
            self._shelf_render_limit = SHELF_RENDER_CHUNK
            self._last_shelf_query = query_key
        render_limit = int(getattr(self, "_shelf_render_limit", SHELF_RENDER_CHUNK))

        self.filtered = []
        for t in ALL_TEXTURES:
            name = t.get("name", "")
            tgenre = t.get("genre", "")
            if tgenre in HIDDEN_GENRES:
                continue

            slot, slot_idx = _slot_for_texture(t)

            # In custom-only mode: hide base T_Bkg slots (keep T_New + custom slots).
            if CUSTOM_ONLY_MODE and name.startswith("T_Bkg_"):
                base_count = GENRES.get(tgenre, {}).get("bkg", 0)
                if slot is None or slot_idx < base_count:
                    continue

            if genre != "All Movies" and tgenre != genre:
                continue

            if search:
                title = (slot.get("pn_name", "") if slot else "")
                if (search not in name.lower()
                        and search not in tgenre.lower()
                        and search not in title.lower()):
                    continue

            has_img = name in self.replacements
            if filt == "Replaced" and not has_img:
                continue
            if filt == "Empty" and has_img:
                continue
            self.filtered.append(t)

        total_visible = 0

        # Group by genre section if "All Movies" — collapsible headers.
        # The headers stay cheap; movie rows are chunked globally.
        if genre == "All Movies":
            groups = {}
            for t in self.filtered:
                groups.setdefault(t["genre"], []).append(t)

            if not hasattr(self, "_genre_collapsed"):
                self._genre_collapsed = {}  # genre -> bool

            remaining = render_limit
            expanded_total = 0
            for g in [gg for gg in GENRES if gg not in HIDDEN_GENRES]:
                genre_movies = groups.get(g, [])
                collapsed  = self._genre_collapsed.get(g, False)
                chevron    = "▸" if collapsed else "▾"

                # Clickable header row with genre color accent
                gc = GENRE_COLORS.get(g, {})
                accent_color = gc.get("bg", DS["border"])
                text_color = _lighten_color(accent_color) if gc else DS["text2"]
                hdr_bg = "#1a1a1a"

                hdr_f = tk.Frame(self._shelf_frame, bg=hdr_bg,
                                 cursor="hand2")
                hdr_f.pack(fill=tk.X, pady=(1, 0))
                # Colored left accent bar
                accent_bar = tk.Frame(hdr_f, bg=accent_color, width=4)
                accent_bar.pack(side=tk.LEFT, fill=tk.Y)
                hdr_inner = tk.Frame(hdr_f, bg=hdr_bg)
                hdr_inner.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, SP[3]), pady=SP[2])
                tk.Label(hdr_inner, text=f"{g.upper()}",
                         font=_f(FS["body"], bold=True), fg=text_color,
                         bg=hdr_bg).pack(side=tk.LEFT)
                # Count badge
                badge_f = tk.Frame(hdr_inner, bg=DS["border"], padx=6, pady=1)
                badge_f.pack(side=tk.LEFT, padx=(8, 0))
                tk.Label(badge_f, text=str(len(genre_movies)),
                         font=_f(FS["meta"]), fg=DS["text3"],
                         bg=DS["border"]).pack()
                # Chevron right-aligned
                tk.Label(hdr_inner, text=chevron,
                         font=_f(FS["body"]), fg=DS["text3"],
                         bg=hdr_bg).pack(side=tk.RIGHT, padx=(0, 4))
                # Store genre info on the frame for sticky header
                hdr_f._genre_name = g
                hdr_f._accent_color = accent_color
                hdr_f._text_color = text_color
                hdr_f._count = len(genre_movies)
                hdr_f._collapsed = collapsed

                # Body frame — hidden when collapsed
                body_f = tk.Frame(self._shelf_frame, bg=DS["bg"])
                if not collapsed:
                    body_f.pack(fill=tk.X)

                # Track header widget for sticky header
                if not hasattr(self, '_genre_header_widgets'):
                    self._genre_header_widgets = []
                self._genre_header_widgets.append(hdr_f)

                def _toggle(event=None, genre_key=g, frame=body_f, hdr=hdr_inner):
                    self._genre_collapsed[genre_key] = \
                        not self._genre_collapsed.get(genre_key, False)
                    self._populate_shelf()

                for w in (hdr_f, hdr_inner) + tuple(hdr_inner.winfo_children()):
                    try: w.bind("<Button-1>", _toggle)
                    except Exception: pass
                    if hasattr(self, "_shelf_enter_fn"):
                        try:
                            w.bind("<Enter>", self._shelf_enter_fn)
                            w.bind("<Leave>", self._shelf_leave_fn)
                        except Exception: pass

                if not collapsed:
                    expanded_total += len(genre_movies)
                    take = max(0, min(len(genre_movies), remaining))
                    if take:
                        self._add_tile_row(body_f, genre_movies[:take])
                        total_visible += take
                        remaining -= take
                    # Keep add-row access cheap. Only one per genre, so it is safe even
                    # when movie rows are chunked.
                    self._make_add_movie_row(body_f, g)

            if expanded_total > total_visible:
                self._make_load_more_row(self._shelf_frame, total_visible, expanded_total)

        elif genre == "New Releases":
            # Show NR slots as a simple list, sorted per persisted preference.
            # The original NR_SLOT_DATA index must be preserved through sorting
            # because the click handler uses it to call _select_nr_slot(idx).
            sel_idx = getattr(self, '_selected_nr_idx', -1)
            sort_key = self._sort_prefs.get("New Releases", DEFAULT_SORT_KEY)
            sorted_nrs = _sort_nr_slots(list(enumerate(NR_SLOT_DATA)), sort_key)

            if sel_idx >= 0:
                for pos, (idx, _nr) in enumerate(sorted_nrs):
                    if idx == sel_idx and pos >= render_limit:
                        render_limit = pos + 1
                        self._shelf_render_limit = render_limit
                        break

            visible_nrs = sorted_nrs[:render_limit]
            for idx, nr in visible_nrs:
                is_sel = (idx == sel_idx)
                row_bg = DS["panel"] if is_sel else DS["surface"]
                row_f = tk.Frame(self._shelf_frame, bg=row_bg,
                                 cursor="hand2",
                                 highlightthickness=1 if is_sel else 0,
                                 highlightbackground=DS["cyan"])
                row_f._nr_row_idx = idx
                row_f.pack(fill=tk.X, padx=SP[2], pady=1)
                # Genre badge with game colors
                _gc = GENRE_COLORS.get(nr.get("genre", ""), {})
                _badge_bg = _gc.get("bg", DS["border"])
                _badge_fg = _gc.get("fg", DS["text3"])
                tk.Label(row_f, text=f" {nr['genre'][:3].upper()} ",
                         font=_f(FS["meta"], bold=True),
                         fg=_badge_fg, bg=_badge_bg
                         ).pack(side=tk.LEFT, padx=(SP[2],SP[1]), pady=SP[1])
                # Title
                title_fg = DS["cyan"] if is_sel else DS["text"]
                tk.Label(row_f, text=nr["title"],
                         font=_f(FS["body"]), fg=title_fg,
                         bg=row_bg, anchor=tk.W
                         ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=SP[1])

                # Click to select
                def _sel(event=None, i=idx):
                    self._select_nr_slot(i)
                for w in [row_f] + row_f.winfo_children():
                    w.bind("<Button-1>", _sel)
                    if hasattr(self, "_shelf_enter_fn"):
                        try:
                            w.bind("<Enter>", self._shelf_enter_fn)
                            w.bind("<Leave>", self._shelf_leave_fn)
                        except Exception: pass

            if len(sorted_nrs) > len(visible_nrs):
                self._make_load_more_row(self._shelf_frame, len(visible_nrs), len(sorted_nrs))

        else:
            # Single-genre tab: apply the tab's saved sort preference.
            # NR tab is handled in its own branch above, this is genre-only.
            sort_key = self._sort_prefs.get(genre, DEFAULT_SORT_KEY)
            sorted_filtered = _sort_textures(self.filtered, sort_key)

            # If the selected movie is outside the first chunk, expand just enough
            # to keep it visible after edits/sort changes.
            if self.selected:
                sel_name = self.selected.get("name")
                for pos, tex in enumerate(sorted_filtered):
                    if tex.get("name") == sel_name and pos >= render_limit:
                        render_limit = pos + 1
                        self._shelf_render_limit = render_limit
                        break

            visible = sorted_filtered[:render_limit]
            self._add_tile_row(self._shelf_frame, visible)
            if len(sorted_filtered) > len(visible):
                self._make_load_more_row(self._shelf_frame, len(visible), len(sorted_filtered))

        # Refresh tab counts and sticky button
        self._update_tab_colors(self._genre_var.get())
        self._update_sticky_add_btn()
        if hasattr(self, "_update_bulk_bar"):
            self._update_bulk_bar()
        self._refresh_stats()
        # Update sticky header after shelf rebuild
        if hasattr(self, '_update_sticky_header'):
            self.root.after(50, self._update_sticky_header)
        # Restore scroll position if we have one saved, otherwise reset to top.
        # _populate_shelf is called both on genre switch (should reset) and on
        # item click/selection (should preserve position).
        if hasattr(self, '_shelf_scroll_save'):
            self._shelf_canvas.update_idletasks()
            self._shelf_canvas.yview_moveto(self._shelf_scroll_save)
            del self._shelf_scroll_save
        else:
            self._shelf_canvas.yview_moveto(0)

    def _add_tile_row(self, parent, textures):
        """Add VHS spine rows — one full-width row per texture."""
        for t in textures:
            self._make_spine_row(parent, t)

    def _make_load_more_row(self, parent, shown, total):
        """Render a cheap non-blocking row that expands the shelf in chunks."""
        hidden = max(0, total - shown)
        if hidden <= 0:
            return
        ROW_H = 34
        row = tk.Canvas(parent, bg=DS["bg"], height=ROW_H,
                        bd=0, highlightthickness=0, cursor="hand2")
        row.pack(fill=tk.X, padx=6, pady=(6, SP[2]))

        def _draw(e=None, hover=False):
            row.delete("all")
            w = row.winfo_width() or 240
            bg_fill  = "#071a1a" if hover else DS["panel"]
            bdr_color = DS["cyan"] if hover else DS["border"]
            next_count = min(SHELF_RENDER_CHUNK, hidden)
            row.create_rectangle(0, 0, w, ROW_H, fill=bg_fill, outline=bdr_color)
            row.create_text(w // 2, ROW_H // 2,
                            text=f"Load {next_count} more movies  ({shown}/{total} shown)",
                            font=_f(FS["body"], bold=True),
                            fill=DS["cyan"], anchor=tk.CENTER)

        def _load_more(_e=None):
            try:
                self._shelf_scroll_save = self._shelf_canvas.yview()[0]
            except Exception:
                pass
            self._shelf_render_limit = min(total, int(getattr(self, "_shelf_render_limit", SHELF_RENDER_CHUNK)) + SHELF_RENDER_CHUNK)
            self._populate_shelf()

        row.bind("<Configure>", _draw)
        row.bind("<Enter>", lambda e: _draw(hover=True))
        row.bind("<Leave>", lambda e: _draw(hover=False))
        row.bind("<Button-1>", _load_more)
        if hasattr(self, "_shelf_enter_fn"):
            row.bind("<Enter>", self._shelf_enter_fn, add="+")
            row.bind("<Leave>", self._shelf_leave_fn, add="+")

    def _make_tile(self, parent, texture, w=0, h=0):
        """Compatibility shim — delegates to spine row."""
        self._make_spine_row(parent, texture)
        return tk.Frame(parent)   # dummy return for old callers

    def _is_placeholder_title(self, title, name):
        """True if the title is a default/placeholder that needs user attention."""
        if not title:
            return True
        # Default title from add_movie_slot
        if title.strip() == "New Movie":
            return True
        # Raw bkg_tex names or auto-generated slot names
        if title.startswith(("T_Bkg_", "T_New_", "LS0", "LS1", "LS2")):
            return True
        # Matches the texture name exactly
        if title == name:
            return True
        return False

    def _make_spine_row(self, parent, texture):
        """Create one horizontal VHS-spine-style row."""
        name    = texture["name"]
        genre   = texture["genre"]
        has_img = name in self.replacements
        is_sel  = (self.selected == texture)
        bulk_mode = bool(getattr(self, "_bulk_mode", False))
        bulk_selected = bulk_mode and name in getattr(self, "_bulk_selected", set())

        base_count = GENRES.get(genre, {}).get("bkg", 0)
        slot, slot_idx = _slot_for_texture(texture)
        is_custom = bool(slot is not None and slot_idx >= base_count)
        title = slot.get("pn_name", "") if slot else ""

        is_placeholder = self._is_placeholder_title(title, name)

        # ── Colour logic (3-channel status system) ──────────────
        # Dimension 1 — Cover status → left color bar (green/red)
        # Dimension 2 — Build status → right badge (EDITED/UNSHIPPED/none)
        # Dimension 3 — Data completeness → left amber dot for placeholder
        has_cover = has_img
        is_edited = hasattr(self, "_edited_slots") and name in self._edited_slots
        is_shipped = hasattr(self, "_shipped_slots") and name in self._shipped_slots

        if is_sel:
            text_fg = DS["cyan"]
        else:
            text_fg = DS["text"]

        row_bg = "#0E2222" if bulk_selected else DS["panel"]
        cover_bar_color = "#2a6b2a" if has_cover else "#6b2a2a"

        ROW_H = 32

        row_canvas = tk.Canvas(parent, bg=row_bg, height=ROW_H,
                               bd=0, highlightthickness=1 if (is_sel or bulk_selected) else 0,
                               highlightbackground=DS["gold"] if bulk_selected else DS["cyan"],
                               cursor="hand2")
        row_canvas._tex_name = name  # tag for lightweight highlight update
        row_canvas.pack(fill=tk.X, padx=6, pady=0)

        # Dimension 1: Cover status bar (3px left edge)
        row_canvas.create_rectangle(0, 0, 3, ROW_H, fill=cover_bar_color,
                                     outline="", tags="cover_bar")

        # Bulk selection checkbox/marker
        TEXT_X = 12
        if bulk_mode:
            row_canvas.create_text(
                TEXT_X, ROW_H // 2,
                text="☑" if bulk_selected else "☐",
                font=_f(FS["body"], bold=True),
                fill=DS["cyan"] if bulk_selected else DS["text3"],
                anchor=tk.W)
            TEXT_X += 20

        # Dimension 3: Amber dot for placeholder titles
        if is_placeholder:
            row_canvas.create_text(
                TEXT_X, ROW_H // 2,
                text="●", font=_f(FS["meta"]),
                fill=DS["gold"], anchor=tk.W)
            TEXT_X += 14

        # Title
        display_title = (title[:24] + "…") if len(title) > 25 else title
        if not display_title:
            display_title = name

        row_canvas.create_text(
            TEXT_X, ROW_H // 2,
            text=display_title,
            font=_f(FS["body"], bold=is_sel),
            fill=text_fg,
            anchor=tk.W, tags="title_text")

        # Dimension 2: Build status badge (right side)
        RIGHT_MARGIN = 10
        badge_x = 9999  # placeholder; fixed in _on_resize

        if not is_shipped:
            row_canvas.create_text(
                badge_x, ROW_H // 2,
                text="UNSHIPPED", font=_f(FS["meta"], bold=True),
                fill="#8B3333",
                anchor=tk.E, tags="badge_mod")
        elif is_edited:
            row_canvas.create_text(
                badge_x, ROW_H // 2,
                text="EDITED", font=_f(FS["meta"], bold=True),
                fill="#F5A623",
                anchor=tk.E, tags="badge_mod")

        if False and is_custom:  # disabled — replaced by build status badges
            row_canvas.create_text(
                badge_x, ROW_H // 2,
                text="NEW", font=_f(FS["meta"], bold=True),
                fill=DS["cyan"] if not is_sel else DS["text_inv"],
                anchor=tk.E, tags="badge_mod")

        def _on_resize(e, canvas=row_canvas, rm=RIGHT_MARGIN):
            w = e.width
            for tag, offset in [("badge_mod", rm)]:
                for item in canvas.find_withtag(tag):
                    coords = canvas.coords(item)
                    if coords:
                        canvas.coords(item, w - offset, ROW_H // 2)

        row_canvas.bind("<Configure>", _on_resize)

        def _click(e, tex=texture):
            if getattr(self, "_bulk_mode", False):
                self._bulk_toggle_texture(tex)
            else:
                self._on_select_texture(tex)
        row_canvas.bind("<Button-1>", _click)

        def _right_click(e, tex=texture):
            if getattr(self, "_bulk_mode", False):
                self._bulk_toggle_texture(tex)
                return
            # Select first so the details panel reflects the movie being moved,
            # then open the quick genre picker at the pointer position.
            self._on_select_texture(tex)
            self._open_move_genre_picker(tex, e.x_root, e.y_root)

        row_canvas.bind("<Button-3>", _right_click)
        row_canvas.bind("<Control-Button-1>", _right_click)  # macOS trackpads
        # Entering/leaving a row widget activates the root-level scroll binding
        if hasattr(self, "_shelf_enter_fn"):
            row_canvas.bind("<Enter>", self._shelf_enter_fn)
            row_canvas.bind("<Leave>", self._shelf_leave_fn)


    def _make_add_movie_row(self, parent, genre):
        """Full-width dashed 'Add movie to Genre' row at bottom of genre list."""
        if genre == "All Movies":
            return

        ROW_H = 28
        row = tk.Canvas(parent, bg=DS["bg"], height=ROW_H,
                        bd=0, highlightthickness=0, cursor="hand2")
        row.pack(fill=tk.X, padx=6, pady=(1, SP[2]))

        def _draw(e=None, hover=False):
            row.delete("all")
            w = row.winfo_width() or 220
            bg_fill  = "#071a1a" if hover else DS["bg"]
            bdr_color = DS["cyan"] if hover else DS["border"]
            row.create_rectangle(0, 0, w, ROW_H, fill=bg_fill, outline="")
            row.create_rectangle(2, 3, w-2, ROW_H-3,
                                 outline=bdr_color, fill="",
                                 dash=(4, 4), width=1)
            txt_color = DS["cyan"] if hover else DS["text3"]
            row.create_text(w // 2, ROW_H // 2,
                            text=f"Add movie to {genre}",
                            font=_f(FS["body"]),
                            fill=txt_color, anchor=tk.CENTER)

        row.bind("<Configure>", _draw)
        row.bind("<Enter>",     lambda e: _draw(hover=True))
        row.bind("<Leave>",     lambda e: _draw(hover=False))
        def _on_add_click(e, g=genre):
            current_tab = self._genre_var.get()
            if current_tab == "All Movies":
                # Stay on All Movies tab — add movie and select it here
                default_title = "New Movie"
                new_bkg = add_movie_slot(g, default_title)
                if not new_bkg:
                    from tkinter import messagebox as _mb
                    _mb.showerror("Cannot Add", f"Could not add a slot to {g}.",
                                  parent=self.root)
                    return
                self._populate_shelf()
                new_tex = next((t for t in ALL_TEXTURES if t["name"] == new_bkg), None)
                if new_tex:
                    self._on_select_texture(new_tex)
                    self.root.after(50, lambda: (
                        self._shelf_canvas.yview_moveto(1.0)
                        if hasattr(self, "_shelf_canvas") else None))
                    def _focus():
                        if hasattr(self, "_inline_title_entry"):
                            self._inline_title_entry.focus_set()
                            self._inline_title_entry.select_range(0, tk.END)
                    self.root.after(100, _focus)
            else:
                self._add_movie_to_genre(g)
        row.bind("<Button-1>", _on_add_click)
        if hasattr(self, "_shelf_enter_fn"):
            row.bind("<Enter>", self._shelf_enter_fn)
            row.bind("<Leave>", self._shelf_leave_fn)

    def _add_movie_to_genre(self, genre):
        """Immediately create a new slot in genre, select it, focus title field."""
        # Create the slot with a default title
        default_title = "New Movie"
        new_bkg = add_movie_slot(genre, default_title)
        if not new_bkg:
            from tkinter import messagebox as _mb
            _mb.showerror("Cannot Add",
                f"Could not add a slot to {genre}. Genre may be full.",
                parent=self.root)
            return

        # Switch to the genre tab and refresh shelf
        self._genre_var.set(genre)
        self._update_tab_colors(genre)
        self._populate_shelf()

        # Find and select the new texture
        new_tex = next((t for t in ALL_TEXTURES if t["name"] == new_bkg), None)
        if new_tex:
            self._on_select_texture(new_tex)

            # Scroll shelf to bottom so new slot is visible
            self.root.after(50, lambda:
                self._shelf_canvas.yview_moveto(1.0)
                if hasattr(self, "_shelf_canvas") else None)

            # Focus and select-all in title field
            def _focus_title():
                if hasattr(self, "_inline_title_entry"):
                    self._inline_title_entry.focus_set()
                    self._inline_title_entry.select_range(0, tk.END)
            self.root.after(80, _focus_title)

    def _on_select_texture(self, texture):
        """Handle selection of a texture tile."""
        self.selected = texture
        self._selected_nr_idx = -1
        self._auto_fit = False
        self._show_nr_panel(False)
        # Restore title trace to genre handler
        self._inline_title_var.trace_remove('write', self._title_trace_id)
        self._title_trace_id = self._inline_title_var.trace_add('write', self._on_inline_title_change)
        self._set_details_enabled(True)
        self._update_upload_btn_state()

        name  = texture["name"]
        genre = texture["genre"]

        # Load title
        entries = self.dt_manager.get_titles_for_texture(texture)
        if entries:
            title_val = entries[0]["title"].rstrip()
            self._loaded_title = title_val
            self._inline_title_var.set(title_val)
        else:
            # Try from slot data
            dt = GENRE_DATATABLE.get(genre)
            if dt:
                for s in CLEAN_DT_SLOT_DATA.get(dt, []):
                    if s.get("bkg_tex") == name:
                        self._inline_title_var.set(s.get("pn_name", ""))
                        break

        # Load SKU / rating / rarity
        self._refresh_slot_rating()
        # Update layout card highlights and overlay toggle for this slot
        if hasattr(self, "_redraw_layout_cards"):
            saved = self._get_saved_layout()
            # Regular movies always display the fixed layout overlay.
            display_layout = FIXED_REGULAR_LAYOUT
            if hasattr(self, "_layout_overlay_var"):
                self._layout_overlay_var.set(True)
                self._layout_preview.set(display_layout)
            if hasattr(self, "_draw_overlay_toggle"):
                self._draw_overlay_toggle(True)
            self._redraw_layout_cards()

        # Show/hide delete button for custom slots
        dt = GENRE_DATATABLE.get(genre)
        base_count = GENRES.get(genre, {}).get("bkg", 0)
        is_custom = False
        if dt:
            slots = CLEAN_DT_SLOT_DATA.get(dt, [])
            for i, s in enumerate(slots):
                if s.get("bkg_tex") == name:
                    is_custom = (i >= base_count)
                    break
        if is_custom:
            self._remove_slot_btn_del.pack(fill=tk.X, padx=10, pady=(6,2))
            # Ensure inner divider is below it
        else:
            self._remove_slot_btn_del.pack_forget()

        # Update current LS button
        if dt:
            for s in CLEAN_DT_SLOT_DATA.get(dt, []):
                if s.get("bkg_tex") == name:
                    self._update_ls_btn_colors(s.get("ls", 1))
                    break

        # Info label
        self._update_info_row()

        # Update zoom slider
        entry = self.replacements.get(name)
        zoom  = entry.get("zoom", 1.0) if isinstance(entry, dict) else 1.0
        self.zoom_var.set(zoom)
        self.zoom_label.config(text=f"{zoom:.1f}x")

        # Load preview
        self._raw_img  = None
        self._base_img = None
        threading.Thread(
            target=lambda: self._load_preview_bg(texture),
            daemon=True).start()

        # Update selection highlight and show controls
        self._update_shelf_highlight()
        self._update_viewport_state()

    def _refresh_shelf_keep_scroll(self):
        """Rebuild the shelf list while preserving the current scroll position."""
        self._shelf_scroll_save = self._shelf_canvas.yview()[0]
        self._populate_shelf()


    def _update_shelf_highlight(self):
        """Update visual selection highlight on shelf rows without full rebuild.
        Only touches the previously selected and newly selected rows."""
        if not hasattr(self, '_shelf_frame'):
            return
        genre = self._genre_var.get()
        sel_name = self.selected["name"] if self.selected else None
        prev_name = getattr(self, '_prev_highlighted', None)

        if genre == "New Releases":
            sel_idx = getattr(self, '_selected_nr_idx', -1)
            for w in self._shelf_frame.winfo_children():
                if not hasattr(w, '_nr_row_idx'):
                    continue
                is_sel = (w._nr_row_idx == sel_idx)
                row_bg = DS["panel"] if is_sel else DS["surface"]
                w.config(bg=row_bg,
                         highlightthickness=1 if is_sel else 0,
                         highlightbackground=DS["cyan"])
                for child in w.winfo_children():
                    try:
                        if isinstance(child, tk.Label):
                            if child.cget("bg") not in (row_bg, DS["panel"], DS["surface"]):
                                continue
                            child.config(bg=row_bg)
                    except Exception:
                        pass
        else:
            # Genre rows: Canvas widgets — update border + title color
            def _walk(widget):
                for w in widget.winfo_children():
                    if isinstance(w, tk.Canvas) and hasattr(w, '_tex_name'):
                        tn = w._tex_name
                        if tn == sel_name or tn == prev_name:
                            is_sel = (tn == sel_name)
                            w.config(highlightthickness=1 if is_sel else 0,
                                     highlightbackground=DS["cyan"])
                            title_fg = DS["cyan"] if is_sel else DS["text"]
                            w.itemconfig("title_text", fill=title_fg,
                                         font=_f(FS["body"], bold=is_sel))
                    elif w.winfo_children():
                        _walk(w)
            _walk(self._shelf_frame)

        self._prev_highlighted = sel_name

    def _populate_list(self, *args):
        """Compatibility shim — delegates to _populate_shelf."""
        self._populate_shelf(*args)

    # ---- Title Editing ----

    def _load_titles(self, *args, **kwargs):
        """Legacy title editing — replaced by inline title in new UI."""
        pass
    def _show_title_entries(self, *args, **kwargs):
        """Legacy title editing — replaced by inline title in new UI."""
        pass
    def _save_title(self, *args, **kwargs):
        """Legacy title editing — replaced by inline title in new UI."""
        pass
    def _drag_start(self, event):
        if not self.selected or self.selected["name"] not in self.replacements:
            return
        self._dragging    = True
        self._drag_start_x = event.x
        self._drag_start_y = event.y
        entry = self.replacements[self.selected["name"]]
        self._drag_orig_x = entry.get("offset_x", 0) if isinstance(entry, dict) else 0
        self._drag_orig_y = entry.get("offset_y", 0) if isinstance(entry, dict) else 0

        # Pre-scale image to display size once for the drag gesture.
        # We store the scaled raw image and bake composites cheaply on each move.
        if self._raw_img is not None:
            cw  = self.canvas.winfo_width()
            ch  = self.canvas.winfo_height()
            vz  = getattr(self, "_viewport_zoom", 1.0)
            dh  = int(min(ch - 10, (cw - 10) * 2) * vz)
            dw  = dh // 2
            pan_x = getattr(self, "_viewport_pan_x", 0)
            pan_y = getattr(self, "_viewport_pan_y", 0)
            self._drag_dw = dw
            self._drag_dh = dh
            self._drag_dx = (cw - dw) // 2 + pan_x
            self._drag_dy = (ch - dh) // 2 + pan_y
            zoom       = entry.get("zoom", 1.0) if isinstance(entry, dict) else 1.0
            # Full canvas coverage for all texture types
            base_scale = max(TEX_WIDTH / self._raw_img.width,
                             TEX_HEIGHT / self._raw_img.height) * zoom
            disp_scale = dw / TEX_WIDTH
            disp_nw    = max(1, int(self._raw_img.width  * base_scale * disp_scale))
            disp_nh    = max(1, int(self._raw_img.height * base_scale * disp_scale))
            # Pre-scale raw image once — reuse across all drag events
            self._drag_scaled = self._raw_img.resize((disp_nw, disp_nh), Image.BILINEAR)
            self._drag_base_scale = base_scale
            self._drag_photo = None  # built per-move

    def _drag_move(self, event):
        if not self._dragging or not self.selected:
            return
        name  = self.selected["name"]
        entry = self.replacements.get(name)
        if not entry:
            return

        cw  = self.canvas.winfo_width()
        ch  = self.canvas.winfo_height()
        vz  = getattr(self, "_viewport_zoom", 1.0)
        dh  = int(min(ch - 10, (cw - 10) * 2) * vz)
        dw  = dh // 2
        scale_x = TEX_WIDTH  / dw
        scale_y = TEX_HEIGHT / dh

        raw_dx = int((event.x - self._drag_start_x) * scale_x)
        raw_dy = int((event.y - self._drag_start_y) * scale_y)
        new_x  = self._drag_orig_x + raw_dx
        new_y  = self._drag_orig_y + raw_dy

        # ── Snap ──────────────────────────────────────────────
        snapped_x = snapped_y = False
        snap_cx   = False
        snap_cy   = False

        nr_standee_drag = (getattr(self, '_selected_nr_idx', -1) >= 0)
        # NR slots always use fullcanvas positioning regardless of VHS/Standee toggle
        nr_standee_view = (nr_standee_drag
                           and getattr(self, '_nr_view_mode', None)
                           and self._nr_view_mode.get() == "Standee")

        if getattr(self, "_snap_enabled", True) and self._raw_img is not None and isinstance(entry, dict):
            SNAP_R    = 40
            zoom       = entry.get("zoom", 1.0)

            if nr_standee_drag:
                # Standee mode: image scaled to fill full canvas, centered
                base_scale = min(TEX_WIDTH / self._raw_img.width,
                                 TEX_HEIGHT / self._raw_img.height) * zoom
                img_w_tex  = int(self._raw_img.width  * base_scale)
                img_h_tex  = int(self._raw_img.height * base_scale)
                img_default_x = (TEX_WIDTH  - img_w_tex) // 2
                img_default_y = (TEX_HEIGHT - img_h_tex) // 2

                # Center snap
                snap_ctr_x = TEX_WIDTH // 2 - img_w_tex // 2 - img_default_x
                if abs(new_x - snap_ctr_x) < SNAP_R:
                    new_x = snap_ctr_x; snapped_x = True; snap_cx = True
                snap_ctr_y = TEX_HEIGHT // 2 - img_h_tex // 2 - img_default_y
                if abs(new_y - snap_ctr_y) < SNAP_R:
                    new_y = snap_ctr_y; snapped_y = True; snap_cy = True

                # Edge snaps — full canvas (0, 0, TEX_WIDTH, TEX_HEIGHT)
                if not snapped_x:
                    snap_left = 0 - img_default_x
                    if abs(new_x - snap_left) < SNAP_R: new_x = snap_left; snapped_x = True
                if not snapped_x:
                    snap_right = (TEX_WIDTH - img_w_tex) - img_default_x
                    if abs(new_x - snap_right) < SNAP_R: new_x = snap_right; snapped_x = True
                if not snapped_y:
                    snap_top = 0 - img_default_y
                    if abs(new_y - snap_top) < SNAP_R: new_y = snap_top; snapped_y = True
                if not snapped_y:
                    snap_bot = (TEX_HEIGHT - img_h_tex) - img_default_y
                    if abs(new_y - snap_bot) < SNAP_R: new_y = snap_bot; snapped_y = True
            else:
                # VHS mode: image scaled to cover full canvas, centered
                base_scale = max(TEX_WIDTH / self._raw_img.width,
                                 TEX_HEIGHT / self._raw_img.height) * zoom
                img_w_tex  = int(self._raw_img.width  * base_scale)
                img_h_tex  = int(self._raw_img.height * base_scale)
                img_default_x = (TEX_WIDTH  - img_w_tex) // 2
                img_default_y = (TEX_HEIGHT - img_h_tex) // 2

                # Use per-layout visible rect for snap targets (matches helper lines)
                _snap_layout = self._layout_preview.get() if hasattr(self, '_layout_preview') else 0
                if _snap_layout < 1 and self.selected:
                    _sel_name = self.selected.get("name", "")
                    _sel_genre = self.selected.get("genre", "")
                    _sel_dt = GENRE_DATATABLE.get(_sel_genre, "")
                    _sel_slot = next((s for s in CLEAN_DT_SLOT_DATA.get(_sel_dt, [])
                                      if s.get("bkg_tex") == _sel_name), None)
                    if _sel_slot:
                        _snap_layout = _sel_slot.get("ls", 0)
                _vis = get_layout_visible_rect(_snap_layout) if _snap_layout >= 1 else None
                if _vis is not None:
                    _sv_top, _sv_bot, _sv_left, _sv_right = _vis
                else:
                    _sv_top = HIDDEN_TOP
                    _sv_bot = TEX_HEIGHT - HIDDEN_BOTTOM
                    _sv_left = HIDDEN_LEFT
                    _sv_right = TEX_WIDTH - HIDDEN_RIGHT
                SAFE_CX = int((_sv_left + _sv_right) / 2)
                SAFE_CY = int((_sv_top + _sv_bot) / 2)

                # Snap targets: (name, offset_value, is_center)
                snap_targets_x = [
                    ("cyan_center_x", SAFE_CX - img_w_tex // 2 - img_default_x, True),
                    ("cyan_left",     round(_sv_left) - img_default_x, False),
                    ("cyan_right",    round(_sv_right) - img_w_tex - img_default_x, False),
                    ("canvas_left",   -img_default_x, False),
                    ("canvas_right",  (TEX_WIDTH - img_w_tex) - img_default_x, False),
                ]
                snap_targets_y = [
                    ("cyan_center_y", SAFE_CY - img_h_tex // 2 - img_default_y, True),
                    ("cyan_top",      round(_sv_top) - img_default_y, False),
                    ("cyan_bottom",   round(_sv_bot) - img_h_tex - img_default_y, False),
                    ("canvas_top",    -img_default_y, False),
                    ("canvas_bottom", (TEX_HEIGHT - img_h_tex) - img_default_y, False),
                ]

                # Find the NEAREST snap target within range
                best_x = None
                best_x_dist = SNAP_R
                for snap_name, snap_val, is_ctr in snap_targets_x:
                    d = abs(new_x - snap_val)
                    if d < best_x_dist:
                        best_x = (snap_name, snap_val, is_ctr)
                        best_x_dist = d

                best_y = None
                best_y_dist = SNAP_R
                for snap_name, snap_val, is_ctr in snap_targets_y:
                    d = abs(new_y - snap_val)
                    if d < best_y_dist:
                        best_y = (snap_name, snap_val, is_ctr)
                        best_y_dist = d

                if best_x:
                    new_x = best_x[1]; snapped_x = True
                    if best_x[2]: snap_cx = True
                if best_y:
                    new_y = best_y[1]; snapped_y = True
                    if best_y[2]: snap_cy = True

                print(f"[SNAP_DEBUG] img={self._raw_img.width}x{self._raw_img.height} "
                      f"zoom={zoom:.2f} base_scale={base_scale:.4f}")
                print(f"[SNAP_DEBUG] img_tex={img_w_tex}x{img_h_tex} "
                      f"defaults=({img_default_x},{img_default_y})")
                print(f"[SNAP_DEBUG] SAFE_CX={SAFE_CX} SAFE_CY={SAFE_CY}")
                print(f"[SNAP_DEBUG] snap_x={best_x[0] if best_x else 'none'}={new_x} "
                      f"snap_y={best_y[0] if best_y else 'none'}={new_y}")
                all_x = [(n,v) for n,v,_ in snap_targets_x]
                all_y = [(n,v) for n,v,_ in snap_targets_y]
                print(f"[SNAP_DEBUG] all_x: {all_x}")
                print(f"[SNAP_DEBUG] all_y: {all_y}")

        if isinstance(entry, dict):
            entry["offset_x"] = new_x
            entry["offset_y"] = new_y
            self._auto_fit = False

        # Fast path: composite a dw×dh black image with the pre-scaled raw image
        # pasted at the correct offset. This is just a PIL paste + PhotoImage — fast.
        if hasattr(self, "_drag_scaled") and self._drag_dw > 0:
            dw, dh = self._drag_dw, self._drag_dh
            ddx, ddy = self._drag_dx, self._drag_dy
            base_scale = self._drag_base_scale
            disp_scale = dw / TEX_WIDTH
            img_nw = int(self._raw_img.width * base_scale)
            img_nh = int(self._raw_img.height * base_scale)
            disp_ox = int(((TEX_WIDTH - img_nw) // 2 + new_x) * disp_scale)
            disp_oy = int(((TEX_HEIGHT - img_nh) // 2 + new_y) * disp_scale)

            # Clip composite to canvas size — same optimisation as _render_preview
            cw_  = self.canvas.winfo_width()
            ch_  = self.canvas.winfo_height()
            vis_x = max(0, -ddx)
            vis_y = max(0, -ddy)
            vis_w = min(dw - vis_x, cw_ - max(0, ddx))
            vis_h = min(dh - vis_y, ch_ - max(0, ddy))
            if vis_w <= 0 or vis_h <= 0:
                return

            comp = Image.new("RGB", (vis_w, vis_h), (26, 26, 26))
            self._fill_checker(comp, 0, 0, vis_w, vis_h)
            comp.paste(self._drag_scaled, (disp_ox - vis_x, disp_oy - vis_y))
            if not nr_standee_view:
                overlay = comp.convert("RGBA")
                self._draw_hidden_overlays_on_image(overlay, dw, dh, ox=vis_x, oy=vis_y)
                comp = overlay.convert("RGB")
            self._drag_photo = ImageTk.PhotoImage(comp)
            self.canvas.delete("drag_img")
            self.canvas.delete("safe_border")
            self.canvas.create_image(ddx + vis_x, ddy + vis_y, anchor=tk.NW,
                                     image=self._drag_photo, tags="drag_img")
            if not nr_standee_view:
                self._draw_safe_border_on_canvas(ddx, ddy, dw, dh)
        else:
            self._render_preview()

        # Show guide lines on canvas while snapped to safe-area center
        if snap_cx or snap_cy:
            self._show_snap_guides(snap_cx, snap_cy)
        else:
            self.canvas.delete("snap_guide")
            if self._snap_guide_job:
                self.root.after_cancel(self._snap_guide_job)
                self._snap_guide_job = None

    def _drag_end(self, event):
        if not self._dragging:
            return
        self._dragging = False
        self._drag_photo  = None
        self._drag_scaled = None
        if self.selected:
            save_replacements(self.replacements)
            self._render_preview()

    def _pan_start(self, event):
        """Middle mouse button pressed — start viewport pan."""
        if not self.selected:
            return
        if self.selected["name"] not in self.replacements and self._raw_img is None:
            return
        self._pan_dragging = True
        self._pan_start_x  = event.x
        self._pan_start_y  = event.y
        self._pan_orig_x   = getattr(self, "_viewport_pan_x", 0)
        self._pan_orig_y   = getattr(self, "_viewport_pan_y", 0)
        self.canvas.config(cursor="fleur")

    def _pan_move(self, event):
        """Middle mouse drag — pan the viewport, clamped so image stays visible."""
        if not self._pan_dragging:
            return
        raw_x = self._pan_orig_x + (event.x - self._pan_start_x)
        raw_y = self._pan_orig_y + (event.y - self._pan_start_y)

        # Clamp pan so at least half the image rect remains on screen
        cw_ = self.canvas.winfo_width()
        ch_ = self.canvas.winfo_height()
        vz  = getattr(self, "_viewport_zoom", 1.0)
        if cw_ > 10 and ch_ > 10:
            base_dh = min(ch_ - 10, (cw_ - 10) * 2)
            base_dw = base_dh // 2
            dw = int(base_dw * vz)
            dh = dw * 2
            max_px = max(dw // 2, cw_ // 2)
            max_py = max(dh // 2, ch_ // 2)
            raw_x = max(-max_px, min(max_px, raw_x))
            raw_y = max(-max_py, min(max_py, raw_y))

        self._viewport_pan_x = raw_x
        self._viewport_pan_y = raw_y

        # Fast path: crop from cached full composite (includes image + overlays + layout)
        pan_comp = getattr(self, "_pan_full_comp", None)
        if pan_comp is not None:
            pc_img, pc_dw, pc_dh = pan_comp
            new_dx = (cw_ - pc_dw) // 2 + raw_x
            new_dy = (ch_ - pc_dh) // 2 + raw_y
            # Crop to visible area
            vis_x = max(0, -new_dx)
            vis_y = max(0, -new_dy)
            vis_w = min(pc_dw - vis_x, cw_ - max(0, new_dx))
            vis_h = min(pc_dh - vis_y, ch_ - max(0, new_dy))
            if vis_w > 0 and vis_h > 0:
                cropped = pc_img.crop((vis_x, vis_y, vis_x + vis_w, vis_y + vis_h))
                self._pan_photo = ImageTk.PhotoImage(cropped)
                self.canvas.delete("all")
                self.canvas.create_image(max(0, new_dx), max(0, new_dy),
                                         anchor=tk.NW, image=self._pan_photo)
            return
        self._render_preview()

    def _pan_end(self, event):
        """Middle mouse released — end viewport pan."""
        self._pan_dragging = False
        self.canvas.config(cursor="fleur")
        # Full render to restore all overlays/buttons that were skipped during fast pan
        self._render_preview()

    def _load_layout_thumbs(self):
        """Load layout thumbnails from cached PNGs, or show numbered placeholders."""
        _tw, _th = self._thumb_size
        _lc_dir = os.path.join(SCRIPT_DIR, LAYOUT_CACHE_DIRNAME)
        for n in range(1, 2):
            _loaded = False
            # Use the fixed full 2048x2048 layout PNG, crop left half (1024x2048)
            _lc_path = os.path.join(_lc_dir, "T_Layout_01_bc_full.png")
            if os.path.exists(_lc_path):
                try:
                    img = Image.open(_lc_path).convert("RGB")
                    # Crop left half of the square layout, trim gray bottom
                    _case_bottom = 1650  # VHS case ends ~y1600, small margin
                    img = img.crop((0, 0, img.width // 2, _case_bottom))
                    img = img.resize((_tw, _th), Image.BILINEAR)
                    self._layout_thumb_photos[n] = ImageTk.PhotoImage(img)
                    _loaded = True
                except Exception:
                    pass
            if not _loaded and n not in self._layout_thumb_photos:
                img = Image.new("RGB", (_tw, _th), (40, 40, 50))
                _d = ImageDraw.Draw(img)
                _d.text((_tw // 2 - 4, _th // 2 - 6), str(n),
                        fill=(200, 200, 200))
                self._layout_thumb_photos[n] = ImageTk.PhotoImage(img)
        # Update existing button labels if they exist
        for v, card in getattr(self, '_layout_card_frames', {}).items():
            for child in card.winfo_children():
                if isinstance(child, tk.Label) and child.cget("image"):
                    if v in self._layout_thumb_photos:
                        child.config(image=self._layout_thumb_photos[v])

    def _toggle_hud(self):
        """Minimize or restore the controls HUD."""
        self._hud_minimized = not getattr(self, "_hud_minimized", False)
        self._render_preview()

    def _toggle_snap(self):
        """Toggle snap-to-guides on/off and redraw the controls HUD."""
        self._snap_enabled = not getattr(self, "_snap_enabled", True)
        self._render_preview()

    def _show_snap_guides(self, show_x, show_y):
        """Draw cyan dashed guide lines while snapped to center axes.
        In VHS mode: guides span the safe area.
        In NR Standee mode: guides span the full canvas rect.
        """
        if self._snap_guide_job:
            self.root.after_cancel(self._snap_guide_job)
            self._snap_guide_job = None
        self.canvas.delete("snap_guide")

        cw  = self.canvas.winfo_width()
        ch  = self.canvas.winfo_height()
        vz  = getattr(self, "_viewport_zoom", 1.0)
        px  = getattr(self, "_viewport_pan_x", 0)
        py  = getattr(self, "_viewport_pan_y", 0)
        base_dh = min(ch - 10, (cw - 10) * 2)
        base_dw = base_dh // 2
        dh  = int(base_dh * vz)
        dw  = int(base_dw * vz)
        dx  = (cw - dw) // 2 + px
        dy  = (ch - dh) // 2 + py
        disp_scale = dw / TEX_WIDTH

        nr_standee = (getattr(self, '_selected_nr_idx', -1) >= 0)

        if nr_standee:
            # Full canvas center
            center_x = dx + dw // 2
            center_y = dy + dh // 2
            if show_x:
                self.canvas.create_line(
                    center_x, dy, center_x, dy + dh,
                    fill=DS["cyan"], width=1, dash=(4, 3), tags="snap_guide")
            if show_y:
                self.canvas.create_line(
                    dx, center_y, dx + dw, center_y,
                    fill=DS["cyan"], width=1, dash=(4, 3), tags="snap_guide")
        else:
            # Fixed visible-art window center
            vis_top, vis_bot, vis_left, vis_right = get_layout_visible_rect()
            safe_cx_disp = dx + int(((vis_left + vis_right) / 2) * disp_scale)
            safe_cy_disp = dy + int(((vis_top + vis_bot) / 2) * disp_scale)
            if show_x:
                safe_y1 = dy + int(vis_top * dh / TEX_HEIGHT)
                safe_y2 = dy + int(vis_bot * dh / TEX_HEIGHT)
                self.canvas.create_line(
                    safe_cx_disp, safe_y1, safe_cx_disp, safe_y2,
                    fill=DS["cyan"], width=1, dash=(4, 3), tags="snap_guide")
            if show_y:
                safe_x1 = dx + int(vis_left * dw / TEX_WIDTH)
                safe_x2 = dx + int(vis_right * dw / TEX_WIDTH)
                self.canvas.create_line(
                    safe_x1, safe_cy_disp, safe_x2, safe_cy_disp,
                    fill=DS["cyan"], width=1, dash=(4, 3), tags="snap_guide")


    def _on_zoom(self, value):
        if not self.selected:
            return
        name  = self.selected["name"]
        zoom  = round(float(value), 2)
        self.zoom_label.config(text=f"{zoom:.1f}x")
        entry = self.replacements.get(name)
        if entry and isinstance(entry, dict):
            entry["zoom"] = zoom
            self._render_preview()

    def _reset_transform(self):
        """Reset offset+zoom to defaults."""
        if not self.selected:
            return
        name  = self.selected["name"]
        entry = self.replacements.get(name)
        if entry and isinstance(entry, dict):
            entry["offset_x"] = 0
            entry["offset_y"] = 0
            entry["zoom"]     = 1.0
            self._auto_fit = False
            self.zoom_var.set(1.0)
            self.zoom_label.config(text="1.0x")
            save_replacements(self.replacements)
            self._render_preview()

    def _fit_full_canvas(self):
        """Fit image to the full 1024×2048 canvas (no layout-aware cropping).
        Same as NR/standee positioning — image covers the entire texture."""
        if not self.selected or self._raw_img is None:
            return
        name = self.selected["name"]
        entry = self.replacements.get(name)
        if not entry or not isinstance(entry, dict):
            return
        entry["offset_x"] = 0
        entry["offset_y"] = 0
        entry["zoom"] = 1.0
        self._auto_fit = False
        self.zoom_var.set(1.0)
        self.zoom_label.config(text="1.0x")
        save_replacements(self.replacements)
        self._render_preview()

    def _fit_to_canvas(self):
        """Auto-scale and center image to fit the visible cyan area (genre movies)
        or the full canvas (NR/standee)."""
        if not self.selected or self._raw_img is None:
            return
        name = self.selected["name"]
        entry = self.replacements.get(name)
        if not entry or not isinstance(entry, dict):
            return
        img = self._raw_img
        is_nr = getattr(self, '_selected_nr_idx', -1) >= 0

        if is_nr:
            # NR: fit to full canvas
            base_scale = max(TEX_WIDTH / img.width, TEX_HEIGHT / img.height)
            entry["zoom"] = 1.0
            entry["offset_x"] = 0
            entry["offset_y"] = 0
        else:
            # Regular movies: fit image to the actual visible black window
            # from the selected/replaced layout PNG.
            layout_n = 0

            if self.selected:
                _fn = self.selected.get("name", "")
                _fg = self.selected.get("genre", "")
                _fdt = GENRE_DATATABLE.get(_fg, "")
                _fslot = next(
                    (s for s in CLEAN_DT_SLOT_DATA.get(_fdt, [])
                    if s.get("bkg_tex") == _fn),
                    None
                )
                if _fslot:
                    layout_n = _fslot.get("ls", 0)

            if layout_n < 1 or layout_n > 5:
                layout_n = self._layout_preview.get()

            if layout_n < 1 or layout_n > 5:
                layout_n = 1

            vis = get_layout_visible_rect(layout_n)

            if vis is None:
                vis_top = HIDDEN_TOP
                vis_bot = TEX_HEIGHT - HIDDEN_BOTTOM
                vis_left = HIDDEN_LEFT
                vis_right = TEX_WIDTH - HIDDEN_RIGHT
            else:
                vis_top, vis_bot, vis_left, vis_right = vis

            target_w = max(1, vis_right - vis_left)
            target_h = max(1, vis_bot - vis_top)
            target_cx = vis_left + target_w / 2
            target_cy = vis_top + target_h / 2

            # Renderer uses full-canvas cover scale as its base.
            base_scale = max(TEX_WIDTH / img.width, TEX_HEIGHT / img.height)

            # Fit Visible should COVER the visible window.
            fit_scale = max(target_w / img.width, target_h / img.height)

            import math as _math
            zoom = _math.ceil((fit_scale / base_scale) * 1000) / 1000
            zoom = max(0.5, min(3.0, zoom))

            entry["zoom"] = zoom

            actual_scale = base_scale * zoom
            img_w_tex = int(img.width * actual_scale)
            img_h_tex = int(img.height * actual_scale)

            img_default_x = (TEX_WIDTH - img_w_tex) // 2
            img_default_y = (TEX_HEIGHT - img_h_tex) // 2

            entry["offset_x"] = int(target_cx - img_w_tex / 2 - img_default_x)
            entry["offset_y"] = int(target_cy - img_h_tex / 2 - img_default_y)

            # Image position in bg texture coords:
            _img_top = img_default_y + entry["offset_y"]
            _img_bot = _img_top + img_h_tex
            _img_left = img_default_x + entry["offset_x"]
            _img_right = _img_left + img_w_tex

        self.zoom_var.set(entry["zoom"])
        self.zoom_label.config(text=f"{entry['zoom']:.1f}x")
        self._auto_fit = True
        save_replacements(self.replacements)
        self._render_preview()

    def _zoom_settle(self):
        """Called after 150ms of no zoom input — re-render at full quality."""
        self._zoom_debounce_id = None
        self._zoom_base_photo = None
        self._zoom_base_pil = None
        self._zoom_quality = "hq"
        # Invalidate caches to force full-quality rebuild
        self._scaled_cache_key = None
        self._full_comp_key = None
        self._pan_full_comp = None
        self._render_preview()

    def _zoom_step(self, delta):
        """Increment zoom by delta, snap to 1.0 if close."""
        if not self.selected:
            return
        name  = self.selected["name"]
        entry = self.replacements.get(name)
        if not entry or not isinstance(entry, dict):
            return
        new_zoom = round(entry.get("zoom", 1.0) + delta, 2)
        new_zoom = max(0.5, min(3.0, new_zoom))
        # Snap to 1.0 if within 0.05
        if abs(new_zoom - 1.0) < 0.05:
            new_zoom = 1.0
            self.zoom_label.config(fg=DS["cyan"])
            self.root.after(600, lambda: self.zoom_label.config(fg=DS["text"]))
        entry["zoom"] = new_zoom
        self.zoom_var.set(new_zoom)
        self.zoom_label.config(text=f"{new_zoom:.1f}x")
        self._auto_fit = False
        self._render_preview()

    def _update_info_row(self):
        """Update the info label with filename + resolution, and upload btn state."""
        if not self.selected:
            self._info_var.set("Select a movie to get started")
            return
        name  = self.selected["name"]
        entry = self.replacements.get(name)
        if entry and isinstance(entry, dict):
            import os as _os
            path = entry.get("path", "")
            fname = _os.path.basename(path) if path else "custom image"
            if self._raw_img is not None:
                w, h = self._raw_img.size
                self._info_var.set(f"{fname}  —  {w} × {h} px")
            else:
                self._info_var.set(fname)
        else:
            self._info_var.set("No image uploaded")
        self._update_upload_btn_state()

    def _update_upload_btn_state(self):
        """Enable/disable Replace Image button based on selection."""
        if hasattr(self, "_vp_replace_btn"):
            if self.selected:
                self._vp_replace_btn.config(state=tk.NORMAL, fg=DS["text"])
            else:
                self._vp_replace_btn.config(state=tk.DISABLED, fg=DS["text3"])

    def _show_canvas_help(self):
        """Show tooltip explaining the hidden zone overlays."""
        messagebox.showinfo(
            "Canvas Guide",
            "Red hatched areas - hidden in-game\n"
            "These regions are covered by the VHS tape model.\n"
            "Your artwork here will not be visible to players.\n\n"
            "Cyan dashed border - visible area\n"
            "Only content inside this border shows on the tape.\n\n"
            "Tip: Use Fit Visible to fill the visible area.",
            parent=self.root)

    def _update_zoom_slider(self):
        """Sync zoom slider to current texture's zoom value."""
        if not self.selected:
            return
        name  = self.selected["name"]
        entry = self.replacements.get(name)
        zoom  = entry.get("zoom", 1.0) if isinstance(entry, dict) else 1.0
        self.zoom_var.set(zoom)
        self.zoom_label.config(text=f"{zoom:.1f}x")

    def _on_select(self, event):
        """Legacy listbox select handler — no-op in new tile UI."""
        pass
    def _load_preview_bg(self, texture):
        """
        Load images into memory cache in background thread.
        After loading, render immediately. Drag/zoom then just
        re-renders from cache without any disk access.
        """
        name  = texture["name"]
        entry = self.replacements.get(name)

        raw_img  = None
        base_img = None

        if entry:
            png_path = entry["path"] if isinstance(entry, dict) else entry
            if os.path.exists(png_path):
                try:
                    raw_img = Image.open(png_path).convert('RGB')
                    print(f"[Preview] Loaded custom image: {raw_img.size}")
                except Exception as e:
                    print(f"[Preview] Error loading image: {e}")

        if raw_img is None:
            # For NR slots, don't load base game preview — show blank canvas
            is_nr = (texture.get("type") == "New Release"
                     or texture.get("name", "").startswith("NR_")
                     or texture.get("name", "").startswith("T_New_"))
            # For custom genre slots (user-created), also show blank — not base game art
            is_custom_slot = False
            if not is_nr:
                tname = texture.get("name", "")
                genre = texture.get("genre", "")
                dt = GENRE_DATATABLE.get(genre)
                if dt:
                    base_count = GENRES.get(genre, {}).get("bkg", 0)
                    slots = CLEAN_DT_SLOT_DATA.get(dt, [])
                    for i, s in enumerate(slots):
                        if s.get("bkg_tex") == tname and i >= base_count:
                            is_custom_slot = True
                            break
            if not is_nr and not is_custom_slot:
                base_img = self.pak_cache.get_preview(texture)
                print(f"[Preview] Loaded pak preview: {base_img.size if base_img else None}")

        # Store in cache and render
        self.root.after(0, lambda: self._set_preview_cache(texture, raw_img, base_img))

    def _set_preview_cache(self, texture, raw_img, base_img):
        """Store loaded images and render. Called on main thread."""
        if self.selected != texture:
            return
        self._raw_img  = raw_img
        self._base_img = base_img
        # Create working-resolution copy capped at 2x canvas size for fast preview.
        # Full-res _raw_img is preserved for export/build only.
        self._update_working_img()
        self._viewport_zoom  = 1.0   # reset viewport on new slot
        self._viewport_pan_x = 0
        self._viewport_pan_y = 0
        # Invalidate all render caches
        self._scaled_cache_key = None
        self._full_comp_key = None
        self._pan_full_comp = None
        self.canvas.config(cursor="fleur")
        self._render_preview()
        self._update_info_row()

    def _update_working_img(self):
        """Create a working-resolution copy of _raw_img capped at 2x canvas size."""
        if self._raw_img is None:
            self._working_img = None
            return
        cw = max(400, self.canvas.winfo_width())
        ch = max(600, self.canvas.winfo_height())
        max_w = cw * 2
        max_h = ch * 2
        raw = self._raw_img
        if raw.width > max_w or raw.height > max_h:
            scale = min(max_w / raw.width, max_h / raw.height)
            nw = max(1, int(raw.width * scale))
            nh = max(1, int(raw.height * scale))
            self._working_img = raw.resize((nw, nh), Image.LANCZOS)
        else:
            self._working_img = raw

    def _render_preview(self):
        """
        Fast render from cached images. No disk access.
        Called on drag, zoom, and initial load.
        """
        if not self.selected:
            return

        name  = self.selected["name"]
        entry = self.replacements.get(name)
        cw    = self.canvas.winfo_width()
        ch    = self.canvas.winfo_height()
        if cw < 10 or ch < 10:
            return

        # Calculate display rect — base fit then apply viewport zoom + pan
        vz     = getattr(self, "_viewport_zoom", 1.0)
        pan_x  = getattr(self, "_viewport_pan_x", 0)
        pan_y  = getattr(self, "_viewport_pan_y", 0)
        max_h  = ch - 10
        max_w  = cw - 10
        base_dh = min(max_h, max_w * 2)
        base_dw = base_dh // 2
        dw     = int(base_dw * vz)
        dh     = dw * 2   # exact 1:2 ratio — prevents sub-pixel gap at bottom
        dx     = (cw - dw) // 2 + pan_x
        dy     = (ch - dh) // 2 + pan_y

        self.canvas.delete("all")

        if self._raw_img is not None:
            offset_x = entry.get("offset_x", 0) if isinstance(entry, dict) else 0
            offset_y = entry.get("offset_y", 0) if isinstance(entry, dict) else 0
            zoom     = entry.get("zoom", 1.0)   if isinstance(entry, dict) else 1.0

            raw        = getattr(self, '_working_img', None) or self._raw_img
            _resample  = Image.NEAREST if getattr(self, '_zoom_quality', 'hq') == 'fast' else Image.BILINEAR
            is_nr_slot = (getattr(self, '_selected_nr_idx', -1) >= 0)
            nr_standee_mode = (is_nr_slot
                               and getattr(self, '_nr_view_mode', None)
                               and self._nr_view_mode.get() == "Standee")
            if is_nr_slot:
                # NR slots: always use fullcanvas positioning (matches build output)
                # Both VHS and Standee view modes use the same image position —
                # only the overlays differ.
                import math
                base_scale = max(TEX_WIDTH / raw.width, TEX_HEIGHT / raw.height) * zoom
                nw         = int(raw.width  * base_scale)
                nh         = int(raw.height * base_scale)
                disp_scale = dw / TEX_WIDTH
                # Round UP to prevent sub-pixel gap at edges
                disp_nw    = max(1, math.ceil(nw * disp_scale))
                disp_nh    = max(1, math.ceil(nh * disp_scale))
                disp_ox    = int(((TEX_WIDTH - nw) // 2 + offset_x) * disp_scale)
                disp_oy    = int(((TEX_HEIGHT - nh) // 2 + offset_y) * disp_scale)
            else:
                base_scale = max(TEX_WIDTH / raw.width, TEX_HEIGHT / raw.height) * zoom
                nw         = int(raw.width  * base_scale)
                nh         = int(raw.height * base_scale)
                disp_scale = dw / TEX_WIDTH
                disp_nw    = max(1, int(nw * disp_scale))
                disp_nh    = max(1, int(nh * disp_scale))
                disp_ox    = int(((TEX_WIDTH - nw) // 2 + offset_x) * disp_scale)
                disp_oy    = int(((TEX_HEIGHT - nh) // 2 + offset_y) * disp_scale)

            # Cache the scaled image to avoid expensive re-resize on pan/scroll
            _cache_key = (id(raw), disp_nw, disp_nh, _resample)
            if getattr(self, "_scaled_cache_key", None) != _cache_key:
                self._scaled_cache_img = raw.resize((disp_nw, disp_nh), _resample)
                self._scaled_cache_key = _cache_key
            img_scaled = self._scaled_cache_img

            # Build a full-size composite (dw×dh) with image on checker background.
            # This is cached so panning and zooming can crop from it without rebuilding.
            _comp_key = (id(raw), dw, dh, disp_nw, disp_nh, disp_ox, disp_oy,
                         offset_x, offset_y, zoom)
            if getattr(self, "_full_comp_key", None) != _comp_key:
                # Cap the composite size to prevent memory issues at extreme zoom
                # At 4x zoom on a 4K display, dw×dh could be ~4000×8000
                # PIL can handle this but PhotoImage conversion is the bottleneck
                full_comp = Image.new('RGB', (dw, dh), (26, 26, 26))
                self._fill_checker(full_comp, 0, 0, dw, dh)
                full_comp.paste(img_scaled, (disp_ox, disp_oy))
                self._full_comp = full_comp
                self._full_comp_key = _comp_key
            else:
                full_comp = self._full_comp

            # Crop to visible area for display
            vis_x  = max(0, -dx)
            vis_y  = max(0, -dy)
            vis_w  = min(dw - vis_x, cw - max(0, dx))
            vis_h  = min(dh - vis_y, ch - max(0, dy))
            if vis_w <= 0 or vis_h <= 0:
                return

            composite = full_comp.crop((vis_x, vis_y, vis_x + vis_w, vis_y + vis_h))
            dx += vis_x
            dy += vis_y
            ov_dw, ov_dh, ov_dx, ov_dy = vis_w, vis_h, vis_x, vis_y

        elif self._base_img is not None:
            composite = self._base_img.resize((dw, dh), _resample if self._raw_img is None else Image.BILINEAR)
            ov_dw, ov_dh, ov_dx, ov_dy = dw, dh, 0, 0
        else:
            # No image loaded — draw upload dropzone
            self.canvas.create_rectangle(dx, dy, dx+dw, dy+dh,
                fill=DS["bg"], outline=DS["border"], width=1, tags="dropzone")
            # Ghost zone outlines — same proportions as the real hidden zones
            # so users understand the layout before uploading
            _hp_bottom = int(HIDDEN_BOTTOM * dh / TEX_HEIGHT)
            _hp_top    = int(HIDDEN_TOP    * dh / TEX_HEIGHT)
            _hp_left   = int(HIDDEN_LEFT   * dw / TEX_WIDTH)
            # Full-canvas outer boundary (faint)
            self.canvas.create_rectangle(dx, dy, dx+dw-1, dy+dh-1,
                fill="", outline="#1A2020", width=1, dash=(2, 4), tags="dropzone")
            # Hidden zones — very faint red outline only
            if _hp_top > 0:
                self.canvas.create_rectangle(dx, dy, dx+dw, dy+_hp_top,
                    fill="#140808", outline="#2A1010", width=1, tags="dropzone")
            if _hp_bottom > 0:
                self.canvas.create_rectangle(dx, dy+dh-_hp_bottom, dx+dw, dy+dh,
                    fill="#140808", outline="#2A1010", width=1, tags="dropzone")
            if _hp_left > 0:
                self.canvas.create_rectangle(dx, dy, dx+_hp_left, dy+dh,
                    fill="#140808", outline="#2A1010", width=1, tags="dropzone")
            # Cyan safe-area ghost outline
            self.canvas.create_rectangle(
                dx+_hp_left, dy+_hp_top,
                dx+dw, dy+dh-_hp_bottom,
                fill="", outline="#003838", width=1, dash=(6, 3), tags="dropzone")
            # Drop prompt
            mid_x = dx + _hp_left + (dw - _hp_left) // 2
            mid_y = dy + _hp_top + (dh - _hp_top - _hp_bottom) // 2
            self.canvas.create_text(mid_x, mid_y - 22,
                text="↑", font=_f(22, bold=True),
                fill=DS["text3"], anchor=tk.CENTER, tags="dropzone")
            self.canvas.create_text(mid_x, mid_y + 6,
                text="Click to browse for an image",
                font=_f(FS["body"]), fill=DS["text2"], anchor=tk.CENTER, tags="dropzone")
            self.canvas.create_text(mid_x, mid_y + 22,
                text="PNG  ·  JPG  ·  WEBP  —  any size",
                font=_f(FS["meta"]), fill=DS["text3"], anchor=tk.CENTER, tags="dropzone")
            # Invisible click catcher over the entire drop zone
            self.canvas.create_rectangle(
                dx, dy, dx+dw, dy+dh,
                fill="", outline="", tags="dropzone")
            self.canvas.tag_bind("dropzone", "<Button-1>", lambda e: self._upload())

            # Standee zone lines on empty dropzone
            nr_idx_dz = getattr(self, '_selected_nr_idx', -1)
            if 0 <= nr_idx_dz < len(NR_SLOT_DATA):
                nr_dz = NR_SLOT_DATA[nr_idx_dz]
                sz_dz = STANDEE_ZONES.get(nr_dz.get("standee_shape", "A"), STANDEE_ZONES["A"])
                tp_y = dy + int(sz_dz["front_end"] * dh / TEX_HEIGHT)
                ft_y = dy + int(sz_dz["title_end"] * dh / TEX_HEIGHT)
                foe_dz = sz_dz.get("footer_end", TEX_HEIGHT)
                pl_dz = sz_dz.get("plate_left", 0)
                pr_dz = sz_dz.get("plate_right", 0)
                self.canvas.create_line(dx, tp_y, dx+dw, tp_y,
                    fill="#FFD84A", width=2, dash=(8, 4))
                self.canvas.create_line(dx, ft_y, dx+dw, ft_y,
                    fill="#AA8830", width=1, dash=(6, 4))
                if foe_dz < TEX_HEIGHT:
                    fo_y = dy + int(foe_dz * dh / TEX_HEIGHT)
                    self.canvas.create_line(dx, fo_y, dx+dw, fo_y,
                        fill="#665522", width=1, dash=(4, 4))
                if pl_dz > 0:
                    pl_x = dx + int(pl_dz * dw / TEX_WIDTH)
                    self.canvas.create_line(pl_x, tp_y, pl_x, dy+dh,
                        fill="#665522", width=1, dash=(3, 3))
                if pr_dz > 0:
                    pr_x = dx + dw - int(pr_dz * dw / TEX_WIDTH)
                    self.canvas.create_line(pr_x, tp_y, pr_x, dy+dh,
                        fill="#665522", width=1, dash=(3, 3))
                self.canvas.create_text(dx+8, (tp_y+ft_y)//2,
                    text="Title plate", anchor=tk.W,
                    font=_f(FS["meta"]), fill="#FFD84A")
                ft_bot = dy + int(foe_dz * dh / TEX_HEIGHT) if foe_dz < TEX_HEIGHT else dy + dh
                self.canvas.create_text(dx+8, (ft_y+ft_bot)//2,
                    text="Footer / base", anchor=tk.W,
                    font=_f(FS["meta"]), fill="#AA8830")

            return

        # Draw hatch zones into composite (clipped to visible area)
        # ov_dx/ov_dy tell the overlay where in the full texture this crop starts.
        # In NR Standee mode: no overlays — standee shows full image
        nr_standee_mode = (getattr(self, '_selected_nr_idx', -1) >= 0
                           and getattr(self, '_nr_view_mode', None)
                           and self._nr_view_mode.get() == "Standee")
        if not nr_standee_mode:
            overlay = composite.convert("RGBA")
            self._draw_hidden_overlays_on_image(overlay, dw, dh, ox=ov_dx, oy=ov_dy)
            composite = overlay.convert("RGB")

        self.preview_photo = ImageTk.PhotoImage(composite)
        self.canvas.create_image(dx, dy, anchor=tk.NW, image=self.preview_photo)

        # Safe border uses full dw/dh for correct positioning
        _real_dx = dx - ov_dx
        _real_dy = dy - ov_dy
        if not nr_standee_mode:
            show_labels = getattr(self, "_show_overlay_labels", False)
            self._draw_safe_border_on_canvas(_real_dx, _real_dy, dw, dh, show_labels=show_labels)
        else:
            # ── Standee zone overlays ──────────────────────────────────
            nr_idx = getattr(self, '_selected_nr_idx', -1)
            if 0 <= nr_idx < len(NR_SLOT_DATA):
                shape = NR_SLOT_DATA[nr_idx].get("standee_shape", "A")
                sz = STANDEE_ZONES.get(shape, STANDEE_ZONES["A"])
                fe = sz["front_end"]
                te = sz["title_end"]
                foe = sz.get("footer_end", TEX_HEIGHT)
                pl = sz.get("plate_left", 0)
                pr = sz.get("plate_right", 0)
                fold_l = sz.get("fold_left", 0)
                fold_r = sz.get("fold_right", 0)
                title_below = sz.get("title_below_footer", False)

                # Title plate line (gold dashed)
                tp_y = _real_dy + int(fe * dh / TEX_HEIGHT)
                self.canvas.create_line(
                    _real_dx, tp_y, _real_dx + dw, tp_y,
                    fill="#FFD84A", width=2, dash=(8, 4), tags="standee_zone")
                # Footer line (dimmer gold dashed)
                ft_y = _real_dy + int(te * dh / TEX_HEIGHT)
                self.canvas.create_line(
                    _real_dx, ft_y, _real_dx + dw, ft_y,
                    fill="#AA8830", width=1, dash=(6, 4), tags="standee_zone")
                # Frame cutoff line (faint, below footer)
                if foe < TEX_HEIGHT:
                    fo_y = _real_dy + int(foe * dh / TEX_HEIGHT)
                    self.canvas.create_line(
                        _real_dx, fo_y, _real_dx + dw, fo_y,
                        fill="#665522", width=1, dash=(4, 4), tags="standee_zone")
                    self.canvas.create_text(
                        max(2, _real_dx - 6), (fo_y + _real_dy + dh) // 2,
                        text="Frame color", anchor=tk.E,
                        font=_f(FS["meta"]), fill="#665522", tags="standee_zone")
                # Side margins for title plate + footer
                if pl > 0:
                    pl_x = _real_dx + int(pl * dw / TEX_WIDTH)
                    self.canvas.create_line(
                        pl_x, tp_y, pl_x, _real_dy + dh,
                        fill="#665522", width=1, dash=(3, 3), tags="standee_zone")
                if pr > 0:
                    pr_x = _real_dx + dw - int(pr * dw / TEX_WIDTH)
                    self.canvas.create_line(
                        pr_x, tp_y, pr_x, _real_dy + dh,
                        fill="#665522", width=1, dash=(3, 3), tags="standee_zone")
                # Fold lines for Standee B (sides fold backward)
                if fold_l > 0:
                    fl_x = _real_dx + int(fold_l * dw / TEX_WIDTH)
                    self.canvas.create_line(
                        fl_x, _real_dy, fl_x, tp_y,
                        fill="#6688AA", width=1, dash=(6, 4), tags="standee_zone")
                if fold_r > 0:
                    fr_x = _real_dx + dw - int(fold_r * dw / TEX_WIDTH)
                    self.canvas.create_line(
                        fr_x, _real_dy, fr_x, tp_y,
                        fill="#6688AA", width=1, dash=(6, 4), tags="standee_zone")
                if fold_l > 0 or fold_r > 0:
                    self.canvas.create_text(
                        max(2, _real_dx - 6),
                        _real_dy + 14,
                        text="fold →", anchor=tk.E,
                        font=_f(FS["meta"]), fill="#6688AA", tags="standee_zone")
                    self.canvas.create_text(
                        _real_dx + dw + 6,
                        _real_dy + 14,
                        text="← fold", anchor=tk.W,
                        font=_f(FS["meta"]), fill="#6688AA", tags="standee_zone")
                # Zone labels
                tp_mid = (tp_y + ft_y) // 2
                title_label = "Title plate"
                footer_label = "Footer / base"
                if title_below:
                    title_label = "Title plate (shown below footer on standee)"
                # Zone labels — positioned just LEFT of the canvas edge
                _label_x = max(2, _real_dx - 6)
                self.canvas.create_text(
                    _label_x, tp_mid,
                    text=title_label, anchor=tk.E,
                    font=_f(FS["meta"]), fill="#FFD84A", tags="standee_zone")
                fo_y_label = _real_dy + int(foe * dh / TEX_HEIGHT) if foe < TEX_HEIGHT else _real_dy + dh
                ft_mid = (ft_y + fo_y_label) // 2
                self.canvas.create_text(
                    _label_x, ft_mid,
                    text=footer_label, anchor=tk.E,
                    font=_f(FS["meta"]), fill="#AA8830", tags="standee_zone")

                # Shape overlays — rounded corners (Standee C) or arch (Standee A)
                corner_r = sz.get("corner_radius", 0)
                arch_cy = sz.get("arch_center_y", 0)
                arch_r = sz.get("arch_radius", 0)

                if arch_cy > 0 and arch_r > 0:
                    # Semicircle arch dome — radius is in texture Y pixels
                    # The arch spans from x=(512-r) to x=(512+r) in texture space
                    # and from y=(cy-r) to y=cy vertically (upper half of circle)
                    acy = _real_dy + int(arch_cy * dh / TEX_HEIGHT)
                    # Convert radius: same value in both X and Y texture pixels,
                    # but display scale differs (dw/1024 vs dh/2048)
                    ar_disp_x = int(arch_r * dw / TEX_WIDTH)
                    ar_disp_y = int(arch_r * dh / TEX_HEIGHT)
                    cx_disp = _real_dx + dw // 2
                    self.canvas.create_arc(
                        cx_disp - ar_disp_x, acy - ar_disp_y,
                        cx_disp + ar_disp_x, acy + ar_disp_y,
                        start=0, extent=180,
                        style=tk.ARC, outline="#6688AA", width=2,
                        dash=(6, 3), tags="standee_zone")
                    # Vertical lines showing where the arch insets from the edges
                    inset_left = _real_dx + int((TEX_WIDTH // 2 - arch_r) * dw / TEX_WIDTH)
                    inset_right = _real_dx + int((TEX_WIDTH // 2 + arch_r) * dw / TEX_WIDTH)
                    self.canvas.create_line(
                        inset_left, _real_dy, inset_left, acy,
                        fill="#6688AA", width=1, dash=(3, 4), tags="standee_zone")
                    self.canvas.create_line(
                        inset_right, _real_dy, inset_right, acy,
                        fill="#6688AA", width=1, dash=(3, 4), tags="standee_zone")
                    # Shoulder line where arch meets rectangle
                    self.canvas.create_line(
                        _real_dx, acy, inset_left, acy,
                        fill="#6688AA", width=1, dash=(3, 4), tags="standee_zone")
                    self.canvas.create_line(
                        inset_right, acy, _real_dx + dw, acy,
                        fill="#6688AA", width=1, dash=(3, 4), tags="standee_zone")
                    self.canvas.create_text(
                        max(2, _real_dx - 6), acy,
                        text="Arch", anchor=tk.E,
                        font=_f(FS["meta"]), fill="#6688AA", tags="standee_zone")

                elif corner_r > 0:
                    # Rounded corners at top — draw quarter-circle arcs
                    r_disp_x = int(corner_r * dw / TEX_WIDTH)
                    r_disp_y = int(corner_r * dh / TEX_HEIGHT)
                    # Top-left corner arc
                    self.canvas.create_arc(
                        _real_dx, _real_dy,
                        _real_dx + 2 * r_disp_x, _real_dy + 2 * r_disp_y,
                        start=90, extent=90,
                        style=tk.ARC, outline="#6688AA", width=1,
                        dash=(4, 3), tags="standee_zone")
                    # Top-right corner arc
                    self.canvas.create_arc(
                        _real_dx + dw - 2 * r_disp_x, _real_dy,
                        _real_dx + dw, _real_dy + 2 * r_disp_y,
                        start=0, extent=90,
                        style=tk.ARC, outline="#6688AA", width=1,
                        dash=(4, 3), tags="standee_zone")
                    # Short horizontal/vertical lines showing where rounding starts
                    self.canvas.create_line(
                        _real_dx, _real_dy + r_disp_y,
                        _real_dx + r_disp_x // 3, _real_dy + r_disp_y,
                        fill="#6688AA", width=1, dash=(3, 3), tags="standee_zone")
                    self.canvas.create_line(
                        _real_dx + r_disp_x, _real_dy,
                        _real_dx + r_disp_x, _real_dy + r_disp_y // 3,
                        fill="#6688AA", width=1, dash=(3, 3), tags="standee_zone")
                    self.canvas.create_line(
                        _real_dx + dw, _real_dy + r_disp_y,
                        _real_dx + dw - r_disp_x // 3, _real_dy + r_disp_y,
                        fill="#6688AA", width=1, dash=(3, 3), tags="standee_zone")
                    self.canvas.create_line(
                        _real_dx + dw - r_disp_x, _real_dy,
                        _real_dx + dw - r_disp_x, _real_dy + r_disp_y // 3,
                        fill="#6688AA", width=1, dash=(3, 3), tags="standee_zone")

        # ── Layout overlay ──────────────────────────────────────────────
        # The layout texture is 2048×2048 square. The background is 1024×2048
        # portrait (dw×dh on screen). At the same scale (dw px = 1024 tex px),
        # the full layout square is dh×dh pixels on screen (dh = dw*2).
        # It is centered on the background rect, extending (dh-dw)//2 to each side.
        # Drawn directly on the Tkinter canvas so it can extend beyond the bg rect.
        layout_n = self._layout_preview.get()
        _is_nr_slot = (getattr(self, '_selected_nr_idx', -1) >= 0)
        if layout_n >= 1 and not _is_nr_slot:
            bc_full = self.pak_cache.get_layout_texture_full(layout_n, "bc")
            if bc_full is not None:
                
        # Position/draw the layout overlay.
        #
        # New behavior:
        #   If USE_LAYOUT_CACHE_GEOMETRY is enabled, draw the layout texture directly
        #   in bg texture space. This means the same PNG that provides the visual
        #   overlay also provides the visible-window geometry.
        #
        # Fallback behavior:
        #   Keep the original calibrated math for the base game's regular layouts.

                if USE_LAYOUT_CACHE_GEOMETRY:
                    # The background preview is 1024×2048.
                    # The full layout texture is 2048×2048.
                    # At the same scale, 2048 layout pixels map to display height dh.
                    #
                    # Since the regular movie background corresponds to the LEFT half
                    # of the 2048×2048 layout, place layout x=0 at bg x=0.
                    sq = dh
                    lx = _real_dx
                    ly = _real_dy

                    _lo_cache_key = ("direct_cache_geometry", layout_n, sq)
                    if getattr(self, "_layout_ovl_cache_key", None) != _lo_cache_key:
                        bc_sq = bc_full.resize((sq, sq), Image.BILINEAR).convert("RGBA")

                        from PIL import ImageChops
                        r, g, b, _ = bc_sq.split()
                        max_ch = ImageChops.lighter(ImageChops.lighter(r, g), b)
                        bc_sq.putalpha(max_ch.point(lambda p: 0 if p < 20 else 255))

                        self._layout_ovl_cache = bc_sq
                        self._layout_ovl_cache_key = _lo_cache_key
                    else:
                        bc_sq = self._layout_ovl_cache

                    # The 2048×2048 layout extends to the right of the 1024×2048 bg.
                    # Fill that outside area so the transparent window does not show black canvas.
                    if lx + sq > _real_dx + dw:
                        self.canvas.create_rectangle(
                            _real_dx + dw, ly,
                            lx + sq, ly + sq,
                            fill="#1a1a1a", outline="", tags="layout_bg"
                        )

                    # Usually not needed, but keep this for safety if the layout ever extends down.
                    if ly + sq > _real_dy + dh:
                        self.canvas.create_rectangle(
                            max(lx, _real_dx), _real_dy + dh,
                            min(lx + sq, _real_dx + dw), ly + sq,
                            fill="#1a1a1a", outline="", tags="layout_bg"
                        )

                    self._layout_photo = ImageTk.PhotoImage(bc_sq)
                    self.canvas.create_image(
                        lx, ly,
                        anchor=tk.NW,
                        image=self._layout_photo
                    )
                
                else:
                    # Fixed direct overlay fallback. Full 2048×2048 layout maps to dh×dh;
                    # the regular VHS background is the left half.
                    sq = dh
                    lx = _real_dx
                    ly = _real_dy
                    _lo_cache_key = ("fixed_direct_fallback", sq)
                    if getattr(self, "_layout_ovl_cache_key", None) != _lo_cache_key:
                        bc_sq = bc_full.resize((sq, sq), Image.BILINEAR).convert("RGBA")
                        from PIL import ImageChops
                        r, g, b, _ = bc_sq.split()
                        max_ch = ImageChops.lighter(ImageChops.lighter(r, g), b)
                        bc_sq.putalpha(max_ch.point(lambda p: 0 if p < 20 else 255))
                        self._layout_ovl_cache = bc_sq
                        self._layout_ovl_cache_key = _lo_cache_key
                    else:
                        bc_sq = self._layout_ovl_cache
                    if lx + sq > _real_dx + dw:
                        self.canvas.create_rectangle(_real_dx + dw, ly, lx + sq, ly + sq,
                            fill="#1a1a1a", outline="", tags="layout_bg")
                    self._layout_photo = ImageTk.PhotoImage(bc_sq)
                    self.canvas.create_image(lx, ly, anchor=tk.NW,
                                            image=self._layout_photo)


        # ── Pan cache: build full composite with overlays + layout baked in ──
        # During middle-mouse pan, we crop from this cached PIL image instead
        # of re-running the full render pipeline. Includes hatch zones and layout.
        try:
            if self._raw_img is not None and hasattr(self, '_full_comp'):
                pan_img = self._full_comp.copy()
                # Bake hatch overlays into the full composite
                nr_standee_mode_c = (getattr(self, '_selected_nr_idx', -1) >= 0
                                     and getattr(self, '_nr_view_mode', None)
                                     and self._nr_view_mode.get() == "Standee")
                if not nr_standee_mode_c:
                    pan_overlay = pan_img.convert("RGBA")
                    self._draw_hidden_overlays_on_image(pan_overlay, dw, dh, ox=0, oy=0)
                    pan_img = pan_overlay.convert("RGB")
                # Bake layout overlay into the full composite (skip for NR slots)
                layout_n_c = self._layout_preview.get()
                _is_nr_c = (getattr(self, '_selected_nr_idx', -1) >= 0)
                if layout_n_c >= 1 and not _is_nr_c:
                    bc_full_c = self.pak_cache.get_layout_texture_full(layout_n_c, "bc")
                    if bc_full_c is not None:
                        sq_c = dh
                        bc_sq_c = bc_full_c.resize((sq_c, sq_c), Image.BILINEAR).convert("RGBA")
                        from PIL import ImageChops
                        r_c, g_c, b_c, _ = bc_sq_c.split()
                        max_ch_c = ImageChops.lighter(ImageChops.lighter(r_c, g_c), b_c)
                        bc_sq_c.putalpha(max_ch_c.point(lambda p: 0 if p < 20 else 255))
                        pan_rgba = pan_img.convert("RGBA")
                        # Full 2048×2048 layout maps to dh×dh. The regular VHS bg is the left half.
                        pan_rgba.paste(bc_sq_c, (0, 0), bc_sq_c)
                        pan_img = pan_rgba.convert("RGB")
                self._pan_full_comp = (pan_img, dw, dh)
            else:
                self._pan_full_comp = None
        except Exception:
            self._pan_full_comp = None

        # ── Canvas overlay buttons ────────────────────────────────────
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()

        # Replace Image button removed from canvas overlay — now in controls toolbar

        # ? — bottom-right of canvas
        qr = 12
        qx = cw - qr - 6;  qy = ch - qr - 6
        self.canvas.create_oval(qx-qr, qy-qr, qx+qr, qy+qr,
            fill=DS["panel"], outline=DS["border"], tags="canvas_help")
        self.canvas.create_text(qx, qy,
            text="?", font=_f(FS["meta"], bold=True),
            fill=DS["text"], anchor=tk.CENTER, tags="canvas_help")
        self.canvas.tag_bind("canvas_help", "<Button-1>",
            lambda e: self._show_canvas_help())

        # ── Controls HUD — bottom-left ────────────────────────────────
        snap_on      = getattr(self, "_snap_enabled", True)
        hud_minimized = getattr(self, "_hud_minimized", False)
        PAD  = 6
        HUD_BG     = "#0B1218"
        HUD_BORDER = "#004D55"    # intentional cyan-tinted border

        if hud_minimized:
            # Show small ⚙ icon at bottom-left, same style as ? icon
            gr = 12
            gx = gr + PAD;  gy = ch - gr - PAD
            self.canvas.create_oval(gx-gr, gy-gr, gx+gr, gy+gr,
                fill=DS["panel"], outline=DS["border"], tags="ctrl_hud_icon")
            self.canvas.create_text(gx, gy,
                text="⚙", font=_f(FS["meta"]+1),
                fill=DS["text"], anchor=tk.CENTER, tags="ctrl_hud_icon")
            self.canvas.tag_bind("ctrl_hud_icon", "<Button-1>",
                lambda e: self._toggle_hud())
        else:
            # Full expanded HUD
            LPAD = 10
            LINE = max(15, FS["meta"] + 6)  # scale line height with font
            KEY_W = max(76, FS["meta"] * 8)  # scale key column width
            GAP   = 8
            rows = [
                ("Scroll",       "Zoom"),
                ("Middle drag",  "Pan viewport"),
                ("Left drag",    "Move image"),
            ]
            hud_h = PAD + LINE + 4 + len(rows)*LINE + 4 + LINE + PAD
            hud_w = max(185, KEY_W + GAP + FS["meta"] * 10 + LPAD * 2)
            hx = PAD
            hy = ch - hud_h - PAD

            # Box
            self.canvas.create_rectangle(hx, hy, hx+hud_w, hy+hud_h,
                fill=HUD_BG, outline=HUD_BORDER, width=1, tags="ctrl_hud")

            y = hy + PAD

            # Header row: "CONTROLS" label + "−" minimize button
            self.canvas.create_text(hx + LPAD, y + LINE//2,
                text="CONTROLS", font=_f(FS["meta"], bold=True),
                fill=DS["text3"], anchor=tk.W, tags="ctrl_hud")
            # Minimize button (−) top-right of header
            min_x = hx + hud_w - LPAD
            self.canvas.create_text(min_x, y + LINE//2,
                text="−", font=_f(FS["body"], bold=True),
                fill=DS["text3"], anchor=tk.E, tags="ctrl_hud_min")
            self.canvas.tag_bind("ctrl_hud_min", "<Button-1>",
                lambda e: self._toggle_hud())
            y += LINE

            # Divider
            self.canvas.create_line(hx+4, y+1, hx+hud_w-4, y+1,
                fill=DS["border"], tags="ctrl_hud")
            y += 4

            # Key/value rows
            for key, val in rows:
                self.canvas.create_text(hx + LPAD + KEY_W, y + LINE//2,
                    text=key, font=_f(FS["meta"], bold=True),
                    fill=DS["text2"], anchor=tk.E, tags="ctrl_hud")
                self.canvas.create_text(hx + LPAD + KEY_W + GAP, y + LINE//2,
                    text=val, font=_f(FS["meta"]),
                    fill=DS["text3"], anchor=tk.W, tags="ctrl_hud")
                y += LINE

            # Divider before snap row
            self.canvas.create_line(hx+4, y+1, hx+hud_w-4, y+1,
                fill=DS["border"], tags="ctrl_hud")
            y += 4

            # Snap toggle — small checkbox (not a pill, less intrusive)
            BOX = 9
            bx = hx + LPAD
            by = y + (LINE - BOX) // 2
            box_bg   = DS["surface"] if snap_on else DS["surface"]
            box_bdr  = DS["cyan"]    if snap_on else DS["border"]
            self.canvas.create_rectangle(bx, by, bx+BOX, by+BOX,
                fill=box_bg, outline=box_bdr, tags="snap_toggle")
            if snap_on:
                # Tick mark — two lines forming a check
                self.canvas.create_line(
                    bx+2, by+BOX//2,
                    bx+BOX//2-1, by+BOX-2,
                    bx+BOX-2, by+2,
                    fill=DS["cyan"], width=1, tags="snap_toggle")
            self.canvas.create_text(bx + BOX + 6, y + LINE//2,
                text="Snap to guides",
                font=_f(FS["meta"]),
                fill=DS["text2"] if snap_on else DS["text3"],
                anchor=tk.W, tags="snap_toggle")
            self.canvas.tag_bind("snap_toggle", "<Button-1>",
                lambda e: self._toggle_snap())
            # Absorb background clicks so they don't propagate to image drag
            self.canvas.tag_bind("ctrl_hud", "<Button-1>",
                lambda e: "break")

        # (Movie name label removed — title shown in right panel and shelf list)

    def _get_checker_tile(self, size=8):
        """Return a cached checker tile for the image background.
        Two near-black grays — visible behind black images, subtle otherwise.
        """
        key = f"_checker_tile_{size}"
        if hasattr(self, key):
            return getattr(self, key)
        c1 = (26, 26, 26)    # #1a1a1a
        c2 = (45, 45, 45)    # #2d2d2d
        tile = Image.new("RGB", (size * 2, size * 2), c1)
        from PIL import ImageDraw as _ID
        d = _ID.Draw(tile)
        d.rectangle([size, 0, size*2-1, size-1],   fill=c2)
        d.rectangle([0,    size, size-1, size*2-1], fill=c2)
        setattr(self, key, tile)
        return tile

    def _fill_checker(self, img, x, y, w, h):
        """Tile the checker pattern into a region of img at (x,y) size w×h."""
        tile = self._get_checker_tile()
        tw   = tile.width
        for ty in range(0, h, tw):
            for tx in range(0, w, tw):
                cx = min(tw, w - tx)
                cy = min(tw, h - ty)
                t  = tile.crop((0, 0, cx, cy)) if (cx < tw or cy < tw) else tile
                img.paste(t, (x + tx, y + ty))

    def _get_hatch_tile(self):
        """Return a cached 16x16 RGBA hatch tile. Built once, reused."""
        if getattr(self, "_hatch_tile_cache", None) is not None:
            return self._hatch_tile_cache
        from PIL import ImageDraw as _ID
        T = 16
        tile = Image.new("RGBA", (T, T), (59, 0, 0, 45))   # fill: ~35% opacity
        d = _ID.Draw(tile)
        # Two diagonal lines per tile (45°)
        d.line([(0, 0), (T, T)],         fill=(200, 30, 30, 80), width=1)
        d.line([(0-T//2, 0), (T//2, T)], fill=(200, 30, 30, 80), width=1)
        d.line([(T//2, 0), (T+T//2, T)], fill=(200, 30, 30, 80), width=1)
        self._hatch_tile_cache = tile
        return tile

    def _draw_hidden_overlays_on_image(self, composite, dw, dh, ox=0, oy=0):
        """Draw hatch zones into PIL composite — tiled pattern, O(1) regardless of size.
        ox, oy: pixel offset into the full dw×dh texture rect (for cropped composites).
        Uses per-layout visible area when a layout is selected.
        """
        layout_n = self._layout_preview.get() if hasattr(self, '_layout_preview') else 0
        # Use the saved layout from slot data when overlay is off
        if layout_n < 1 and self.selected:
            _sel_name = self.selected.get("name", "")
            _sel_genre = self.selected.get("genre", "")
            _sel_dt = GENRE_DATATABLE.get(_sel_genre, "")
            _sel_slot = next((s for s in CLEAN_DT_SLOT_DATA.get(_sel_dt, [])
                              if s.get("bkg_tex") == _sel_name), None)
            if _sel_slot:
                layout_n = _sel_slot.get("ls", 0)
        vis = get_layout_visible_rect(layout_n) if layout_n >= 1 else None
        if vis is not None:
            vis_top, vis_bot, vis_left, vis_right = vis
            hp_top    = int(vis_top * dh / TEX_HEIGHT)
            hp_bottom = int((TEX_HEIGHT - vis_bot) * dh / TEX_HEIGHT)
            hp_left   = int(vis_left * dw / TEX_WIDTH)
            hp_right  = int((TEX_WIDTH - vis_right) * dw / TEX_WIDTH)
        else:
            hp_bottom = int(HIDDEN_BOTTOM * dh / TEX_HEIGHT)
            hp_top    = int(HIDDEN_TOP    * dh / TEX_HEIGHT)
            hp_left   = int(HIDDEN_LEFT   * dw / TEX_WIDTH)
            hp_right  = int(HIDDEN_RIGHT  * dw / TEX_WIDTH)

        cw, ch = composite.size  # actual composite size (may be smaller than dw×dh)

        tile = self._get_hatch_tile()
        T    = tile.width

        def fill_hatch(rx1, ry1, rx2, ry2):
            # Translate zone to composite coordinates, clip to composite bounds
            rx1 -= ox; rx2 -= ox; ry1 -= oy; ry2 -= oy
            rx1 = max(0, rx1); ry1 = max(0, ry1)
            rx2 = min(cw, rx2); ry2 = min(ch, ry2)
            if rx2 <= rx1 or ry2 <= ry1:
                return
            rw = max(1, rx2 - rx1)
            rh = max(1, ry2 - ry1)
            # Tile the hatch pattern — crop each tile to stay within zone bounds
            for ty in range(0, rh, T):
                for tx in range(0, rw, T):
                    # Crop tile if it would extend past zone boundary
                    cx2 = min(T, rw - tx)
                    cy2 = min(T, rh - ty)
                    t_crop = tile.crop((0, 0, cx2, cy2)) if (cx2 < T or cy2 < T) else tile
                    composite.paste(t_crop, (rx1 + tx, ry1 + ty), t_crop)
            # Bright border
            from PIL import ImageDraw as _ID
            _ID.Draw(composite).rectangle(
                [rx1, ry1, rx2-1, ry2-1],
                outline=(200, 30, 30, 120), width=1)

        if hp_bottom > 0: fill_hatch(0, dh-hp_bottom, dw, dh)
        if hp_top    > 0: fill_hatch(0, 0, dw, hp_top)
        if hp_left   > 0: fill_hatch(0, 0, hp_left, dh)
        if hp_right  > 0: fill_hatch(dw-hp_right, 0, dw, dh)

    def _draw_safe_border_on_canvas(self, dx, dy, dw, dh, show_labels=False):
        """Draw the cyan safe-zone border as canvas items (1 rect, no hatch).
        Uses per-layout visible area matching the layout overlay position.
        Labels are positioned to the LEFT of the canvas, outside the image."""
        layout_n = self._layout_preview.get() if hasattr(self, '_layout_preview') else 0
        # Use the saved layout from slot data when overlay is off
        if layout_n < 1 and self.selected:
            _sel_name = self.selected.get("name", "")
            _sel_genre = self.selected.get("genre", "")
            _sel_dt = GENRE_DATATABLE.get(_sel_genre, "")
            _sel_slot = next((s for s in CLEAN_DT_SLOT_DATA.get(_sel_dt, [])
                              if s.get("bkg_tex") == _sel_name), None)
            if _sel_slot:
                layout_n = _sel_slot.get("ls", 0)
        vis = get_layout_visible_rect(layout_n) if layout_n >= 1 else None
        if vis is not None:
            vis_top, vis_bot, vis_left, vis_right = vis
            hp_top    = int(vis_top * dh / TEX_HEIGHT)
            hp_bottom = int((TEX_HEIGHT - vis_bot) * dh / TEX_HEIGHT)
            hp_left   = int(vis_left * dw / TEX_WIDTH)
            hp_right  = int((TEX_WIDTH - vis_right) * dw / TEX_WIDTH)
        else:
            hp_bottom = int(HIDDEN_BOTTOM * dh / TEX_HEIGHT)
            hp_top    = int(HIDDEN_TOP    * dh / TEX_HEIGHT)
            hp_left   = int(HIDDEN_LEFT   * dw / TEX_WIDTH)
            hp_right  = int(HIDDEN_RIGHT  * dw / TEX_WIDTH)

        sx1 = dx + hp_left
        sy1 = dy + hp_top
        sx2 = dx + dw - max(hp_right, 1)
        sy2 = dy + dh - hp_bottom

        # Only draw helper lines and labels when layout overlay is OFF
        overlay_on = getattr(self, '_layout_overlay_var', None)
        overlay_active = overlay_on.get() if overlay_on else False
        if not overlay_active:
            self.canvas.create_rectangle(sx1, sy1, sx2, sy2,
                outline=DS["cyan"], width=1, dash=(6, 3), tags="safe_border")

            if show_labels:
                # Labels positioned just left of the canvas edge
                _label_x = max(2, dx - 6)
                safe_mid_y = (sy1 + sy2) // 2
                self.canvas.create_text(_label_x, sy1 + 10,
                    text="Tape visible area", anchor=tk.E,
                    font=_f(FS["meta"]), fill=DS["cyan"], tags="overlay_label")
                self.canvas.create_text(_label_x, dy + 10,
                    text="Full canvas bounds", anchor=tk.E,
                    font=_f(FS["meta"]), fill=DS["text3"], tags="overlay_label")

    def _draw_preview_image(self, texture, img):
        """Legacy redirect to new render system."""
        if self.selected != texture:
            return
        self._render_preview()


    def _draw_preview(self):
        """Called on resize or selection change."""
        if not self.selected:
            cw = self.canvas.winfo_width()
            ch = self.canvas.winfo_height()
            self.canvas.delete("all")
            if cw > 10 and ch > 10:
                genre = self._genre_var.get()
                if genre == "New Releases":
                    tab_has = bool(NR_SLOT_DATA)
                elif genre == "All Movies":
                    tab_has = self._has_custom_movies()
                else:
                    dt = GENRE_DATATABLE.get(genre)
                    if dt:
                        base_count = GENRES.get(genre, {}).get("bkg", 0)
                        tab_has = len(CLEAN_DT_SLOT_DATA.get(dt, [])) > base_count
                    else:
                        tab_has = False
                if tab_has:
                    # Movies exist but none selected
                    self.canvas.create_text(cw//2, ch//2,
                        text="Select a movie to get started",
                        font=_f(FS["body"]), fill=DS["text3"])
                else:
                    # No movies at all — onboarding
                    self.canvas.create_text(cw//2, ch//2 - 20,
                        text="📼", font=("Segoe UI Emoji", 36),
                        fill="#1A2A2A")
                    self.canvas.create_text(cw//2, ch//2 + 30,
                        text="Your shelf is empty",
                        font=_f(FS["body"], bold=True), fill=DS["text3"])
                    self.canvas.create_text(cw//2, ch//2 + 55,
                        text="Add a movie from the left panel to get started",
                        font=_f(FS["meta"]), fill=DS["text3"])
            # Update viewport state (hide controls)
            self._update_viewport_state()
            return
        # If we have cached images, render immediately
        if self._raw_img is not None or self._base_img is not None:
            self._render_preview()
        else:
            # Need to load from disk/pak — show watch cursor while loading
            self.canvas.config(cursor="watch")
            t = threading.Thread(target=self._load_preview_bg,
                                 args=(self.selected,), daemon=True)
            t.start()

    # ---- Actions ----

    def _upload(self):
        if not self.selected:
            messagebox.showwarning("No Selection", "Please select a texture slot first.")
            return
        path = filedialog.askopenfilename(
            title="Select replacement image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp"), ("All", "*.*")]
        )
        if not path:
            return
        name = self.selected["name"]
        # Preserve existing offset/zoom if re-uploading
        existing = self.replacements.get(name, {})
        self.replacements[name] = {
            "path":     path,
            "offset_x": existing.get("offset_x", 0) if isinstance(existing, dict) else 0,
            "offset_y": existing.get("offset_y", 0) if isinstance(existing, dict) else 0,
            "zoom":     existing.get("zoom", 1.0)   if isinstance(existing, dict) else 1.0,
        }
        if name in self.pak_cache._cache:
            del self.pak_cache._cache[name]
        # Clear in-memory cache so new image loads
        self._raw_img  = None
        self._base_img = None
        save_replacements(self.replacements)
        self._mark_edited(name)
        self._refresh_shelf_keep_scroll()
        self.canvas.config(cursor="watch")
        # Show overlay labels briefly on new image load
        self._show_overlay_labels = True
        if hasattr(self, "_overlay_label_job") and self._overlay_label_job:
            self.root.after_cancel(self._overlay_label_job)
        self._overlay_label_job = self.root.after(
            3000, lambda: setattr(self, "_show_overlay_labels", False))
        t = threading.Thread(target=self._load_preview_bg,
                             args=(self.selected,), daemon=True)
        t.start()
        self._refresh_stats()
        self._update_info_row()

    def _add_movie_slot(self):
        """Show dialog to add a new movie slot to a genre."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Add Movie Slot")
        dlg.configure(bg=C["bg"])
        dlg.focus_force(); self.lift() if hasattr(self, "lift") else None
        dlg.resizable(True, True)

        # Override DPI scaling so declared pixels = actual pixels
        try:
            dlg.tk.call('tk', 'scaling', 1.0)
        except Exception:
            pass

        def lbl(text):
            tk.Label(dlg, text=text, font=("Courier New", 9), fg=C["text_dim"],
                     bg=C["bg"], anchor=tk.W).pack(fill=tk.X, padx=20, pady=(6,1))

        tk.Label(dlg, text="＋  ADD MOVIE SLOT",
                 font=("Courier New", 12, "bold"), fg=C["pink"], bg=C["bg"],
                 pady=12).pack(fill=tk.X, padx=20)

        # Genre
        supported = [g for g in GENRES if GENRE_DATATABLE.get(g) in CLEAN_DT_SLOT_DATA and g not in HIDDEN_GENRES]
        genre_var = tk.StringVar(value=supported[0] if supported else "Horror")
        lbl("Genre:")
        genre_cb = ttk.Combobox(dlg, textvariable=genre_var, values=supported,
                                state="readonly", font=("Courier New", 9))
        genre_cb.pack(fill=tk.X, padx=20)

        slot_info_var = tk.StringVar()
        slot_info_lbl = tk.Label(dlg, textvariable=slot_info_var,
                                 font=("Courier New", 8), fg=C["green"], bg=C["bg"],
                                 anchor=tk.W)
        slot_info_lbl.pack(fill=tk.X, padx=20)

        def _update_slot_info(*_):
            genre   = genre_var.get()
            dt_name = GENRE_DATATABLE.get(genre)
            info    = GENRES.get(genre, {})
            base    = info.get("bkg", 0)
            cap     = info.get("bkg_max")
            total   = len(CLEAN_DT_SLOT_DATA.get(dt_name, []))
            custom  = total - base
            if cap:
                remaining = cap - total
                colour = "#44dd88" if remaining > 10 else "#ffaa00" if remaining > 0 else "#ff4444"
                slot_info_lbl.config(fg=colour)
                slot_info_var.set(f"Slots used: {total}/{cap}  ({custom} custom,  {remaining} remaining)")
            else:
                slot_info_var.set(f"Slots used: {total}  ({custom} custom)")

        _update_slot_info()

        # Title
        lbl(f"Movie title (max {MOVIE_TITLE_MAX_BYTES} UTF-8 bytes):")
        title_var = tk.StringVar()
        title_entry = tk.Entry(dlg, textvariable=title_var, bg=C["input_bg"], fg="white",
                               insertbackground=C["text"], font=("Courier New", 9), relief=tk.FLAT)
        title_entry.pack(fill=tk.X, padx=20, ipady=4)
        title_entry.focus_set()

        # Stars
        star_labels = list(STAR_OPTIONS.keys())
        star_var = tk.StringVar(value=star_labels[1])
        lbl("Stars / Critic tag:")
        star_cb = ttk.Combobox(dlg, textvariable=star_var, values=star_labels,
                               state="readonly", font=("Courier New", 9))
        star_cb.pack(fill=tk.X, padx=20)

        # Rarity
        rarity_var = tk.StringVar(value=RARITY_OPTIONS[0])
        lbl("Rarity:")
        rarity_cb = ttk.Combobox(dlg, textvariable=rarity_var, values=RARITY_OPTIONS,
                                 state="readonly", font=("Courier New", 9))
        rarity_cb.pack(fill=tk.X, padx=20)

        star_cb.bind("<<ComboboxSelected>>", lambda e: None)
        rarity_cb.bind("<<ComboboxSelected>>", lambda e: None)
        genre_cb.bind("<<ComboboxSelected>>", lambda e: _update_slot_info())

        def do_add():
            title = title_var.get().strip()
            if not title:
                messagebox.showwarning("Missing Title", "Please enter a movie title.", parent=dlg)
                return
            title_err = _movie_title_error(title)
            if title_err:
                messagebox.showwarning("Too Long", title_err, parent=dlg)
                return
            genre   = genre_var.get()
            dt_name = GENRE_DATATABLE.get(genre)
            info    = GENRES.get(genre, {})
            cap     = info.get("bkg_max")
            total   = len(CLEAN_DT_SLOT_DATA.get(dt_name, []))
            if cap and total >= cap:
                messagebox.showerror("Slot Limit Reached",
                    f"{genre} has reached the maximum of {cap} slots.\n\n"
                    f"You have used all {cap} slots.", parent=dlg)
                return
            last2   = STAR_OPTIONS.get(star_var.get(), 93)
            rarity  = rarity_var.get()
            new_tex = add_movie_slot(genre, title, ls=0, lsc=4, last2=last2, rarity=rarity)
            if new_tex:
                self.dt_manager._clean_builders.clear()
                self.dt_manager._titles_cache.clear()
                dlg.destroy()
                self._populate_shelf()
                for i, tex in enumerate(ALL_TEXTURES):
                    if tex["name"] == new_tex:
                        pass  # tile shelf refreshed below
                        self._on_select(None)
                        break
                slot_data  = CLEAN_DT_SLOT_DATA.get(GENRE_DATATABLE.get(genre), [])
                added_slot = next((s for s in slot_data if s["bkg_tex"] == new_tex), None)
                sku_val    = added_slot["sku"] if added_slot else 0
                messagebox.showinfo("Slot Added",
                    f"✅  '{title}' added as {new_tex}\n\n"
                    f"SKU: {sku_val}  →  {sku_display(sku_val)}\n\n"
                    f"Upload a cover image for this slot,\n"
                    f"then Build & Install to apply.",
                    parent=self.root)
            else:
                messagebox.showerror("Error", f"Could not add slot to {genre}.", parent=dlg)

        ttk.Separator(dlg).pack(fill=tk.X, padx=8, pady=8)

        tk.Button(dlg, text="✅  Add Slot", command=do_add,
                  bg=C["green"], fg=C["text"], activebackground=C["green"],
                  font=("Courier New", 10, "bold"), relief=tk.FLAT,
                  pady=10, cursor="hand2").pack(fill=tk.X, padx=20, pady=(0,16))

        dlg.bind("<Return>", lambda e: do_add())
        dlg.update_idletasks()
        dlg.minsize(dlg.winfo_reqwidth(), dlg.winfo_reqheight())


    def _refresh_slot_rating(self):
        """Update the star display, rarity dropdown and catalog ID for selected slot."""
        if not self.selected:
            self._update_star_display(None)
            self._catalog_id_var.set("—")
            return

        name  = self.selected["name"]
        genre = self.selected["genre"]
        dt    = GENRE_DATATABLE.get(genre)
        if not dt:
            self._update_star_display(None)
            self._catalog_id_var.set("—")
            return

        for slot in CLEAN_DT_SLOT_DATA.get(dt, []):
            if slot.get("bkg_tex") == name:
                sku = slot.get("sku", 0)
                self._catalog_id_var.set(str(sku) if sku else "—")
                self._update_star_display(sku)
                # Set rarity dropdown
                rarity = sku_to_rarity(sku) if sku else "Common"
                self._rarity_var.set(rarity)
                # Sync Old tape checkbox
                if hasattr(self, "_old_tape_var"):
                    self._old_tape_var.set(rarity in ("Common (Old)", "Limited Edition (holo)"))
                if hasattr(self, "_update_rarity_buttons"):
                    self._update_rarity_buttons()
                # Note only shown for Limited
                if hasattr(self, "_rar_note"):
                    self._rar_note.config(
                        text="Included with Limited."
                        if rarity == "Limited Edition (holo)" else "")
                # Old cb state
                if hasattr(self, "_old_cb"):
                    if rarity == "Limited Edition (holo)":
                        self._old_cb.config(state=tk.DISABLED, fg=DS["text3"])
                    else:
                        self._old_cb.config(state=tk.NORMAL, fg=DS["text2"])
                return

        self._update_star_display(None)
        self._catalog_id_var.set("—")
    def _edit_slot_rating(self):
        """Modal to change stars and rarity for the selected slot."""
        if not self.selected:
            return
        name    = self.selected["name"]
        genre   = self.selected["genre"]
        dt_name = GENRE_DATATABLE.get(genre)
        if not dt_name:
            return
        slot = next((s for s in CLEAN_DT_SLOT_DATA.get(dt_name, [])
                     if s.get("bkg_tex") == name), None)
        if slot is None:
            messagebox.showinfo("Not editable",
                                f"{name} is not in the custom slot table.",
                                parent=self.root)
            return

        old_sku   = slot.get("sku", 0)
        old_last2 = old_sku % 100
        old_holo  = _sku_is_holo(old_sku)
        slot_list = CLEAN_DT_SLOT_DATA.get(dt_name, [])
        slot_idx  = next((i for i, s in enumerate(slot_list)
                          if s.get("bkg_tex") == name), None)

        dlg = tk.Toplevel(self.root)
        dlg.title("Edit Stars / Rarity")
        dlg.geometry("400x420")
        dlg.configure(bg=C["bg"])
        dlg.focus_force(); self.lift() if hasattr(self, "lift") else None
        dlg.resizable(False, False)

        tk.Label(dlg, text=f"\u270f  EDIT RATING  \u2014  {name}",
                 font=("Courier New", 10, "bold"), fg=C["pink"], bg=C["bg"],
                 pady=10).pack(fill=tk.X)

        frame = tk.Frame(dlg, bg=C["bg"], padx=20)
        frame.pack(fill=tk.BOTH, expand=True)

        def row(label, widget_fn):
            tk.Label(frame, text=label, font=("Courier New", 9), fg=C["text_dim"],
                     bg=C["bg"], anchor=tk.W).pack(fill=tk.X, pady=(6, 1))
            return widget_fn()

        star_labels = list(STAR_OPTIONS.keys())
        best_label  = min(star_labels, key=lambda l: abs(STAR_OPTIONS[l] - old_last2))
        star_var    = tk.StringVar(value=best_label)

        def make_stars():
            cb = ttk.Combobox(frame, textvariable=star_var,
                              values=star_labels, state="readonly",
                              font=("Courier New", 9))
            cb.pack(fill=tk.X)
            cb.bind("<<ComboboxSelected>>", lambda e: _update_preview())
            return cb
        row("Stars / Critic tag:", make_stars)

        old_is_old  = _sku_is_old(old_sku)
        if old_holo:
            default_rarity = "Limited Edition (holo)" if old_holo else "Common (Old)" if old_is_old else "Common"
        elif old_is_old:
            default_rarity = "Common (Old)"
        else:
            default_rarity = "Common"
        rarity_var = tk.StringVar(value=default_rarity)

        def make_rarity():
            cb = ttk.Combobox(frame, textvariable=rarity_var,
                              values=RARITY_OPTIONS, state="readonly",
                              font=("Courier New", 9))
            cb.pack(fill=tk.X)
            cb.bind("<<ComboboxSelected>>", lambda e: _update_preview())
            return cb
        row("Rarity:", make_rarity)

        # ── Layout Color ──
        # Layout style is fixed; only the existing color field remains editable.
        ls_var  = tk.IntVar(value=FIXED_REGULAR_LAYOUT)
        lsc_var = tk.IntVar(value=slot.get("lsc", 4))

        tk.Label(frame, text=f"Layout: fixed {FIXED_REGULAR_LAYOUT}", font=("Courier New", 9),
                 fg=C["text_dim"], bg=C["bg"], anchor=tk.W).pack(fill=tk.X, pady=(10,1))

        tk.Label(frame, text="Layout Color  (1–10):", font=("Courier New", 9),
                 fg=C["text_dim"], bg=C["bg"], anchor=tk.W).pack(fill=tk.X, pady=(6,1))
        lsc_sb = tk.Spinbox(frame, textvariable=lsc_var, from_=1, to=10,
                            width=6, font=("Courier New", 9),
                            bg=C["input_bg"], fg="white", buttonbackground="#1a3a6a",
                            relief=tk.FLAT, insertbackground=C["text"])
        lsc_sb.pack(anchor=tk.W)

        preview_var = tk.StringVar(value="")
        tk.Label(frame, textvariable=preview_var,
                 font=("Courier New", 8), fg=C["text_dim"], bg=C["bg"],
                 anchor=tk.W).pack(fill=tk.X, pady=(8, 0))

        def _update_preview():
            l2     = STAR_OPTIONS.get(star_var.get(), 93)
            rarity = rarity_var.get()
            idx    = (slot_idx + 1) if slot_idx is not None else 1
            sku    = generate_sku(genre, idx, last2=l2, rarity=rarity)
            preview_var.set(f"New SKU: {sku}  \u2192  {sku_display(sku)}")

        _update_preview()

        def do_apply():
            l2      = STAR_OPTIONS.get(star_var.get(), 93)
            rarity  = rarity_var.get()
            idx     = (slot_idx + 1) if slot_idx is not None else 1
            new_sku = generate_sku(genre, idx, last2=l2, rarity=rarity)
            slot["sku"] = new_sku
            try:
                slot["ls"]  = FIXED_REGULAR_LAYOUT
                slot["lsc"] = max(1, min(10, int(lsc_var.get())))
            except (ValueError, tk.TclError):
                pass
            save_custom_slots()
            if hasattr(self, "dt_manager"):
                self.dt_manager._clean_builders.clear()
                self.dt_manager._titles_cache.clear()
            self._refresh_slot_rating()
            dlg.destroy()

        tk.Button(dlg, text="\u2705  Apply",
                  command=do_apply,
                  bg=C["green"], fg=C["text"], activebackground=C["green"],
                  font=("Courier New", 10, "bold"), relief=tk.FLAT,
                  pady=10, cursor="hand2").pack(fill=tk.X, padx=20, pady=12)

    # ------------------------------------------------------------------
    # Fast Movie Editor
    # ------------------------------------------------------------------

    def _open_movie_editor(self):
        """Fast editor: all movie properties on one screen, edit or create."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Movie Editor")
        dlg.configure(bg=C["bg"])
        dlg.resizable(False, False)
        dlg.focus_force(); self.lift() if hasattr(self, "lift") else None

        BG   = "#1a1a2e"
        FG   = "#ffffff"
        ACC  = "#e94560"
        ENTR = "#0f3460"
        FONT = ("Courier New", 9)

        # ── collect initial state from selected slot ──
        sel        = self.selected
        init_genre = (sel["genre"] if sel else None)
        init_bkg   = (sel["name"]  if sel else None)
        supported  = [g for g in GENRES
                      if GENRE_DATATABLE.get(g) in CLEAN_DT_SLOT_DATA
                      and g not in HIDDEN_GENRES]
        if init_genre not in supported:
            init_genre = supported[0] if supported else ""

        def _slot_list(genre):
            dt = GENRE_DATATABLE.get(genre, "")
            return CLEAN_DT_SLOT_DATA.get(dt, [])

        def _find_slot(genre, bkg):
            return next((s for s in _slot_list(genre)
                         if s.get("bkg_tex") == bkg), None)

        init_slot  = _find_slot(init_genre, init_bkg) if init_bkg else None
        init_title = ""
        if init_slot and init_bkg:
            init_tex = next((t for t in ALL_TEXTURES if t["name"] == init_bkg), None)
            if init_tex:
                entries = self.dt_manager.get_titles_for_texture(init_tex)
                if entries:
                    init_title = entries[0]["title"].rstrip()

        init_sku    = init_slot["sku"] if init_slot else 0
        init_last2  = init_sku % 100 if init_sku else 93
        star_labels = list(STAR_OPTIONS.keys())
        best_star   = min(star_labels, key=lambda l: abs(STAR_OPTIONS[l] - init_last2))

        if init_sku and _sku_is_holo(init_sku):
            init_rarity = "Limited Edition (holo)" if _sku_is_holo(init_sku) else "Common (Old)" if _sku_is_old(init_sku) else "Common"
        elif init_sku and _sku_is_old(init_sku):
            init_rarity = "Common (Old)"
        else:
            init_rarity = "Common"

        # ── header ──
        tk.Label(dlg, text="🎬  MOVIE EDITOR",
                 font=("Courier New", 13, "bold"), fg=ACC, bg=BG,
                 pady=10, padx=16, anchor=tk.W).pack(fill=tk.X)

        ttk.Separator(dlg).pack(fill=tk.X, padx=8)

        # ── mode ──
        mode_var = tk.StringVar(value="edit" if init_slot else "new")
        mf = tk.Frame(dlg, bg=BG, padx=16, pady=6)
        mf.pack(fill=tk.X)
        tk.Label(mf, text="Mode:", font=FONT, fg=C["text_dim"], bg=BG,
                 width=12, anchor=tk.W).pack(side=tk.LEFT)
        tk.Radiobutton(mf, text="Edit existing", variable=mode_var, value="edit",
                       bg=BG, fg=FG, selectcolor=ACC, activebackground=BG,
                       font=FONT, command=lambda: _on_mode()).pack(side=tk.LEFT, padx=(0,10))
        tk.Radiobutton(mf, text="Create new", variable=mode_var, value="new",
                       bg=BG, fg=FG, selectcolor=ACC, activebackground=BG,
                       font=FONT, command=lambda: _on_mode()).pack(side=tk.LEFT)

        def _row(label, widget):
            f = tk.Frame(dlg, bg=BG, padx=16, pady=3)
            f.pack(fill=tk.X)
            tk.Label(f, text=label, font=FONT, fg=C["text_dim"], bg=BG,
                     width=12, anchor=tk.W).pack(side=tk.LEFT)
            widget(f)
            return f

        # ── genre ──
        genre_var = tk.StringVar(value=init_genre)
        def _w_genre(f):
            cb = ttk.Combobox(f, textvariable=genre_var, values=supported,
                              state="readonly", font=FONT, width=28)
            cb.pack(side=tk.LEFT, fill=tk.X, expand=True)
            cb.bind("<<ComboboxSelected>>", lambda e: _on_genre())
        _row("Genre:", _w_genre)

        # ── slot (edit mode) ──
        slot_var = tk.StringVar()
        slot_row = tk.Frame(dlg, bg=BG, padx=16, pady=3)
        slot_row.pack(fill=tk.X)
        tk.Label(slot_row, text="Slot:", font=FONT, fg=C["text_dim"], bg=BG,
                 width=12, anchor=tk.W).pack(side=tk.LEFT)
        slot_cb = ttk.Combobox(slot_row, textvariable=slot_var,
                               state="readonly", font=FONT, width=28)
        slot_cb.pack(side=tk.LEFT, fill=tk.X, expand=True)
        slot_cb.bind("<<ComboboxSelected>>", lambda e: _on_slot_select())

        # ── title ──
        title_var = tk.StringVar(value=init_title)
        title_entry = [None]
        def _w_title(f):
            e = tk.Entry(f, textvariable=title_var, bg=ENTR, fg=FG,
                         insertbackground=FG, font=FONT, relief=tk.FLAT, width=28)
            e.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
            title_entry[0] = e
        _row("Title:", _w_title)
        tk.Label(dlg, text=f"  max {MOVIE_TITLE_MAX_BYTES} UTF-8 bytes", font=("Courier New", 7),
                 fg=C["text_dim"], bg=BG).pack(anchor=tk.W, padx=16)

        # ── stars ──
        star_var = tk.StringVar(value=best_star)
        def _w_stars(f):
            cb = ttk.Combobox(f, textvariable=star_var, values=star_labels,
                              state="readonly", font=FONT, width=28)
            cb.pack(side=tk.LEFT, fill=tk.X, expand=True)
            cb.bind("<<ComboboxSelected>>", lambda e: _refresh_preview())
        _row("Stars:", _w_stars)

        # ── rarity / old ──
        rarity_var = tk.StringVar(value=init_rarity)
        def _w_rarity(f):
            cb = ttk.Combobox(f, textvariable=rarity_var, values=RARITY_OPTIONS,
                              state="readonly", font=FONT, width=28)
            cb.pack(side=tk.LEFT, fill=tk.X, expand=True)
            cb.bind("<<ComboboxSelected>>", lambda e: _refresh_preview())
        _row("Rarity / Old:", _w_rarity)

        # ── Layout Color ──
        # Layout style is fixed; no per-movie layout selector is shown.
        init_ls  = FIXED_REGULAR_LAYOUT
        init_lsc = init_slot.get("lsc", 4) if init_slot else 4
        ls_var  = tk.IntVar(value=FIXED_REGULAR_LAYOUT)
        lsc_var = tk.IntVar(value=init_lsc)

        def _w_ls(f):
            tk.Label(f, text=f"Fixed layout {FIXED_REGULAR_LAYOUT}",
                     font=("Courier New", 8), fg=C["text_dim"], bg=BG
                     ).pack(side=tk.LEFT)
        _row("Layout:", _w_ls)

        def _w_lsc(f):
            sb = tk.Spinbox(f, textvariable=lsc_var, from_=1, to=10,
                            width=6, font=FONT, bg=ENTR, fg=FG,
                            buttonbackground="#1a3a6a", relief=tk.FLAT,
                            insertbackground=FG)
            sb.pack(side=tk.LEFT)
            tk.Label(f, text="  (1–10)", font=("Courier New", 7), fg=C["text_dim"], bg=BG
                     ).pack(side=tk.LEFT)
        _row("Layout Color:", _w_lsc)

        ttk.Separator(dlg).pack(fill=tk.X, padx=8, pady=(8,0))

        # ── SKU preview ──
        sku_var = tk.StringVar(value="")
        pf = tk.Frame(dlg, bg=BG, padx=16, pady=4)
        pf.pack(fill=tk.X)
        tk.Label(pf, text="SKU preview:", font=FONT, fg=C["text_dim"], bg=BG,
                 width=12, anchor=tk.W).pack(side=tk.LEFT)
        tk.Label(pf, textvariable=sku_var, font=("Courier New", 9, "bold"),
                 fg=C["yellow"], bg=BG, anchor=tk.W).pack(side=tk.LEFT)

        # ── cap info ──
        cap_var = tk.StringVar(value="")
        cap_lbl = tk.Label(dlg, textvariable=cap_var, font=("Courier New", 8),
                           fg=C["green"], bg=BG, padx=16, anchor=tk.W)
        cap_lbl.pack(fill=tk.X)

        ttk.Separator(dlg).pack(fill=tk.X, padx=8, pady=(4,0))

        # ── buttons ──
        bf = tk.Frame(dlg, bg=BG, padx=16, pady=10)
        bf.pack(fill=tk.X)
        apply_btn = tk.Button(bf, text="✅  Apply Changes",
                              bg=C["green"], fg=FG, activebackground=C["green"],
                              font=("Courier New", 10, "bold"), relief=tk.FLAT,
                              pady=8, cursor="hand2", command=lambda: _do_apply())
        apply_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,6))
        tk.Button(bf, text="✕ Close", command=dlg.destroy,
                  bg=C["sel_bg"], fg=FG, relief=tk.FLAT, font=FONT,
                  cursor="hand2", pady=8).pack(side=tk.LEFT)

        # ── helpers ──

        def _refresh_preview():
            genre  = genre_var.get()
            l2     = STAR_OPTIONS.get(star_var.get(), 93)
            rarity = rarity_var.get()
            slots  = _slot_list(genre)
            if mode_var.get() == "edit":
                bkg  = slot_var.get()
                slot = next((s for s in slots if s.get("bkg_tex") == bkg), None)
                idx  = (slots.index(slot) + 1) if slot else 1
            else:
                idx  = len(slots) + 1
            sku = generate_sku(genre, idx, last2=l2, rarity=rarity)
            sku_var.set(f"{sku}  →  {sku_display(sku)}")

        def _refresh_cap():
            genre  = genre_var.get()
            info   = GENRES.get(genre, {})
            slots  = _slot_list(genre)
            total  = len(slots)
            base   = info.get("bkg", 0)
            cap    = info.get("bkg_max")
            if mode_var.get() == "new" and cap:
                rem = cap - total
                col = "#44dd88" if rem > 5 else "#ffaa00" if rem > 0 else "#ff4444"
                cap_lbl.config(fg=col)
                cap_var.set(f"  Slots used: {total}/{cap}  ({total-base} custom, {rem} remaining)")
            else:
                cap_var.set("")

        def _populate_slot_cb():
            genre  = genre_var.get()
            slots  = _slot_list(genre)
            labels = [s["bkg_tex"] for s in slots]
            slot_cb.config(values=labels)
            if init_bkg and init_bkg in labels:
                slot_var.set(init_bkg)
            elif labels:
                slot_var.set(labels[-1])
            else:
                slot_var.set("")

        def _on_slot_select():
            genre = genre_var.get()
            bkg   = slot_var.get()
            if not bkg:
                return
            slot = _find_slot(genre, bkg)
            if slot:
                init_tex = next((t for t in ALL_TEXTURES if t["name"] == bkg), None)
                if init_tex:
                    entries = self.dt_manager.get_titles_for_texture(init_tex)
                    if entries:
                        title_var.set(entries[0]["title"].rstrip())
                sku = slot.get("sku", 0)
                if sku:
                    l2   = sku % 100
                    best = min(star_labels, key=lambda l: abs(STAR_OPTIONS[l] - l2))
                    star_var.set(best)
                    rarity_var.set("Limited Edition (holo)" if _sku_is_holo(sku)
                                   else "Common (Old)" if _sku_is_old(sku) else "Common")
                ls_var.set(FIXED_REGULAR_LAYOUT)
                lsc_var.set(slot.get("lsc", 4))
            _refresh_preview()

        def _on_genre():
            _populate_slot_cb()
            _on_slot_select()
            _refresh_cap()
            _refresh_preview()

        def _on_mode():
            if mode_var.get() == "edit":
                slot_cb.config(state="readonly")
                tk.Label(slot_row, text="", bg=BG).pack_forget()  # no-op, just trigger
                for w in slot_row.winfo_children():
                    if isinstance(w, tk.Label):
                        w.config(fg=C["text_dim"])
                apply_btn.config(text="✅  Apply Changes")
            else:
                slot_cb.config(state="disabled")
                for w in slot_row.winfo_children():
                    if isinstance(w, tk.Label):
                        w.config(fg=C["border"])
                apply_btn.config(text="＋  Create Slot")
            _refresh_cap()
            _refresh_preview()

        def _do_apply():
            genre  = genre_var.get()
            title  = title_var.get().strip()
            l2     = STAR_OPTIONS.get(star_var.get(), 93)
            rarity = rarity_var.get()

            if not title:
                messagebox.showwarning("Missing Title", "Enter a movie title.", parent=dlg)
                return
            title_err = _movie_title_error(title)
            if title_err:
                messagebox.showwarning("Too Long", title_err, parent=dlg)
                return

            if mode_var.get() == "edit":
                bkg   = slot_var.get()
                slots = _slot_list(genre)
                slot  = next((s for s in slots if s.get("bkg_tex") == bkg), None)
                if not slot:
                    messagebox.showerror("Not found", f"Slot '{bkg}' not found.", parent=dlg)
                    return
                idx     = slots.index(slot) + 1
                new_sku = generate_sku(genre, idx, last2=l2, rarity=rarity)
                slot["sku"]     = new_sku
                slot["pn_name"] = title
                try:
                    slot["ls"]  = FIXED_REGULAR_LAYOUT
                    slot["lsc"] = max(1, min(10, int(lsc_var.get())))
                except (ValueError, tk.TclError):
                    pass
                save_custom_slots()
                bkg_tex = next((t for t in ALL_TEXTURES if t["name"] == bkg), None)
                dt_entries = self.dt_manager.get_titles_for_texture(bkg_tex) if bkg_tex else []
                if dt_entries and title != dt_entries[0]["title"].rstrip():
                    self.dt_manager.patch_title(
                        dt_entries[0]["dt_name"], dt_entries[0]["offset"],
                        dt_entries[0]["original_title"], title, self.title_changes)
                self.dt_manager._clean_builders.clear()
                self.dt_manager._titles_cache.clear()
                self._refresh_slot_rating()
                self._populate_shelf()
                sku_var.set(f"{new_sku}  →  {sku_display(new_sku)}")
                messagebox.showinfo("Saved",
                    f"✅  {bkg}\nTitle: {title}\nSKU: {new_sku}  →  {sku_display(new_sku)}\n"
                    f"Layout: fixed {FIXED_REGULAR_LAYOUT} / color {slot['lsc']}",
                    parent=dlg)
            else:
                dt_name = GENRE_DATATABLE.get(genre)
                info    = GENRES.get(genre, {})
                cap     = info.get("bkg_max")
                if cap and len(_slot_list(genre)) >= cap:
                    messagebox.showerror("Slot Limit",
                        f"{genre} is full ({cap} slots).", parent=dlg)
                    return
                try:
                    ls_val  = FIXED_REGULAR_LAYOUT
                    lsc_val = max(1, min(10, int(lsc_var.get())))
                except (ValueError, tk.TclError):
                    ls_val, lsc_val = FIXED_REGULAR_LAYOUT, 4
                new_tex = add_movie_slot(genre, title, ls=ls_val, lsc=lsc_val, last2=l2, rarity=rarity)
                if not new_tex:
                    messagebox.showerror("Error", f"Could not add slot to {genre}.", parent=dlg)
                    return
                self.dt_manager._clean_builders.clear()
                self.dt_manager._titles_cache.clear()
                self._populate_shelf()
                for i, tex in enumerate(ALL_TEXTURES):
                    if tex["name"] == new_tex:
                        pass  # tile shelf refreshed below
                        self._on_select(None)
                        break
                slot_data  = CLEAN_DT_SLOT_DATA.get(dt_name, [])
                added      = next((s for s in slot_data if s["bkg_tex"] == new_tex), None)
                sku_val    = added["sku"] if added else 0
                # clear title for rapid batch entry, keep genre/stars/rarity
                title_var.set("")
                _populate_slot_cb()
                _refresh_cap()
                _refresh_preview()
                messagebox.showinfo("Slot Added",
                    f"✅  '{title}' → {new_tex}\nSKU: {sku_val}  →  {sku_display(sku_val)}\n\n"
                    f"Title cleared — ready for next entry.",
                    parent=dlg)

        # ── initial setup ──
        _populate_slot_cb()
        if mode_var.get() == "new":
            slot_cb.config(state="disabled")
            for w in slot_row.winfo_children():
                if isinstance(w, tk.Label):
                    w.config(fg=C["border"])
            apply_btn.config(text="＋  Create Slot")
        _refresh_cap()
        _refresh_preview()
        if title_entry[0]:
            title_entry[0].focus_set()
        dlg.bind("<Return>", lambda e: _do_apply())
        dlg.update_idletasks()
        dlg.minsize(max(dlg.winfo_reqwidth(), 440), dlg.winfo_reqheight())

    def _create_layout_test_slots(self):
        """Obsolete: regular movies now use one fixed layout."""
        messagebox.showinfo(
            "Fixed Layout",
            "Layout test slots are no longer needed. Regular movies now use one fixed layout.",
            parent=self.root)
    def _clear_slot(self):
        if not self.selected:
            return
        name = self.selected["name"]
        if name in self.replacements:
            del self.replacements[name]
            if name in self.pak_cache._cache:
                del self.pak_cache._cache[name]
            save_replacements(self.replacements)
            self._populate_shelf()
            self._draw_preview()
            self._refresh_stats()

    def _clear_all(self):
        if not messagebox.askyesno("Clear All", "Remove all custom images?"):
            return
        self.replacements = {}
        self.pak_cache._cache.clear()
        save_replacements(self.replacements)
        self._populate_shelf()
        self._draw_preview()
        self._refresh_stats()



    def _open_setup(self):
        SetupDialog(self.root, self.config, self._on_setup_complete)

    def _on_setup_complete(self, cfg):
        # Defer processing to avoid event conflicts during dialog destroy
        self.root.after(50, lambda: self._apply_setup(cfg))

    def _apply_setup(self, cfg):
        self.config = cfg
        self.pak_cache = PakCache(
            cfg.get("base_game_pak", ""), cfg.get("repak", "")
        )
        self.dt_manager = DataTableManager(self.pak_cache, self.title_changes)

        # Re-validate: check if all issues are resolved
        issues = []
        for key, name in [("texconv", "texconv.exe"), ("repak", "repak.exe")]:
            if not cfg.get(key) or not os.path.exists(cfg.get(key, "")):
                issues.append(("tool", key, name))
        pak = cfg.get("base_game_pak", "")
        if not pak or not os.path.exists(pak):
            issues.append(("critical", "base_game_pak", "Game pak file"))
        mods = cfg.get("mods_folder", "")
        if mods and not os.path.isdir(mods):
            try:
                os.makedirs(mods, exist_ok=True)
            except Exception:
                issues.append(("tool", "mods_folder", "~mods folder"))

        self._startup_issues = issues

        # Remove warning banner if issues are resolved
        if hasattr(self, '_warning_banner') and self._warning_banner:
            try:
                self._warning_banner.place_forget()
            except Exception:
                pass

        if issues:
            self._show_startup_warning(issues)
        else:
            # All clear — re-enable Ship to Store
            if hasattr(self, '_ship_canvas'):
                self._ship_canvas.bind("<Button-1>", lambda e: self._build())
                self._draw_ship_btn()
            self._update_viewport_state()

        # Toggle dev button visibility
        if hasattr(self, '_dev_btn'):
            try:
                if cfg.get("dev_mode", False):
                    self._dev_btn.pack(side=tk.LEFT, padx=(4, 0))
                else:
                    self._dev_btn.pack_forget()
            except Exception:
                pass

    def _scan_asset_registry(self):
        """Extract AssetRegistry.bin and produce a compact summary. Dev tool."""
        repak = self.config.get("repak","")
        base_pak = self.config.get("base_game_pak","")
        if not repak or not base_pak or not os.path.exists(base_pak):
            messagebox.showerror("Scan Failed",
                "Configure repak.exe and base game pak path first.", parent=self.root)
            return
        import re, tempfile
        tmp = tempfile.mkdtemp()
        ar_dest = os.path.join(tmp, "RetroRewind", "AssetRegistry.bin")
        subprocess.run([repak, "unpack", "-o", tmp, "-f",
                       "-i", "RetroRewind/AssetRegistry.bin", base_pak],
                      capture_output=True, timeout=30)
        if not os.path.exists(ar_dest):
            messagebox.showerror("Scan Failed",
                "Could not extract AssetRegistry.bin from base game pak.", parent=self.root)
            return
        data = open(ar_dest, 'rb').read()
        ar_size = len(data)
        # --- Extract clean asset-path-like strings only ---
        # Match UE-style identifiers: T_Bkg_Hor_01, NewRelease_Details, BP_Standee_01, etc.
        raw_tokens = set(s.decode('ascii','replace')
                         for s in re.findall(rb'[A-Za-z_][A-Za-z0-9_]{4,}', data))
        # Also grab /Game/... paths
        raw_paths = set(s.decode('ascii','replace')
                        for s in re.findall(rb'/Game/[A-Za-z0-9_/]+', data))
        # --- Categorise ---
        # T_New_* textures: group by genre code
        t_new_by_genre = {}  # code -> set of slot nums
        t_new_other = []
        for s in raw_tokens:
            m = re.match(r'T_New_([A-Za-z]{3})_(\d+)$', s)
            if m:
                code, num = m.group(1), int(m.group(2))
                t_new_by_genre.setdefault(code, set()).add(num)
            elif s.startswith('T_New_'):
                t_new_other.append(s)
        # T_Bkg_* textures: group by genre code
        t_bkg_by_genre = {}
        for s in raw_tokens:
            m = re.match(r'T_Bkg_([A-Za-z]{3})_(\d+)$', s)
            if m:
                code, num = m.group(1), int(m.group(2))
                t_bkg_by_genre.setdefault(code, set()).add(num)
        # Standee references
        standee = sorted(set(s for s in raw_tokens | raw_paths
                             if 'tandee' in s.lower() or 'Standee' in s))
        # NewRelease references — just unique tokens, not full string dump
        newrel_tokens = sorted(set(s for s in raw_tokens
                                   if 'newrelease' in s.lower() or 'new_release' in s.lower()))
        newrel_paths = sorted(set(s for s in raw_paths
                                  if 'NewRelease' in s or 'new_release' in s.lower()))
        # --- Build compact report ---
        R = []
        R.append(f"AssetRegistry Scan — {ar_size:,} bytes")
        R.append("=" * 55)
        # T_New summary
        R.append(f"\n--- T_New_* textures (by genre) ---")
        if t_new_by_genre:
            for code in sorted(t_new_by_genre):
                nums = sorted(t_new_by_genre[code])
                R.append(f"  T_New_{code}: {nums[0]:02d}–{nums[-1]:02d}  ({len(nums)} slots)")
        else:
            R.append("  (none found)")
        if t_new_other:
            R.append(f"  Other T_New: {', '.join(sorted(set(t_new_other))[:10])}")
        # Standee summary
        R.append(f"\n--- Standee refs ({len(standee)}) ---")
        for s in standee[:15]:
            R.append(f"  {s}")
        if not standee:
            R.append("  (none found)")
        if len(standee) > 15:
            R.append(f"  ... and {len(standee)-15} more")
        # NewRelease summary
        R.append(f"\n--- NewRelease refs ---")
        R.append(f"  Tokens: {', '.join(newrel_tokens[:10]) or '(none)'}")
        if newrel_paths:
            R.append(f"  Paths:")
            for p in newrel_paths[:10]:
                R.append(f"    {p}")
        # T_Bkg summary
        R.append(f"\n--- T_Bkg_* slots per genre ---")
        if t_bkg_by_genre:
            for code in sorted(t_bkg_by_genre):
                nums = t_bkg_by_genre[code]
                R.append(f"  T_Bkg_{code}: {min(nums):02d}–{max(nums):02d}  ({len(nums)} registered)")
        else:
            R.append("  (none found)")
        # Total line count for sanity
        R.append(f"\n--- Summary ---")
        R.append(f"  Report: {len(R)+1} lines")
        # Show in a scrollable dialog
        dlg = tk.Toplevel(self.root)
        dlg.title("AssetRegistry Scan")
        dlg.geometry("620x480")
        dlg.configure(bg=C["bg"])
        dlg.focus_force(); self.lift() if hasattr(self, "lift") else None
        txt = tk.Text(dlg, font=("Courier New",9), fg=C["text"], bg=C["card"],
                      wrap=tk.NONE, relief=tk.FLAT)
        vsb = tk.Scrollbar(dlg, command=txt.yview)
        hsb = tk.Scrollbar(dlg, orient=tk.HORIZONTAL, command=txt.xview)
        txt.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        txt.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        report_text = "\n".join(R)
        txt.insert(tk.END, report_text)
        txt.config(state=tk.DISABLED)
        btn_frame = tk.Frame(dlg, bg=C["bg"])
        btn_frame.pack(pady=4)
        tk.Button(btn_frame, text="Copy to Clipboard", font=("Courier New",9),
                  command=lambda: (self.root.clipboard_clear(),
                                   self.root.clipboard_append(report_text)),
                  bg=C["border"], fg=C["text"], relief=tk.FLAT
                  ).pack(side=tk.LEFT, padx=4)
        def _save_report():
            path = filedialog.asksaveasfilename(
                parent=dlg, title="Save Scan Report",
                defaultextension=".txt",
                filetypes=[("Text files","*.txt"),("All files","*.*")],
                initialfile="AssetRegistry_Scan_Result.txt")
            if path:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(report_text)
                messagebox.showinfo("Saved", f"Report saved to:\n{path}", parent=dlg)
        tk.Button(btn_frame, text="Save to File", font=("Courier New",9),
                  command=_save_report,
                  bg=C["border"], fg=C["text"], relief=tk.FLAT
                  ).pack(side=tk.LEFT, padx=4)
        shutil.rmtree(tmp, ignore_errors=True)

    def _extract_asset_registry(self, work_dir):
        """
        Extract AssetRegistry.bin from the base game pak and copy it
        into the mod build folder unchanged.

        The game merges AssetRegistry files from all loaded paks at startup.
        Including a copy here lets us later add new slot entries to extend
        genre caps without modifying the base game pak.

        Returns the path to the copied file, or None on failure.
        """
        base_pak = self.config.get("base_game_pak", "")
        repak    = self.config.get("repak", "")
        if not base_pak or not os.path.exists(base_pak):
            print(f"[AssetRegistry] Base pak not found: '{base_pak}'")
            return None

        extract_dir = self.pak_cache._extract_dir
        # repak extracts preserving the internal pak path, so the file lands at:
        # extract_dir/RetroRewind/AssetRegistry.bin
        ar_extract = os.path.join(extract_dir, "RetroRewind", "AssetRegistry.bin")

        if not os.path.exists(ar_extract):
            os.makedirs(extract_dir, exist_ok=True)
            result = subprocess.run(
                [repak, "unpack", "-o", extract_dir, "-f",
                 "-i", "RetroRewind/AssetRegistry.bin", base_pak],
                capture_output=True, text=True, timeout=30
            )
            print(f"[AssetRegistry] repak stdout: {result.stdout[:200]}")
            print(f"[AssetRegistry] repak stderr: {result.stderr[:200]}")

        if not os.path.exists(ar_extract):
            print(f"[AssetRegistry] Extraction failed — file not at {ar_extract}")
            return None

        # Copy into the build tree at the game's expected root path
        dest = os.path.join(work_dir, "RetroRewind", "AssetRegistry.bin")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(ar_extract, dest)
        return dest

    def _build(self):
        # Allow build even with no replacements if there are custom slots
        has_custom = any(
            len(slots) > GENRES[g]["bkg"]
            for g, dt in GENRE_DATATABLE.items()
            for dt_n, slots in CLEAN_DT_SLOT_DATA.items()
            if dt == dt_n
        )
        has_nr = len(NR_SLOT_DATA) > 0
        if not self.replacements and not has_custom and not has_nr and not CUSTOM_ONLY_MODE:
            messagebox.showwarning("Nothing to Build", "Upload at least one image first.")
            return

        # Pre-build summary: count movies and NR per genre
        genre_data = []  # (name, movie_count, movie_no_img, nr_count, nr_no_img)
        total_movies = 0
        total_nr = 0
        # Count NR per genre
        nr_by_genre = {}
        nr_noimg_by_genre = {}
        for nr in NR_SLOT_DATA:
            g = nr.get("genre", "")
            nr_by_genre[g] = nr_by_genre.get(g, 0) + 1
            if f"NR_{nr['sku']}" not in self.replacements:
                nr_noimg_by_genre[g] = nr_noimg_by_genre.get(g, 0) + 1
        for genre, info in GENRES.items():
            if genre in HIDDEN_GENRES:
                continue
            dt = GENRE_DATATABLE.get(genre)
            if dt:
                base = info.get("bkg", 0)
                custom_slots = CLEAN_DT_SLOT_DATA.get(dt, [])[base:]
                m_count = len(custom_slots)
                m_no_img = sum(1 for s in custom_slots
                               if s.get("bkg_tex", "") not in self.replacements)
                n_count = nr_by_genre.get(genre, 0)
                n_no_img = nr_noimg_by_genre.get(genre, 0)
                genre_data.append((genre, m_count, m_no_img, n_count, n_no_img))
                total_movies += m_count
                total_nr += n_count

        total_all = total_movies + total_nr

        # Custom confirmation dialog — centered on parent
        dlg = tk.Toplevel(self.root)
        dlg.title("Ship to Store")
        dlg.configure(bg=C["bg"])
        dlg.resizable(False, False)
        dlg.focus_force(); self.lift() if hasattr(self, "lift") else None
        dlg.transient(self.root)
        dlg.withdraw()  # hide until positioned

        _result = [False]

        tk.Label(dlg, text=f"Ready to build {total_all} movie{'s' if total_all != 1 else ''}",
                 font=_vcr(13, bold=True), fg=C["text"], bg=C["bg"]
                 ).pack(padx=24, pady=(18, 10))

        # Table frame
        tbl = tk.Frame(dlg, bg=C["bg"])
        tbl.pack(padx=24, fill=tk.X)

        # Column headers — full words, no abbreviations
        _hdr_font = _vcr(9, bold=True)
        _dim = C["text_dim"]
        tk.Label(tbl, text="Genre", font=_hdr_font, fg=_dim,
                 bg=C["bg"], anchor=tk.W).grid(row=0, column=0, sticky="w", padx=(0, 12))
        tk.Label(tbl, text="Movies", font=_hdr_font, fg=_dim,
                 bg=C["bg"], anchor=tk.CENTER).grid(row=0, column=1, padx=6)
        tk.Label(tbl, text="Without Image", font=_hdr_font, fg=_dim,
                 bg=C["bg"], anchor=tk.CENTER).grid(row=0, column=2, padx=6)
        tk.Label(tbl, text="New Releases", font=_hdr_font, fg=_dim,
                 bg=C["bg"], anchor=tk.CENTER).grid(row=0, column=3, padx=6)
        tk.Label(tbl, text="Without Image", font=_hdr_font, fg=_dim,
                 bg=C["bg"], anchor=tk.CENTER).grid(row=0, column=4, padx=(6, 0))

        sep = tk.Frame(tbl, bg=C["border"], height=1)
        sep.grid(row=1, column=0, columnspan=5, sticky="ew", pady=2)

        _empty_fg = "#664444"
        _cell_font = _vcr(9)
        _amber = "#CC8844"

        def _cell(row, col, text, fg, bold=False):
            """Place a centered cell in the table."""
            f = _vcr(9, bold=True) if bold else _cell_font
            tk.Label(tbl, text=text, font=f, fg=fg,
                     bg=C["bg"], anchor=tk.CENTER).grid(
                         row=row, column=col, padx=6)

        row_i = 2
        for genre, m_count, m_no_img, n_count, n_no_img in genre_data:
            has_anything = m_count > 0 or n_count > 0

            # Genre name (left-aligned)
            fg_name = C["text"] if has_anything else _empty_fg
            tk.Label(tbl, text=genre, font=_cell_font, fg=fg_name,
                     bg=C["bg"], anchor=tk.W).grid(row=row_i, column=0, sticky="w", padx=(0, 12))

            # Movies
            _cell(row_i, 1,
                  str(m_count) if m_count > 0 else "—",
                  C["cyan"] if m_count > 0 else _empty_fg)

            # Movies without image
            _cell(row_i, 2,
                  str(m_no_img) if m_no_img > 0 else "—",
                  _amber if m_no_img > 0 else _empty_fg)

            # NR
            _cell(row_i, 3,
                  str(n_count) if n_count > 0 else "—",
                  C["cyan"] if n_count > 0 else _empty_fg)

            # NR without image
            _cell(row_i, 4,
                  str(n_no_img) if n_no_img > 0 else "—",
                  _amber if n_no_img > 0 else _empty_fg)

            row_i += 1

        # Totals row
        sep2 = tk.Frame(tbl, bg=C["border"], height=1)
        sep2.grid(row=row_i, column=0, columnspan=5, sticky="ew", pady=2)
        row_i += 1
        tk.Label(tbl, text="Total", font=_vcr(9, bold=True), fg=C["text"],
                 bg=C["bg"], anchor=tk.W).grid(row=row_i, column=0, sticky="w", padx=(0, 12))
        _cell(row_i, 1,
              str(total_movies) if total_movies > 0 else "—",
              C["cyan"] if total_movies > 0 else _empty_fg, bold=True)
        _cell(row_i, 3,
              str(total_nr) if total_nr > 0 else "—",
              C["cyan"] if total_nr > 0 else _empty_fg, bold=True)

        # Buttons
        btn_f = tk.Frame(dlg, bg=C["bg"])
        btn_f.pack(pady=(16, 18))
        tk.Button(btn_f, text="  Build  ", font=_vcr(11, bold=True),
                  bg=C["cyan"], fg=C["bg"], relief=tk.FLAT, cursor="hand2",
                  padx=20, pady=6,
                  command=lambda: (_result.__setitem__(0, True), dlg.destroy())
                  ).pack(side=tk.LEFT, padx=8)
        tk.Button(btn_f, text="Cancel", font=_vcr(11),
                  bg=C["border"], fg=C["text_dim"], relief=tk.FLAT, cursor="hand2",
                  padx=14, pady=6,
                  command=dlg.destroy
                  ).pack(side=tk.LEFT, padx=8)

        # Center dialog on parent window
        dlg.update_idletasks()
        dw = dlg.winfo_reqwidth()
        dh = dlg.winfo_reqheight()
        px = self.root.winfo_x() + (self.root.winfo_width() - dw) // 2
        py = self.root.winfo_y() + (self.root.winfo_height() - dh) // 2
        dlg.geometry(f"+{max(0, px)}+{max(0, py)}")
        dlg.deiconify()

        dlg.wait_window()
        if not _result[0]:
            return

        texconv = self.config.get("texconv", "")
        repak   = self.config.get("repak", "")
        mods    = self.config.get("mods_folder", "")

        for tool, n in [(texconv,"texconv.exe"),(repak,"repak.exe")]:
            if not os.path.exists(tool):
                messagebox.showerror("Missing Tool",
                    f"{n} not found.\nOpen ⚙ Setup to fix paths.")
                return

        work = os.path.join(OUTPUT_DIR, "build")
        if os.path.exists(work):
            shutil.rmtree(work)
        os.makedirs(work, exist_ok=True)

        success, errors = 0, []

        # Custom slots that have no uploaded image (need T_Bkg clone files)
        custom_slots_needing_files = []
        for dt_name_key, slot_list in CLEAN_DT_SLOT_DATA.items():
            genre_key  = next((g for g, d in GENRE_DATATABLE.items() if d == dt_name_key), None)
            base_count = GENRES[genre_key]["bkg"] if genre_key else 0
            for slot in slot_list[base_count:]:
                tex = next((t for t in ALL_TEXTURES if t["name"] == slot["bkg_tex"]), None)
                if tex and slot["bkg_tex"] not in self.replacements:
                    custom_slots_needing_files.append((tex, slot))

        total = len(self.replacements) + len(custom_slots_needing_files)
        prog = tk.Toplevel(self.root)
        prog.title("Building...")
        prog.geometry("420x130")
        prog.configure(bg=C["bg"])
        prog.focus_force(); self.lift() if hasattr(self, "lift") else None
        prog_lbl = tk.Label(prog, text="Starting...",
                            font=("Courier New", 10), fg=C["text"], bg=C["bg"])
        prog_lbl.pack(pady=18)
        prog_bar = ttk.Progressbar(prog, maximum=max(total, 1), mode='determinate')
        prog_bar.pack(fill=tk.X, padx=20)

        # Build SKU→NR_SLOT_DATA lookup for NR replacements
        _nr_by_stable_key = {}
        for nr in NR_SLOT_DATA:
            _nr_by_stable_key[f"NR_{nr['sku']}"] = nr
        print(f"[Build] Replacement keys: {list(self.replacements.keys())}")

        # NR donor pre-pass: stage T_New_<code>_<NN:03d> in pak_cache._base_dir
        # for every NR slot. The tool ships custom NRs as 3-digit assets only
        # — we don't reuse base game NR slots. Building all donors up-front
        # ensures the replacement loop and ensure_nr_texture both find valid
        # 3-digit source files instead of falling into legacy buggy paths.
        for _nr in NR_SLOT_DATA:
            _gcode = _nr.get("genre_code", "")
            _tnum = _nr.get("tex_num", 1)
            prepare_nr_donor_in_cache(self.pak_cache, _gcode, _tnum)

        # Process user-uploaded textures (T_Bkg + NR injection).
        #
        # The expensive part is prepare_image() + texconv + writing the finished
        # uasset/uexp/ubulk files.  Pak-cache preparation stays sequential on
        # purpose: get_base_files() may extract/clone shared donor files, and
        # parallelizing that layer risks two threads writing the same cache file.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        texture_jobs = []
        replacement_items = list(self.replacements.items())

        for i, (name, entry) in enumerate(replacement_items):
            prog_lbl.config(text=f"Preparing {i+1}/{max(len(replacement_items), 1)}: {name}")
            prog_bar['value'] = i
            prog.update()

            png_path = entry["path"] if isinstance(entry, dict) else entry
            if not os.path.exists(png_path):
                errors.append(f"[E002] Skipped: {name}")
                continue

            # Check if this is an NR replacement (stable key "NR_{sku}")
            nr_slot = _nr_by_stable_key.get(name)
            if nr_slot:
                # NR texture: construct texture dict from NR slot data
                code = nr_slot["genre_code"]
                bkg_tex = nr_slot["bkg_tex"]
                texture = {
                    "name": bkg_tex,
                    "genre": nr_slot["genre"],
                    "folder": f"T_Bkg_{code}",
                    "type": "New Release",
                }
            else:
                texture = next((t for t in ALL_TEXTURES if t["name"] == name), None)

            if not texture:
                errors.append(f"[E002] Skipped: {name}")
                continue

            try:
                base_dir = self.pak_cache.get_base_files(texture)
                texture_jobs.append((name, texture, entry, base_dir))
            except Exception as e:
                import traceback
                print(f"[Build] ERROR preparing {name}: {e}")
                traceback.print_exc()
                errors.append(f"[E001] {name}: {e}")

        def _inject_job(job):
            name, texture, entry, base_dir = job
            inject_texture(texture, entry, work, texconv, base_dir)
            return name

        if texture_jobs:
            workers = min(TEXTURE_BUILD_WORKERS, len(texture_jobs))
            print(f"[Build] Injecting {len(texture_jobs)} texture(s) with {workers} worker(s)")
            prog_lbl.config(text=f"Processing {len(texture_jobs)} texture(s) with {workers} workers...")
            prog.update()

            done_jobs = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_to_name = {pool.submit(_inject_job, job): job[0] for job in texture_jobs}
                for fut in as_completed(future_to_name):
                    name = future_to_name[fut]
                    done_jobs += 1
                    prog_lbl.config(text=f"Processing {done_jobs}/{len(texture_jobs)}: {name}")
                    prog_bar['value'] = done_jobs
                    prog.update()

                    try:
                        fut.result()
                        success += 1
                    except Exception as e:
                        import traceback
                        print(f"[Build] ERROR injecting {name}: {e}")
                        traceback.print_exc()
                        errors.append(f"[E001] {name}: {e}")

        # Write cloned T_Bkg files for custom slots without uploaded images
        for j, (texture, slot) in enumerate(custom_slots_needing_files):
            name = texture["name"]
            prog_lbl.config(text=f"Cloning slot {j+1}/{len(custom_slots_needing_files)}: {name}")
            prog_bar['value'] = len(self.replacements) + j
            prog.update()
            try:
                base_dir = self.pak_cache.get_base_files(texture)
                folder   = texture["folder"]
                dest     = os.path.join(work, "RetroRewind", "Content", "VideoStore",
                                        "asset", "prop", "vhs", "Background", folder)
                os.makedirs(dest, exist_ok=True)

                # uasset: copy cloned file (has correct slot name in package header)
                uasset_src = os.path.join(base_dir, f"{name}.uasset")
                if os.path.exists(uasset_src):
                    shutil.copy2(uasset_src, os.path.join(dest, f"{name}.uasset"))

                # uexp: ALWAYS use the template — never copy from source slot.
                # Source uexp contains embedded lower-mip pixel data that overrides
                # the ubulk, causing the game to show the source slot's image instead.
                with open(os.path.join(dest, f"{name}.uexp"), 'wb') as fh:
                    fh.write(_TBKG_UEXP_TEMPLATE)

                # ubulk: black placeholder (5 mips, all zeros = black)
                FULL_UBULK_SIZE = sum(
                    ((TEX_WIDTH >> m) // 4) * ((TEX_HEIGHT >> m) // 4) * 8
                    for m in range(5)
                )  # = 1,396,736
                ubulk_dst = os.path.join(dest, f"{name}.ubulk")
                if not os.path.exists(ubulk_dst):
                    with open(ubulk_dst, 'wb') as fh:
                        fh.write(b'\x00' * FULL_UBULK_SIZE)

                print(f"[Build] Cloned T_Bkg (template uexp): {name}")
                success += 1
            except Exception as e:
                errors.append(f"[E003] {name} (clone): {e}")

        # ---------------------------------------------------------------
        # Inject transparent T_Sub textures into the pak.
        #
        # Two groups:
        #   T_Sub_01..T_Sub_77  — always injected as transparent so the
        #       base game's procedural subject graphics are suppressed.
        #       These replace the base game versions via pak priority.
        #   T_Sub_78+           — custom slot dedicated SIs (also transparent).
        #
        # Clone sources (all from the base game pak, pixel data replaced):
        #   8-char names (T_Sub_01..09): clone from T_Sub_01
        #   9-char names (T_Sub_10..99): clone from T_Sub_10
        # ---------------------------------------------------------------

        # Collect custom-slot SIs (T_Sub_78+)
        custom_si_needed = set()
        for dt_name_key, slot_list in CLEAN_DT_SLOT_DATA.items():
            genre_key  = next((g for g, d in GENRE_DATATABLE.items() if d == dt_name_key), None)
            base_count = GENRES[genre_key]["bkg"] if genre_key else 0
            for slot in slot_list[base_count:]:
                si = slot.get("sub_tex", "")
                if si and si.startswith("T_Sub_"):
                    try:
                        if int(si.replace("T_Sub_", "")) >= TSUB_CUSTOM_BASE:
                            custom_si_needed.add(si)
                    except ValueError:
                        pass

        # Full set: T_Sub_01..77 always + custom slots
        all_tsub_transparent = (
            {f"T_Sub_{i:02d}" for i in range(1, 78)} | custom_si_needed
        )

        extract_dir = self.pak_cache._extract_dir
        sub_dir = os.path.join(extract_dir, "RetroRewind", "Content",
                               "VideoStore", "asset", "prop", "vhs", "Subject")
        os.makedirs(sub_dir, exist_ok=True)
        base_pak = self.config.get("base_game_pak", self.pak_cache.pak_path)

        # Inject transparent T_Sub textures using embedded template.
        # No extraction from base game pak needed — source is hardcoded.
        sub_dest = os.path.join(work, "RetroRewind", "Content",
                                "VideoStore", "asset", "prop", "vhs", "Subject")
        os.makedirs(sub_dest, exist_ok=True)
        injected = 0
        for si_name in sorted(all_tsub_transparent):
            new_ua, new_ue = build_transparent_tsub(None, None, si_name)
            with open(os.path.join(sub_dest, f"{si_name}.uasset"), "wb") as f:
                f.write(new_ua)
            with open(os.path.join(sub_dest, f"{si_name}.uexp"), "wb") as f:
                f.write(new_ue)
            injected += 1
        print(f"[Build] Injected {injected} transparent T_Sub textures "
              f"(01-77 + {len(custom_si_needed)} custom)")
        # Print clone summary
        if hasattr(self.pak_cache, '_clone_counts') and self.pak_cache._clone_counts:
            for key, count in self.pak_cache._clone_counts.items():
                code, src = key.split(':')
                print(f"[Build] Cloned {count} T_Bkg_{code} textures from slot {src}")
            self.pak_cache._clone_counts = {}

        prog.destroy()

        if success == 0 and not CUSTOM_ONLY_MODE and not has_nr:
            messagebox.showerror("Build Failed",
                "No textures processed.\n\n" + "\n".join(errors[:5]))
            return
        # In custom-only mode or NR-only mode with no user images, success==0 is expected —
        # the DataTables and T_Sub files are still written below.

        # Save DataTables:
        # - Genres with CleanDataTableBuilder are always rebuilt (Horror etc.)
        # - Other genres only if titles were edited
        modified = self.dt_manager.get_modified_datatables()
        print(f"[Build] DataTables to save: {list(modified.keys())}")
        dt_saved = 0
        for dt_name in modified:
            print(f"[Build] Saving DataTable: {dt_name}")
            try:
                if self.dt_manager.save_datatable(dt_name, work):
                    dt_saved += 1
                    print(f"[Build] Saved {dt_name} OK")
                else:
                    errors.append(f"[E004] DataTable {dt_name} build failed")
                    print(f"[Build] FAILED to save {dt_name}")
            except struct.error as e:
                errors.append(f"[E015] {dt_name}: structure mismatch — possible game update ({e})")
                print(f"[Build] STRUCT ERROR saving {dt_name}: {e}")
            except Exception as e:
                errors.append(f"[E004] {dt_name}: {e}")
                print(f"[Build] ERROR saving {dt_name}: {e}")
        print(f"[Build] {dt_saved} DataTable(s) saved")

        # --- NEW RELEASE TEST ---
        # Build NewRelease_Details_-_Data with a single test row.
        # Uses an existing T_New texture AND existing title to avoid
        # name table extension for this first test.
        # "13 Bodies" is row 4 in the base game NR table (Horror, T_New_Hor_01, SKU=21599)
        # We reuse its title but give it a new SKU to prove our build works.
        # --- Build New Release DataTable + standee blueprints ---
        if NR_SLOT_DATA:
            # Pre-pass: ensure a T_New texture file exists for every NR slot.
            # For tex_num within the base game range this is a no-op (the
            # 3-digit T_New donors aren't in the base pak, so this clones
            # from T_New_Hor_01 for every NR slot that doesn't already have
            # a user image. Idempotent — skips if inject_texture already
            # wrote the files.
            for _nr in NR_SLOT_DATA:
                _gcode = _nr.get("genre_code", "")
                _tnum = _nr.get("tex_num", 1)
                ensure_nr_texture(self.pak_cache, _gcode, _tnum, work)

            # New Release DataTable rows use the 3-digit asset names stored in
            # NR_SLOT_DATA (for example T_New_Hor_001). Older builds also
            # co-injected matching 2-digit assets (T_New_Hor_01) for legacy
            # save compatibility, but those assets are redundant for current
            # builds and make FModel/Unreal Browser show duplicate New Release
            # textures. Ship only the 3-digit asset that the DataTable actually
            # references.

            nr_ok = build_newrelease_datatable(self.pak_cache, NR_SLOT_DATA, work)
            if nr_ok:
                print(f"[Build] NewRelease_Details built OK ({len(NR_SLOT_DATA)} NR slots)")
                for nr in NR_SLOT_DATA:
                    clone_standee_blueprint(
                        self.pak_cache, nr["sku"], nr["standee_shape"],
                        nr["genre_code"], nr["tex_num"], work)
                    create_mi_for_nr(
                        nr["genre_code"], nr["tex_num"],
                        nr["standee_shape"], work)
                    create_standee_thumbnail(
                        nr["sku"], nr["standee_shape"],
                        work, texconv)
            else:
                errors.append("[E006] New Release DataTable build failed"); print("[Build] NewRelease_Details build FAILED")

        # Include a copy of the base game's AssetRegistry.bin in the mod pak.
        # This is currently a passthrough (no modifications) to test that the game
        # accepts a mod pak with an AssetRegistry without crashing.
        ar_src = self._extract_asset_registry(work)
        if ar_src:
            print(f"[Build] AssetRegistry.bin included ({round(os.path.getsize(ar_src)/1024)} KB)")
        else:
            errors.append("[E011] AssetRegistry.bin not included"); print("[Build] WARNING: AssetRegistry.bin not included")

        pak = os.path.join(OUTPUT_DIR, "zzzzzz_MovieWorkshop_P.pak")
        if os.path.exists(pak):
            os.remove(pak)

        r = subprocess.run([repak,"pack","--version","V11", work, pak],
                           capture_output=True, text=True)
        if not os.path.exists(pak):
            messagebox.showerror("Pack Failed", f"[E009] repak error:\n{r.stderr}")
            return

        installed = False
        if os.path.exists(mods):
            dst = os.path.join(mods, "zzzzzz_MovieWorkshop_P.pak")
            # Retry loop: the pak may be locked by the game or Steam for a few seconds
            for attempt in range(10):
                try:
                    shutil.copy2(pak, dst)
                    installed = True
                    break
                except PermissionError:
                    if attempt == 0:
                        # First failure — show a non-blocking message
                        prog.title("Waiting for file lock...")
                    import time
                    time.sleep(1)
                    prog.update()
            if not installed:
                messagebox.showwarning(
                    "File Locked",
                    "Could not copy the pak to the ~mods folder.\n\n"
                    "The file is in use by another process (the game may be running).\n\n"
                    "Please close the game, then copy manually:\n"
                    f"From: {pak}\n"
                    f"To:   {dst}",
                    parent=self.root)
                return

        size = round(os.path.getsize(pak)/1024/1024, 2)
        msg  = (f"✅ {success} texture(s) replaced\n"
                f"📦 {size} MB\n\n")
        msg += ("✅ Installed to ~mods!\nLaunch the game to see changes."
                if installed else
                f"⚠️ ~mods not found.\nPak saved to:\n{pak}")
        if errors:
            msg += f"\n\n⚠️ {len(errors)} issue(s):\n" + "\n".join(errors[:5])
            msg += "\n\nNote error codes (e.g. E001) for bug reports."
        msg += f"\n\n🔖 {TOOL_VERSION}"

        if installed:
            self._edited_slots = clear_edited_slots()
            # Hide the ship description after first build
            if hasattr(self, '_ship_desc'):
                self._ship_desc.pack_forget()
            # Mark all current slots as shipped
            for t in ALL_TEXTURES:
                self._shipped_slots.add(t["name"])
            for nr in NR_SLOT_DATA:
                self._shipped_slots.add(f"NR_{nr['sku']}")
            save_shipped_slots(self._shipped_slots)
        messagebox.showinfo("Build Complete!", msg)
        self._populate_shelf()

# ============================================================
# ENTRY POINT
# ============================================================


def main():
    root = tk.Tk()
    root.withdraw()

    def _on_close():
        """Close the main window and all child dialogs."""
        for widget in list(root.winfo_children()):
            if isinstance(widget, tk.Toplevel):
                try:
                    widget.destroy()
                except Exception:
                    pass
        try:
            root.destroy()
        except Exception:
            import os
            os._exit(0)

    root.protocol("WM_DELETE_WINDOW", _on_close)

    _try_load_vcr_font()

    style = ttk.Style()
    style.theme_use("clam")

    config = load_config()

    needs_setup = not os.path.exists(CONFIG_FILE)

    def _validate_config(cfg):
        """Validate config paths. Returns (level, issues) where:
        level 1 = all ok, level 2 = non-critical, level 3 = critical."""
        issues = []

        # Check tools
        for key, name in [("texconv", "texconv.exe"), ("repak", "repak.exe")]:
            if not cfg.get(key) or not os.path.exists(cfg.get(key, "")):
                issues.append(("tool", key, name))

        # Check game pak (critical)
        pak = cfg.get("base_game_pak", "")
        if not pak or not os.path.exists(pak):
            issues.append(("critical", "base_game_pak", "Game pak file"))

        # Check/recreate mods folder (silent fix)
        mods = cfg.get("mods_folder", "")
        if mods and not os.path.isdir(mods):
            try:
                os.makedirs(mods, exist_ok=True)
                print(f"[Startup] Recreated ~mods folder: {mods}")
            except Exception:
                issues.append(("tool", "mods_folder", "~mods folder"))
        elif not mods:
            # Try to derive from pak path
            if pak and os.path.exists(pak):
                mods_derived = os.path.join(os.path.dirname(pak), "~mods")
                try:
                    os.makedirs(mods_derived, exist_ok=True)
                    cfg["mods_folder"] = mods_derived
                    save_config(cfg)
                    print(f"[Startup] Created ~mods folder: {mods_derived}")
                except Exception:
                    issues.append(("tool", "mods_folder", "~mods folder"))

        if not issues:
            return 1, []
        if any(i[0] == "critical" for i in issues):
            return 3, issues
        return 2, issues

    def launch(cfg):
        """Show loading screen, then launch the main app."""
        root.deiconify()
        loader = LoadingScreen(root)

        def _do_load():
            import time

            # Step 1: config
            loader.step_config(ok=True)
            time.sleep(0.15)

            # Step 2: custom slots  (already loaded at module level)
            total_custom = sum(
                max(0, len(CLEAN_DT_SLOT_DATA.get(dt, [])) - GENRES.get(g,{}).get("bkg",0))
                for g, dt in GENRE_DATATABLE.items()
            )
            loader.step_slots(total_custom)
            time.sleep(0.15)

            loader.finish()
            root.after(0, lambda: _launch_app(cfg))

        def _launch_app(cfg):
            app = VHSToolApp(root, cfg)
            level, issues = _validate_config(cfg)
            if level == 3:
                app._show_critical_warning(issues)
            elif level == 2:
                app._show_startup_warning(issues)

        import threading
        threading.Thread(target=_do_load, daemon=True).start()

    if needs_setup:
        # First-time setup: no config at all — show Setup, then launch
        SetupDialog(root, config, launch)
        root.deiconify()
    else:
        # Config exists — always launch the main app.
        # Level 2/3 warnings are shown INSIDE the app after launch.
        launch(config)

    root.mainloop()


if __name__ == "__main__":
    main()