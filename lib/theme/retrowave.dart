import 'package:flutter/material.dart';

/// Design system ported from Retro Rewind VHS Tool — terminal-inspired flat UI.
/// One color = one role. No gradients, no glow, max 2px rounded corners.
class Retrowave {
  // Backgrounds
  static const Color bg = Color(0xFF050505);
  static const Color panel = Color(0xFF0B0F14);
  static const Color surface = Color(0xFF101722);
  static const Color divider = Color(0xFF1C1C1C);
  static const Color border = Color(0xFF333333);

  // Accents (each color has ONE meaning)
  static const Color cyan = Color(0xFF00F5FF); // Active / Selected / CTA / Progress / OK
  static const Color pink = Color(0xFFFF0055); // Edit / Custom / Error / Warning
  static const Color gold = Color(0xFFFFD84A); // Rarity / Highlight / Star
  static const Color disabled = Color(0xFF5A5A5A);

  // Text
  static const Color text = Color(0xFFF2F5F7);
  static const Color text2 = Color(0xFFA8B0B8);
  static const Color text3 = Color(0xFF6A7A7A);
  static const Color textInv = Color(0xFF050505);

  // Semantic aliases
  static const Color success = cyan;
  static const Color error = pink;
  static const Color warn = gold;

  // Legacy aliases (so existing widget code keeps working without churn)
  static const Color bgDeep = bg;
  static const Color panelAlt = surface;
  static const Color magenta = pink;
  static const Color hotPink = pink;
  static const Color purple = border;
  static const Color yellow = gold;
  static const Color sunset = pink;
  static const Color textDim = text2;
  static const Color logBg = bg;
  static const Color logFg = cyan;
  static const Color gridLine = border;

  // Spacing grid (4px base)
  static const double sp1 = 4;
  static const double sp2 = 8;
  static const double sp3 = 12;
  static const double sp4 = 16;
  static const double sp6 = 24;

  // Font sizes (1080p baseline) — globally scaled via MaterialApp textScaler.
  static const double fsApp = 15;
  static const double fsSec = 14;
  static const double fsBody = 13;
  static const double fsMeta = 11;

  /// Font stack: Consolas → Cascadia Mono → Courier New (system).
  static const String _primary = 'Consolas';
  static const List<String> _fallback = [
    'Cascadia Mono',
    'Cascadia Code',
    'Courier New',
    'monospace',
  ];

  /// In-game genre colors lifted from RR_VHS_Tool. bg = badge background, fg = label.
  static const Map<String, ({Color bg, Color fg})> genreColors = {
    'Action':     (bg: Color(0xFF9ACEFF), fg: Color(0xFF1A1A2E)),
    'Adult':      (bg: Color(0xFF3D0559), fg: Color(0xFFE8E8E8)),
    'Adventure':  (bg: Color(0xFF0099FF), fg: Color(0xFFE8E8E8)),
    'Comedy':     (bg: Color(0xFFFFCE00), fg: Color(0xFF1A1A2E)),
    'Police':     (bg: Color(0xFFFFFFFF), fg: Color(0xFF1A1A2E)),
    'Drama':      (bg: Color(0xFFA8BAFF), fg: Color(0xFF1A1A2E)),
    'Fantasy':    (bg: Color(0xFF4B590E), fg: Color(0xFFE8E8E8)),
    'History':    (bg: Color(0xFF663300), fg: Color(0xFFE8E8E8)),
    'Horror':     (bg: Color(0xFFE50000), fg: Color(0xFFFFFFFF)),
    'Kids':       (bg: Color(0xFFA081FF), fg: Color(0xFF1A1A2E)),
    'Music':      (bg: Color(0xFFCC00FF), fg: Color(0xFFE8E8E8)),
    'Romance':    (bg: Color(0xFFEF74FF), fg: Color(0xFF1A1A2E)),
    'Sci-Fi':     (bg: Color(0xFF78FFD9), fg: Color(0xFF1A1A2E)),
    'Sports':     (bg: Color(0xFF006600), fg: Color(0xFFE8E8E8)),
    'Thriller':   (bg: Color(0xFF000099), fg: Color(0xFFE8E8E8)),
    'Western':    (bg: Color(0xFFFFB53F), fg: Color(0xFF1A1A2E)),
    'Xmas':       (bg: Color(0xFFBEFF00), fg: Color(0xFF1A1A2E)),
    'Documentary':(bg: Color(0xFFFFFFFF), fg: Color(0xFF1A1A2E)),
  };

