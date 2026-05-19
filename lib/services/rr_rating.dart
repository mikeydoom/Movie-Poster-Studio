import 'dart:typed_data';

/// RR Movie Workshop star-rating table.
/// last2 = SKU % 100. The game decodes it back to a star count.
/// Source: RR_VHS_Tool.STAR_OPTIONS (no 3.0★ tier exists in-game).
/// Keyed by `stars * 10` (int) so the table can be `const` in Dart.
const Map<int, int> _starTimes10ToLast2 = {
  50: 0,
  45: 93,   // Good Critic
  40: 83,   // Good Critic
  35: 53,
  30: 38,
  25: 33,
  20: 23,
  15: 22,   // Bad Critic
  10: 12,   // Bad Critic
  5: 3,     // Bad Critic
  0: 2,     // Bad Critic
};

int rrStarToLast2(double stars) =>
    _starTimes10ToLast2[(stars * 10).round()] ?? 2;

/// Critic tag for a given star value, matching RR_VHS_Tool labels.
String rrCriticTag(double stars) {
  if (stars >= 4.0) return 'Good Critic';
  if (stars <= 1.5 && stars > 0) return 'Bad Critic';
  if (stars == 0.0) return 'Bad Critic';
  return '';
}

/// Map a TMDB vote_average (0–10) to the closest RR star bucket.
double rrStarsFromVoteAverage(double vote) {
  if (vote >= 9.5) return 5.0;
  if (vote >= 8.5) return 4.5;
  if (vote >= 7.5) return 4.0;
  if (vote >= 6.0) return 3.5;
  if (vote >= 5.5) return 3.0;
  if (vote >= 5.0) return 2.5;
  if (vote >= 4.0) return 2.0;
  if (vote >= 3.0) return 1.5;
  if (vote >= 2.0) return 1.0;
  if (vote >= 1.0) return 0.5;
  return 0.0;
}

int rrLast2FromVoteAverage(double vote) =>
    rrStarToLast2(rrStarsFromVoteAverage(vote));

/// SKU prefix per genre — matched to RR_VHS_Tool.GENRE_SKU_PREFIX.
const Map<String, int> rrGenreSkuPrefix = {
  'Action': 1,
  'Adult': 16,
  'Adventure': 2,
  'Comedy': 3,
  'Police': 14,
  'Drama': 4,
  'Fantasy': 7,
  'Horror': 5,
  'History': 11,
  'Kid': 12,
  'Kids': 12,
  'Music': 9,
  'Romance': 10,
  'Sci-Fi': 6,
  'Sports': 15,
  'Thriller': 8,
  'Western': 17,
  'Xmas': 18,
  'Documentary': 13,
};

/// LCG-derived float in [0, 1) — same formula UE4 uses for the random stream
/// keyed off the SKU. Match RR_VHS_Tool._sku_is_holo / _sku_is_old.
double _skuLcgF(int sku) {
  final seed = (sku * 196314165 + 907633515) & 0xFFFFFFFF;
  final fBits = ((seed >> 9) | 0x3F800000) & 0xFFFFFFFF;
  final bd = ByteData(4)..setUint32(0, fBits, Endian.little);
  return bd.getFloat32(0, Endian.little) - 1.0;
}

bool rrSkuIsHolo(int sku) => _skuLcgF(sku) < 0.019;
bool rrSkuIsOld(int sku) => _skuLcgF(sku) < 0.20;

/// Generate a SKU satisfying the requested rarity and last2 (star) value.
/// Mirrors RR_VHS_Tool.generate_sku. Returns the SKU; caller is expected to
/// add it to `usedSkus` before generating the next.
int rrGenerateSku({
  required String genre,
  required int slotIndex,
  required int last2,
  String rarity = 'Common',
  Set<int>? usedSkus,
}) {
  final used = usedSkus ?? <int>{};
  final prefix = rrGenreSkuPrefix[genre] ?? 5;
  int? fallback;
  for (var step = 0; step < 50000; step += 100) {
    final sku = prefix * 10000000 + slotIndex * 10000 + step + last2;
    if (used.contains(sku)) continue;
    final isHolo = rrSkuIsHolo(sku);
    final isOld = rrSkuIsOld(sku);
    bool ok;
    switch (rarity) {
      case 'Common':
        ok = !isHolo && !isOld;
        break;
      case 'Common (Old)':
        ok = !isHolo && isOld;
        break;
      case 'Limited Edition (holo)':
        ok = isHolo;
        break;
      default: // Random
        ok = true;
    }
    if (ok) return sku;
    fallback ??= sku;
  }
  return fallback ?? (prefix * 10000000 + slotIndex * 10000 + last2);
}
