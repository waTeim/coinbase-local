from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP, getcontext
from multiprocessing import AuthenticationError
from typing import Any, Deque, Dict, Iterable, List, Literal, Optional, Tuple

from coinbase.rest import RESTClient
from coinbase.websocket import (
    WSClient,
    WSClientConnectionClosedException,
    WSClientException,
    WebsocketResponse,
)
from requests.exceptions import HTTPError

from .config import AppConfig


logger = logging.getLogger(__name__)
getcontext().prec = 28


class OrderBookNotReady(Exception):
    """A book is temporarily unavailable (warming up or reconnecting to Coinbase).

    Distinct from ValueError (which signals genuine client errors such as an
    unknown product) so the API layer can return 503 (transient, retry) not 400.
    """


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

    def update_from_snapshot(
        self,
        bids: Iterable[PriceLevel],
        asks: Iterable[PriceLevel],
        sequence: int,
    ) -> None:
        self.bids = {level.price: level for level in bids if level.size > 0}
        self.asks = {level.price: level for level in asks if level.size > 0}
        self.sequence = sequence
        self.mark_ready()

    def apply_update(
        self,
        changes: Iterable[Tuple[str, Decimal, Decimal, int]],
        sequence: int,
    ) -> None:
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

    def clear_ready(self) -> None:
        """Clear the ready state, typically called when connection is lost."""
        self.ready.clear()

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
            product: ProductOrderBook(product, config.market_order_buffer)
            for product in config.products
        }
        self._rest_client = RESTClient(
            api_key=config.api_key,
            api_secret=config.api_secret,
        )
        self._ws_client = WSClient(
            api_key=config.api_key,
            api_secret=config.api_secret,
            base_url=config.ws_url,
            max_size=None,
            on_message=self._on_ws_message,
        )
        self._ws_task: Optional[asyncio.Task] = None
        self._running = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_message_time: Optional[datetime] = None
        self._heartbeat_timeout = 30  # seconds without messages triggers reconnection

    async def start(self) -> None:
        logger.info("Starting CoinbaseOrderBookManager for products: %s", self._config.products)
        await self._prime_snapshots()
        self._loop = asyncio.get_running_loop()
        self._running.set()
        logger.info("Starting WebSocket connection loop")
        self._ws_task = asyncio.create_task(self._websocket_loop())

    async def stop(self) -> None:
        self._running.clear()
        if self._ws_client.websocket:
            try:
                await self._ws_client.unsubscribe_all_async()
            except WSClientException:
                logger.debug("Websocket unsubscribe failed during shutdown", exc_info=True)
            try:
                await self._ws_client.close_async()
            except WSClientException:
                pass
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None

    def is_running(self) -> bool:
        return self._running.is_set()

    def is_ready(self) -> bool:
        return self.is_running() and all(book.ready.is_set() for book in self._books.values())

    def _handle_task_exception(self, task: asyncio.Task) -> None:
        """Callback to log exceptions from WebSocket message processing tasks."""
        try:
            task.result()  # Raises exception if task failed
        except Exception as exc:
            logger.error(
                "WebSocket message processing task failed: %s",
                exc,
                exc_info=True
            )

    async def _prime_snapshots(self) -> None:
        logger.info("Priming initial snapshots for %d products: %s", len(self._config.products), self._config.products)
        for product_id in self._config.products:
            try:
                await self._load_snapshot(product_id)
            except Exception as exc:  # pragma: no cover - defensive
                logger.error(
                    "Failed to load initial snapshot for %s: %s. Service will attempt to reload on WebSocket connection.",
                    product_id, exc, exc_info=True
                )

    async def _load_snapshot(self, product_id: str) -> None:
        response = None
        used_public = False
        try:
            logger.debug("Fetching authenticated snapshot for %s", product_id)
            response = await asyncio.to_thread(
                self._rest_client.get_product_book,
                product_id,
                500,
            )
        except AuthenticationError:
            logger.warning(
                "API credentials unavailable for %s snapshot; falling back to public endpoint",
                product_id,
            )
            used_public = True
            response = await asyncio.to_thread(
                self._rest_client.get_public_product_book,
                product_id,
                500,
            )
        except HTTPError as exc:
            status = exc.response.status_code if exc.response else None
            if status == 401:
                logger.warning(
                    "Snapshot unauthorized for %s; falling back to public endpoint", product_id
                )
                used_public = True
                response = await asyncio.to_thread(
                    self._rest_client.get_public_product_book,
                    product_id,
                    500,
                )
            else:
                logger.error("Failed to pull snapshot for %s: %s", product_id, exc)
                raise
        except Exception as exc:
            logger.error("Unexpected error fetching snapshot for %s: %s", product_id, exc, exc_info=True)
            raise

        logger.debug("Snapshot response type: %s, public=%s", type(response).__name__, used_public)

        pricebook = getattr(response, "pricebook", None)
        if pricebook is None:
            logger.error(
                "Snapshot payload missing pricebook for %s. Response attributes: %s",
                product_id,
                dir(response) if response else "None"
            )
            raise ValueError(f"Snapshot payload missing pricebook for {product_id}")

        raw_bids = getattr(pricebook, "bids", []) or []
        raw_asks = getattr(pricebook, "asks", []) or []

        logger.debug(
            "Pricebook for %s has %d raw bids, %d raw asks",
            product_id, len(raw_bids), len(raw_asks)
        )

        if len(raw_bids) == 0 and len(raw_asks) == 0:
            logger.error(
                "Pricebook for %s is completely empty. Pricebook attributes: %s",
                product_id,
                dir(pricebook)
            )

        bids: List[PriceLevel] = []
        skipped_bids = 0
        for entry in raw_bids:
            price = self._safe_decimal(getattr(entry, "price", None))
            size = self._safe_decimal(getattr(entry, "size", None))
            if price is None or size is None:
                skipped_bids += 1
                if skipped_bids <= 3:  # Log first few failures
                    logger.debug(
                        "Skipping bid entry for %s: price=%s, size=%s, entry type=%s",
                        product_id, getattr(entry, "price", None), getattr(entry, "size", None), type(entry).__name__
                    )
                continue
            bids.append(PriceLevel(price=price, size=size, num_orders=1))

        if skipped_bids > 0:
            logger.warning("Skipped %d invalid bid entries for %s", skipped_bids, product_id)

        bids.sort(key=lambda level: level.price, reverse=True)

        asks: List[PriceLevel] = []
        skipped_asks = 0
        for entry in raw_asks:
            price = self._safe_decimal(getattr(entry, "price", None))
            size = self._safe_decimal(getattr(entry, "size", None))
            if price is None or size is None:
                skipped_asks += 1
                if skipped_asks <= 3:  # Log first few failures
                    logger.debug(
                        "Skipping ask entry for %s: price=%s, size=%s, entry type=%s",
                        product_id, getattr(entry, "price", None), getattr(entry, "size", None), type(entry).__name__
                    )
                continue
            asks.append(PriceLevel(price=price, size=size, num_orders=1))

        if skipped_asks > 0:
            logger.warning("Skipped %d invalid ask entries for %s", skipped_asks, product_id)

        asks.sort(key=lambda level: level.price)

        if len(bids) == 0 or len(asks) == 0:
            logger.warning(
                "REST snapshot for %s is empty: %d bids, %d asks (from %d raw bids, %d raw asks). "
                "Will rely on WebSocket snapshot instead.",
                product_id, len(bids), len(asks), len(raw_bids), len(raw_asks)
            )
            # Don't raise - let WebSocket populate the book instead
            return

        book = self._books[product_id]
        async with book.lock:
            book.update_from_snapshot(bids=bids, asks=asks, sequence=0)
        logger.debug(
            "Snapshot primed for %s with %d bids / %d asks", product_id, len(bids), len(asks)
        )

    async def get_interval(self, product_id: str, aggregation: int, depth: int) -> Dict[str, Any]:
        book = self._get_book(product_id)
        await self._ensure_book_ready(book)
        async with book.lock:
            asks = book.sorted_asks()
            bids = book.sorted_bids()
            if not asks or not bids:
                logger.error(
                    "Order book for %s is empty (asks=%d, bids=%d, ready=%s). "
                    "This may indicate a WebSocket reconnection issue.",
                    product_id, len(asks), len(bids), book.ready.is_set()
                )
                raise OrderBookNotReady(
                    f"Order book for {product_id} is empty. "
                    "The service may be reconnecting to Coinbase. Please retry in a few seconds."
                )

            midpoint = (asks[0].price + bids[0].price) / Decimal("2")
            now = datetime.now(timezone.utc)
            sequence = book.sequence

            if aggregation == 0:
                ask_levels = [
                    [_dec_to_str(level.price), _dec_to_str(level.size), level.num_orders]
                    for level in asks[:depth]
                ]
                bid_levels = [
                    [_dec_to_str(level.price), _dec_to_str(level.size), level.num_orders]
                    for level in bids[:depth]
                ]
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

    async def _websocket_loop(self) -> None:
        backoff = 1
        while self._running.is_set():
            try:
                logger.info("Opening WebSocket connection to Coinbase")
                await self._ws_client.open_async()
                logger.info("WebSocket connection established")

                # Check if any books are empty and reload snapshots if needed
                for product_id in self._config.products:
                    book = self._books[product_id]
                    needs_reload = False
                    async with book.lock:
                        if not book.asks or not book.bids:
                            logger.debug("Order book for %s is empty, reloading snapshot", product_id)
                            # Clear ready state so clients wait for fresh data
                            book.clear_ready()
                            needs_reload = True
                    # Reload snapshot outside the lock to avoid blocking
                    if needs_reload:
                        try:
                            await self._load_snapshot(product_id)
                        except Exception as exc:
                            logger.warning("Failed to reload snapshot for %s: %s", product_id, exc)

                logger.info("Subscribing to level2 channel for products: %s", self._config.products)
                await self._ws_client.level2_async(self._config.products)
                logger.info("Subscribing to market_trades channel for products: %s", self._config.products)
                await self._ws_client.market_trades_async(self._config.products)

                # Reset backoff on successful connection
                backoff = 1

                logger.info("WebSocket subscriptions complete, listening for updates")
                while self._running.is_set() and self._ws_client.websocket:
                    await asyncio.sleep(1)
                    self._ws_client.raise_background_exception()

                    # Check for stalled connection (no messages received recently)
                    if self._last_message_time:
                        elapsed = (datetime.now(timezone.utc) - self._last_message_time).total_seconds()
                        if elapsed > self._heartbeat_timeout:
                            logger.warning(
                                "No WebSocket messages received for %.1f seconds (timeout=%d), forcing reconnection",
                                elapsed,
                                self._heartbeat_timeout
                            )
                            break  # Exit loop to trigger reconnection
            except WSClientConnectionClosedException as exc:
                logger.warning("Websocket connection closed: %s", exc)
            except WSClientException as exc:
                logger.warning("Websocket error: %s", exc)
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Unexpected websocket exception: %s", exc)
            finally:
                # Clear ready state for all books when connection is lost
                for book in self._books.values():
                    book.clear_ready()

                if self._ws_client.websocket:
                    try:
                        await self._ws_client.close_async()
                    except WSClientException:
                        pass

            if self._running.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _on_ws_message(self, message: str) -> None:
        if not self._loop:
            return
        self._last_message_time = datetime.now(timezone.utc)
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            logger.debug("Discarding malformed websocket payload", exc_info=True)
            return
        self._loop.call_soon_threadsafe(self._enqueue_ws_payload, payload)

    def _enqueue_ws_payload(self, payload: Dict[str, Any]) -> None:
        task = asyncio.create_task(self._process_ws_payload(payload))
        task.add_done_callback(self._handle_task_exception)

    async def _process_ws_payload(self, payload: Dict[str, Any]) -> None:
        try:
            # Coinbase's websocket envelope no longer always includes every field
            # the SDK's WebsocketResponse treats as mandatory (it pops `client_id`,
            # `timestamp`, `sequence_num`, and `events` with no default). Current
            # frames omit `client_id`, so the constructor raises KeyError and the
            # message is dropped by the handler below — meaning *every* l2_data
            # update is lost and the book never fills. We only use
            # channel/sequence_num/events, so backfill harmless defaults for any
            # absent envelope keys before parsing.
            for key, default in (
                ("client_id", ""),
                ("timestamp", ""),
                ("sequence_num", 0),
                ("events", []),
            ):
                payload.setdefault(key, default)
            response = WebsocketResponse(payload)
            channel = response.channel

            if channel == "l2_data":
                for event in response.events or []:
                    await self._handle_level2_event(response, event)
            elif channel == "market_trades":
                for event in response.events or []:
                    await self._handle_market_trades_event(response, event)
        except Exception as exc:
            logger.error(
                "Failed to process WebSocket payload (channel=%s): %s",
                payload.get("channel", "unknown"),
                exc,
                exc_info=True
            )
            # Don't re-raise - allow processing of subsequent messages to continue

    async def _handle_level2_event(self, response: WebsocketResponse, event: Any) -> None:
        product_id = getattr(event, "product_id", None)
        if not product_id or product_id not in self._books:
            return

        updates = getattr(event, "updates", None) or []
        sequence = response.sequence_num
        book = self._books[product_id]

        if getattr(event, "type", None) == "snapshot":
            bids: List[PriceLevel] = []
            asks: List[PriceLevel] = []
            for update in updates:
                price = self._safe_decimal(getattr(update, "price_level", None))
                size = self._safe_decimal(getattr(update, "new_quantity", None))
                if price is None or size is None or size <= 0:
                    continue
                side = (getattr(update, "side", "") or "").lower()
                level = PriceLevel(price=price, size=size, num_orders=1)
                if side.startswith("bid"):
                    bids.append(level)
                else:
                    asks.append(level)
            bids.sort(key=lambda level: level.price, reverse=True)
            asks.sort(key=lambda level: level.price)
            async with book.lock:
                book.update_from_snapshot(bids, asks, sequence)
            logger.debug(
                "WebSocket snapshot received for %s: %d bids, %d asks (sequence=%d)",
                product_id, len(bids), len(asks), sequence
            )
            return

        changes: List[Tuple[str, Decimal, Decimal, int]] = []
        for update in updates:
            price = self._safe_decimal(getattr(update, "price_level", None))
            size = self._safe_decimal(getattr(update, "new_quantity", None))
            if price is None or size is None:
                continue
            side = (getattr(update, "side", "") or "").lower()
            changes.append(("buy" if side.startswith("bid") else "sell", price, size, 1))

        if changes:
            async with book.lock:
                book.apply_update(changes, sequence)

    async def _handle_market_trades_event(
        self,
        response: WebsocketResponse,
        event: Any,
    ) -> None:
        trades = getattr(event, "trades", None) or []
        sequence = response.sequence_num

        for trade in trades:
            product_id = getattr(trade, "product_id", None) or getattr(event, "product_id", None)
            if not product_id or product_id not in self._books:
                continue

            price = self._safe_decimal(getattr(trade, "price", None))
            size = self._safe_decimal(getattr(trade, "size", None))
            if price is None or size is None:
                continue

            side_raw = (getattr(trade, "side", "") or "").lower()
            side = "buy" if side_raw.startswith("b") else "sell"
            timestamp = self._parse_timestamp(getattr(trade, "time", None))

            entry = TradeEntry(
                sequence=sequence,
                side=side,
                price=price,
                size=size,
                timestamp=timestamp,
            )

            book = self._books[product_id]
            async with book.lock:
                book.record_trade(entry)

    def _safe_decimal(self, value: Optional[Any]) -> Optional[Decimal]:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (ArithmeticError, ValueError):
            return None

    def _parse_timestamp(self, value: Optional[Any]) -> datetime:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.now(timezone.utc)

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
            raise OrderBookNotReady(f"Order book for {book.product_id} is not ready yet") from exc

    def _aggregate_side(
        self,
        levels: List[PriceLevel],
        aggregation: Decimal,
        depth: int,
        *,
        side: Literal["ask", "bid"],
    ) -> List[List[Any]]:
        if depth <= 0:
            return []

        if not levels:
            # With no levels, synthesize prices from the base bucket derived from
            # the best-known price for the side (fall back to 0 if unknown).
            best_price = Decimal("0")
            if side == "ask":
                base_idx = int((best_price / aggregation).to_integral_value(rounding=ROUND_UP))
                def bucket_price(i: int) -> Decimal:
                    return (Decimal(base_idx + i) * aggregation)
            else:
                base_idx = int((best_price / aggregation).to_integral_value(rounding=ROUND_DOWN))
                def bucket_price(i: int) -> Decimal:
                    return (Decimal(base_idx - i) * aggregation)

            return [[
                _dec_to_str(bucket_price(i)),
                _dec_to_str(Decimal("0")),
                0,
            ] for i in range(depth)]

        bucket_totals: Dict[int, Tuple[Decimal, Decimal, int]] = {}

        if side == "ask":
            base_index = int((levels[0].price / aggregation).to_integral_value(rounding=ROUND_UP))
            for level in levels:
                bucket_index = int((level.price / aggregation).to_integral_value(rounding=ROUND_UP))
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

            def bucket_price(i: int) -> Decimal:
                return (Decimal(base_index + i) * aggregation)

        else:  # bid
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

            def bucket_price(i: int) -> Decimal:
                return (Decimal(base_index - i) * aggregation)

        results: List[List[Any]] = []
        for idx in range(depth):
            price_sum, size_sum, order_sum = bucket_totals.get(idx, (Decimal("0"), Decimal("0"), 0))
            if size_sum > 0:
                avg_price = price_sum / size_sum
                results.append([
                    _dec_to_str(avg_price),
                    _dec_to_str(size_sum),
                    order_sum,
                ])
            else:
                # Emit an empty bucket with its representative price.
                bp = bucket_price(idx)
                results.append([
                    _dec_to_str(bp),
                    _dec_to_str(Decimal("0")),
                    0,
                ])

        return results  # guaranteed len == depth



__all__ = ["CoinbaseOrderBookManager", "ProductOrderBook"]
