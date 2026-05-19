import 'package:flutter/material.dart';
import '../theme/retrowave.dart';

/// Flat dark background — terminal aesthetic, no decoration.
class RetroBackground extends StatelessWidget {
  final Widget child;
  const RetroBackground({super.key, required this.child});

  @override
  Widget build(BuildContext context) {
    return ColoredBox(color: Retrowave.bg, child: child);
  }
}
