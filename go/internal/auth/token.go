package auth

import (
	"context"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"fmt"
	"math"
	"math/big"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/golang-jwt/jwt/v5"

	"coinbase-local/go/internal/config"
)

const coinbaseIssuer = "coinbase-cloud"

type tokenCacheKey struct {
	method string
	scheme string
	host   string
	path   string
	query  string
}

type cachedToken struct {
	token   string
	expires time.Time
}

var nonceMax = big.NewInt(math.MaxInt64)

type TokenProvider struct {
	keyID         string
	subject       string
	audience      string
	privateKey    interface{}
	signingMethod jwt.SigningMethod

	mu    sync.Mutex
	cache map[tokenCacheKey]cachedToken
}

func NewTokenProvider(cfg config.Config) (*TokenProvider, error) {
	pk, err := parsePrivateKey(cfg.PrivateKey)
	if err != nil {
		return nil, err
	}

	method, err := signingMethod(pk)
	if err != nil {
		return nil, err
	}

	subject := strings.TrimSpace(cfg.KeyIdentifier)
	if subject == "" {
		subject = strings.TrimSpace(cfg.KeyID)
	}

	keyID := strings.TrimSpace(cfg.KeyIdentifier)
	if keyID == "" {
		keyID = strings.TrimSpace(cfg.KeyID)
	}

	return &TokenProvider{
		keyID:         keyID,
		subject:       subject,
		audience:      strings.TrimSpace(cfg.TokenAudience),
		privateKey:    pk,
		signingMethod: method,
		cache:         make(map[tokenCacheKey]cachedToken),
	}, nil
}

func (p *TokenProvider) GetToken(ctx context.Context, method string, endpoint *url.URL) (string, error) {
	_ = ctx // reserved for future cancellation support
	if endpoint == nil {
		return "", errors.New("endpoint is nil")
	}

	method = strings.ToUpper(strings.TrimSpace(method))
	if method == "" {
		return "", errors.New("method is required")
	}

	scheme := strings.ToLower(strings.TrimSpace(endpoint.Scheme))
	host := strings.TrimSpace(endpoint.Host)
	if host == "" {
		return "", errors.New("endpoint host is required")
	}

	path := endpoint.EscapedPath()
	if path == "" {
		path = "/"
	}

	query := endpoint.RawQuery

	key := tokenCacheKey{method: method, scheme: scheme, host: strings.ToLower(host), path: path, query: query}
	now := time.Now()

	p.mu.Lock()
	if entry, ok := p.cache[key]; ok {
		if now.Add(20 * time.Second).Before(entry.expires) {
			token := entry.token
			p.mu.Unlock()
			return token, nil
		}
		delete(p.cache, key)
	}
	p.mu.Unlock()

	signed, expiry, err := p.buildToken(key, now)
	if err != nil {
		return "", err
	}

	p.mu.Lock()
	p.cache[key] = cachedToken{token: signed, expires: expiry}
	p.mu.Unlock()

	return signed, nil
}

func (p *TokenProvider) buildToken(key tokenCacheKey, now time.Time) (string, time.Time, error) {
	expires := now.Add(1 * time.Minute)

	audience := jwt.ClaimStrings{}
	audience = appendIfMissing(audience, p.audience)
	audience = appendIfMissing(audience, key.host)
	if key.scheme != "" {
		schemeHost := fmt.Sprintf("%s://%s", key.scheme, key.host)
		audience = appendIfMissing(audience, schemeHost)
		scheme := strings.ToLower(strings.TrimSpace(key.scheme))
		if scheme == "ws" || scheme == "wss" {
			httpsHost := fmt.Sprintf("https://%s", key.host)
			audience = appendIfMissing(audience, httpsHost)
		}
	}

	claims := struct {
		jwt.RegisteredClaims
		URIs []string `json:"uris"`
	}{
		RegisteredClaims: jwt.RegisteredClaims{
			Subject:   p.subject,
			Issuer:    coinbaseIssuer,
			Audience:  audience,
			NotBefore: jwt.NewNumericDate(now),
			IssuedAt:  jwt.NewNumericDate(now),
			ExpiresAt: jwt.NewNumericDate(expires),
		},
		URIs: buildURIs(key),
	}

	token := jwt.NewWithClaims(p.signingMethod, claims)
	if p.keyID != "" {
		token.Header["kid"] = p.keyID
	}
	nonce, err := rand.Int(rand.Reader, nonceMax)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("generate nonce: %w", err)
	}
	token.Header["nonce"] = nonce.String()
	signed, err := token.SignedString(p.privateKey)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("sign jwt: %w", err)
	}

	return signed, expires, nil
}

