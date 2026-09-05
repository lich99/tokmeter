import argparse
import os
import threading
import webbrowser
from pathlib import Path

from . import __version__
from .engine import Store
from .pricing import Catalog
from .server import bind
from .sources import Sources


def main():
    parser = argparse.ArgumentParser(
        description="Local Claude/Codex usage dashboard. No persistent usage cache."
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("TOKMETER_PORT", os.environ.get("CLAUDE_USAGE_PORT", 8765))),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--interval", type=float, default=1.0, help="Background scan interval in seconds")
    defaults = Sources.defaults()
    parser.add_argument("--claude-dir", type=Path, default=defaults.claude, help="Claude projects directory")
    parser.add_argument(
        "--codex-dir",
        type=Path,
        default=defaults.codex,
        help="Codex home containing sessions and archived_sessions",
    )
    parser.add_argument("--prices", type=Path, help="Explicit local price/alias overrides (JSON)")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535 or not 1 <= args.workers <= 32 or not 0.2 <= args.interval <= 3600:
        parser.error("port must be 0..65535, workers 1..32, and interval 0.2..3600")
    store = Store(
        Sources(args.claude_dir.expanduser().resolve(), args.codex_dir.expanduser().resolve()),
        Catalog(args.prices),
        args.workers,
        args.interval,
    )
    server = bind(store, args.host, args.port)
    url = f"http://{args.host}:{server.server_address[1]}/"
    print(
        f"tokmeter {__version__} → {url}\nReading local logs in the background; no persistent usage cache.",
        flush=True,
    )
    store.start()
    if not args.no_open:
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