  /// Genre background color for a normalized genre name (case-insensitive).
  /// Returns the panel color as a neutral fallback.
  static Color genreBg(String name) {
    final key = _matchGenre(name);
    return key == null ? panel : genreColors[key]!.bg;
  }

  /// Genre foreground (label) color matching genreBg.
  static Color genreFg(String name) {
    final key = _matchGenre(name);
    return key == null ? text : genreColors[key]!.fg;
  }

  static String? _matchGenre(String name) {
    final lower = name.toLowerCase();
    // Check internal keys first.
    for (final k in genreColors.keys) {
      if (k.toLowerCase() == lower) return k;
    }
    // Fall back to reverse-lookup display names (e.g. "Crime" → "Police").
    for (final entry in _genreDisplayNames.entries) {
      if (entry.value.toLowerCase() == lower) return entry.key;
    }
    return null;
  }

  /// UI display names for genres whose internal tag differs from the label shown to the user.
  /// Internal tags (Police, Kids, Xmas) are used for folders, export, config, and filter state.
  static const Map<String, String> _genreDisplayNames = {
    'Police': 'Crime',
    'Kids': 'Family',
    'Xmas': 'Holiday',
  };

  /// Returns the UI display name for an internal genre tag.
  /// For most genres this is a no-op; Police→Crime, Kids→Family, Xmas→Holiday.
  static String displayName(String internalName) =>
      _genreDisplayNames[internalName] ?? internalName;

  // ─── Typography helpers ──────────────────────────────────────

  static TextStyle _base({
    required double size,
    required Color color,
    FontWeight weight = FontWeight.normal,
    double? letterSpacing,
  }) =>
      TextStyle(
        fontFamily: _primary,
        fontFamilyFallback: _fallback,
        fontSize: size,
        color: color,
        fontWeight: weight,
        letterSpacing: letterSpacing,
        height: 1.25,
      );

  static TextStyle display([double size = fsApp]) =>
      _base(size: size, color: cyan, weight: FontWeight.bold, letterSpacing: 2);

  static TextStyle heading([double size = fsSec, Color? color]) =>
      _base(size: size, color: color ?? text, weight: FontWeight.bold, letterSpacing: 1);

  static TextStyle label([double size = fsMeta]) =>
      _base(size: size, color: text2, letterSpacing: 1);

  static TextStyle body([double size = fsBody, Color? color]) =>
      _base(size: size, color: color ?? text);

  static TextStyle mono([double size = fsBody, Color? color]) =>
      _base(size: size, color: color ?? text);

  // ─── Theme ──────────────────────────────────────────────────

  static ThemeData themeData() {
    return ThemeData(
      brightness: Brightness.dark,
      scaffoldBackgroundColor: bg,
      canvasColor: bg,
      primaryColor: cyan,
      dividerColor: divider,
      fontFamily: _primary,
      fontFamilyFallback: _fallback,
      colorScheme: const ColorScheme.dark(
        primary: cyan,
        secondary: pink,
        surface: panel,
        onPrimary: textInv,
        onSecondary: text,
        onSurface: text,
        error: pink,
      ),
      textTheme: TextTheme(
        bodyMedium: body(),
        bodySmall: body(fsMeta),
        labelMedium: label(),
        titleMedium: heading(fsSec),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: surface,
        contentPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
        hintStyle: body(fsBody, text3),
        labelStyle: label(),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(2),
          borderSide: const BorderSide(color: border, width: 1),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(2),
          borderSide: const BorderSide(color: border, width: 1),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(2),
          borderSide: const BorderSide(color: cyan, width: 1),
        ),
        errorBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(2),
          borderSide: const BorderSide(color: pink, width: 1),
        ),
      ),
      dropdownMenuTheme: DropdownMenuThemeData(
        textStyle: body(fsBody),
        menuStyle: const MenuStyle(
          backgroundColor: WidgetStatePropertyAll(panel),
        ),
      ),
      scrollbarTheme: const ScrollbarThemeData(
        thumbColor: WidgetStatePropertyAll(border),
        trackColor: WidgetStatePropertyAll(panel),
      ),
    );
  }
}
