from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query

from .config import AppConfig
from .orderbook import CoinbaseOrderBookManager
from .schemas import MarketOrderIntervalResponse, OrderBookIntervalResponse


logger = logging.getLogger(__name__)


def create_app(config: AppConfig) -> FastAPI:
    app = FastAPI(
        title="Coinbase Advanced Trade Order Book",
        version="0.1.0",
        description="FastAPI replica of the Coinbase order book endpoints",
    )

    manager = CoinbaseOrderBookManager(config)

    @app.on_event("startup")
    async def _startup() -> None:  # pragma: no cover - framework hook
        await manager.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:  # pragma: no cover - framework hook
        await manager.stop()

    def get_manager() -> CoinbaseOrderBookManager:
        return manager

    ManagerDep = Annotated[CoinbaseOrderBookManager, Depends(get_manager)]

    @app.get("/api/orderBook/interval", response_model=OrderBookIntervalResponse)
    async def get_interval(
        product: str = Query(..., min_length=3),
        aggregation: int = Query(0, ge=0),
        depth: int = Query(50, gt=0, le=500),
        orderbook: ManagerDep = Depends(get_manager),
    ) -> OrderBookIntervalResponse:
        try:
            payload = await orderbook.get_interval(product, aggregation, depth)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return OrderBookIntervalResponse(**payload)

    @app.get("/api/orderBook/marketOrders", response_model=MarketOrderIntervalResponse)
    async def get_market_orders(
        product: str = Query(..., min_length=3),
        since: int | None = Query(None, ge=0),
        orderbook: ManagerDep = Depends(get_manager),
    ) -> MarketOrderIntervalResponse:
        try:
            payload = await orderbook.get_market_orders(product, since)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return MarketOrderIntervalResponse(**payload)

    return app


__all__ = ["create_app"]
