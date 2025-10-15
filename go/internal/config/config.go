package config

import (
	encodingjson "encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type RawKey struct {
	ID         string `json:"id"`
	Name       string `json:"name"`
	Key        string `json:"key"`
	KeyName    string `json:"keyName"`
	KeyID      string `json:"keyId"`
	PrivateKey string `json:"privateKey"`
	Secret     string `json:"secret"`
	Passphrase string `json:"passphrase"`
}

type Config struct {
	Products          []string
	Port              int
	RESTURL           string
	WSURL             string
	KeyIdentifier     string
	KeyID             string
	PrivateKey        string
	Passphrase        string
	TokenAudience     string
	TokenURL          string
	HTTPTimeout       time.Duration
	MarketOrderBuffer int
}

func Load() (Config, error) {
	var (
		productList = flag.String("products", "", "Colon or comma separated list of product IDs (e.g. BTC-USD:ETH-USD)")
		port        = flag.Int("port", envInt("PORT", 63200), "Port to listen on")
		keyFile     = flag.String("key-file", os.Getenv("COINBASE_API_KEY_FILE"), "Path to Coinbase API key JSON")
		keyID       = flag.String("api-key", os.Getenv("COINBASE_API_KEY"), "Coinbase API key identifier or JSON file when --key-file not used")
		secret      = flag.String("api-secret", os.Getenv("COINBASE_API_SECRET"), "Coinbase API private key (base64 or PEM)")
		passphrase  = flag.String("api-passphrase", os.Getenv("COINBASE_API_PASSPHRASE"), "Coinbase API passphrase (optional)")
	)
	flag.Parse()

	cfg := Config{
		RESTURL:           envString("COINBASE_API_REST_URL", "https://api.coinbase.com/api/v3"),
		WSURL:             envString("COINBASE_API_WS_URL", "wss://advanced-trade-ws.coinbase.com"),
		TokenAudience:     envString("COINBASE_TOKEN_AUDIENCE", ""),
		TokenURL:          envString("COINBASE_TOKEN_URL", ""),
		HTTPTimeout:       10 * time.Second,
		MarketOrderBuffer: envInt("MARKET_ORDER_BUFFER", 2000),
	}

	products := strings.TrimSpace(*productList)
	if products == "" {
		products = os.Getenv("PRODUCTS")
	}
	cfg.Products = splitProducts(products)
	if len(cfg.Products) == 0 {
		return Config{}, errors.New("at least one product must be specified via --products or PRODUCTS env")
	}

	cfg.Port = *port

	// Resolve key credentials.
	sourceKey := strings.TrimSpace(*keyID)
	if *keyFile != "" {
		if err := loadKeyJSON(*keyFile, &cfg); err != nil {
			return Config{}, err
		}
	} else if isLikelyJSONPath(sourceKey) {
		if err := loadKeyJSON(sourceKey, &cfg); err != nil {
			return Config{}, err
		}
	} else {
		cfg.KeyIdentifier = sourceKey
		cfg.PrivateKey = strings.TrimSpace(*secret)
		cfg.Passphrase = strings.TrimSpace(*passphrase)
		cfg.KeyID = firstNonEmpty(cfg.KeyID, cfg.KeyIdentifier)
	}

	if cfg.KeyIdentifier == "" || cfg.PrivateKey == "" {
		return Config{}, errors.New("coinbase API key identifier and private key are required; provide --key-file or --api-key/--api-secret")
	}

	if cfg.Passphrase == "" {
		cfg.Passphrase = strings.TrimSpace(*passphrase)
	}

	if cfg.KeyID == "" {
		cfg.KeyID = cfg.KeyIdentifier
	}

	return cfg, nil
}

func splitProducts(raw string) []string {
	if raw == "" {
		return nil
	}
	delims := []string{":", ",", " "}
	for _, d := range delims {
		if strings.Contains(raw, d) {
			parts := strings.Split(raw, d)
			out := make([]string, 0, len(parts))
			for _, p := range parts {
				p = strings.TrimSpace(p)
				if p != "" {
					out = append(out, p)
				}
			}
			return out
		}
	}
	return []string{strings.TrimSpace(raw)}
}

func envString(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		if parsed, err := strconv.Atoi(v); err == nil {
			return parsed
		}
	}
	return fallback
}

func isLikelyJSONPath(input string) bool {
	if input == "" {
		return false
	}
	if strings.HasSuffix(input, ".json") {
		if _, err := os.Stat(input); err == nil {
			return true
		}
	}
	return false
}

func loadKeyJSON(path string, cfg *Config) error {
	abs, err := filepath.Abs(path)
	if err != nil {
		return fmt.Errorf("resolve key file: %w", err)
	}
	data, err := os.ReadFile(abs)
	if err != nil {
		return fmt.Errorf("read key file: %w", err)
	}
	var raw RawKey
	if err := encodingjson.Unmarshal(data, &raw); err != nil {
		return fmt.Errorf("parse key json: %w", err)
	}

	identifier := firstNonEmpty(raw.Name, raw.Key, raw.KeyName)
	fallbackID := firstNonEmpty(raw.ID, raw.KeyID)

	if identifier == "" {
		identifier = fallbackID
	}
	cfg.KeyIdentifier = identifier
	cfg.KeyID = firstNonEmpty(fallbackID, cfg.KeyID, cfg.KeyIdentifier)
	cfg.PrivateKey = firstNonEmpty(raw.PrivateKey, raw.Secret)
	cfg.Passphrase = firstNonEmpty(raw.Passphrase, cfg.Passphrase)

	if cfg.KeyIdentifier == "" || cfg.PrivateKey == "" {
		return errors.New("key json missing required fields (id/name/privateKey)")
	}
	return nil
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}
