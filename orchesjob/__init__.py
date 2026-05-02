# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""orchesjob - Lightweight idempotent one-shot job runner."""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("orchesjob")
except PackageNotFoundError:
    __version__ = "unknown"
