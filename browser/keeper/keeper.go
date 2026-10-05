package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"math/rand"
	"net"
	"net/http"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// apiErr is an answer with a fixed code; message is product language.
type apiErr struct {
	status  int
	code    string
	message string
}

func (e *apiErr) Error() string { return e.code }

// Keeper holds the proxy state: the applied config, its credentials, the
// current upstream and what the last probe saw.
type Keeper struct {
	secretDir     string
	statePath     string
	relayRequired bool
	g             *guard
	rl            *relay
	keys          atomic.Pointer[keys]
	launcher      *launcherClient
	now           func() time.Time
	rnd           *rand.Rand

	mu           sync.Mutex
	cfg          *Config
	creds        Creds
	applied      int64
	current      int
	lastChangeIP map[string]int64
	firstApply   int64
	lastRotation int64
	generation   int64

	exitIP, country string
	measuredAt      time.Time
	rotatedAt       time.Time
	nextRotationAt  time.Time
	lastError       string
	chromeVersion   string
	probing         atomic.Bool

	// engine is "" for a Chrome browser's sidecar and engineCamoufox for a
	// Camoufox one (KEEPER_ENGINE). Unset, every answer is Chrome's own.
	engine         string
	browserVersion string // Camoufox only, like chromeVersion for Chrome
	playwright     string // Camoufox only: the Playwright its server speaks

	rotMu sync.Mutex // one rotation at a time

	// Overridable for tests.
	probeEvery   time.Duration
	rotateProbe  time.Duration // how long a rotation waits for a new IP
	changeProbe  time.Duration // the same after a change-IP call
	probePoll    time.Duration
	changeClient *http.Client
}

func newKeeper(secretDir, statePath string, relayRequired bool, launcherURL string) *Keeper {
	k := &Keeper{
		secretDir:     secretDir,
		statePath:     statePath,
		relayRequired: relayRequired,
		g:             newGuard(),
		launcher:      newLauncherClient(launcherURL),
		now:           time.Now,
		rnd:           rand.New(rand.NewSource(time.Now().UnixNano())),
		lastChangeIP:  map[string]int64{},
		creds:         Creds{},
		probeEvery:    10 * time.Minute,
		rotateProbe:   25 * time.Second, // + the swap: a rotation answers within 30 s
		changeProbe:   85 * time.Second, // + the 30 s change-IP call: within 120 s
		probePoll:     3 * time.Second,
	}
	k.rl = &relay{onError: k.setError}
	k.changeClient = &http.Client{
		Timeout: 30 * time.Second,
		Transport: &http.Transport{
			DialContext:       k.g.dialSafe,
			DisableKeepAlives: true,
		},
	}
	k.rl.swap(newRoute(0, k.initialMode(), nil, nil, k.g))
	return k
}

func (k *Keeper) initialMode() string {
	if k.relayRequired {
		return "waiting"
	}
	return "direct"
}

// swapRouteLocked installs a new route. What the last probe saw belongs to
// the old exit, so it is cleared until the new one is measured.
func (k *Keeper) swapRouteLocked(r *route) {
	k.rl.swap(r)
	k.exitIP, k.country, k.measuredAt = "", "", time.Time{}
}

func (k *Keeper) setError(code string) {
	k.mu.Lock()
	k.lastError = code
	k.mu.Unlock()
}

// ── keys ──

func (k *Keeper) reloadKeys() {
	nk, err := loadKeys(filepath.Join(k.secretDir, "control-key"))
	if err != nil {
		if errors.Is(err, errNoKey) {
			return // keep what we have; not_ready until one exists
		}
		log.Printf("control key unreadable")
		return
	}
	if old := k.keys.Load(); old == nil || old.raw != nk.raw {
		k.keys.Store(nk)
		log.Printf("control key loaded")
	}
}

// ── config ──

// boot loads the persisted state and the files and applies the newer.
func (k *Keeper) boot() {
	k.reloadKeys()
	st, _ := loadState(k.statePath)
	fs, err := readFiles(k.secretDir)
	if err != nil {
		k.setError("config_invalid")
	}
	k.mu.Lock()
	defer k.mu.Unlock()
	if st != nil {
		k.current = st.Current
		k.firstApply = st.FirstApply
		k.lastRotation = st.LastRotation
		k.generation = st.Generation
		if st.LastChangeIP != nil {
			k.lastChangeIP = st.LastChangeIP
		}
		if st.Config != nil && st.Current < len(st.Config.Upstreams) {
			// Keeps the current upstream across a container restart.
			k.cfg = st.Config
		}
	}
	switch {
	case fs.cfg != nil && (st == nil || st.Config == nil || fs.cfg.Version >= st.Config.Version):
		k.applyLocked(fs.cfg, fs.creds, false)
	case st != nil && st.Config != nil:
		k.applyLocked(st.Config, st.Creds, false)
	}
}

