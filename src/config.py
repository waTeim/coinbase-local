from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


def _split_products(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    for delimiter in (",", ":", " "):
        if delimiter in raw:
            parts = [p.strip() for p in raw.split(delimiter) if p.strip()]
            if parts:
                return parts
    return [raw.strip()] if raw.strip() else []


def _resolve_credentials(
    api_key: Optional[str],
    api_secret: Optional[str],
    api_passphrase: Optional[str],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    candidate_key = api_key or os.getenv("COINBASE_API_KEY")
    candidate_secret = api_secret or os.getenv("COINBASE_API_SECRET")
    candidate_passphrase = api_passphrase or os.getenv("COINBASE_API_PASSPHRASE")

    key_file = None
    if candidate_key and Path(candidate_key).is_file():
        key_file = Path(candidate_key)
    else:
        file_env = os.getenv("COINBASE_API_KEY_FILE")
        if file_env and Path(file_env).is_file():
            key_file = Path(file_env)

    if key_file:
        raw_key_payload = key_file.read_text().strip()
        try:
            data = json.loads(raw_key_payload)
        except json.JSONDecodeError:
            candidate_key = raw_key_payload
        else:
            candidate_key = (
                data.get("id")
                or data.get("key")
                or data.get("keyName")
                or data.get("name")
                or data.get("keyId")
                or candidate_key
            )
            candidate_secret = data.get("privateKey") or data.get("secret") or candidate_secret
            candidate_passphrase = data.get("passphrase", candidate_passphrase)

    secret_file = None
    if candidate_secret and Path(candidate_secret).is_file():
        secret_file = Path(candidate_secret)
    else:
        secret_env = os.getenv("COINBASE_API_SECRET_FILE")
        if secret_env and Path(secret_env).is_file():
            secret_file = Path(secret_env)

    if secret_file:
        candidate_secret = secret_file.read_text().strip()

    if candidate_secret and "\\n" in candidate_secret and "-----BEGIN" in candidate_secret:
        candidate_secret = candidate_secret.replace("\\n", "\n")

    if candidate_secret:
        candidate_secret = candidate_secret.strip()

    return candidate_key, candidate_secret, candidate_passphrase




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

    @classmethod
    def from_args(
        cls,
        *,
        products: Optional[List[str]] = None,
        port: Optional[int] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        api_passphrase: Optional[str] = None,
    ) -> "AppConfig":
        env_products = _split_products(os.getenv("PRODUCTS"))
        resolved_products = products or env_products
        if not resolved_products:
            raise ValueError("At least one product must be provided via --products or PRODUCTS env var.")

        resolved_port = port or int(os.getenv("PORT", "4201"))
        resolved_key, resolved_secret, resolved_passphrase = _resolve_credentials(api_key, api_secret, api_passphrase)

        return cls(
            products=resolved_products,
            port=resolved_port,
            api_key=resolved_key,
            api_secret=resolved_secret,
            api_passphrase=resolved_passphrase,
        )


__all__ = ["AppConfig"]
