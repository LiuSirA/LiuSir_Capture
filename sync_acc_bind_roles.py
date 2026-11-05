# -*- coding: utf-8 -*-
"""Scan cameras and print SCAN_IDS for PCIe role binding."""
from __future__ import annotations

import json
import sys
from pathlib import Path

CODE = Path(r"J:\Mywork\Project_LH\git\LiuSir_Capture")
sys.path.insert(0, str(CODE / "host"))
sys.path.insert(0, str(CODE))

from qhyccd_highlevel import QhyApi, scan_cameras  # noqa: E402


def main() -> int:
    api = QhyApi(CODE / "host")
    ids = scan_cameras(api)
    names = []
    for b in ids:
        s = b.split(b"\x00", 1)[0].decode("ascii", "replace")
        names.append(s)
    print("SCAN_N=%d" % len(names))
    print("SCAN_IDS=" + ",".join(names))
    if len(names) >= 2:
        doc = {"master": names[0], "slave": names[1], "scan": names}
        print("JSON=" + json.dumps(doc, ensure_ascii=False))
    return 0 if len(names) >= 2 else 2


if __name__ == "__main__":
    raise SystemExit(main())