from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, getcontext
from typing import Any, Deque, Dict, Iterable, List, Literal, Optional, Tuple

import urllib.parse

import httpx
import websockets
from websockets import WebSocketClientProtocol

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from .config import AppConfig


logger = logging.getLogger(__name__)
getcontext().prec = 28


def _dec_to_str(value: Decimal) -> str:
    normalised = value.normalize()
    return format(normalised, "f")


@dataclass
class PriceLevel:
    price: Decimal
    size: Decimal
    num_orders: int


@dataclass
class TradeEntry:
    sequence: int
    side: Literal["buy", "sell"]
    price: Decimal
    size: Decimal
    timestamp: datetime


class ProductOrderBook:
    def __init__(self, product_id: str, buffer_size: int) -> None:
        self.product_id = product_id
        self.asks: Dict[Decimal, PriceLevel] = {}
        self.bids: Dict[Decimal, PriceLevel] = {}
        self.sequence: int = 0
        self.market_orders: Deque[TradeEntry] = deque(maxlen=buffer_size)
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()

    def update_from_snapshot(self, bids: Iterable[PriceLevel], asks: Iterable[PriceLevel], sequence: int) -> None:
        self.bids = {level.price: level for level in bids if level.size > 0}
        self.asks = {level.price: level for level in asks if level.size > 0}
        self.sequence = sequence
        self.mark_ready()

    def apply_update(self, changes: Iterable[Tuple[str, Decimal, Decimal, int]], sequence: int) -> None:
        for side, price, size, num_orders in changes:
            book = self.bids if side == "buy" else self.asks
            if size == 0:
                book.pop(price, None)
            else:
                book[price] = PriceLevel(price=price, size=size, num_orders=num_orders or 1)
        if sequence >= self.sequence:
            self.sequence = sequence
        self.mark_ready()

    def mark_ready(self) -> None:
        if not self.ready.is_set() and self.asks and self.bids:
            self.ready.set()

    def record_trade(self, entry: TradeEntry) -> None:
        self.market_orders.appendleft(entry)
        if entry.sequence > self.sequence:
            self.sequence = entry.sequence

    def sorted_bids(self) -> List[PriceLevel]:
        return sorted(self.bids.values(), key=lambda level: level.price, reverse=True)

    def sorted_asks(self) -> List[PriceLevel]:
        return sorted(self.asks.values(), key=lambda level: level.price)