func appendIfMissing(list jwt.ClaimStrings, value string) jwt.ClaimStrings {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return list
	}
	for _, existing := range list {
		if existing == trimmed {
			return list
		}
	}
	return append(list, trimmed)
}

func buildURIs(key tokenCacheKey) []string {
	pathComponent := key.path
	pathWithQuery := ""
	if key.query != "" {
		pathWithQuery = fmt.Sprintf("%s?%s", pathComponent, key.query)
	}

	uris := make([]string, 0, 6)
	uris = appendUniqueURI(uris, fmt.Sprintf("%s %s%s", key.method, key.host, pathComponent))
	if pathWithQuery != "" {
		uris = appendUniqueURI(uris, fmt.Sprintf("%s %s%s", key.method, key.host, pathWithQuery))
	}
	if pathComponent == "/" {
		uris = appendUniqueURI(uris, fmt.Sprintf("%s %s", key.method, key.host))
	}

	scheme := strings.ToLower(strings.TrimSpace(key.scheme))
	if scheme == "" {
		return uris
	}
	schemeHost := fmt.Sprintf("%s://%s", key.scheme, key.host)

	switch scheme {
	case "ws", "wss":
		uris = appendUniqueURI(uris, fmt.Sprintf("%s %s%s", key.method, schemeHost, pathComponent))
		if pathWithQuery != "" {
			uris = appendUniqueURI(uris, fmt.Sprintf("%s %s%s", key.method, schemeHost, pathWithQuery))
		}
		uris = appendUniqueURI(uris, fmt.Sprintf("%s %s", key.method, schemeHost))
		httpsHost := fmt.Sprintf("https://%s", key.host)
		uris = appendUniqueURI(uris, fmt.Sprintf("%s %s%s", key.method, httpsHost, pathComponent))
		if pathWithQuery != "" {
			uris = appendUniqueURI(uris, fmt.Sprintf("%s %s%s", key.method, httpsHost, pathWithQuery))
		}
		uris = appendUniqueURI(uris, fmt.Sprintf("%s %s", key.method, httpsHost))
		if pathComponent == "/" {
			uris = appendUniqueURI(uris, fmt.Sprintf("%s %s", key.method, key.host))
			uris = appendUniqueURI(uris, fmt.Sprintf("%s %s", key.method, schemeHost))
			uris = appendUniqueURI(uris, fmt.Sprintf("%s %s", key.method, httpsHost))
		}
	case "http", "https":
		uris = appendUniqueURI(uris, fmt.Sprintf("%s %s%s", key.method, schemeHost, pathComponent))
		if pathWithQuery != "" {
			uris = appendUniqueURI(uris, fmt.Sprintf("%s %s%s", key.method, schemeHost, pathWithQuery))
		}
	}

	return uris
}

func appendUniqueURI(list []string, value string) []string {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return list
	}
	for _, existing := range list {
		if existing == trimmed {
			return list
		}
	}
	return append(list, trimmed)
}

func signingMethod(privateKey interface{}) (jwt.SigningMethod, error) {
	switch privateKey.(type) {
	case ed25519.PrivateKey:
		return jwt.SigningMethodEdDSA, nil
	case *ecdsa.PrivateKey:
		return jwt.SigningMethodES256, nil
	default:
		return nil, errors.New("unsupported private key type")
	}
}

func parsePrivateKey(raw string) (interface{}, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, errors.New("empty private key")
	}

	if strings.HasPrefix(raw, "-----BEGIN") {
		block, _ := pem.Decode([]byte(raw))
		if block == nil {
			return nil, errors.New("failed to decode PEM private key")
		}
		key, err := x509.ParseECPrivateKey(block.Bytes)
		if err == nil {
			return key, nil
		}
		// PEM may contain PKCS8 encoding.
		parsed, err2 := x509.ParsePKCS8PrivateKey(block.Bytes)
		if err2 != nil {
			return nil, fmt.Errorf("parse pem private key: %v %v", err, err2)
		}
		switch k := parsed.(type) {
		case *ecdsa.PrivateKey:
			return k, nil
		case ed25519.PrivateKey:
			return k, nil
		default:
			return nil, errors.New("unsupported private key in PKCS8 container")
		}
	}

	decoded, err := base64.StdEncoding.DecodeString(raw)
	if err != nil {
		return nil, fmt.Errorf("decode base64 private key: %w", err)
	}
	if len(decoded) == ed25519.SeedSize {
		return ed25519.NewKeyFromSeed(decoded), nil
	}
	if len(decoded) == ed25519.PrivateKeySize {
		return ed25519.PrivateKey(decoded), nil
	}
	return nil, errors.New("unrecognized private key format")
}
