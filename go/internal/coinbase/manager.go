package coinbase

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/gorilla/websocket"
	"github.com/shopspring/decimal"

	"coinbase-local/go/internal/auth"
	"coinbase-local/go/internal/config"
)

type Manager struct {
	cfg           config.Config
	tokenProvider *auth.TokenProvider
	httpClient    *http.Client

	books map[string]*ProductBook
	mu    sync.RWMutex

	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup
}

func NewManager(cfg config.Config, provider *auth.TokenProvider) *Manager {
	httpClient := &http.Client{Timeout: cfg.HTTPTimeout}
	books := make(map[string]*ProductBook, len(cfg.Products))
	for _, p := range cfg.Products {
		books[p] = newProductBook(cfg.MarketOrderBuffer)
	}
	return &Manager{
		cfg:           cfg,
		tokenProvider: provider,
		httpClient:    httpClient,
		books:         books,
	}
}

func (m *Manager) Start(ctx context.Context) error {
	m.ctx, m.cancel = context.WithCancel(ctx)

	for _, product := range m.cfg.Products {
		if err := m.primeSnapshot(m.ctx, product); err != nil {
			return fmt.Errorf("prime snapshot %s: %w", product, err)
		}
	}

	m.wg.Add(1)
	go func() {
		defer m.wg.Done()
		m.runWebsocket(m.ctx)
	}()

	return nil
}

func (m *Manager) Stop() {
	if m.cancel != nil {
		m.cancel()
	}
	m.wg.Wait()
}

func (m *Manager) GetInterval(ctx context.Context, product string, aggregation, depth int) (IntervalResponse, error) {
	book := m.getBook(product)
	if book == nil {
		return IntervalResponse{}, fmt.Errorf("unknown product %s", product)
	}
	if err := book.waitReady(ctx); err != nil {
		return IntervalResponse{}, err
	}
	return book.buildInterval(aggregation, depth)
}

func (m *Manager) GetMarketOrders(ctx context.Context, product string, since *int64) (MarketOrderInterval, error) {
	book := m.getBook(product)
	if book == nil {
		return MarketOrderInterval{}, fmt.Errorf("unknown product %s", product)
	}
	if err := book.waitReady(ctx); err != nil {
		return MarketOrderInterval{}, err
	}
	return book.buildMarketInterval(since)
}

func (m *Manager) getBook(product string) *ProductBook {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.books[product]
}

// Prime snapshot fetches the current book via REST.
func (m *Manager) primeSnapshot(ctx context.Context, product string) error {
	// Coinbase Advanced Trade caps the REST snapshot page size at 100; requesting more yields 400.
	endpoint := fmt.Sprintf("%s/brokerage/product_book?product_id=%s&limit=100", strings.TrimSuffix(m.cfg.RESTURL, "/"), url.QueryEscape(product))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return err
	}

	token, err := m.tokenProvider.GetToken(ctx, req.Method, req.URL)
	if err != nil {
		return fmt.Errorf("build auth token: %w", err)
	}
	logTokenClaims("rest", product, token)
	req.Header.Set("Authorization", "Bearer "+token)

	resp, err := m.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(resp.Body)
		msg := strings.TrimSpace(string(body))
		if resp.StatusCode == http.StatusUnauthorized {
			return fmt.Errorf("unauthorized when fetching snapshot: %s", msg)
		}
		if msg != "" {
			return fmt.Errorf("snapshot request failed: %s: %s", resp.Status, msg)
		}
		return fmt.Errorf("snapshot request failed: %s", resp.Status)
	}

	var snapshot bookSnapshotResponse
	if err := json.NewDecoder(resp.Body).Decode(&snapshot); err != nil {
		return fmt.Errorf("decode snapshot: %w", err)
	}

	book := m.getBook(product)
	if book == nil {
		return fmt.Errorf("no book holder for product %s", product)
	}

	return book.applySnapshot(snapshot)
}

func (m *Manager) runWebsocket(ctx context.Context) {
	backoff := time.Second
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		if err := m.streamOnce(ctx); err != nil {
			if !errors.Is(err, context.Canceled) {
				fmt.Printf("websocket error: %v\n", err)
			}
			time.Sleep(backoff)
			backoff *= 2
			if backoff > 30*time.Second {
				backoff = 30 * time.Second
			}
		} else {
			backoff = time.Second
		}
	}
}

