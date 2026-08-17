"""Launcher for the Meridian servers.

    python3 -m meridian.serve risk                # dual-era stdio (what clients want today)
    python3 -m meridian.serve risk --modern-only  # 2026-07-28 only
    python3 -m meridian.serve risk --http 8931
    python3 -m meridian.serve all --http 8931     # every server, one process, distinct ports

Dual-era is the default because that is what actually connects to the clients
shipping in 2026. Pass `--modern-only` when you want to see a strict
2026-07-28 server refuse a handshake, which is the behaviour Chapter 2
describes and Chapter 3 measures.
"""

from __future__ import annotations

import argparse
import sys
import time

from .protocol import tasks as tasks_ext
from .protocol.legacy import LegacyBridge
from .protocol.http import StreamableHttpServer
from .protocol.stdio import StdioServerTransport
from .servers import compliance, fraud, marketdata, risk

BUILDERS = {
    "risk": lambda **kw: _with_tasks(risk.build_server(**kw)),
    "compliance": lambda **kw: compliance.build_server(**kw),
    "fraud": lambda **kw: fraud.build_server(),
    "marketdata": lambda **kw: marketdata.build_server(),
}

DEFAULT_PORTS = {"risk": 8931, "compliance": 8932,
                 "fraud": 8933, "marketdata": 8934}


def _with_tasks(server):
    tasks_ext.install(server)
    return server


def build(name: str, **kw):
    builder = BUILDERS.get(name)
    if builder is None:
        raise SystemExit(f"unknown server {name!r}; "
                         f"choose from {', '.join(BUILDERS)}")
    return builder(**kw)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("server", choices=[*BUILDERS, "all"])
    ap.add_argument("--http", type=int, metavar="PORT",
                    help="serve Streamable HTTP instead of stdio")
    ap.add_argument("--modern-only", action="store_true",
                    help="refuse the legacy `initialize` handshake")
    ap.add_argument("--fat", action="store_true",
                    help="risk only: serve the pre-Chapter-5 catalogue")
    ap.add_argument("--poisoned", action="store_true",
                    help="compliance only: the Chapter 19 scenario")
    args = ap.parse_args(argv)

    if args.server == "all":
        return _serve_all(args)

    kwargs = {}
    if args.server == "risk" and args.fat:
        kwargs["fat_catalogue"] = True
    if args.server == "compliance" and args.poisoned:
        kwargs["poisoned"] = True

    server = build(args.server, **kwargs)
    endpoint = server if args.modern_only else LegacyBridge(server)

    if args.http:
        http = StreamableHttpServer(endpoint, port=args.http)
        print(f"{args.server} on {http.url}", file=sys.stderr, flush=True)
        http.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            http.stop()
        return 0

    StdioServerTransport(endpoint).serve_forever()
    return 0


def _serve_all(args) -> int:
    servers = []
    base = args.http or 8931
    for offset, name in enumerate(BUILDERS):
        server = build(name)
        endpoint = server if args.modern_only else LegacyBridge(server)
        http = StreamableHttpServer(endpoint, port=base + offset)
        http.start()
        servers.append((name, http))
        print(f"{name:<12} {http.url}", file=sys.stderr, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        for _, http in servers:
            http.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
