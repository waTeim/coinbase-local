package auth

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"net/url"
	"testing"

	"github.com/golang-jwt/jwt/v5"

	"coinbase-local/go/internal/config"
)

func randomSeed(t *testing.T) string {
	t.Helper()
	seed := make([]byte, 32)
	if _, err := rand.Read(seed); err != nil {
		t.Fatalf("rand.Read: %v", err)
	}
	return base64.StdEncoding.EncodeToString(seed)
}

func TestBuildTokenAudienceDefaultsToHost(t *testing.T) {
	cfg := config.Config{
		PrivateKey:    randomSeed(t),
		KeyIdentifier: "test-key",
		KeyID:         "test-key",
	}

	provider, err := NewTokenProvider(cfg)
	if err != nil {
		t.Fatalf("NewTokenProvider: %v", err)
	}

	endpoint, _ := url.Parse("wss://advanced-trade-ws.coinbase.com/")

	token, err := provider.GetToken(context.Background(), "GET", endpoint)
	if err != nil {
		t.Fatalf("GetToken: %v", err)
	}

	parsed, _, err := new(jwt.Parser).ParseUnverified(token, jwt.MapClaims{})
	if err != nil {
		t.Fatalf("ParseUnverified: %v", err)
	}

	claims, ok := parsed.Claims.(jwt.MapClaims)
	if !ok {
		t.Fatalf("unexpected claims type %T", parsed.Claims)
	}

	audRaw, ok := claims["aud"].([]interface{})
	if !ok {
		t.Fatalf("aud claim unexpected type %T", claims["aud"])
	}
	aud := make([]string, len(audRaw))
	for i, v := range audRaw {
		aud[i], ok = v.(string)
		if !ok {
			t.Fatalf("aud entry %d unexpected type %T", i, v)
		}
	}
	if len(aud) != 3 {
		t.Fatalf("unexpected audience length %d (values: %#v)", len(aud), aud)
	}
	requiredAud := map[string]bool{
		"advanced-trade-ws.coinbase.com":         false,
		"wss://advanced-trade-ws.coinbase.com":   false,
		"https://advanced-trade-ws.coinbase.com": false,
	}
	for _, a := range aud {
		if _, ok := requiredAud[a]; ok {
			requiredAud[a] = true
		}
	}
	for label, seen := range requiredAud {
		if !seen {
			t.Fatalf("missing audience entry %q in %#v", label, aud)
		}
	}

	urisRaw, ok := claims["uris"].([]interface{})
	if !ok {
		t.Fatalf("uris claim unexpected type %T", claims["uris"])
	}
	uris := make([]string, len(urisRaw))
	for i, v := range urisRaw {
		uris[i], ok = v.(string)
		if !ok {
			t.Fatalf("uris entry %d unexpected type %T", i, v)
		}
	}

	expected := map[string]bool{
		"GET advanced-trade-ws.coinbase.com/":         false,
		"GET advanced-trade-ws.coinbase.com":          false,
		"GET wss://advanced-trade-ws.coinbase.com":    false,
		"GET wss://advanced-trade-ws.coinbase.com/":   false,
		"GET https://advanced-trade-ws.coinbase.com":  false,
		"GET https://advanced-trade-ws.coinbase.com/": false,
	}
	for _, uri := range uris {
		if _, exists := expected[uri]; exists {
			expected[uri] = true
		}
	}
	for uri, seen := range expected {
		if !seen {
			t.Fatalf("missing uri %q in %v", uri, uris)
		}
	}
}

func TestBuildTokenURIsForHTTPS(t *testing.T) {
	cfg := config.Config{
		PrivateKey:    randomSeed(t),
		KeyIdentifier: "test-key",
		KeyID:         "test-key",
	}

	provider, err := NewTokenProvider(cfg)
	if err != nil {
		t.Fatalf("NewTokenProvider: %v", err)
	}

	endpoint, _ := url.Parse("https://api.coinbase.com/api/v3/brokerage/product_book?limit=100")

	token, err := provider.GetToken(context.Background(), "GET", endpoint)
	if err != nil {
		t.Fatalf("GetToken: %v", err)
	}

	parsed, _, err := new(jwt.Parser).ParseUnverified(token, jwt.MapClaims{})
	if err != nil {
		t.Fatalf("ParseUnverified: %v", err)
	}

	claims, ok := parsed.Claims.(jwt.MapClaims)
	if !ok {
		t.Fatalf("unexpected claims type %T", parsed.Claims)
	}

	urisRaw, ok := claims["uris"].([]interface{})
	if !ok {
		t.Fatalf("uris claim unexpected type %T", claims["uris"])
	}
	uris := make([]string, len(urisRaw))
	for i, v := range urisRaw {
		uris[i], ok = v.(string)
		if !ok {
			t.Fatalf("uris entry %d unexpected type %T", i, v)
		}
	}

	expected := map[string]bool{
		"GET api.coinbase.com/api/v3/brokerage/product_book":                   false,
		"GET api.coinbase.com/api/v3/brokerage/product_book?limit=100":         false,
		"GET https://api.coinbase.com/api/v3/brokerage/product_book":           false,
		"GET https://api.coinbase.com/api/v3/brokerage/product_book?limit=100": false,
	}
	for _, uri := range uris {
		if _, exists := expected[uri]; exists {
			expected[uri] = true
		}
	}
	for uri, seen := range expected {
		if !seen {
			t.Fatalf("missing uri %q in %v", uri, uris)
		}
	}
}
