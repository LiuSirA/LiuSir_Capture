#!/usr/bin/env python3
"""Expand the register-write presets in fpga_soft_modes.json into plain steps.

Only what the synchronized-capture test needs: a preset is a list of steps, and a
`GEN_WRITE_SPLIT` step (one multi-byte value written to consecutive indexes) is
expanded into single-byte register writes.
"""

from __future__ import annotations


def _auto_byte_len(value: int) -> int:
    if value < 0:
        raise RuntimeError("split value must be >= 0")
    if value <= 0xFF:
        return 1
    if value <= 0xFFFF:
        return 2
    if value <= 0xFFFFFF:
        return 3
    if value <= 0xFFFFFFFF:
        return 4
    if value <= 0xFFFFFFFFFFFFFFFF:
        return 8
    raise RuntimeError("split value too large (max u64)")


def _uN_bytes(value: int, length: int, endian: str) -> list[int]:
    if length not in {1, 2, 3, 4, 8}:
        raise RuntimeError(f"unsupported split length: {length}")
    if value < 0 or value > (1 << (8 * length)) - 1:
        raise RuntimeError(f"value out of range for {length} byte(s)")
    b = [(value >> (8 * i)) & 0xFF for i in range(length)]
    if endian.upper() == "LE":
        return b
    if endian.upper() == "BE":
        return list(reversed(b))
    raise RuntimeError("endian must be LE or BE")


def expand_mode_steps(steps: list[dict]) -> list[dict]:
    expanded: list[dict] = []
    for step in steps:
        cmd = str(step.get("cmd", "")).upper()
        if cmd == "GEN_WRITE_SPLIT":
            gen = dict(step.get("gen") or {})
            target = str(gen.get("target", "")).upper()
            if target not in {"WRITE_CMOS", "WRITE_FPGA", "WRITE_FPGA2", "WRITE_FPGA3"}:
                raise RuntimeError(f"GEN_WRITE_SPLIT target unsupported: {target}")
            base_index = int(gen.get("base_index"))
            value = int(gen.get("value"))
            stride = int(gen.get("stride", 1))
            endian = str(gen.get("endian", "LE")).upper()
            length_raw = gen.get("length", "auto")
            if isinstance(length_raw, str) and length_raw.lower() == "auto":
                length = _auto_byte_len(value)
            else:
                length = int(length_raw)
            if stride <= 0:
                raise RuntimeError("GEN_WRITE_SPLIT stride must be > 0")
            bytes_list = _uN_bytes(value, length, endian)
            base_comment = str(step.get("comment", "")).strip()
            for i, b in enumerate(bytes_list):
                idx = base_index + i * stride
                comment = f"split {length}B {endian} byte{i}"
                if base_comment:
                    comment = base_comment + " | " + comment
                expanded.append(
                    {
                        "cmd": target,
                        "params": {"index": idx, "value": b},
                        "comment": comment,
                    }
                )
            continue
        expanded.append(step)
    return expanded
