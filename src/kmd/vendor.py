"""Single point of contact with the vendored Kronos source tree.

Kronos lives at third_party/kronos/ (see NOTES/kronos_api.md for how it
got there and the verified API). Its `model` package expects to be
importable as a top-level package, so this module puts
third_party/kronos/ on sys.path exactly once. Every module that needs
`from model import Kronos, KronosTokenizer, KronosPredictor` must import
this module first (its import has the side effect of extending
sys.path) rather than duplicating a sys.path hack locally.
"""

from __future__ import annotations

import sys

from kmd.config import KRONOS_VENDOR_ROOT

if not KRONOS_VENDOR_ROOT.is_dir():
    raise RuntimeError(
        f"Vendored Kronos source not found at {KRONOS_VENDOR_ROOT}. "
        "Expected third_party/kronos/model/ to exist."
    )

_vendor_path = str(KRONOS_VENDOR_ROOT)
if _vendor_path not in sys.path:
    sys.path.insert(0, _vendor_path)
