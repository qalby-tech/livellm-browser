package main

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

type world struct {
	ip       string
	echo     string // host:port
	a, b     *fakeHTTPProxy
	aAddr    string
	bAddr    string
	socks    *fakeSocks
	socksAdr string
}

func newWorld(t *testing.T) *world {
	w := &world{ip: testIP(t)}
	_, w.echo = listenOn(t, w.ip, echoHandler())
	w.a = &fakeHTTPProxy{name: "proxy-a"}
	_, w.aAddr = listenOn(t, w.ip, w.a)
	w.b = &fakeHTTPProxy{name: "proxy-b", user: "bee", pass: "s3cret-b-pass"}
	_, w.bAddr = listenOn(t, w.ip, w.b)
	w.socks = &fakeSocks{user: "sock", pass: "s3cret-socks-pass", alias: w.echo}
	w.socksAdr = startSocks(t, w.ip, w.socks)
	return w
}

func (w *world) config(version int64) Config {
	return Config{
		Version: version, Relay: true,
		Upstreams: []Upstream{
			{Name: "a", Server: "http://" + w.aAddr},
			{Name: "b", Server: "http://" + w.bAddr},
		},
		Rotation: Rotation{Mode: "off"},
		CheckURL: "http://" + w.echo + "/ip",
	}
}

func bLogin() map[string]string {
	return map[string]string{loginKey("b"): `{"username":"bee","password":"s3cret-b-pass"}`}
}

func fetch(t *testing.T, e *env, url string) (int, map[string]any) {
	t.Helper()
	resp, err := e.viaRelay().Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		io.Copy(io.Discard, resp.Body)
		return resp.StatusCode, nil
	}
	var m map[string]any
	json.NewDecoder(resp.Body).Decode(&m)
	return 200, m
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func TestWaitingModeRefusesUntilConfigured(t *testing.T) {
	w := newWorld(t)
	e := newEnv(t, true)
	if st := e.k.status(); st.Mode != "waiting" {
		t.Fatalf("mode %q, want waiting", st.Mode)
	}
	if code, _ := fetch(t, e, "http://"+w.echo+"/"); code != http.StatusBadGateway {
		t.Fatalf("waiting relay answered %d, want 502", code)
	}
	_, _, code := e.tunnel(w.echo)
	if code != http.StatusBadGateway {
		t.Fatalf("waiting CONNECT answered %d, want 502", code)
	}
	if w.a.forwards.Load()+w.a.connects.Load() != 0 {
		t.Fatal("waiting relay reached an upstream")
	}
	// An explicit empty list is direct.
	resp := e.pushConfig(Config{Version: 5, Relay: true}, nil, nil)
	if resp.StatusCode != 200 {
		t.Fatalf("push: %d", resp.StatusCode)
	}
	if code, m := fetch(t, e, "http://"+w.echo+"/"); code != 200 || m["via"] != "" {
		t.Fatalf("direct fetch: %d %v", code, m)
	}
}

func TestRelayForwardsAndTunnelsThroughHTTPUpstreams(t *testing.T) {
	w := newWorld(t)
	e := newEnv(t, true)
	if resp := e.pushConfig(w.config(10), bLogin(), nil); resp.StatusCode != 200 {
		t.Fatalf("push: %d", resp.StatusCode)
	}
	// absolute-URI forwarding through proxy-a
	code, m := fetch(t, e, "http://"+w.echo+"/page")
	if code != 200 || !strings.Contains(m["via"].(string), "proxy-a") {
		t.Fatalf("forward: %d %v", code, m)
	}
	// CONNECT tunnel through proxy-a
	c, br, code := e.tunnel(w.echo)
	if code != 200 {
		t.Fatalf("CONNECT: %d", code)
	}
	defer c.Close()
	if m := getThrough(t, c, br, w.echo); !strings.Contains(m["via"].(string), "proxy-a") {
		t.Fatalf("tunnel did not go through proxy-a: %v", m)
	}
	waitFor(t, "probe", func() bool { return e.k.status().ExitIP == "203.0.113.1" })
	if st := e.k.status(); st.Country != "XA" || st.Upstream.Name != "a" || st.Mode != "proxy" {
		t.Fatalf("status %+v", st)
	}

	// rotate to b (with a login) — the open tunnel is cut, the exit flips
	gen := e.k.status().Generation
	resp := e.call("POST", "/v1/rotate", []byte(`{}`), nil)
	st := readJSON(t, resp)
	if resp.StatusCode != 200 || st["exitIp"] != "203.0.113.2" || st["country"] != "XB" {
		t.Fatalf("rotate: %d %v", resp.StatusCode, st)
	}
	if int64(st["generation"].(float64)) != gen+1 {
		t.Fatalf("generation %v, want %d", st["generation"], gen+1)
	}
	c.SetReadDeadline(time.Now().Add(3 * time.Second))
	if _, err := c.Write([]byte("GET /x HTTP/1.1\r\nHost: x\r\n\r\n")); err == nil {
		if _, err := br.ReadByte(); err == nil {
			t.Fatal("the old tunnel still answers after a rotation")
		}
	}
	code, m = fetch(t, e, "http://"+w.echo+"/page")
	if code != 200 || !strings.Contains(m["via"].(string), "proxy-b") {
		t.Fatalf("after rotate: %d %v", code, m)
	}
	c2, br2, code := e.tunnel(w.echo)
	if code != 200 {
		t.Fatalf("CONNECT via b: %d", code)
	}
	defer c2.Close()
	if m := getThrough(t, c2, br2, w.echo); !strings.Contains(m["via"].(string), "proxy-b") {
		t.Fatalf("tunnel via b: %v", m)
	}
}