// pollFiles re-reads the mounted Secret: a newer version applies wholesale;
// on an equal version the files win (applied only if they differ).
func (k *Keeper) pollFiles() {
	k.reloadKeys()
	fs, err := readFiles(k.secretDir)
	if err != nil {
		k.setError("config_invalid")
		return
	}
	if fs.cfg == nil {
		return
	}
	k.mu.Lock()
	defer k.mu.Unlock()
	if fs.cfg.Version > k.applied ||
		(fs.cfg.Version == k.applied && (!reflect.DeepEqual(fs.cfg, k.cfg) || !reflect.DeepEqual(fs.creds.keepOnly(fs.cfg), k.creds))) {
		k.applyLocked(fs.cfg, fs.creds, true)
	}
}

// push applies a sealed PUT /v1/config.
func (k *Keeper) push(version int64, body []byte) *apiErr {
	ks := k.keys.Load()
	if ks == nil {
		return &apiErr{503, "not_ready", "not ready"}
	}
	p, err := openPush(ks, version, body)
	if err != nil {
		return &apiErr{400, "config_invalid", "config does not open"}
	}
	k.mu.Lock()
	defer k.mu.Unlock()
	if version <= k.applied {
		return &apiErr{409, "stale_version", "a newer config is applied"}
	}
	creds := mergeCreds(k.creds, p)
	if err := k.applyLocked(p.Config, creds, true); err != nil {
		return &apiErr{422, "config_invalid", "config is not valid"}
	}
	return nil
}

func sameUpstream(a, b *Upstream) bool {
	if a == nil || b == nil {
		return a == b
	}
	return a.Name == b.Name && a.Server == b.Server
}

// applyLocked installs a config; the route (and its connections) changes
// only when the effective upstream or its login changed.
func (k *Keeper) applyLocked(cfg *Config, creds Creds, probe bool) error {
	c := *cfg
	c.Upstreams = append([]Upstream(nil), cfg.Upstreams...)
	c.normalize()
	if err := c.validate(); err != nil {
		k.lastError = "config_invalid"
		return err
	}
	creds = creds.keepOnly(&c)
	prevName := ""
	if k.cfg != nil && k.current < len(k.cfg.Upstreams) {
		prevName = k.cfg.Upstreams[k.current].Name
	}
	k.current = 0
	for i, u := range c.Upstreams {
		if u.Name == prevName {
			k.current = i
		}
	}
	k.cfg = &c
	k.creds = creds
	k.applied = c.Version
	now := k.now().Unix()
	if k.firstApply == 0 {
		k.firstApply = now
	}

	mode, up, login := "direct", (*Upstream)(nil), (*Login)(nil)
	if len(c.Upstreams) > 0 {
		mode = "proxy"
		u := c.Upstreams[k.current]
		up = &u
		login = creds.login(u.Name)
	}
	cur := k.rl.route()
	if cur == nil || cur.mode != mode || !sameUpstream(cur.up, up) || !reflect.DeepEqual(cur.login, login) {
		k.generation++
		k.swapRouteLocked(newRoute(k.generation, mode, up, login, k.g))
	}
	if c.Rotation.Mode == "interval" && mode == "proxy" {
		base := k.lastRotation
		if base == 0 {
			base = now
		}
		k.nextRotationAt = time.Unix(base, 0).Add(time.Duration(c.Rotation.EveryMinutes) * time.Minute)
		if k.nextRotationAt.Before(k.now()) {
			k.nextRotationAt = k.now().Add(time.Duration(c.Rotation.EveryMinutes) * time.Minute)
		}
	} else {
		k.nextRotationAt = time.Time{}
	}
	if k.lastError == "config_invalid" {
		k.lastError = ""
	}
	k.saveLocked()
	if probe {
		go k.probeOnce(context.Background(), true)
	}
	return nil
}

func (k *Keeper) saveLocked() {
	err := saveState(k.statePath, &persisted{
		Config: k.cfg, Creds: k.creds, Current: k.current, LastChangeIP: k.lastChangeIP,
		FirstApply: k.firstApply, LastRotation: k.lastRotation, Generation: k.generation,
	})
	if err != nil {
		log.Printf("state not saved")
	}
}

// ── status ──