func (m *Manager) streamOnce(ctx context.Context) error {
	wsURL, err := url.Parse(m.cfg.WSURL)
	if err != nil {
		return fmt.Errorf("parse websocket url: %w", err)
	}

	token, err := m.tokenProvider.GetToken(ctx, http.MethodGet, wsURL)
	if err != nil {
		return fmt.Errorf("build websocket token: %w", err)
	}
	logTokenClaims("websocket", strings.Join(m.cfg.Products, ","), token)

	headers := http.Header{}
	headers.Set("Authorization", "Bearer "+token)

	dialer := websocket.Dialer{
		Proxy:            http.ProxyFromEnvironment,
		HandshakeTimeout: 20 * time.Second,
	}

	conn, _, err := dialer.DialContext(ctx, m.cfg.WSURL, headers)
	if err != nil {
		return err
	}
	defer conn.Close()

	subscribe := map[string]interface{}{
		"type":        "subscribe",
		"product_ids": m.cfg.Products,
		"channels": []map[string]interface{}{
			{"name": "level2", "product_ids": m.cfg.Products},
			{"name": "market_trades", "product_ids": m.cfg.Products},
		},
		"jwt": token,
	}
	if err := conn.WriteJSON(subscribe); err != nil {
		return fmt.Errorf("send subscribe: %w", err)
	}

	for {
		_, message, err := conn.ReadMessage()
		if err != nil {
			return err
		}
		if err := m.handleMessage(message); err != nil {
			fmt.Printf("handle message error: %v\n", err)
		}
	}
}

func logTokenClaims(channel, target string, token string) {
	parser := jwt.Parser{}
	parsed, _, err := parser.ParseUnverified(token, jwt.MapClaims{})
	if err != nil {
		log.Printf("debug token parse failed for %s %s: %v", channel, target, err)
		return
	}
	claims, ok := parsed.Claims.(jwt.MapClaims)
	if !ok {
		log.Printf("debug token claims unexpected type for %s %s", channel, target)
		return
	}
	exp := getTimeClaim(claims["exp"])
	nbf := getTimeClaim(claims["nbf"])
	iat := getTimeClaim(claims["iat"])
	log.Printf("auth token (%s %s) iss=%v sub=%v aud=%v exp=%v nbf=%v iat=%v uris=%v", channel, target, claims["iss"], claims["sub"], claims["aud"], exp, nbf, iat, claims["uris"])
}

func getTimeClaim(val interface{}) time.Time {
	switch v := val.(type) {
	case float64:
		return time.Unix(int64(v), 0)
	case json.Number:
		i, _ := v.Int64()
		return time.Unix(i, 0)
	case int64:
		return time.Unix(v, 0)
	case int:
		return time.Unix(int64(v), 0)
	default:
		return time.Time{}
	}
}