func TestLoginKeptAcrossPushWithoutValues(t *testing.T) {
	w := newWorld(t)
	e := newEnv(t, true)
	cfg := w.config(10)
	cfg.Upstreams = cfg.Upstreams[1:] // b only
	e.pushConfig(cfg, bLogin(), nil)
	// change only the rotation mode: no set, no remove — the login stays
	cfg.Version = 11
	cfg.Rotation = Rotation{Mode: "session"}
	if resp := e.pushConfig(cfg, nil, nil); resp.StatusCode != 200 {
		t.Fatalf("push 11: %d", resp.StatusCode)
	}
	if code, m := fetch(t, e, "http://"+w.echo+"/"); code != 200 || !strings.Contains(m["via"].(string), "proxy-b") {
		t.Fatalf("b lost its login: %d %v", code, m)
	}
	// removing it: proxy-b refuses (407 passes through to the page)
	cfg.Version = 12
	e.pushConfig(cfg, nil, []string{loginKey("b")})
	if code, _ := fetch(t, e, "http://"+w.echo+"/"); code != http.StatusProxyAuthRequired {
		t.Fatalf("after removing the login: %d, want 407", code)
	}
	if _, _, code := e.tunnel(w.echo); code != http.StatusBadGateway {
		t.Fatalf("CONNECT without login: %d, want 502", code)
	}
	waitFor(t, "lastError", func() bool { return e.k.status().LastError == "upstream_login_refused" })
}

func TestSocks5UpstreamWithLoginSendsNamesUnresolved(t *testing.T) {
	w := newWorld(t)
	e := newEnv(t, true)
	cfg := Config{Version: 3, Relay: true, Upstreams: []Upstream{{Name: "s", Server: "socks5://" + w.socksAdr}}}
	set := map[string]string{loginKey("s"): `{"username":"sock","password":"s3cret-socks-pass"}`}
	if resp := e.pushConfig(cfg, set, nil); resp.StatusCode != 200 {
		t.Fatalf("push: %d", resp.StatusCode)
	}
	// Only the socks server can resolve this name.
	_, port, _ := net.SplitHostPort(w.echo)
	c, br, code := e.tunnel("echo-only.test:" + port)
	if code != 200 {
		t.Fatalf("CONNECT via socks: %d", code)
	}
	defer c.Close()
	if m := getThrough(t, c, br, "echo-only.test"); m["ip"] != "203.0.113.3" {
		t.Fatalf("socks tunnel: %v", m)
	}
	code, m := fetch(t, e, "http://echo-only.test:"+port+"/")
	if code != 200 || m["ip"] != "203.0.113.3" {
		t.Fatalf("socks forward: %d %v", code, m)
	}
	asked := w.socks.asked()
	if len(asked) == 0 || asked[0] != "echo-only.test" {
		t.Fatalf("socks was asked %v, want the unresolved name", asked)
	}
}

