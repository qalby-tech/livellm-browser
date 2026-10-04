package main

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const testControlKey = "6b6565706572207465737420636f6e74726f6c206b6579206e6f742072656164"

// testIP is a non-loopback address of this host: fake upstreams listen on
// it, because the relay refuses loopback upstreams and targets.
func testIP(t *testing.T) string {
	t.Helper()
	addrs, _ := net.InterfaceAddrs()
	for _, a := range addrs {
		if n, ok := a.(*net.IPNet); ok {
			if v4 := n.IP.To4(); v4 != nil && !v4.IsLoopback() && !v4.IsLinkLocalUnicast() {
				return v4.String()
			}
		}
	}
	t.Skip("no non-loopback IPv4 address on this host")
	return ""
}

func listenOn(t *testing.T, ip string, h http.Handler) (*httptest.Server, string) {
	t.Helper()
	l, err := net.Listen("tcp", net.JoinHostPort(ip, "0"))
	if err != nil {
		t.Fatal(err)
	}
	s := httptest.NewUnstartedServer(h)
	s.Listener.Close()
	s.Listener = l
	s.Start()
	t.Cleanup(s.Close)
	return s, l.Addr().String()
}

// echo answers {"ip","via","country"}: the exit IP depends on the Via header,
// so a probe through proxy-a and proxy-b sees different exits.
func echoHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		via := r.Header.Get("Via")
		ip, cc := "203.0.113.9", "XZ"
		switch {
		case strings.Contains(via, "proxy-a"):
			ip, cc = "203.0.113.1", "XA"
		case strings.Contains(via, "proxy-b"):
			ip, cc = "203.0.113.2", "XB"
		case r.Header.Get("X-Socks") != "":
			ip, cc = "203.0.113.3", "XS"
		}
		json.NewEncoder(w).Encode(map[string]string{"ip": ip, "via": via, "country": cc, "path": r.URL.Path})
	})
}

// fakeHTTPProxy is a forwarding proxy that tags requests with Via and can
// require a basic login.
type fakeHTTPProxy struct {
	name     string
	user     string
	pass     string
	connects atomic.Int64
	forwards atomic.Int64
	mu       sync.Mutex
	conns    []net.Conn
}

func (p *fakeHTTPProxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if p.user != "" {
		want := "Basic " + base64.StdEncoding.EncodeToString([]byte(p.user+":"+p.pass))
		if r.Header.Get("Proxy-Authorization") != want {
			w.Header().Set("Proxy-Authenticate", `Basic realm="x"`)
			w.WriteHeader(http.StatusProxyAuthRequired)
			return
		}
	}
	if r.Method == http.MethodConnect {
		p.connects.Add(1)
		up, err := net.Dial("tcp", r.Host)
		if err != nil {
			w.WriteHeader(502)
			return
		}
		c, brw, _ := w.(http.Hijacker).Hijack()
		p.mu.Lock()
		p.conns = append(p.conns, c, up)
		p.mu.Unlock()
		io.WriteString(c, "HTTP/1.1 200 OK\r\n\r\n")
		// Tag tunnelled plain-HTTP requests too, so tests can tell the path.
		go func() {
			br := bufio.NewReader(io.MultiReader(brw.Reader, c))
			for {
				req, err := http.ReadRequest(br)
				if err != nil {
					up.Close()
					return
				}
				req.Header.Set("Via", "1.1 "+p.name)
				req.Write(up)
			}
		}()
		go func() { io.Copy(c, up); c.Close() }()
		return
	}
	p.forwards.Add(1)
	out := r.Clone(r.Context())
	out.RequestURI = ""
	out.Header.Del("Proxy-Authorization")
	out.Header.Set("Via", "1.1 "+p.name)
	resp, err := http.DefaultTransport.RoundTrip(out)
	if err != nil {
		w.WriteHeader(502)
		return
	}
	defer resp.Body.Close()
	for k, vs := range resp.Header {
		for _, v := range vs {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body)
}

// fakeSocks is a SOCKS5 server with a username/password login. It records
// the names it was asked for and resolves "echo-only.test" itself.
type fakeSocks struct {
	user, pass string
	alias      string // the address echo-only.test resolves to
	mu         sync.Mutex
	names      []string
	l          net.Listener
}

func startSocks(t *testing.T, ip string, s *fakeSocks) string {
	l, err := net.Listen("tcp", net.JoinHostPort(ip, "0"))
	if err != nil {
		t.Fatal(err)
	}
	s.l = l
	t.Cleanup(func() { l.Close() })
	go func() {
		for {
			c, err := l.Accept()
			if err != nil {
				return
			}
			go s.serve(c)
		}
	}()
	return l.Addr().String()
}

func (s *fakeSocks) serve(c net.Conn) {
	defer func() {
		if r := recover(); r != nil {
			c.Close()
		}
	}()
	br := bufio.NewReader(c)
	hdr := make([]byte, 2)
	io.ReadFull(br, hdr)
	methods := make([]byte, hdr[1])
	io.ReadFull(br, methods)
	c.Write([]byte{5, 2})
	// RFC 1929
	v := make([]byte, 2)
	io.ReadFull(br, v)
	u := make([]byte, v[1])
	io.ReadFull(br, u)
	pl := make([]byte, 1)
	io.ReadFull(br, pl)
	p := make([]byte, pl[0])
	io.ReadFull(br, p)
	if string(u) != s.user || string(p) != s.pass {
		c.Write([]byte{1, 1})
		c.Close()
		return
	}
	c.Write([]byte{1, 0})
	req := make([]byte, 4)
	io.ReadFull(br, req)
	var host string
	switch req[3] {
	case 1:
		b := make([]byte, 4)
		io.ReadFull(br, b)
		host = net.IP(b).String()
	case 3:
		l := make([]byte, 1)
		io.ReadFull(br, l)
		b := make([]byte, l[0])
		io.ReadFull(br, b)
		host = string(b)
		s.mu.Lock()
		s.names = append(s.names, host)
		s.mu.Unlock()
	default:
		c.Close()
		return
	}
	pb := make([]byte, 2)
	io.ReadFull(br, pb)
	port := binary.BigEndian.Uint16(pb)
	addr := net.JoinHostPort(host, fmt.Sprint(port))
	if host == "echo-only.test" {
		addr = s.alias
	}
	up, err := net.Dial("tcp", addr)
	if err != nil {
		c.Write([]byte{5, 5, 0, 1, 0, 0, 0, 0, 0, 0})
		c.Close()
		return
	}
	c.Write([]byte{5, 0, 0, 1, 0, 0, 0, 0, 0, 0})
	go func() {
		// Mark requests through the socks path.
		r := bufio.NewReader(io.MultiReader(br, c))
		for {
			req, err := http.ReadRequest(r)
			if err != nil {
				up.Close()
				return
			}
			req.Header.Set("X-Socks", "1")
			req.Write(up)
		}
	}()
	io.Copy(c, up)
	c.Close()
}

func (s *fakeSocks) asked() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.names...)
}

