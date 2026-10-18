"""Root daemon entry point (mystic-center.service runs this)."""

from __future__ import annotations

from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
