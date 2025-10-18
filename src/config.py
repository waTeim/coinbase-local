from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:  # pragma: no cover - Python <3.11 fallback
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - fallback for older interpreters
    import tomli as tomllib  # type: ignore[assignment]

_LOG_LEVELS = {"critical", "error", "warning", "info", "debug"}


def load_toml_config(path: Path | None) -> Dict[str, Any]:
    if path is None:
        return {}
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"Config file '{path}' does not exist.") from exc
    try:
        return tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Unable to parse config file '{path}': {exc}") from exc


def _split_products(raw: str) -> List[str]:
    return [part for part in re.split(r"[\s,:]+", raw) if part]


def _coerce_products(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return _split_products(value)
    if isinstance(value, Iterable):
        products: List[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("Product identifiers must be strings.")
            stripped = item.strip()
            if stripped:
                products.append(stripped)
        return products
    raise ValueError("Products must be provided as a string or a list of strings.")


def _normalize_optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _resolve_path(path_value: str | Path, base_dir: Optional[Path]) -> Path:
    path = Path(path_value)
    if not path.is_absolute() and base_dir is not None:
        path = (base_dir / path).resolve()
    return path


def _read_file_text(path_value: Optional[str | Path], base_dir: Optional[Path]) -> Optional[str]:
    if not path_value:
        return None
    path = _resolve_path(path_value, base_dir)
    if not path.is_file():
        raise ValueError(f"Credential file '{path}' does not exist.")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Credential file '{path}' is empty.")
    if "\\n" in text and "-----BEGIN" in text:
        text = text.replace("\\n", "\n")
    return text


def _apply_credentials_file(
    api_key: Optional[str],
    api_secret: Optional[str],
    api_passphrase: Optional[str],
    file_value: Optional[str | Path],
    base_dir: Optional[Path],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if not file_value:
        return api_key, api_secret, api_passphrase
    path = _resolve_path(file_value, base_dir)
    if not path.is_file():
        raise ValueError(f"Credential file '{path}' does not exist.")
    data = path.read_text(encoding="utf-8").strip()
    if not data:
        raise ValueError(f"Credential file '{path}' is empty.")
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        if api_secret is None:
            api_secret = data
        return api_key, api_secret, api_passphrase

    api_key = api_key or payload.get("id") or payload.get("key") or payload.get("keyName") or payload.get("name") or payload.get("keyId")
    api_secret = api_secret or payload.get("privateKey") or payload.get("secret")
    api_passphrase = api_passphrase or payload.get("passphrase")
    return api_key, api_secret, api_passphrase


@dataclass
class AppConfig:
    products: List[str] = field(default_factory=list)
    port: int = 4201
    rest_url: str = "https://api.coinbase.com/api/v3"
    ws_url: str = "wss://advanced-trade-ws.coinbase.com"
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_passphrase: Optional[str] = None
    market_order_buffer: int = 2000
    http_timeout: float = 10.0
    log_level: str = "info"

    @classmethod
    def from_sources(
        cls,
        config_data: Dict[str, Any],
        args: argparse.Namespace,
        *,
        config_path: Optional[Path] = None,
    ) -> "AppConfig":
        base_dir = config_path.parent if config_path else None

        products_source: Any = args.products if getattr(args, "products", None) else config_data.get("products")
        products = _coerce_products(products_source)
        if not products:
            raise ValueError("At least one product must be provided via the config file or command line.")

        port_value = args.port if getattr(args, "port", None) is not None else config_data.get("port", cls.port)
        try:
            port = int(port_value)
        except (TypeError, ValueError):
            raise ValueError("Port must be an integer.")

        rest_url = _normalize_optional_str(getattr(args, "rest_url", None)) or _normalize_optional_str(config_data.get("rest_url")) or cls.rest_url
        ws_url = _normalize_optional_str(getattr(args, "ws_url", None)) or _normalize_optional_str(config_data.get("ws_url")) or cls.ws_url

        mob_value = getattr(args, "market_order_buffer", None)
        if mob_value is None:
            mob_value = config_data.get("market_order_buffer", cls.market_order_buffer)
        try:
            market_order_buffer = int(mob_value)
        except (TypeError, ValueError):
            raise ValueError("market_order_buffer must be an integer.")

        timeout_value = getattr(args, "http_timeout", None)
        if timeout_value is None:
            timeout_value = config_data.get("http_timeout", cls.http_timeout)
        try:
            http_timeout = float(timeout_value)
        except (TypeError, ValueError):
            raise ValueError("http_timeout must be numeric.")

        log_level_candidate = _normalize_optional_str(getattr(args, "log_level", None)) or _normalize_optional_str(config_data.get("log_level")) or cls.log_level
        log_level = log_level_candidate.lower()
        if log_level not in _LOG_LEVELS:
            raise ValueError(f"Unsupported log level '{log_level_candidate}'. Choose from {sorted(_LOG_LEVELS)}.")

        api_config = config_data.get("api", {})
        if api_config and not isinstance(api_config, dict):
            raise ValueError("The 'api' section of the config file must be a table.")

        api_key = _normalize_optional_str(getattr(args, "api_key", None)) or _normalize_optional_str(api_config.get("key"))
        api_secret = _normalize_optional_str(getattr(args, "api_secret", None)) or _normalize_optional_str(api_config.get("secret"))
        api_passphrase = _normalize_optional_str(getattr(args, "api_passphrase", None)) or _normalize_optional_str(api_config.get("passphrase"))

        cli_key_file = getattr(args, "api_key_file", None)
        cli_secret_file = getattr(args, "api_secret_file", None)
        cli_passphrase_file = getattr(args, "api_passphrase_file", None)
        cli_credentials_file = getattr(args, "api_credentials_file", None)

        cfg_key_file = api_config.get("key_file")
        cfg_secret_file = api_config.get("secret_file")
        cfg_passphrase_file = api_config.get("passphrase_file")
        cfg_credentials_file = api_config.get("credentials_file")

        if api_key is None:
            api_key = _read_file_text(cli_key_file or cfg_key_file, None if cli_key_file else base_dir)
        if api_secret is None:
            api_secret = _read_file_text(cli_secret_file or cfg_secret_file, None if cli_secret_file else base_dir)
        if api_passphrase is None:
            api_passphrase = _read_file_text(cli_passphrase_file or cfg_passphrase_file, None if cli_passphrase_file else base_dir)

        api_key, api_secret, api_passphrase = _apply_credentials_file(
            api_key,
            api_secret,
            api_passphrase,
            cli_credentials_file or cfg_credentials_file,
            None if cli_credentials_file else base_dir,
        )

        if api_secret and "\\n" in api_secret and "-----BEGIN" in api_secret:
            api_secret = api_secret.replace("\\n", "\n")

        return cls(
            products=products,
            port=port,
            rest_url=rest_url,
            ws_url=ws_url,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
            market_order_buffer=market_order_buffer,
            http_timeout=http_timeout,
            log_level=log_level,
        )


__all__ = ["AppConfig", "load_toml_config"]