// fakeLauncher records pause/resume and serves /version.
type fakeLauncher struct {
	pauses, resumes atomic.Int64
	cookies         atomic.Int64
	major           int
	tz, locale      string
	failPause       atomic.Bool
	srv             *httptest.Server
}

func startLauncher(t *testing.T) *fakeLauncher {
	fl := &fakeLauncher{major: 154}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /version", func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{"chrome": fmt.Sprintf("%d.0.8037.57", fl.major), "chromeMajor": fl.major, "image": "2.3.0", "timezone": fl.tz, "locale": fl.locale})
	})
	mux.HandleFunc("POST /browsers/default/pause", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Livellm-Keeper") != "1" {
			w.WriteHeader(403)
			return
		}
		if fl.failPause.Load() {
			w.WriteHeader(503)
			return
		}
		fl.pauses.Add(1)
		w.Write([]byte(`{"status":"paused"}`))
	})
	mux.HandleFunc("POST /browsers/default/resume", func(w http.ResponseWriter, r *http.Request) {
		fl.resumes.Add(1)
		w.Write([]byte(`{"status":"ok"}`))
	})
	mux.HandleFunc("POST /browsers/default/cookies", func(w http.ResponseWriter, r *http.Request) {
		var arr []any
		json.NewDecoder(r.Body).Decode(&arr)
		fl.cookies.Add(int64(len(arr)))
		w.Write([]byte(`{"status":"success"}`))
	})
	fl.srv = httptest.NewServer(mux)
	t.Cleanup(fl.srv.Close)
	return fl
}

type env struct {
	t         *testing.T
	secretDir string
	runDir    string
	profiles  string
	k         *Keeper
	p         *profileStore
	api       *httptest.Server
	relay     *httptest.Server
	launcher  *fakeLauncher
	logs      *bytes.Buffer
}

// newEnv builds a keeper over temp dirs; relay=true listens like
// KEEPER_RELAY=required.
func newEnv(t *testing.T, relay bool) *env {
	t.Helper()
	e := &env{t: t, secretDir: t.TempDir(), runDir: t.TempDir(), profiles: t.TempDir(), logs: &bytes.Buffer{}}
	log.SetOutput(io.MultiWriter(e.logs, os.Stderr))
	t.Cleanup(func() { log.SetOutput(os.Stderr) })
	e.launcher = startLauncher(t)
	e.writeSecret("control-key", testControlKey)
	e.start(relay)
	return e
}

