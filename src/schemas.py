from __future__ import annotations

from datetime import datetime
from typing import List, Tuple, Optional

from pydantic import BaseModel, Field, ConfigDict


PriceLevelTuple = Tuple[str, str, int]


class OrderBookIntervalResponse(BaseModel):
    aggregation: float
    depth: int
    date: datetime
    midpoint: str
    sequence: int
    asks: List[PriceLevelTuple]
    bids: List[PriceLevelTuple]

    model_config = ConfigDict(
        json_encoders={datetime: lambda value: value.isoformat()},
        populate_by_name=True,
    )


class MarketSideSummary(BaseModel):
    price: float
    size: float
    numOrders: int = Field(alias="numOrders")

    model_config = ConfigDict(populate_by_name=True)


class MarketOrderIntervalResponse(BaseModel):
    sequence: int
    buy: MarketSideSummary
    sell: MarketSideSummary

    model_config = ConfigDict(populate_by_name=True)


__all__ = [
    "OrderBookIntervalResponse",
    "MarketOrderIntervalResponse",
    "MarketSideSummary",
]
