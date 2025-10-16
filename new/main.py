from __future__ import annotations

import argparse
import logging
from typing import Sequence

import uvicorn

from .config import AppConfig
from .server import create_app


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Coinbase Advanced Trade FastAPI server")
    parser.add_argument("products", nargs="*", help="Product IDs to subscribe to (e.g. BTC-USD ETH-USD)")
    parser.add_argument("--port", type=int, default=None, help="Port to bind the FastAPI app (default 63200 or PORT env)")
    parser.add_argument("--api-key", dest="api_key", default=None, help="Coinbase API key")
    parser.add_argument("--api-secret", dest="api_secret", default=None, help="Coinbase API secret (base64)")
    parser.add_argument("--api-passphrase", dest="api_passphrase", default=None, help="Coinbase API passphrase")
    parser.add_argument(
        "--log-level",
        dest="log_level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug"],
        help="Logging level for uvicorn",
    )
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> AppConfig:
    try:
        return AppConfig.from_args(
            products=args.products if args.products else None,
            port=args.port,
            api_key=args.api_key,
            api_secret=args.api_secret,
            api_passphrase=args.api_passphrase,
        )
    except ValueError as exc:
        raise SystemExit(str(exc))


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    config = build_config(args)

    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    app = create_app(config)
    uvicorn.run(app, host="0.0.0.0", port=config.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
