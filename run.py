#!/usr/bin/env python
"""SO-101 debug console — entry point.

    tools\\debug_console\\.venv\\Scripts\\python.exe tools\\debug_console\\run.py

Runs in its own virtualenv on purpose: the console needs only fastapi / uvicorn /
pyserial / opencv plus the vendored motor layer, and **no torch**. The project's
main .venv keeps its hand-installed CUDA torch completely untouched.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `app` and `vendor` importable regardless of the current directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    """Parse arguments and start the server."""
    parser = argparse.ArgumentParser(description="SO-101 debug console")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    parser.add_argument("--port", type=int, default=8000, help="bind port")
    args = parser.parse_args()

    # Import after parsing so --help stays fast (the import pulls in cv2).
    from app.main import run

    print(f"\n  SO-101 debug console  ->  http://{args.host}:{args.port}\n")
    print("  Ctrl+C to quit. Torque is released and ports are closed on exit.\n")
    try:
        run(host=args.host, port=args.port)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
