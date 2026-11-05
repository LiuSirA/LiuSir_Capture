#!/usr/bin/env python3
"""Path helpers for the capture app: where this checkout lives and where the QHY SDK DLLs are searched.

Environment:
  FPGA_TOOLS_ROOT    Optional extra directory searched for `remoteupdate_deps/` (SDK DLLs).
  FPGA_PROJECT_ROOT  Optional project root passed to the capture app.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_TOOLS_ROOT = "FPGA_TOOLS_ROOT"
ENV_PROJECT_ROOT = "FPGA_PROJECT_ROOT"


def package_root() -> Path:
    """This checkout (directory of fpga_paths.py). Independent of FPGA_TOOLS_ROOT."""
    return Path(__file__).resolve().parent


def tools_root() -> Path:
    """FPGA_TOOLS_ROOT if set and it exists, else package_root()."""
    env = os.environ.get(ENV_TOOLS_ROOT, "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        if p.is_dir():
            return p
    return package_root()


def search_roots() -> list[Path]:
    """Directories searched for files that ship with this code, in order, without duplicates."""
    roots: list[Path] = []
    seen: set[Path] = set()
    for raw in (package_root(), tools_root()):
        try:
            root = raw.resolve()
        except OSError:
            continue
        if root in seen:
            continue
        seen.add(root)
        roots.append(root)
    return roots


def resolve_project_root(explicit: str | Path | None = None, *, required: bool = True) -> Path | None:
    """Explicit argument, else FPGA_PROJECT_ROOT, else None (or exit if `required`)."""
    if explicit is not None and str(explicit).strip():
        return Path(explicit).expanduser().resolve()

    env = os.environ.get(ENV_PROJECT_ROOT, "").strip()
    if env:
        return Path(env).expanduser().resolve()

    if not required:
        return None

    raise SystemExit(
        "Cannot infer the project root.\n"
        f"  Pass it explicitly or set {ENV_PROJECT_ROOT}."
    )