func (e *env) start(relay bool) {
	e.k = newKeeper(e.secretDir, filepath.Join(e.runDir, "state.json"), relay, e.launcher.srv.URL)
	e.k.g.ownIPs = func() []net.IP { return nil }
	e.k.rotateProbe = 2 * time.Second
	e.k.changeProbe = 2 * time.Second
	e.k.probePoll = 50 * time.Millisecond
	e.k.boot()
	e.p = newProfileStore(e.profiles, e.k, 10, 64)
	e.p.freeBytesFn = func(string) int64 { return 10 << 30 }
	srv := &server_{k: e.k, p: e.p, nonces: newNonceCache(), now: time.Now}
	e.api = httptest.NewServer(srv.routes())
	e.t.Cleanup(e.api.Close)
	e.relay = httptest.NewServer(e.k.rl)
	e.t.Cleanup(e.relay.Close)
}

func (e *env) writeSecret(name, value string) {
	if err := os.WriteFile(filepath.Join(e.secretDir, name), []byte(value), 0o600); err != nil {
		e.t.Fatal(err)
	}
}

func (e *env) writeConfig(c Config) {
	b, _ := json.Marshal(c)
	e.writeSecret("config.json", string(b))
}

func randNonce() string {
	b := make([]byte, 16)
	rand.Read(b)
	return hex.EncodeToString(b)
}

// call sends a signed request to the control API.
func (e *env) call(method, path string, body []byte, hdr map[string]string) *http.Response {
	e.t.Helper()
	req, _ := http.NewRequest(method, e.api.URL+path, bytes.NewReader(body))
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	ks, _ := deriveKeys(testControlKey)
	signRequest(req, ks.auth, body, method == "POST" && strings.HasPrefix(path, "/v1/profile/import"), time.Now(), randNonce())
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatal(err)
	}
	return resp
}

func readJSON(t *testing.T, resp *http.Response) map[string]any {
	t.Helper()
	defer resp.Body.Close()
	var m map[string]any
	b, _ := io.ReadAll(resp.Body)
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("not JSON (%d): %s", resp.StatusCode, b)
	}
	return m
}

// pushConfig seals and PUTs a config.
func (e *env) pushConfig(c Config, set map[string]string, remove []string) *http.Response {
	ks, _ := deriveKeys(testControlKey)
	pb, _ := json.Marshal(pushBody{Version: c.Version, Config: &c, Set: set, Remove: remove})
	return e.call("PUT", fmt.Sprintf("/v1/config?version=%d", c.Version), seal(ks.cfg, pb, configAAD(c.Version)), nil)
}

// viaRelay is an HTTP client whose proxy is the relay.
func (e *env) viaRelay() *http.Client {
	pu, _ := http.NewRequest("GET", e.relay.URL, nil)
	return &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{Proxy: http.ProxyURL(pu.URL), DisableKeepAlives: true}}
}

// tunnel opens a CONNECT tunnel through the relay; returns the conn and the
// relay's status code.
func (e *env) tunnel(target string) (net.Conn, *bufio.Reader, int) {
	e.t.Helper()
	c, err := net.Dial("tcp", strings.TrimPrefix(e.relay.URL, "http://"))
	if err != nil {
		e.t.Fatal(err)
	}
	fmt.Fprintf(c, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n", target, target)
	br := bufio.NewReader(c)
	resp, err := http.ReadResponse(br, &http.Request{Method: "CONNECT"})
	if err != nil {
		e.t.Fatal(err)
	}
	return c, br, resp.StatusCode
}

func getThrough(t *testing.T, c net.Conn, br *bufio.Reader, host string) map[string]any {
	t.Helper()
	fmt.Fprintf(c, "GET /x HTTP/1.1\r\nHost: %s\r\n\r\n", host)
	c.SetReadDeadline(time.Now().Add(5 * time.Second))
	resp, err := http.ReadResponse(br, nil)
	if err != nil {
		t.Fatal(err)
	}
	return readJSON(t, resp)
}

func timeNow() time.Time { return time.Now() }

// openRoot opens dir as an os.Root for the archive helpers.
func openRoot(t *testing.T, dir string) *os.Root {
	t.Helper()
	r, err := os.OpenRoot(dir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { r.Close() })
	return r
}

// snapDir and snapPath are the absolute paths of the store's snapshot files,
// for tests that damage or read them directly.
func (s *profileStore) snapDir() string { return filepath.Join(s.root, filepath.FromSlash(snapRel)) }
func (s *profileStore) snapPath(id string) string {
	return filepath.Join(s.root, filepath.FromSlash(snapFile(id)))
}