type upstreamView struct {
	Name   string `json:"name"`
	Server string `json:"server"`
}

type Status struct {
	ConfigVersion  int64         `json:"configVersion"`
	Mode           string        `json:"mode"`
	Relay          bool          `json:"relay"`
	Upstream       *upstreamView `json:"upstream"`
	ExitIP         string        `json:"exitIp,omitempty"`
	Country        string        `json:"country,omitempty"`
	MeasuredAt     *time.Time    `json:"measuredAt"`
	RotatedAt      *time.Time    `json:"rotatedAt"`
	Generation     int64         `json:"generation"`
	NextRotationAt *time.Time    `json:"nextRotationAt"`
	LastError      string        `json:"lastError,omitempty"`
	ChromeVersion  string        `json:"chromeVersion,omitempty"`
	// Camoufox only (omitted for Chrome, whose answer stays as it was).
	Engine         string `json:"engine,omitempty"`
	BrowserVersion string `json:"browserVersion,omitempty"`
	Playwright     string `json:"playwright,omitempty"`
}

func tp(t time.Time) *time.Time {
	if t.IsZero() {
		return nil
	}
	u := t.UTC()
	return &u
}

func (k *Keeper) status() Status {
	k.mu.Lock()
	defer k.mu.Unlock()
	r := k.rl.route()
	s := Status{
		ConfigVersion: k.applied, Mode: r.mode, Relay: k.relayRequired,
		ExitIP: k.exitIP, Country: k.country, MeasuredAt: tp(k.measuredAt), RotatedAt: tp(k.rotatedAt),
		Generation: k.generation, NextRotationAt: tp(k.nextRotationAt), LastError: k.lastError,
		ChromeVersion: k.chromeVersion,
	}
	if k.engine == engineCamoufox {
		s.ChromeVersion = ""
		s.Engine, s.BrowserVersion, s.Playwright = k.engine, k.browserVersion, k.playwright
	}
	if r.up != nil {
		s.Upstream = &upstreamView{Name: r.up.Name, Server: r.up.Server}
	}
	return s
}

// ── rotation ──

func (k *Keeper) rotate(ctx context.Context, to string) (Status, *apiErr) {
	k.rotMu.Lock()
	defer k.rotMu.Unlock()

	k.mu.Lock()
	r := k.rl.route()
	if k.cfg == nil || r.mode != "proxy" || len(k.cfg.Upstreams) == 0 {
		k.mu.Unlock()
		return Status{}, &apiErr{409, "nothing_to_rotate", "There is no proxy to rotate to."}
	}
	n := len(k.cfg.Upstreams)
	idx := -1
	if to != "" {
		for i, u := range k.cfg.Upstreams {
			if u.Name == to {
				idx = i
			}
		}
		if idx < 0 {
			k.mu.Unlock()
			return Status{}, &apiErr{404, "unknown_upstream", "No proxy by that name."}
		}
	} else if n == 1 {
		idx = 0
	} else if k.cfg.Rotation.Order == "random" {
		idx = k.rnd.Intn(n - 1)
		if idx >= k.current {
			idx++
		}
	} else {
		idx = (k.current + 1) % n
	}
	up := k.cfg.Upstreams[idx]
	changeURL := k.creds.changeIP(up.Name)
	if n == 1 && changeURL == "" {
		k.mu.Unlock()
		return Status{}, &apiErr{409, "nothing_to_rotate", "Nothing to rotate to: add another proxy or a change-IP link."}
	}
	now := k.now().Unix()
	if changeURL != "" {
		if last := k.lastChangeIP[up.Name]; last != 0 && now-last < int64(up.MinChangeIPSeconds) {
			k.lastError = "change_ip_too_soon"
			k.mu.Unlock()
			return Status{}, &apiErr{429, "change_ip_too_soon", "Too soon to change this proxy's IP again."}
		}
		k.lastChangeIP[up.Name] = now
		k.saveLocked()
	}
	prevIP := k.exitIP
	k.mu.Unlock()

	if changeURL != "" {
		if err := k.callChangeIP(ctx, up.ChangeIPMethod, changeURL); err != nil {
			k.setError("change_ip_failed")
			return k.status(), &apiErr{502, "change_ip_failed", "The proxy's change-IP link failed."}
		}
	}

	k.mu.Lock()
	k.current = idx
	k.generation++
	login := k.creds.login(up.Name)
	upc := up
	k.swapRouteLocked(newRoute(k.generation, "proxy", &upc, login, k.g))
	k.lastRotation = k.now().Unix()
	k.rotatedAt = k.now()
	if k.cfg.Rotation.Mode == "interval" {
		k.nextRotationAt = k.now().Add(time.Duration(k.cfg.Rotation.EveryMinutes) * time.Minute)
	}
	k.saveLocked()
	k.mu.Unlock()

	// Probe until the exit changes or the wait runs out (not fatal). The
	// probes themselves end at the deadline too.
	wait := k.rotateProbe
	if changeURL != "" {
		wait = k.changeProbe
	}
	deadline := k.now().Add(wait)
	pctx, pcancel := context.WithDeadline(ctx, deadline)
	defer pcancel()
	for {
		k.probeOnce(pctx, false)
		st := k.status()
		if st.ExitIP != "" && st.ExitIP != prevIP {
			break
		}
		if !k.now().Before(deadline) || pctx.Err() != nil {
			if st.LastError == "" {
				k.setError("exit_ip_unchanged")
			}
			break
		}
		select {
		case <-pctx.Done():
		case <-time.After(k.probePoll):
		}
	}
	return k.status(), nil
}

