package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"encoding/base64"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"golang.org/x/net/proxy"
)

var (
	errForbiddenTarget = errors.New("forbidden target")
	errWaiting         = errors.New("waiting for config")
	errUnreachable     = errors.New("upstream_unreachable")
	errLoginRefused    = errors.New("upstream_login_refused")
)

// ── target guard ──

type guard struct {
	mu     sync.Mutex
	own    []net.IP
	loaded time.Time
	// resolve is net.DefaultResolver.LookupIPAddr unless a test sets it.
	resolve func(ctx context.Context, host string) ([]net.IPAddr, error)
	// ownIPs lists the pod's addresses unless a test sets it.
	ownIPs func() []net.IP
}

func newGuard() *guard {
	return &guard{resolve: net.DefaultResolver.LookupIPAddr, ownIPs: interfaceIPs}
}

func interfaceIPs() []net.IP {
	var out []net.IP
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		return nil
	}
	for _, a := range addrs {
		if n, ok := a.(*net.IPNet); ok {
			out = append(out, n.IP)
		}
	}
	return out
}

func (g *guard) own_() []net.IP {
	g.mu.Lock()
	defer g.mu.Unlock()
	if time.Since(g.loaded) > time.Minute {
		g.own = g.ownIPs()
		g.loaded = time.Now()
	}
	return g.own
}

var zeroNet = &net.IPNet{IP: net.IPv4zero, Mask: net.CIDRMask(8, 32)}

func (g *guard) forbiddenIP(ip net.IP) bool {
	if v4 := ip.To4(); v4 != nil {
		ip = v4
	}
	if ip.IsLoopback() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() ||
		ip.IsInterfaceLocalMulticast() || ip.IsUnspecified() || zeroNet.Contains(ip) {
		return true
	}
	for _, o := range g.own_() {
		if o.Equal(ip) {
			return true
		}
	}
	return false
}

// checkHost refuses names and literals that point back into the pod.
func (g *guard) checkHost(host string) error {
	h := strings.TrimSuffix(strings.ToLower(strings.Trim(host, "[]")), ".")
	if h == "" || h == "localhost" || strings.HasSuffix(h, ".localhost") {
		return errForbiddenTarget
	}
	if ip := net.ParseIP(h); ip != nil && g.forbiddenIP(ip) {
		return errForbiddenTarget
	}
	return nil
}

// dialSafe resolves addr itself, refuses it when ANY address is forbidden
// (no DNS rebinding: the checked address is the one dialled).
func (g *guard) dialSafe(ctx context.Context, network, addr string) (net.Conn, error) {
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return nil, err
	}
	if err := g.checkHost(host); err != nil {
		return nil, err
	}
	var ips []net.IP
	if ip := net.ParseIP(strings.Trim(host, "[]")); ip != nil {
		ips = []net.IP{ip}
	} else {
		addrs, err := g.resolve(ctx, host)
		if err != nil {
			return nil, err
		}
		for _, a := range addrs {
			if g.forbiddenIP(a.IP) {
				return nil, errForbiddenTarget
			}
			ips = append(ips, a.IP)
		}
	}
	if len(ips) == 0 {
		return nil, errors.New("no address")
	}
	d := net.Dialer{Timeout: 15 * time.Second, KeepAlive: 30 * time.Second}
	var last error
	for _, ip := range ips {
		c, err := d.DialContext(ctx, "tcp", net.JoinHostPort(ip.String(), port))
		if err == nil {
			return c, nil
		}
		last = err
	}
	return nil, last
}

// ── routes: one per generation ──

type route struct {
	gen   int64
	mode  string // waiting | direct | proxy
	up    *Upstream
	srv   server
	login *Login

	g         *guard
	transport *http.Transport

	mu     sync.Mutex
	conns  map[net.Conn]struct{}
	closed bool
}

func newRoute(gen int64, mode string, up *Upstream, login *Login, g *guard) *route {
	r := &route{gen: gen, mode: mode, up: up, login: login, g: g, conns: map[net.Conn]struct{}{}}
	if up != nil {
		r.srv, _ = parseServer(up.Server)
	}
	t := &http.Transport{
		DialContext:           r.trackedDial,
		ForceAttemptHTTP2:     false,
		DisableCompression:    true,
		MaxIdleConnsPerHost:   8,
		IdleConnTimeout:       60 * time.Second,
		TLSHandshakeTimeout:   15 * time.Second,
		ResponseHeaderTimeout: 60 * time.Second,
	}
	if mode == "proxy" {
		pu := &url.URL{Scheme: r.srv.scheme, Host: r.srv.addr()}
		if login != nil {
			pu.User = url.UserPassword(login.Username, login.Password)
		}
		t.Proxy = http.ProxyURL(pu)
	}
	r.transport = t
	return r
}