func (m *Manager) handleMessage(raw []byte) error {
	var envelope struct {
		Channel string            `json:"channel"`
		Events  []json.RawMessage `json:"events"`
		Type    string            `json:"type"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return fmt.Errorf("decode envelope: %w", err)
	}

	if envelope.Type == "error" {
		return fmt.Errorf("websocket error: %s", string(raw))
	}

	if len(envelope.Events) == 0 {
		if envelope.Type != "" {
			return m.handleEvent(raw)
		}
		return nil
	}

	for _, ev := range envelope.Events {
		if err := m.handleEvent(ev); err != nil {
			return err
		}
	}
	return nil
}

func (m *Manager) handleEvent(raw json.RawMessage) error {
	var meta struct {
		Type      string `json:"type"`
		ProductID string `json:"product_id"`
	}
	if err := json.Unmarshal(raw, &meta); err != nil {
		return err
	}
	if meta.ProductID == "" {
		return nil
	}

	book := m.getBook(meta.ProductID)
	if book == nil {
		return nil
	}

	switch strings.ToLower(meta.Type) {
	case "snapshot":
		var snapshot bookSnapshotEvent
		if err := json.Unmarshal(raw, &snapshot); err != nil {
			return err
		}
		return book.applySnapshot(bookSnapshotResponse{
			Asks:     snapshot.Asks,
			Bids:     snapshot.Bids,
			Sequence: snapshot.Sequence,
		})
	case "update":
		var update bookUpdateEvent
		if err := json.Unmarshal(raw, &update); err != nil {
			return err
		}
		return book.applyUpdate(update)
	case "trade":
		var trade tradeEvent
		if err := json.Unmarshal(raw, &trade); err != nil {
			return err
		}
		book.applyTrades(trade)
	}
	return nil
}

// ----- Data structures -----

type PriceLevel struct {
	Price     decimal.Decimal
	Size      decimal.Decimal
	NumOrders int
}

type TradeEntry struct {
	Sequence int64
	Side     string
	Price    decimal.Decimal
	Size     decimal.Decimal
	Time     time.Time
}

type ProductBook struct {
	mu           sync.RWMutex
	asks         map[string]PriceLevel
	bids         map[string]PriceLevel
	sequence     int64
	marketOrders []TradeEntry
	bufferSize   int
	readyOnce    sync.Once
	readyCh      chan struct{}
}

func newProductBook(buffer int) *ProductBook {
	return &ProductBook{
		asks:       make(map[string]PriceLevel),
		bids:       make(map[string]PriceLevel),
		bufferSize: buffer,
		readyCh:    make(chan struct{}),
	}
}

func (b *ProductBook) markReady() {
	b.readyOnce.Do(func() { close(b.readyCh) })
}

func (b *ProductBook) waitReady(ctx context.Context) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-b.readyCh:
		return nil
	}
}

type bookSnapshotResponse struct {
	Bids     [][]string `json:"bids"`
	Asks     [][]string `json:"asks"`
	Sequence string     `json:"sequence"`
}

type bookSnapshotEvent struct {
	Type      string     `json:"type"`
	ProductID string     `json:"product_id"`
	Bids      [][]string `json:"bids"`
	Asks      [][]string `json:"asks"`
	Sequence  string     `json:"sequence"`
}

type bookUpdateEvent struct {
	Type      string     `json:"type"`
	ProductID string     `json:"product_id"`
	Changes   [][]string `json:"changes"`
	Sequence  string     `json:"sequence"`
}

type tradeEvent struct {
	Type      string        `json:"type"`
	ProductID string        `json:"product_id"`
	Trades    []tradeDetail `json:"trades"`
}

type tradeDetail struct {
	TradeID  string `json:"trade_id"`
	Side     string `json:"side"`
	Price    string `json:"price"`
	Size     string `json:"size"`
	Sequence string `json:"sequence"`
	Time     string `json:"time"`
}

func (b *ProductBook) applySnapshot(snapshot bookSnapshotResponse) error {
	b.mu.Lock()
	defer b.mu.Unlock()

	b.asks = make(map[string]PriceLevel, len(snapshot.Asks))
	for _, row := range snapshot.Asks {
		if len(row) < 2 {
			continue
		}
		price, err := decimal.NewFromString(row[0])
		if err != nil {
			continue
		}
		size, err := decimal.NewFromString(row[1])
		if err != nil {
			continue
		}
		num := parseInt(row, 2)
		b.asks[row[0]] = PriceLevel{Price: price, Size: size, NumOrders: num}
	}

	b.bids = make(map[string]PriceLevel, len(snapshot.Bids))
	for _, row := range snapshot.Bids {
		if len(row) < 2 {
			continue
		}
		price, err := decimal.NewFromString(row[0])
		if err != nil {
			continue
		}
		size, err := decimal.NewFromString(row[1])
		if err != nil {
			continue
		}
		num := parseInt(row, 2)
		b.bids[row[0]] = PriceLevel{Price: price, Size: size, NumOrders: num}
	}

	b.sequence = parseInt64(snapshot.Sequence)
	b.markReady()
	return nil
}

func (b *ProductBook) applyUpdate(update bookUpdateEvent) error {
	b.mu.Lock()
	defer b.mu.Unlock()

	for _, change := range update.Changes {
		if len(change) < 3 {
			continue
		}
		side := strings.ToLower(change[0])
		priceKey := change[1]
		sizeStr := change[2]
		num := parseInt(change, 3)

		if sizeStr == "0" || sizeStr == "0.0" {
			if side == "buy" || side == "bid" {
				delete(b.bids, priceKey)
			} else {
				delete(b.asks, priceKey)
			}
			continue
		}

		price, err1 := decimal.NewFromString(priceKey)
		size, err2 := decimal.NewFromString(sizeStr)
		if err1 != nil || err2 != nil {
			continue
		}
		level := PriceLevel{Price: price, Size: size, NumOrders: num}
		if side == "buy" || side == "bid" {
			b.bids[priceKey] = level
		} else {
			b.asks[priceKey] = level
		}
	}

	seq := parseInt64(update.Sequence)
	if seq > 0 {
		b.sequence = seq
	}
	b.markReady()
	return nil
}

func (b *ProductBook) applyTrades(event tradeEvent) {
	b.mu.Lock()
	defer b.mu.Unlock()

	for _, trade := range event.Trades {
		price, err1 := decimal.NewFromString(trade.Price)
		size, err2 := decimal.NewFromString(trade.Size)
		if err1 != nil || err2 != nil {
			continue
		}
		sequence := parseInt64(trade.Sequence)
		timestamp, _ := time.Parse(time.RFC3339Nano, trade.Time)
		entry := TradeEntry{
			Sequence: sequence,
			Side:     strings.ToLower(trade.Side),
			Price:    price,
			Size:     size,
			Time:     timestamp,
		}
		b.marketOrders = append([]TradeEntry{entry}, b.marketOrders...)
		if len(b.marketOrders) > b.bufferSize {
			b.marketOrders = b.marketOrders[:b.bufferSize]
		}
		if sequence > b.sequence {
			b.sequence = sequence
		}
	}
	b.markReady()
}

func (b *ProductBook) sortedAsks() []PriceLevel {
	levels := make([]PriceLevel, 0, len(b.asks))
	for _, lvl := range b.asks {
		levels = append(levels, lvl)
	}
	sort.Slice(levels, func(i, j int) bool {
		return levels[i].Price.LessThan(levels[j].Price)
	})
	return levels
}

func (b *ProductBook) sortedBids() []PriceLevel {
	levels := make([]PriceLevel, 0, len(b.bids))
	for _, lvl := range b.bids {
		levels = append(levels, lvl)
	}
	sort.Slice(levels, func(i, j int) bool {
		return levels[i].Price.GreaterThan(levels[j].Price)
	})
	return levels
}

// Interval and market responses.

type IntervalResponse struct {
	Aggregation int             `json:"aggregation"`
	Depth       int             `json:"depth"`
	Date        time.Time       `json:"date"`
	Midpoint    string          `json:"midpoint"`
	Sequence    int64           `json:"sequence"`
	Asks        [][]interface{} `json:"asks"`
	Bids        [][]interface{} `json:"bids"`
}

type MarketOrderInterval struct {
	Sequence int64             `json:"sequence"`
	Buy      MarketSideSummary `json:"buy"`
	Sell     MarketSideSummary `json:"sell"`
}

type MarketSideSummary struct {
	Price     float64 `json:"price"`
	Size      float64 `json:"size"`
	NumOrders int     `json:"numOrders"`
}

func (b *ProductBook) buildInterval(aggregation, depth int) (IntervalResponse, error) {
	b.mu.RLock()
	defer b.mu.RUnlock()

	asks := b.sortedAsks()
	bids := b.sortedBids()
	if len(asks) == 0 || len(bids) == 0 {
		return IntervalResponse{}, errors.New("order book empty")
	}

	midpoint := asks[0].Price.Add(bids[0].Price).Div(decimal.NewFromInt(2))

	var askLevels [][]interface{}
	var bidLevels [][]interface{}

	if aggregation <= 0 {
		askLevels = buildRawLevels(asks, depth)
		bidLevels = buildRawLevels(bids, depth)
	} else {
		agg := decimal.NewFromInt(int64(aggregation))
		askLevels = buildAggregatedLevels(asks, depth, agg, true)
		bidLevels = buildAggregatedLevels(bids, depth, agg, false)
	}

	return IntervalResponse{
		Aggregation: aggregation,
		Depth:       depth,
		Date:        time.Now().UTC(),
		Midpoint:    midpoint.String(),
		Sequence:    b.sequence,
		Asks:        askLevels,
		Bids:        bidLevels,
	}, nil
}

func buildRawLevels(levels []PriceLevel, depth int) [][]interface{} {
	if depth > len(levels) {
		depth = len(levels)
	}
	out := make([][]interface{}, 0, depth)
	for i := 0; i < depth; i++ {
		lvl := levels[i]
		out = append(out, []interface{}{lvl.Price.String(), lvl.Size.String(), lvl.NumOrders})
	}
	return out
}

func buildAggregatedLevels(levels []PriceLevel, depth int, aggregation decimal.Decimal, isAsk bool) [][]interface{} {
	if len(levels) == 0 {
		return nil
	}
	buckets := make(map[int64]*aggregateBucket)
	keys := make([]int64, 0, len(levels))
	seen := make(map[int64]struct{})

	for _, lvl := range levels {
		idx := bucketIndex(lvl.Price, aggregation, isAsk)
		bucket := buckets[idx]
		if bucket == nil {
			bucket = &aggregateBucket{}
			buckets[idx] = bucket
			if _, ok := seen[idx]; !ok {
				keys = append(keys, idx)
				seen[idx] = struct{}{}
			}
		}
		bucket.priceSum = bucket.priceSum.Add(lvl.Price.Mul(lvl.Size))
		bucket.sizeSum = bucket.sizeSum.Add(lvl.Size)
		if lvl.NumOrders > 0 {
			bucket.orderCount += lvl.NumOrders
		} else {
			bucket.orderCount++
		}
	}

	if isAsk {
		sort.Slice(keys, func(i, j int) bool { return keys[i] < keys[j] })
	} else {
		sort.Slice(keys, func(i, j int) bool { return keys[i] > keys[j] })
	}

	out := make([][]interface{}, 0, depth)
	for _, idx := range keys {
		bucket := buckets[idx]
		if bucket.sizeSum.Sign() == 0 {
			continue
		}
		avg := bucket.priceSum.Div(bucket.sizeSum)
		out = append(out, []interface{}{avg.String(), bucket.sizeSum.String(), bucket.orderCount})
		if len(out) >= depth {
			break
		}
	}
	return out
}

type aggregateBucket struct {
	priceSum   decimal.Decimal
	sizeSum    decimal.Decimal
	orderCount int
}

func bucketIndex(price, aggregation decimal.Decimal, isAsk bool) int64 {
	if aggregation.Equal(decimal.Zero) {
		return price.IntPart()
	}
	ratio := price.Div(aggregation)
	if isAsk {
		ratio = ratio.Ceil()
	} else {
		ratio = ratio.Floor()
	}
	return ratio.IntPart()
}

func (b *ProductBook) buildMarketInterval(since *int64) (MarketOrderInterval, error) {
	b.mu.RLock()
	defer b.mu.RUnlock()

	if since == nil {
		return MarketOrderInterval{
			Sequence: b.sequence,
			Buy:      MarketSideSummary{},
			Sell:     MarketSideSummary{},
		}, nil
	}

	buyPriceSum := decimal.Zero
	buySizeSum := decimal.Zero
	sellPriceSum := decimal.Zero
	sellSizeSum := decimal.Zero
	buyOrders := 0
	sellOrders := 0
	sequence := b.sequence

	for _, entry := range b.marketOrders {
		if entry.Sequence <= *since {
			continue
		}
		if entry.Sequence > sequence {
			sequence = entry.Sequence
		}
		notional := entry.Price.Mul(entry.Size)
		switch entry.Side {
		case "buy":
			buyPriceSum = buyPriceSum.Add(notional)
			buySizeSum = buySizeSum.Add(entry.Size)
			buyOrders++
		case "sell":
			sellPriceSum = sellPriceSum.Add(notional)
			sellSizeSum = sellSizeSum.Add(entry.Size)
			sellOrders++
		}
	}

	result := MarketOrderInterval{Sequence: sequence}
	if buySizeSum.Sign() > 0 {
		avg := buyPriceSum.Div(buySizeSum)
		result.Buy.Price, _ = avg.Float64()
		result.Buy.Size, _ = buySizeSum.Float64()
		result.Buy.NumOrders = buyOrders
	}
	if sellSizeSum.Sign() > 0 {
		avg := sellPriceSum.Div(sellSizeSum)
		result.Sell.Price, _ = avg.Float64()
		result.Sell.Size, _ = sellSizeSum.Float64()
		result.Sell.NumOrders = sellOrders
	}
	return result, nil
}

func parseInt(values []string, idx int) int {
	if idx >= len(values) {
		return 0
	}
	if v, err := strconv.Atoi(values[idx]); err == nil {
		return v
	}
	if f, err := strconv.ParseFloat(values[idx], 64); err == nil {
		return int(f)
	}
	return 0
}

func parseInt64(raw string) int64 {
	if raw == "" {
		return 0
	}
	if v, err := strconv.ParseInt(raw, 10, 64); err == nil {
		return v
	}
	if f, err := strconv.ParseFloat(raw, 64); err == nil {
		return int64(f)
	}
	return 0
}