func (k *Keeper) callChangeIP(ctx context.Context, method, rawURL string) error {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, method, rawURL, nil)
	if err != nil {
		return err
	}
	resp, err := k.changeClient.Do(req)
	if err != nil {
		return err
	}
	io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
	resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return errors.New("change-ip status")
	}
	return nil
}

// sessionStart is the Browser API's hint that a session just started here.
func (k *Keeper) sessionStart(ctx context.Context, openSessions int) (map[string]any, *apiErr) {
	k.mu.Lock()
	if k.cfg == nil || k.cfg.Rotation.Mode != "session" || k.rl.route().mode != "proxy" {
		k.mu.Unlock()
		return map[string]any{"rotated": false, "reason": "not rotating per session"}, nil
	}
	if openSessions > 0 {
		k.mu.Unlock()
		return map[string]any{"rotated": false, "reason": "other sessions in use"}, nil
	}
	wait := int64(30)
	for _, u := range k.cfg.Upstreams {
		if k.creds.changeIP(u.Name) != "" && int64(u.MinChangeIPSeconds) > wait {
			wait = int64(u.MinChangeIPSeconds)
		}
	}
	since := k.lastRotation
	if since == 0 {
		since = k.firstApply
	}
	if k.now().Unix()-since < wait {
		k.mu.Unlock()
		return map[string]any{"rotated": false, "reason": "rotated moments ago"}, nil
	}
	k.mu.Unlock()
	if _, e := k.rotate(ctx, ""); e != nil {
		return map[string]any{"rotated": false, "reason": e.code}, nil
	}
	return map[string]any{"rotated": true}, nil
}

// ── exit probe ──

type probeResult struct {
	ip, country string
}

var errProbeUnreadable = errors.New("probe_unreadable")

func parseProbe(body []byte) (probeResult, error) {
	t := strings.TrimSpace(string(body))
	if ip := net.ParseIP(t); ip != nil {
		return probeResult{ip: ip.String()}, nil
	}
	var m map[string]any
	if json.Unmarshal([]byte(t), &m) == nil {
		ipStr, _ := m["ip"].(string)
		if ip := net.ParseIP(strings.TrimSpace(ipStr)); ip != nil {
			pr := probeResult{ip: ip.String()}
			for _, key := range []string{"country_code", "countryCode", "country"} {
				if c, ok := m[key].(string); ok && len(c) == 2 {
					pr.country = strings.ToUpper(c)
					break
				}
			}
			return pr, nil
		}
	}
	return probeResult{}, errProbeUnreadable
}

func (k *Keeper) checkURL() string {
	k.mu.Lock()
	defer k.mu.Unlock()
	if k.cfg == nil {
		return ""
	}
	return k.cfg.CheckURL
}

