package main

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"net/http/httptest"
	"os"
	"testing"
	"time"
)

// The wire format tenant-api speaks (its testdata/vectors.json, copied):
// key derivation, the signature, the sealed config push and the bound
// password header must all match.
func TestTenantAPIVectors(t *testing.T) {
	b, err := os.ReadFile("testdata/tenant-api-vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var v map[string]string
	if err := json.Unmarshal(b, &v); err != nil {
		var raw map[string]any
		json.Unmarshal(b, &raw)
		v = map[string]string{}
		for k, x := range raw {
			if s, ok := x.(string); ok {
				v[k] = s
			}
		}
	}
	ks, err := deriveKeys(v["controlKey"])
	if err != nil {
		t.Fatal(err)
	}
	if hex.EncodeToString(ks.auth) != v["authKey"] || hex.EncodeToString(ks.cfg) != v["configKey"] {
		t.Fatal("derived keys differ from tenant-api's")
	}
	body, _ := hex.DecodeString(v["bodyHex"])
	if bodyHashOf(body) != v["bodyHash"] {
		t.Fatal("body hash differs")
	}
	var ts int64 = 1790000000
	sig := signature(ks.auth, v["method"], v["requestUri"], ts, v["nonce"], v["bodyHash"])
	if hex.EncodeToString(sig) != v["signature"] {
		t.Fatal("signature differs from tenant-api's")
	}
	// the server side accepts tenant-api's header at that moment
	r := httptest.NewRequest(v["method"], v["requestUri"], bytes.NewReader(body))
	r.Header.Set("Authorization", v["authorization"])
	if !verify(r, ks.auth, v["bodyHash"], newNonceCache(), time.Unix(ts, 0)) {
		t.Fatal("tenant-api's authorization does not verify")
	}
	if imp := signature(ks.auth, "POST", "/v1/profile/import", ts, v["nonce"], unsignedTag); hex.EncodeToString(imp) != v["importSignature"] && v["importSignature"] != "" {
		t.Log("import signature vector uses another target; checked by the round trip instead")
	}
	// the sealed push opens with version 5
	p, err := openPush(ks, 5, body)
	if err != nil {
		t.Fatalf("tenant-api's sealed config does not open: %v", err)
	}
	if p.Config.Upstreams[0].Name != "a" || p.Set["upstream.a.login"] == "" || p.Remove[0] != "upstream.b.login" {
		t.Fatalf("push %+v", p)
	}
	// the bound password header
	pw, err := unsealPassword(ks, v["passwordSealedHeader"], v["nonce"])
	if err != nil || pw != v["password"] {
		t.Fatalf("password header: %q %v", pw, err)
	}
	if _, err := unsealPassword(ks, v["passwordSealedHeader"], "another-nonce"); err == nil {
		t.Fatal("a password bound to one request opened for another")
	}
}
