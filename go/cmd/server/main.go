package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"coinbase-local/go/internal/auth"
	"coinbase-local/go/internal/coinbase"
	"coinbase-local/go/internal/config"
	"coinbase-local/go/internal/server"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("load config: %v", err)
	}

	tokenProvider, err := auth.NewTokenProvider(cfg)
	if err != nil {
		log.Fatalf("init token provider: %v", err)
	}

	manager := coinbase.NewManager(cfg, tokenProvider)

	rootCtx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if err := manager.Start(rootCtx); err != nil {
		log.Fatalf("start manager: %v", err)
	}

	srv := server.New(manager)
	httpServer := &http.Server{
		Addr:    fmt.Sprintf(":%d", cfg.Port),
		Handler: srv.Router(),
	}

	go func() {
		<-rootCtx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := httpServer.Shutdown(shutdownCtx); err != nil {
			log.Printf("http shutdown error: %v", err)
		}
		manager.Stop()
	}()

	log.Printf("listening on http://0.0.0.0:%d", cfg.Port)
	if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("http server: %v", err)
	}
}