type trackedConn struct {
	net.Conn
	r    *route
	once sync.Once
}

func (c *trackedConn) Close() error {
	c.once.Do(func() { c.r.forget(c) })
	return c.Conn.Close()
}

func (r *route) track(c net.Conn) (net.Conn, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		c.Close()
		return nil, false
	}
	tc := &trackedConn{Conn: c, r: r}
	r.conns[tc] = struct{}{}
	return tc, true
}

func (r *route) forget(c net.Conn) {
	r.mu.Lock()
	delete(r.conns, c)
	r.mu.Unlock()
}

// cut closes every connection of this generation.
func (r *route) cut() {
	r.mu.Lock()
	r.closed = true
	conns := r.conns
	r.conns = map[net.Conn]struct{}{}
	r.mu.Unlock()
	for c := range conns {
		if tc, ok := c.(*trackedConn); ok {
			tc.Conn.Close()
		} else {
			c.Close()
		}
	}
	r.transport.CloseIdleConnections()
}

func (r *route) openConns() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.conns)
}

func (r *route) trackedDial(ctx context.Context, network, addr string) (net.Conn, error) {
	c, err := r.g.dialSafe(ctx, network, addr)
	if err != nil {
		return nil, err
	}
	tc, ok := r.track(c)
	if !ok {
		return nil, errUnreachable
	}
	return tc, nil
}

// dialTarget opens a byte stream to host:port through this route.
func (r *route) dialTarget(ctx context.Context, target string) (net.Conn, error) {
	host, _, err := net.SplitHostPort(target)
	if err != nil {
		return nil, errForbiddenTarget
	}
	if err := r.g.checkHost(host); err != nil {
		return nil, err
	}
	switch r.mode {
	case "waiting":
		return nil, errWaiting
	case "direct":
		return r.trackedDial(ctx, "tcp", target)
	}
	switch r.srv.scheme {
	case "socks5":
		var auth *proxy.Auth
		if r.login != nil {
			auth = &proxy.Auth{User: r.login.Username, Password: r.login.Password}
		}
		d, err := proxy.SOCKS5("tcp", r.srv.addr(), auth, dialerFunc(r.trackedDial))
		if err != nil {
			return nil, errUnreachable
		}
		// The name goes to the proxy unresolved (ATYP domain).
		c, err := d.(proxy.ContextDialer).DialContext(ctx, "tcp", target)
		if err != nil {
			if strings.Contains(err.Error(), "authentication failed") || strings.Contains(err.Error(), "username/password") {
				return nil, errLoginRefused
			}
			return nil, errUnreachable
		}
		return c, nil
	default: // http, https: CONNECT by name
		c, err := r.trackedDial(ctx, "tcp", r.srv.addr())
		if err != nil {
			return nil, errUnreachable
		}
		if r.srv.scheme == "https" {
			tc := tls.Client(c, &tls.Config{ServerName: r.srv.host, MinVersion: tls.VersionTLS12})
			hctx, cancel := context.WithTimeout(ctx, 15*time.Second)
			err := tc.HandshakeContext(hctx)
			cancel()
			if err != nil {
				c.Close()
				return nil, errUnreachable
			}
			c = tc
		}
		return connectThrough(c, target, r.login)
	}
}

type dialerFunc func(ctx context.Context, network, addr string) (net.Conn, error)

func (f dialerFunc) Dial(network, addr string) (net.Conn, error) {
	return f(context.Background(), network, addr)
}
func (f dialerFunc) DialContext(ctx context.Context, network, addr string) (net.Conn, error) {
	return f(ctx, network, addr)
}

func basicAuth(l *Login) string {
	return "Basic " + base64.StdEncoding.EncodeToString([]byte(l.Username+":"+l.Password))
}

// connectThrough sends CONNECT over an open connection to an HTTP proxy.
func connectThrough(c net.Conn, target string, login *Login) (net.Conn, error) {
	c.SetDeadline(time.Now().Add(30 * time.Second))
	var b strings.Builder
	b.WriteString("CONNECT " + target + " HTTP/1.1\r\nHost: " + target + "\r\n")
	if login != nil {
		b.WriteString("Proxy-Authorization: " + basicAuth(login) + "\r\n")
	}
	b.WriteString("\r\n")
	if _, err := io.WriteString(c, b.String()); err != nil {
		c.Close()
		return nil, errUnreachable
	}
	br := bufio.NewReader(c)
	resp, err := http.ReadResponse(br, &http.Request{Method: http.MethodConnect})
	if err != nil {
		c.Close()
		return nil, errUnreachable
	}
	resp.Body.Close()
	if resp.StatusCode == http.StatusProxyAuthRequired {
		c.Close()
		return nil, errLoginRefused
	}
	if resp.StatusCode != http.StatusOK {
		c.Close()
		return nil, errUnreachable
	}
	c.SetDeadline(time.Time{})
	if br.Buffered() > 0 {
		return &bufferedConn{Conn: c, r: br}, nil
	}
	return c, nil
}

