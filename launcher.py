#!/usr/bin/env python3
"""Mystic Center launcher: no args → GUI; args → CLI."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mystic_center.cli import main as cli_main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))