func TestRelayRefusesLoopbackTargetsInEveryMode(t *testing.T) {
	w := newWorld(t)
	e := newEnv(t, true)
	// a loopback service (stands in for the launcher / keeper)
	l, _ := net.Listen("tcp", "127.0.0.1:0")
	defer l.Close()
	loop := l.Addr().String()
	_, port, _ := net.SplitHostPort(loop)

	check := func(mode string) {
		for _, target := range []string{loop, "localhost:" + port, "x.localhost:" + port, "[::1]:" + port, "169.254.169.254:80", "0.0.0.0:" + port} {
			if c, _, code := e.tunnel(target); code != http.StatusForbidden {
				t.Errorf("%s: CONNECT %s answered %d, want 403", mode, target, code)
			} else {
				c.Close()
			}
		}
		if code, _ := fetch(t, e, "http://"+loop+"/v1/status"); code != http.StatusForbidden {
			t.Errorf("%s: GET loopback answered %d, want 403", mode, code)
		}
		if code, _ := fetch(t, e, "http://localhost:"+port+"/"); code != http.StatusForbidden {
			t.Errorf("%s: GET localhost answered %d, want 403", mode, code)
		}
	}
	e.pushConfig(Config{Version: 1, Relay: true}, nil, nil) // direct
	check("direct")
	e.pushConfig(w.config(2), bLogin(), nil) // proxy
	check("proxy")
	if w.a.connects.Load() != 0 {
		t.Fatal("a loopback target reached the upstream")
	}
}

func TestDirectModeChecksResolvedAddresses(t *testing.T) {
	e := newEnv(t, true)
	e.pushConfig(Config{Version: 1, Relay: true}, nil, nil)
	l, _ := net.Listen("tcp", "127.0.0.1:0")
	defer l.Close()
	_, port, _ := net.SplitHostPort(l.Addr().String())
	e.k.g.resolve = func(_ context.Context, host string) ([]net.IPAddr, error) {
		return []net.IPAddr{{IP: net.ParseIP("127.0.0.1")}}, nil
	}
	if _, _, code := e.tunnel("rebind.example:" + port); code != http.StatusForbidden {
		t.Fatalf("a name resolving to loopback answered %d, want 403", code)
	}
}

func TestLoopbackUpstreamIsRefused(t *testing.T) {
	w := newWorld(t)
	e := newEnv(t, true)
	l, _ := net.Listen("tcp", "127.0.0.1:0")
	defer l.Close()
	cfg := Config{Version: 1, Relay: true, Upstreams: []Upstream{{Name: "a", Server: "http://" + l.Addr().String()}}}
	e.pushConfig(cfg, nil, nil)
	if _, _, code := e.tunnel(w.echo); code != http.StatusBadGateway {
		t.Fatalf("loopback upstream answered %d, want 502", code)
	}
}

func TestKillSwitchNeverFallsBackToDirect(t *testing.T) {
	w := newWorld(t)
	e := newEnv(t, true)
	dead, _ := net.Listen("tcp", w.ip+":0")
	addr := dead.Addr().String()
	dead.Close()
	cfg := Config{Version: 1, Relay: true, Upstreams: []Upstream{{Name: "a", Server: "http://" + addr}}, CheckURL: "http://" + w.echo + "/"}
	e.pushConfig(cfg, nil, nil)
	if code, _ := fetch(t, e, "http://"+w.echo+"/"); code != http.StatusBadGateway {
		t.Fatalf("dead upstream answered %d, want 502", code)
	}
	if _, _, code := e.tunnel(w.echo); code != http.StatusBadGateway {
		t.Fatalf("dead upstream CONNECT answered %d, want 502", code)
	}
	waitFor(t, "lastError", func() bool { return e.k.status().LastError == "upstream_unreachable" })
}

func TestFailoverToNextUpstreamAfterFailedProbe(t *testing.T) {
	w := newWorld(t)
	e := newEnv(t, true)
	dead, _ := net.Listen("tcp", w.ip+":0")
	addr := dead.Addr().String()
	dead.Close()
	cfg := Config{Version: 1, Relay: true, Upstreams: []Upstream{
		{Name: "dead", Server: "http://" + addr},
		{Name: "a", Server: "http://" + w.aAddr},
	}, CheckURL: "http://" + w.echo + "/"}
	e.pushConfig(cfg, nil, nil)
	waitFor(t, "failover", func() bool {
		st := e.k.status()
		return st.Upstream != nil && st.Upstream.Name == "a" && st.ExitIP == "203.0.113.1"
	})
}