class CoinbaseOrderBookManager:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._books: Dict[str, ProductOrderBook] = {
            product: ProductOrderBook(product, config.market_order_buffer) for product in config.products
        }
        self._client: Optional[httpx.AsyncClient] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._ws: Optional[WebSocketClientProtocol] = None
        self._running = asyncio.Event()
        self._reconnect_lock = asyncio.Lock()
        self._signing_key = self._load_signing_key()

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._config.http_timeout)
        await self._prime_snapshots()
        self._running.set()
        self._ws_task = asyncio.create_task(self._websocket_loop())

    async def stop(self) -> None:
        self._running.clear()
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _prime_snapshots(self) -> None:
        for product_id in self._config.products:
            try:
                await self._load_snapshot(product_id)
            except httpx.HTTPStatusError:
                continue

    async def _load_snapshot(self, product_id: str) -> None:
        if self._client is None:
            raise RuntimeError("HTTP client not initialised")

        path = f"/brokerage/products/{product_id}/book"
        url = f"{self._config.rest_url}{path}"
        params = {"limit": 500}
        headers = self._auth_headers("GET", path, params=params)
        logger.debug("Snapshot request headers for %s: %s", product_id, headers)

        try:
            response = await self._client.get(url, params=params, headers=headers or None)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response else 'unknown'
            body = exc.response.text if exc.response is not None else '<no body>'
            if status == 401:
                logger.warning('Snapshot fetch unauthorized for %s, relying on websocket snapshot. Body: %s', product_id, body)
                return
            logger.error('Failed to pull snapshot for %s (status %s): %s', product_id, status, body)
            raise
        except httpx.HTTPError as exc:
            logger.error('HTTP error pulling snapshot for %s: %s', product_id, exc)
            raise

        payload = response.json()
        bids = self._parse_price_levels(payload.get("bids", []))
        asks = self._parse_price_levels(payload.get("asks", []))
        sequence = int(payload.get("sequence", 0))

        book = self._books[product_id]
        async with book.lock:
            book.update_from_snapshot(bids=bids, asks=asks, sequence=sequence)
        logger.info("Snapshot primed for %s at sequence %s", product_id, sequence)

    async def get_interval(self, product_id: str, aggregation: int, depth: int) -> Dict[str, Any]:
        book = self._get_book(product_id)
        await self._ensure_book_ready(book)
        async with book.lock:
            asks = book.sorted_asks()
            bids = book.sorted_bids()
            if not asks or not bids:
                raise ValueError(f"Order book for {product_id} is empty")

            midpoint = (asks[0].price + bids[0].price) / Decimal("2")
            now = datetime.now(timezone.utc)
            sequence = book.sequence

            if aggregation == 0:
                ask_levels = [[_dec_to_str(level.price), _dec_to_str(level.size), level.num_orders] for level in asks[:depth]]
                bid_levels = [[_dec_to_str(level.price), _dec_to_str(level.size), level.num_orders] for level in bids[:depth]]
            else:
                agg = Decimal(aggregation)
                ask_levels = self._aggregate_side(asks, agg, depth, side="ask")
                bid_levels = self._aggregate_side(bids, agg, depth, side="bid")

            return {
                "aggregation": aggregation,
                "depth": depth,
                "date": now,
                "midpoint": _dec_to_str(midpoint),
                "sequence": sequence,
                "asks": ask_levels,
                "bids": bid_levels,
            }

    async def get_market_orders(self, product_id: str, since: Optional[int]) -> Dict[str, Any]:
        book = self._get_book(product_id)
        await self._ensure_book_ready(book)
        async with book.lock:
            sequence = book.sequence
            if since is None:
                return {
                    "sequence": sequence,
                    "buy": {"price": 0.0, "size": 0.0, "numOrders": 0},
                    "sell": {"price": 0.0, "size": 0.0, "numOrders": 0},
                }

            buy_price_sum = Decimal("0")
            buy_size_sum = Decimal("0")
            buy_orders = 0
            sell_price_sum = Decimal("0")
            sell_size_sum = Decimal("0")
            sell_orders = 0
            latest_sequence = sequence

            for entry in book.market_orders:
                if entry.sequence <= since:
                    break
                latest_sequence = max(latest_sequence, entry.sequence)
                notional = entry.price * entry.size
                if entry.side == "buy":
                    buy_price_sum += notional
                    buy_size_sum += entry.size
                    buy_orders += 1
                else:
                    sell_price_sum += notional
                    sell_size_sum += entry.size
                    sell_orders += 1

            buy_price = float((buy_price_sum / buy_size_sum) if buy_size_sum > 0 else Decimal("0"))
            sell_price = float((sell_price_sum / sell_size_sum) if sell_size_sum > 0 else Decimal("0"))

            return {
                "sequence": latest_sequence,
                "buy": {
                    "price": buy_price,
                    "size": float(buy_size_sum),
                    "numOrders": buy_orders,
                },
                "sell": {
                    "price": sell_price,
                    "size": float(sell_size_sum),
                    "numOrders": sell_orders,
                },
            }

    def _get_book(self, product_id: str) -> ProductOrderBook:
        try:
            return self._books[product_id]
        except KeyError as exc:
            raise ValueError(f"Unknown product {product_id}") from exc

    async def _ensure_book_ready(self, book: ProductOrderBook) -> None:
        if book.ready.is_set():
            return
        try:
            await asyncio.wait_for(book.ready.wait(), timeout=30)
        except asyncio.TimeoutError as exc:
            raise ValueError(f"Order book for {book.product_id} is not ready yet") from exc

    def _aggregate_side(
        self,
        levels: List[PriceLevel],
        aggregation: Decimal,
        depth: int,
        *,
        side: Literal["ask", "bid"],
    ) -> List[List[Any]]:
        if not levels:
            return []

        bucket_totals: Dict[int, Tuple[Decimal, Decimal, int]] = {}
        if side == "ask":
            base_index = int((levels[0].price / aggregation).to_integral_value(rounding=ROUND_DOWN))
            for level in levels:
                bucket_index = int((level.price / aggregation).to_integral_value(rounding=ROUND_DOWN))
                relative = bucket_index - base_index
                if relative < 0:
                    continue
                if relative >= depth:
                    break
                price_sum, size_sum, order_sum = bucket_totals.get(relative, (Decimal("0"), Decimal("0"), 0))
                price_sum += level.price * level.size
                size_sum += level.size
                order_sum += level.num_orders or 1
                bucket_totals[relative] = (price_sum, size_sum, order_sum)
            order_keys = range(depth)
        else:
            base_index = int((levels[0].price / aggregation).to_integral_value(rounding=ROUND_DOWN))
            for level in levels:
                bucket_index = int((level.price / aggregation).to_integral_value(rounding=ROUND_DOWN))
                relative = base_index - bucket_index
                if relative < 0:
                    continue
                if relative >= depth:
                    break
                price_sum, size_sum, order_sum = bucket_totals.get(relative, (Decimal("0"), Decimal("0"), 0))
                price_sum += level.price * level.size
                size_sum += level.size
                order_sum += level.num_orders or 1
                bucket_totals[relative] = (price_sum, size_sum, order_sum)
            order_keys = range(depth)

        results: List[List[Any]] = []
        for idx in order_keys:
            price_sum, size_sum, order_sum = bucket_totals.get(idx, (Decimal("0"), Decimal("0"), 0))
            if size_sum <= 0:
                continue
            average_price = price_sum / size_sum
            results.append([
                _dec_to_str(average_price),
                _dec_to_str(size_sum),
                order_sum,
            ])
        return results[:depth]

    def _parse_price_levels(self, raw_levels: Iterable[Any]) -> List[PriceLevel]:
        levels: List[PriceLevel] = []
        for raw in raw_levels:
            if isinstance(raw, dict):
                price = Decimal(str(raw.get("price")))
                size = Decimal(str(raw.get("size", "0")))
                num_orders = int(raw.get("num_orders", raw.get("numOrders", raw.get("count", 1))))
            else:
                price = Decimal(str(raw[0]))
                size = Decimal(str(raw[1]))
                num_orders = int(raw[2]) if len(raw) > 2 else 1
            levels.append(PriceLevel(price=price, size=size, num_orders=num_orders))
        return levels

    def _parse_changes(self, raw_changes: Iterable[Any]) -> List[Tuple[str, Decimal, Decimal, int]]:
        changes: List[Tuple[str, Decimal, Decimal, int]] = []
        for change in raw_changes:
            if isinstance(change, dict):
                side = (change.get("side") or change.get("type", "buy")).lower()
                price = Decimal(str(change.get("price")))
                size = Decimal(str(change.get("size", change.get("remaining", "0"))))
                num_orders = int(change.get("num_orders", change.get("count", 1)))
            else:
                side = str(change[0]).lower()
                price = Decimal(str(change[1]))
                size = Decimal(str(change[2]))
                num_orders = int(change[3]) if len(change) > 3 else 1
            normalised_side = "buy" if side.startswith("b") else "sell"
            changes.append((normalised_side, price, size, num_orders))
        return changes

    async def _websocket_loop(self) -> None:
        backoff = 1
        while self._running.is_set():
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - runtime protection
                logger.exception("Websocket loop error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            else:
                backoff = 1

    async def _connect_and_stream(self) -> None:
        payload = self._subscription_payload()
        async with self._reconnect_lock:
            logger.info("Connecting to Coinbase Advanced Trade websocket at %s", self._config.ws_url)
            async with websockets.connect(self._config.ws_url, ping_interval=20, ping_timeout=20) as ws:
                self._ws = ws
                await ws.send(json.dumps(payload))
                async for raw_message in ws:
                    await self._handle_message(json.loads(raw_message))

    async def _handle_message(self, message: Dict[str, Any]) -> None:
        channel = message.get("channel") or message.get("type")
        events = message.get("events")

        if channel == "subscriptions":
            logger.debug("Subscribed to channels: %s", events)
            return

        if isinstance(events, list):
            for event in events:
                await self._handle_event(event, channel)
            return

        await self._handle_event(message, channel)

    async def _handle_event(self, event: Dict[str, Any], channel: Optional[str]) -> None:
        event_type = event.get("type")
        product_id = event.get("product_id") or event.get("productId")
        if not product_id or product_id not in self._books:
            return

        if channel == "level2" or event_type in {"snapshot", "update"}:
            await self._process_level2_event(product_id, event)
        elif channel == "market_trades" or event_type == "trade" or event.get("trades"):
            await self._process_trade_event(product_id, event)

    async def _process_level2_event(self, product_id: str, event: Dict[str, Any]) -> None:
        book = self._books[product_id]
        sequence = int(event.get("sequence", book.sequence))

        if event.get("type") == "snapshot":
            bids = self._parse_price_levels(event.get("bids", []))
            asks = self._parse_price_levels(event.get("asks", []))
            async with book.lock:
                book.update_from_snapshot(bids, asks, sequence)
            return

        changes = self._parse_changes(event.get("changes") or event.get("updates", []))
        async with book.lock:
            book.apply_update(changes, sequence)

    async def _process_trade_event(self, product_id: str, event: Dict[str, Any]) -> None:
        book = self._books[product_id]
        trades = event.get("trades") or [event]
        parsed: List[TradeEntry] = []

        for trade in trades:
            sequence = int(trade.get("sequence", book.sequence))
            side = (trade.get("side") or trade.get("taker_side", "buy")).lower()
            price = Decimal(str(trade.get("price")))
            size = Decimal(str(trade.get("size", trade.get("quantity", "0"))))
            timestamp_raw = trade.get("time") or trade.get("timestamp")
            if isinstance(timestamp_raw, (int, float)):
                timestamp = datetime.fromtimestamp(float(timestamp_raw), tz=timezone.utc)
            elif isinstance(timestamp_raw, str):
                timestamp = datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
            else:
                timestamp = datetime.now(timezone.utc)
            parsed.append(
                TradeEntry(
                    sequence=sequence,
                    side="buy" if side.startswith("b") else "sell",
                    price=price,
                    size=size,
                    timestamp=timestamp,
                )
            )

        async with book.lock:
            for entry in parsed:
                book.record_trade(entry)

    def _subscription_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "type": "subscribe",
            "product_ids": self._config.products,
            "channels": [
                {"name": "level2", "product_ids": self._config.products},
                {"name": "market_trades", "product_ids": self._config.products},
            ],
        }

        if self._has_credentials:
            timestamp = str(int(time.time()))
            message = f"{timestamp}GET/users/self".encode()
            signature = self._sign_message(message)
            payload.update(
                {
                    "signature": signature,
                    "key": self._config.api_key,
                    "timestamp": timestamp,
                }
            )
            if self._config.api_passphrase:
                payload["passphrase"] = self._config.api_passphrase

        return payload

    def _load_signing_key(self) -> Optional[ec.EllipticCurvePrivateKey | Ed25519PrivateKey]:
        secret = self._config.api_secret
        if not secret:
            return None
        secret = secret.strip()

        if secret.startswith("-----BEGIN"):
            key = load_pem_private_key(secret.encode(), password=None)
            if isinstance(key, (Ed25519PrivateKey, ec.EllipticCurvePrivateKey)):
                return key
            raise ValueError("Unsupported private key type in PEM payload")

        try:
            raw = base64.b64decode(secret)
        except Exception as exc:
            raise ValueError('Failed to base64 decode Coinbase private key') from exc
        if len(raw) == 64:
            raw = raw[:32]
        if len(raw) != 32:
            raise ValueError('Unexpected Coinbase private key length; expected 32 or 64 bytes')
        return Ed25519PrivateKey.from_private_bytes(raw)

    def _sign_message(self, message: bytes) -> str:
        if self._signing_key is None:
            raise RuntimeError('No signing key configured for Coinbase API access')
        if isinstance(self._signing_key, Ed25519PrivateKey):
            signature = self._signing_key.sign(message)
        elif isinstance(self._signing_key, ec.EllipticCurvePrivateKey):
            signature = self._signing_key.sign(message, ec.ECDSA(hashes.SHA256()))
        else:
            raise RuntimeError('Unsupported signing key type loaded for Coinbase API access')
        return base64.b64encode(signature).decode()

    @property
    def _has_credentials(self) -> bool:
        return bool(self._config.api_key and self._signing_key)

    def _auth_headers(
        self,
        method: str,
        path: str,
        *,
        body: str = "",
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        if not self._has_credentials:
            return {}
        timestamp = str(int(time.time()))
        request_path = path if path.startswith('/api/') else f'/api/v3{path}'
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            if query:
                request_path = f'{request_path}?{query}'
        message = f'{timestamp}{method.upper()}{request_path}{body}'.encode()
        signature = self._sign_message(message)
        headers = {
            "CB-ACCESS-KEY": self._config.api_key or "",
            "CB-ACCESS-SIGN": signature,
            "CB-ACCESS-TIMESTAMP": timestamp,
        }
        if self._config.api_passphrase:
            headers["CB-ACCESS-PASSPHRASE"] = self._config.api_passphrase
        headers["Content-Type"] = "application/json"
        return headers


__all__ = ["CoinbaseOrderBookManager", "ProductOrderBook"]