type bufferedConn struct {
	net.Conn
	r *bufio.Reader
}

func (c *bufferedConn) Read(p []byte) (int, error) { return c.r.Read(p) }

// ── the relay: Chrome's proxy on 127.0.0.1:3128 ──

type relay struct {
	cur     atomic.Pointer[route]
	onError func(code string) // records lastError (vocabulary only)
}

func (rl *relay) route() *route { return rl.cur.Load() }

// swap installs a new route and cuts the old one.
func (rl *relay) swap(r *route) {
	old := rl.cur.Swap(r)
	if old != nil && old != r {
		old.cut()
	}
}

func (rl *relay) noteErr(err error) {
	if rl.onError == nil {
		return
	}
	switch {
	case errors.Is(err, errLoginRefused):
		rl.onError("upstream_login_refused")
	case errors.Is(err, errUnreachable):
		rl.onError("upstream_unreachable")
	}
}

var hopHeaders = []string{
	"Connection", "Proxy-Connection", "Keep-Alive", "Proxy-Authenticate",
	"Proxy-Authorization", "Te", "Trailer", "Transfer-Encoding", "Upgrade",
}

func (rl *relay) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	r := rl.route()
	if r == nil || r.mode == "waiting" {
		http.Error(w, "browser proxy is not ready", http.StatusBadGateway)
		return
	}
	if req.Method == http.MethodConnect {
		rl.serveConnect(w, req, r)
		return
	}
	if req.URL.Scheme != "http" || req.URL.Host == "" {
		http.Error(w, "unsupported proxy request", http.StatusBadRequest)
		return
	}
	if err := r.g.checkHost(req.URL.Hostname()); err != nil {
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}
	out := req.Clone(req.Context())
	out.RequestURI = ""
	for _, h := range hopHeaders {
		out.Header.Del(h)
	}
	resp, err := r.transport.RoundTrip(out)
	if err != nil {
		if errors.Is(err, errForbiddenTarget) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		http.Error(w, "upstream proxy failed", http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()
	for _, h := range hopHeaders {
		resp.Header.Del(h)
	}
	for k, vs := range resp.Header {
		for _, v := range vs {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	flushCopy(w, resp.Body)
}

func flushCopy(w http.ResponseWriter, body io.Reader) {
	f, _ := w.(http.Flusher)
	buf := make([]byte, 32*1024)
	for {
		n, err := body.Read(buf)
		if n > 0 {
			if _, werr := w.Write(buf[:n]); werr != nil {
				return
			}
			if f != nil {
				f.Flush()
			}
		}
		if err != nil {
			return
		}
	}
}

func (rl *relay) serveConnect(w http.ResponseWriter, req *http.Request, r *route) {
	target := req.Host
	if _, _, err := net.SplitHostPort(target); err != nil {
		http.Error(w, "bad target", http.StatusBadRequest)
		return
	}
	ctx, cancel := context.WithTimeout(req.Context(), 45*time.Second)
	up, err := r.dialTarget(ctx, target)
	cancel()
	if err != nil {
		if errors.Is(err, errForbiddenTarget) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		rl.noteErr(err)
		http.Error(w, "upstream proxy failed", http.StatusBadGateway)
		return
	}
	hj, ok := w.(http.Hijacker)
	if !ok {
		up.Close()
		http.Error(w, "no hijack", http.StatusInternalServerError)
		return
	}
	client, brw, err := hj.Hijack()
	if err != nil {
		up.Close()
		return
	}
	tc, ok := r.track(client)
	if !ok {
		up.Close()
		return
	}
	if _, err := io.WriteString(tc, "HTTP/1.1 200 Connection established\r\n\r\n"); err != nil {
		tc.Close()
		up.Close()
		return
	}
	go pipe(tc, up, brw.Reader)
}

func pipe(client, up net.Conn, pending *bufio.Reader) {
	done := make(chan struct{}, 2)
	go func() {
		if pending != nil && pending.Buffered() > 0 {
			b, _ := pending.Peek(pending.Buffered())
			up.Write(b)
		}
		io.Copy(up, client)
		done <- struct{}{}
	}()
	go func() {
		io.Copy(client, up)
		done <- struct{}{}
	}()
	<-done
	client.Close()
	up.Close()
	<-done
}
