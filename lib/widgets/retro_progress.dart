import 'package:flutter/material.dart';
import '../theme/retrowave.dart';

/// 12-segment terminal-style progress bar (cyan filled, border empty).
class RetroProgressBar extends StatelessWidget {
  final double value;
  final String label;
  final Color color;
  final int? current;
  final int? total;
  final int segments;

  const RetroProgressBar({
    super.key,
    required this.value,
    required this.label,
    this.color = Retrowave.cyan,
    this.current,
    this.total,
    this.segments = 24,
  });

  @override
  Widget build(BuildContext context) {
    final v = value.clamp(0.0, 1.0);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Row(
          children: [
            Text(label.toUpperCase(), style: Retrowave.label()),
            const Spacer(),
            if (total != null && total! > 0)
              Text('${current ?? 0} / $total',
                  style: Retrowave.body(Retrowave.fsMeta, color)),
          ],
        ),
        const SizedBox(height: Retrowave.sp1),
        SizedBox(
          height: 10,
          child: LayoutBuilder(builder: (ctx, box) {
            return Row(
              children: List.generate(segments, (i) {
                final filled = (i + 1) / segments <= v + 1e-6;
                return Expanded(
                  child: Container(
                    margin: EdgeInsets.only(right: i == segments - 1 ? 0 : 2),
                    decoration: BoxDecoration(
                      color: filled ? color : Retrowave.surface,
                      border: Border.all(
                          color: filled ? color : Retrowave.border, width: 1),
                    ),
                  ),
                );
              }),
            );
          }),
        ),
      ],
    );
  }
}
