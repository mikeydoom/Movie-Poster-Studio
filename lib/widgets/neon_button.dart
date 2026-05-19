import 'package:flutter/material.dart';
import '../theme/retrowave.dart';

/// Terminal-style button. Three flavors via `primary`/`color`:
///   - primary: solid cyan fill, dark text (one per screen)
///   - default: transparent fill, 1px border in `color`
class NeonButton extends StatefulWidget {
  final String label;
  final VoidCallback? onPressed;
  final Color color;
  final IconData? icon;
  final bool primary;
  final EdgeInsets padding;

  const NeonButton({
    super.key,
    required this.label,
    required this.onPressed,
    this.color = Retrowave.text,
    this.icon,
    this.primary = false,
    this.padding = const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
  });

  @override
  State<NeonButton> createState() => _NeonButtonState();
}

class _NeonButtonState extends State<NeonButton> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final enabled = widget.onPressed != null;
    final accent = widget.primary ? Retrowave.cyan : widget.color;
    final borderC = enabled
        ? (_hover ? Retrowave.cyan : (widget.primary ? Retrowave.cyan : Retrowave.border))
        : Retrowave.divider;
    final fillC = widget.primary
        ? (enabled ? Retrowave.cyan : Retrowave.disabled)
        : (_hover ? Retrowave.surface : Colors.transparent);
    final fg = widget.primary
        ? Retrowave.textInv
        : (enabled ? accent : Retrowave.disabled);

    return MouseRegion(
      cursor: enabled ? SystemMouseCursors.click : SystemMouseCursors.basic,
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: GestureDetector(
        onTap: widget.onPressed,
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 80),
          padding: widget.padding,
          decoration: BoxDecoration(
            color: fillC,
            border: Border.all(color: borderC, width: 1),
            borderRadius: BorderRadius.circular(2),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (widget.icon != null) ...[
                Icon(widget.icon, color: fg, size: Retrowave.fsBody),
                const SizedBox(width: 6),
              ],
              Text(
                widget.label.toUpperCase(),
                style: Retrowave.heading(Retrowave.fsBody, fg),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
