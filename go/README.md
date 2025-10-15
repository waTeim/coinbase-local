# Go Order Book Service

This Go service mirrors the original TypeScript `/api/orderBook/interval` and `/api/orderBook/marketOrders` endpoints using Coinbase's OAuth flow for Advanced Trade.

## Prerequisites

* Go 1.21+
* A Coinbase Advanced Trade API key JSON (same file consumed by `cdpcurl`).

## Install dependencies

```bash
cd go
GO111MODULE=on go mod tidy
```

## Run the server

```bash
cd go
GO111MODULE=on go run ./cmd/server \
  --products BTC-USD:ETH-USD \
  --key-file /path/to/key.json \
  --port 63200
```

Environment variables `PRODUCTS`, `COINBASE_API_KEY_FILE`, `PORT`, etc. provide the same configuration knobs; `--api-key/--api-secret` remain available for legacy key formats.

## Endpoints

* `GET /api/orderBook/interval?product=BTC-USD&aggregation=0&depth=50`
* `GET /api/orderBook/marketOrders?product=BTC-USD&since=<sequence>`

Both endpoints require the service to maintain a websocket connection to Coinbase's Advanced Trade market feeds; the server obtains snapshots via REST and streams updates via websocket, aggregating depth buckets and market order summaries in memory.