// probeOnce measures the exit through the current route. failover moves to
// the next upstream after an unreachable or refused one.
func (k *Keeper) probeOnce(ctx context.Context, failover bool) {
	u := k.checkURL()
	r := k.rl.route()
	if u == "" || r.mode == "waiting" {
		return
	}
	if !k.probing.CompareAndSwap(false, true) {
		return
	}
	defer k.probing.Store(false)
	parent := ctx
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	code := ""
	var res probeResult
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		code = "probe_failed"
	} else {
		cl := &http.Client{Transport: r.transport}
		resp, err := cl.Do(req)
		switch {
		case err != nil:
			code = classifyProbeErr(err)
		case resp.StatusCode == http.StatusProxyAuthRequired:
			resp.Body.Close()
			code = "upstream_login_refused"
		case resp.StatusCode == http.StatusBadGateway && r.mode == "proxy":
			resp.Body.Close()
			code = "upstream_unreachable"
		case resp.StatusCode < 200 || resp.StatusCode > 299:
			resp.Body.Close()
			code = "probe_failed"
		default:
			b, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
			resp.Body.Close()
			if pr, err := parseProbe(b); err != nil {
				code = "probe_unreadable"
			} else {
				res = pr
			}
		}
	}
	if parent.Err() != nil {
		return // the caller stopped waiting: not a measurement
	}
	k.mu.Lock()
	if k.rl.route() != r {
		k.mu.Unlock()
		return // rotated meanwhile; this result is for an old exit
	}
	if code != "" {
		k.lastError = code
		k.mu.Unlock()
		if failover && r.mode == "proxy" && (code == "upstream_unreachable" || code == "upstream_login_refused") {
			k.failover(r)
		}
		return
	}
	k.exitIP, k.country, k.measuredAt = res.ip, res.country, k.now()
	k.lastError = ""
	k.mu.Unlock()
}

func classifyProbeErr(err error) string {
	s := err.Error()
	switch {
	case strings.Contains(s, "Proxy Authentication Required") || strings.Contains(s, "authentication failed") || strings.Contains(s, "username/password"):
		return "upstream_login_refused"
	case strings.Contains(s, "proxyconnect") || strings.Contains(s, "socks connect") || strings.Contains(s, "connection refused"):
		return "upstream_unreachable"
	}
	return "probe_failed"
}

// failover moves to the next upstream in order (no change-IP call).
func (k *Keeper) failover(failed *route) {
	k.rotMu.Lock()
	defer k.rotMu.Unlock()
	k.mu.Lock()
	defer k.mu.Unlock()
	if k.rl.route() != failed || k.cfg == nil || len(k.cfg.Upstreams) < 2 {
		return
	}
	k.current = (k.current + 1) % len(k.cfg.Upstreams)
	up := k.cfg.Upstreams[k.current]
	k.generation++
	k.swapRouteLocked(newRoute(k.generation, "proxy", &up, k.creds.login(up.Name), k.g))
	k.saveLocked()
	go k.probeOnce(context.Background(), false)
}

// ── loops ──

func (k *Keeper) run(ctx context.Context) {
	files := time.NewTicker(30 * time.Second)
	tick := time.NewTicker(5 * time.Second)
	defer files.Stop()
	defer tick.Stop()
	lastProbe := k.now()
	go k.probeOnce(ctx, true)
	if k.engine == engineCamoufox {
		// The connect answer reads the browser's Playwright version from the
		// status: known from the first seconds, not after the first 30.
		go k.refreshChromeVersion(ctx)
	}
	for {
		select {
		case <-ctx.Done():
			return
		case <-files.C:
			k.pollFiles()
			k.refreshChromeVersion(ctx)
		case <-tick.C:
			k.mu.Lock()
			due := !k.nextRotationAt.IsZero() && !k.now().Before(k.nextRotationAt)
			if due && k.cfg != nil {
				// Moved on now, so a slow or refused rotation is not retried every tick.
				k.nextRotationAt = k.now().Add(time.Duration(k.cfg.Rotation.EveryMinutes) * time.Minute)
			}
			k.mu.Unlock()
			if due {
				go k.rotate(ctx, "")
			}
			if k.rl.route().mode == "proxy" && k.now().Sub(lastProbe) >= k.probeEvery {
				lastProbe = k.now()
				go k.probeOnce(ctx, true)
			}
		}
	}
}

func (k *Keeper) refreshChromeVersion(ctx context.Context) {
	v, err := k.launcher.version(ctx)
	if k.engine == engineCamoufox {
		if err != nil || v.BrowserVersion == "" {
			return
		}
		k.mu.Lock()
		k.browserVersion, k.playwright = v.BrowserVersion, v.Playwright
		k.mu.Unlock()
		return
	}
	if err != nil || v.Chrome == "" {
		return
	}
	k.mu.Lock()
	k.chromeVersion = v.Chrome
	k.mu.Unlock()
}

// ── engines ──

// engineCamoufox is KEEPER_ENGINE for a Camoufox browser's sidecar. Any other
// value (unset included) is a Chrome browser's, with Chrome's answers.
const engineCamoufox = "camoufox"

func engineFromEnv(v string) string {
	if strings.TrimSpace(v) == engineCamoufox {
		return engineCamoufox
	}
	return ""
}
