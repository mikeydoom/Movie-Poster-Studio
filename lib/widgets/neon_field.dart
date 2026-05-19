import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../theme/retrowave.dart';

class NeonField extends StatelessWidget {
  final TextEditingController controller;
  final String? label;
  final String? hint;
  final bool digitsOnly;
  final int? maxLength;
  final double? width;
  final ValueChanged<String>? onChanged;
  final VoidCallback? onSubmitted;

  const NeonField({
    super.key,
    required this.controller,
    this.label,
    this.hint,
    this.digitsOnly = false,
    this.maxLength,
    this.width,
    this.onChanged,
    this.onSubmitted,
  });

  @override
  Widget build(BuildContext context) {
    final field = TextField(
      controller: controller,
      style: Retrowave.body(Retrowave.fsBody, Retrowave.text),
      cursorColor: Retrowave.cyan,
      cursorWidth: 1,
      onChanged: onChanged,
      onSubmitted: (_) => onSubmitted?.call(),
      inputFormatters: [
        if (digitsOnly) FilteringTextInputFormatter.digitsOnly,
        if (maxLength != null) LengthLimitingTextInputFormatter(maxLength),
      ],
      decoration: InputDecoration(
        hintText: hint,
        counterText: '',
      ),
    );
    final boxed = width == null ? field : SizedBox(width: width, child: field);
    if (label == null) return boxed;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(label!.toUpperCase(), style: Retrowave.label()),
        const SizedBox(height: Retrowave.sp1),
        boxed,
      ],
    );
  }
}

class NeonDropdown<T> extends StatelessWidget {
  final T value;
  final List<T> items;
  final ValueChanged<T?> onChanged;
  final String? label;
  final String Function(T)? display;
  final double? width;

  const NeonDropdown({
    super.key,
    required this.value,
    required this.items,
    required this.onChanged,
    this.label,
    this.display,
    this.width,
  });

  @override
  Widget build(BuildContext context) {
    final dd = Container(
      width: width,
      padding: const EdgeInsets.symmetric(horizontal: Retrowave.sp2),
      decoration: BoxDecoration(
        color: Retrowave.surface,
        border: Border.all(color: Retrowave.border),
        borderRadius: BorderRadius.circular(2),
      ),
      child: DropdownButtonHideUnderline(
        child: DropdownButton<T>(
          value: value,
          isExpanded: width != null,
          dropdownColor: Retrowave.panel,
          iconEnabledColor: Retrowave.text2,
          style: Retrowave.body(Retrowave.fsBody, Retrowave.text),
          items: items
              .map((e) => DropdownMenuItem<T>(
                    value: e,
                    child: Text(display?.call(e) ?? e.toString()),
                  ))
              .toList(),
          onChanged: onChanged,
        ),
      ),
    );
    if (label == null) return dd;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(label!.toUpperCase(), style: Retrowave.label()),
        const SizedBox(height: Retrowave.sp1),
        dd,
      ],
    );
  }
}
