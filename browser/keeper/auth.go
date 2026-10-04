package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Request signatures:
//
//	Authorization: LLK1 ts=<unix seconds>,n=<32 hex>,sig=<hex>
//	sig = hex(HMAC-SHA256(K_auth, METHOD "\n" target "\n" ts "\n" n "\n" bodyHash))
//
// target is the request-target exactly as sent: the path, plus "?" and the
// raw query when there is one. bodyHash is hex(sha256(body)), or the literal
// UNSIGNED for POST /v1/profile/import only (a streamed body).
const (
	authScheme  = "LLK1"
	maxSkew     = 60 * time.Second
	nonceWindow = 120 * time.Second
	unsignedTag = "UNSIGNED"
)

type nonceCache struct {
	mu   sync.Mutex
	seen map[string]time.Time
}

func newNonceCache() *nonceCache { return &nonceCache{seen: map[string]time.Time{}} }

// add records a nonce; false when it was seen inside the window.
func (c *nonceCache) add(n string, now time.Time) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	for k, t := range c.seen {
		if now.Sub(t) > nonceWindow {
			delete(c.seen, k)
		}
	}
	if _, ok := c.seen[n]; ok {
		return false
	}
	c.seen[n] = now
	return true
}

type authHeader struct {
	ts    int64
	nonce string
	sig   []byte
}

func parseAuth(h string) (authHeader, bool) {
	var a authHeader
	rest, ok := strings.CutPrefix(h, authScheme+" ")
	if !ok {
		return a, false
	}
	for _, part := range strings.Split(rest, ",") {
		k, v, ok := strings.Cut(strings.TrimSpace(part), "=")
		if !ok {
			return a, false
		}
		switch k {
		case "ts":
			n, err := strconv.ParseInt(v, 10, 64)
			if err != nil {
				return a, false
			}
			a.ts = n
		case "n":
			if len(v) != 32 {
				return a, false
			}
			if _, err := hex.DecodeString(v); err != nil {
				return a, false
			}
			a.nonce = v
		case "sig":
			b, err := hex.DecodeString(v)
			if err != nil {
				return a, false
			}
			a.sig = b
		}
	}
	return a, a.ts != 0 && a.nonce != "" && len(a.sig) == sha256.Size
}

func signature(key []byte, method, target string, ts int64, nonce, bodyHash string) []byte {
	m := hmac.New(sha256.New, key)
	fmt.Fprintf(m, "%s\n%s\n%d\n%s\n%s", method, target, ts, nonce, bodyHash)
	return m.Sum(nil)
}

// signRequest is the client side (tests, and the reference for tenant-api).
func signRequest(r *http.Request, key []byte, body []byte, unsigned bool, now time.Time, nonce string) {
	bh := unsignedTag
	if !unsigned {
		sum := sha256.Sum256(body)
		bh = hex.EncodeToString(sum[:])
	}
	ts := now.Unix()
	sig := signature(key, r.Method, r.URL.RequestURI(), ts, nonce, bh)
	r.Header.Set("Authorization", fmt.Sprintf("%s ts=%d,n=%s,sig=%s", authScheme, ts, nonce, hex.EncodeToString(sig)))
}

// verify checks the signature over the given body hash.
func verify(r *http.Request, key []byte, bodyHash string, nonces *nonceCache, now time.Time) bool {
	a, ok := parseAuth(r.Header.Get("Authorization"))
	if !ok {
		return false
	}
	t := time.Unix(a.ts, 0)
	if now.Sub(t) > maxSkew || t.Sub(now) > maxSkew {
		return false
	}
	want := signature(key, r.Method, r.RequestURI, a.ts, a.nonce, bodyHash)
	if !hmac.Equal(want, a.sig) {
		return false
	}
	// Only a valid signature spends the nonce.
	return nonces.add(a.nonce, now)
}

func bodyHashOf(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}
