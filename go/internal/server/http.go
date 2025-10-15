package server

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5"

	"coinbase-local/go/internal/coinbase"
)

type Server struct {
	manager *coinbase.Manager
}

func New(manager *coinbase.Manager) *Server {
	return &Server{manager: manager}
}

func (s *Server) Router() http.Handler {
	r := chi.NewRouter()
	r.Get("/api/orderBook/interval", s.handleInterval)
	r.Get("/api/orderBook/marketOrders", s.handleMarketOrders)
	return r
}

func (s *Server) handleInterval(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := contextWithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	product := r.URL.Query().Get("product")
	if product == "" {
		writeError(w, http.StatusBadRequest, "product is required")
		return
	}

	aggregation := parseQueryInt(r, "aggregation", 0)
	depth := parseQueryInt(r, "depth", 50)
	if depth <= 0 {
		designDepth := 1
		depth = designDepth
	}

	resp, err := s.manager.GetInterval(ctx, product, aggregation, depth)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, resp)
}

func (s *Server) handleMarketOrders(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := contextWithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	product := r.URL.Query().Get("product")
	if product == "" {
		writeError(w, http.StatusBadRequest, "product is required")
		return
	}

	var sincePtr *int64
	if sinceStr := r.URL.Query().Get("since"); sinceStr != "" {
		if parsed, err := strconv.ParseInt(sinceStr, 10, 64); err == nil {
			sincePtr = &parsed
		}
	}

	resp, err := s.manager.GetMarketOrders(ctx, product, sincePtr)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, resp)
}

func writeJSON(w http.ResponseWriter, payload interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(payload)
}

func writeError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]string{"error": message})
}

func parseQueryInt(r *http.Request, key string, fallback int) int {
	if v := r.URL.Query().Get(key); v != "" {
		if parsed, err := strconv.Atoi(v); err == nil {
			return parsed
		}
	}
	return fallback
}

func contextWithTimeout(parent context.Context, timeout time.Duration) (context.Context, context.CancelFunc) {
	if timeout <= 0 {
		return parent, func() {}
	}
	return context.WithTimeout(parent, timeout)
}
