import 'package:flutter/material.dart';
import '../theme/retrowave.dart';

/// Flat terminal-style panel — 1px border, no glow, no gradient.
class NeonPanel extends StatelessWidget {
  final Widget child;
  final EdgeInsetsGeometry padding;
  final Color borderColor;
  final String? title;

  const NeonPanel({
    super.key,
    required this.child,
    this.padding = const EdgeInsets.all(Retrowave.sp3),
    this.borderColor = Retrowave.border,
    this.title,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: Retrowave.panel,
        border: Border.all(color: borderColor, width: 1),
        borderRadius: BorderRadius.circular(2),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        mainAxisSize: MainAxisSize.min,
        children: [
          if (title != null)
            Container(
              padding: const EdgeInsets.symmetric(
                  horizontal: Retrowave.sp3, vertical: Retrowave.sp2),
              decoration: const BoxDecoration(
                border: Border(bottom: BorderSide(color: Retrowave.divider)),
              ),
              child: Text(title!.toUpperCase(),
                  style: Retrowave.heading(Retrowave.fsMeta, Retrowave.text2)),
            ),
          Padding(padding: padding, child: child),
        ],
      ),
    );
  }
}
