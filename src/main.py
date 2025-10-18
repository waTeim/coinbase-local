from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

import uvicorn

from .config import AppConfig, load_toml_config
from .server import create_app


class HealthzAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return "/healthz" not in record.getMessage()
        except Exception:
            return True


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Coinbase Advanced Trade FastAPI server")
    parser.add_argument("products", nargs="*", help="Product IDs to subscribe to (overrides config if provided)")
    parser.add_argument("--config", dest="config", default=None, help="Path to a TOML config file")
    parser.add_argument("--port", type=int, default=None, help="Port to bind the FastAPI app")
    parser.add_argument("--rest-url", dest="rest_url", default=None, help="Override the REST API base URL")
    parser.add_argument("--ws-url", dest="ws_url", default=None, help="Override the websocket feed URL")
    parser.add_argument("--market-order-buffer", dest="market_order_buffer", type=int, default=None, help="Number of market trades to retain in memory")
    parser.add_argument("--http-timeout", dest="http_timeout", type=float, default=None, help="HTTP client timeout in seconds")
    parser.add_argument("--api-key", dest="api_key", default=None, help="Coinbase API key")
    parser.add_argument("--api-key-file", dest="api_key_file", default=None, help="Path to a file containing the API key")
    parser.add_argument("--api-secret", dest="api_secret", default=None, help="Coinbase API secret")
    parser.add_argument("--api-secret-file", dest="api_secret_file", default=None, help="Path to a file containing the API secret")
    parser.add_argument("--api-credentials-file", dest="api_credentials_file", default=None, help="Path to a JSON file containing key/secret")
    parser.add_argument(
        "--log-level",
        dest="log_level",
        default=None,
        choices=["critical", "error", "warning", "info", "debug"],
        help="Logging level for uvicorn",
    )
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> AppConfig:
    config_path = Path(args.config).expanduser() if args.config else None
    try:
        config_data = load_toml_config(config_path)
        return AppConfig.from_sources(config_data, args, config_path=config_path)
    except ValueError as exc:
        raise SystemExit(str(exc))


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    config = build_config(args)

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("uvicorn.access").addFilter(HealthzAccessFilter())

    app = create_app(config)
    uvicorn.run(app, host="0.0.0.0", port=config.port, log_level=config.log_level)


if __name__ == "__main__":
    main()
