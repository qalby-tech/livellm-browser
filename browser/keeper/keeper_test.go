package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestAuthRefusesUnsignedReplayedAndStale(t *testing.T) {
	e := newEnv(t, false)
	// unsigned
	resp, _ := http.Get(e.api.URL + "/v1/status")
	if resp.StatusCode != 401 {
		t.Fatalf("unsigned: %d", resp.StatusCode)
	}
	// healthz needs nothing
	resp, _ = http.Get(e.api.URL + "/healthz")
	if resp.StatusCode != 200 {
		t.Fatalf("healthz: %d", resp.StatusCode)
	}
	ks, _ := deriveKeys(testControlKey)
	// signed, then replayed with the same header
	req, _ := http.NewRequest("GET", e.api.URL+"/v1/status", nil)
	signRequest(req, ks.auth, nil, false, time.Now(), randNonce())
	if resp, _ := http.DefaultClient.Do(req); resp.StatusCode != 200 {
		t.Fatalf("signed: %d", resp.StatusCode)
	}
	again, _ := http.NewRequest("GET", e.api.URL+"/v1/status", nil)
	again.Header.Set("Authorization", req.Header.Get("Authorization"))
	if resp, _ := http.DefaultClient.Do(again); resp.StatusCode != 401 {
		t.Fatalf("replay: %d", resp.StatusCode)
	}
	// stale ts
	old, _ := http.NewRequest("GET", e.api.URL+"/v1/status", nil)
	signRequest(old, ks.auth, nil, false, time.Now().Add(-2*time.Minute), randNonce())
	if resp, _ := http.DefaultClient.Do(old); resp.StatusCode != 401 {
		t.Fatalf("stale: %d", resp.StatusCode)
	}
	// a signature over another path / body does not carry over
	other, _ := http.NewRequest("POST", e.api.URL+"/v1/rotate", strings.NewReader(`{"to":"x"}`))
	signRequest(other, ks.auth, []byte(`{}`), false, time.Now(), randNonce())
	if resp, _ := http.DefaultClient.Do(other); resp.StatusCode != 401 {
		t.Fatalf("body mismatch: %d", resp.StatusCode)
	}
	// wrong key
	wk, _ := deriveKeys(strings.Repeat("ab", 32))
	bad, _ := http.NewRequest("GET", e.api.URL+"/v1/status", nil)
	signRequest(bad, wk.auth, nil, false, time.Now(), randNonce())
	if resp, _ := http.DefaultClient.Do(bad); resp.StatusCode != 401 {
		t.Fatalf("wrong key: %d", resp.StatusCode)
	}
}

func TestNotReadyWithoutKey(t *testing.T) {
	e := &env{t: t, secretDir: t.TempDir(), runDir: t.TempDir(), profiles: t.TempDir(), logs: &bytes.Buffer{}}
	e.launcher = startLauncher(t)
	e.start(true)
	resp, _ := http.Get(e.api.URL + "/v1/status")
	if resp.StatusCode != 503 {
		t.Fatalf("no key: %d, want 503", resp.StatusCode)
	}
	// session-start stays open (no secrets in it)
	resp, _ = http.Post(e.api.URL+"/v1/session-start", "application/json", strings.NewReader(`{"openSessions":0}`))
	if resp.StatusCode != 200 {
		t.Fatalf("session-start: %d", resp.StatusCode)
	}
}

func TestSealedPushVersionOrderingAndMerge(t *testing.T) {
	w := newWorld(t)
	e := newEnv(t, true)
	cfg := w.config(100)
	if resp := e.pushConfig(cfg, bLogin(), nil); resp.StatusCode != 200 {
		t.Fatalf("push 100: %d", resp.StatusCode)
	}
	if resp := e.pushConfig(w.config(100), nil, nil); resp.StatusCode != 409 {
		t.Fatalf("equal version: %d, want 409", resp.StatusCode)
	} else if m := readJSON(t, resp); m["code"] != "stale_version" {
		t.Fatalf("code %v", m["code"])
	}
	if resp := e.pushConfig(w.config(99), nil, nil); resp.StatusCode != 409 {
		t.Fatalf("older version: %d, want 409", resp.StatusCode)
	}
	// a body sealed for another version does not open
	ks, _ := deriveKeys(testControlKey)
	pb, _ := json.Marshal(pushBody{Version: 101, Config: &cfg})
	resp := e.call("PUT", "/v1/config?version=102", seal(ks.cfg, pb, configAAD(101)), nil)
	if resp.StatusCode != 400 {
		t.Fatalf("aad mismatch: %d", resp.StatusCode)
	}
	// removing an upstream drops its credentials
	cfg2 := w.config(103)
	cfg2.Upstreams = cfg2.Upstreams[:1]
	e.pushConfig(cfg2, nil, nil)
	e.k.mu.Lock()
	_, kept := e.k.creds[loginKey("b")]
	e.k.mu.Unlock()
	if kept {
		t.Fatal("credentials of a removed upstream were kept")
	}
}

func TestStateReloadAfterRestartAndFilesPrecedence(t *testing.T) {
	w := newWorld(t)
	e := newEnv(t, true)
	cfg := w.config(200)
	e.pushConfig(cfg, bLogin(), nil)
	e.call("POST", "/v1/rotate", []byte(`{"to":"b"}`), nil)
	if st := e.k.status(); st.Upstream.Name != "b" {
		t.Fatalf("rotate to b: %+v", st)
	}
	// the container restarts: state survives in /run/keeper, files are older
	old := w.config(150)
	e.writeConfig(old)
	e.start(true)
	st := e.k.status()
	if st.ConfigVersion != 200 || st.Upstream == nil || st.Upstream.Name != "b" {
		t.Fatalf("after restart: %+v", st)
	}
	if code, m := fetch(t, e, "http://"+w.echo+"/"); code != 200 || !strings.Contains(m["via"].(string), "proxy-b") {
		t.Fatalf("b's login lost across a restart: %d %v", code, m)
	}
	// newer files win wholesale
	nc := w.config(300)
	nc.Upstreams = nc.Upstreams[:1]
	e.writeConfig(nc)
	e.k.pollFiles()
	if st := e.k.status(); st.ConfigVersion != 300 || st.Upstream.Name != "a" {
		t.Fatalf("newer files: %+v", st)
	}
	// a pod restart: no state, files only
	e.runDir = t.TempDir()
	e.start(true)
	if st := e.k.status(); st.ConfigVersion != 300 || st.Mode != "proxy" {
		t.Fatalf("pod restart: %+v", st)
	}
}

func TestSessionStartRules(t *testing.T) {
	w := newWorld(t)
	e := newEnv(t, true)
	cfg := w.config(1)
	cfg.Rotation = Rotation{Mode: "session"}
	e.pushConfig(cfg, bLogin(), nil)
	post := func(n int) map[string]any {
		resp, _ := http.Post(e.api.URL+"/v1/session-start", "application/json", strings.NewReader(fmt.Sprintf(`{"openSessions":%d}`, n)))
		return readJSON(t, resp)
	}
	if m := post(0); m["rotated"] != false || m["reason"] != "rotated moments ago" {
		t.Fatalf("right after apply: %v", m)
	}
	// 31 s later
	e.k.mu.Lock()
	e.k.firstApply -= 31
	e.k.mu.Unlock()
	if m := post(1); m["rotated"] != false || m["reason"] != "other sessions in use" {
		t.Fatalf("with another session: %v", m)
	}
	if m := post(0); m["rotated"] != true {
		t.Fatalf("alone: %v", m)
	}
	if st := e.k.status(); st.Upstream.Name != "b" {
		t.Fatalf("did not rotate: %+v", st)
	}
	if m := post(0); m["rotated"] != false {
		t.Fatalf("again at once: %v", m)
	}
}

func TestChangeIPCallRateLimitAndVocabulary(t *testing.T) {
	w := newWorld(t)
	e := newEnv(t, true)
	var calls atomic.Int64
	var fail atomic.Bool
	_, cip := listenOn(t, w.ip, http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if fail.Load() {
			time.Sleep(200 * time.Millisecond)
			rw.WriteHeader(500)
			return
		}
		rw.WriteHeader(200)
	}))
	key := "k3y-zz-9471"
	cfg := Config{Version: 1, Relay: true, Upstreams: []Upstream{{Name: "m", Server: "http://" + w.aAddr, ChangeIPMethod: "POST", MinChangeIPSeconds: 60}}, CheckURL: "http://" + w.echo + "/"}
	set := map[string]string{changeIPKey("m"): "http://" + cip + "/rot?key=" + key}
	e.pushConfig(cfg, set, nil)
	resp := e.call("POST", "/v1/rotate", nil, nil)
	if resp.StatusCode != 200 || calls.Load() != 1 {
		t.Fatalf("rotate: %d, calls %d", resp.StatusCode, calls.Load())
	}
	resp = e.call("POST", "/v1/rotate", nil, nil)
	if resp.StatusCode != 429 {
		t.Fatalf("too soon: %d", resp.StatusCode)
	}
	if calls.Load() != 1 {
		t.Fatal("a too-early rotate called the link")
	}
	// a failing link: change_ip_failed, the URL appears nowhere
	e.k.mu.Lock()
	e.k.lastChangeIP["m"] = 0
	e.k.mu.Unlock()
	fail.Store(true)
	resp = e.call("POST", "/v1/rotate", nil, nil)
	body := readJSON(t, resp)
	if resp.StatusCode != 502 || body["code"] != "change_ip_failed" {
		t.Fatalf("failing link: %d %v", resp.StatusCode, body)
	}
	st := e.k.status()
	if st.LastError != "change_ip_failed" {
		t.Fatalf("lastError %q", st.LastError)
	}
	sb, _ := json.Marshal(st)
	all := string(sb) + fmt.Sprint(body) + e.logs.String()
	if strings.Contains(all, key) || strings.Contains(all, cip) {
		t.Fatal("the change-IP link leaked into an answer or a log")
	}
	// one upstream without a link: nothing to rotate to
	e.pushConfig(Config{Version: 2, Relay: true, Upstreams: []Upstream{{Name: "a", Server: "http://" + w.aAddr}}}, nil, nil)
	if resp := e.call("POST", "/v1/rotate", nil, nil); resp.StatusCode != 409 {
		t.Fatalf("nothing to rotate: %d", resp.StatusCode)
	}
}

var vocabulary = map[string]bool{
	"": true, "upstream_unreachable": true, "upstream_login_refused": true, "change_ip_failed": true,
	"change_ip_too_soon": true, "exit_ip_unchanged": true, "probe_failed": true, "probe_unreadable": true,
	"config_invalid": true,
}

func TestLastErrorIsVocabularyOnly(t *testing.T) {
	w := newWorld(t)
	e := newEnv(t, true)
	_, junk := listenOn(t, w.ip, http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) { rw.Write([]byte("<html>no</html>")) }))
	cases := []Config{
		{Version: 1, Relay: true, Upstreams: []Upstream{{Name: "a", Server: "http://" + w.aAddr}}, CheckURL: "http://" + junk + "/?secret=q1"},
		{Version: 2, Relay: true, Upstreams: []Upstream{{Name: "a", Server: "http://" + w.aAddr}}, CheckURL: "http://no-such-host.invalid/?secret=q2"},
		{Version: 3, Relay: true, Upstreams: []Upstream{{Name: "b", Server: "http://" + w.bAddr}}, CheckURL: "http://" + w.echo + "/?secret=q3"},
	}
	for _, c := range cases {
		e.pushConfig(c, nil, nil)
		waitFor(t, "a probe error", func() bool { return e.k.status().LastError != "" })
		st := e.k.status()
		if !vocabulary[st.LastError] {
			t.Fatalf("lastError %q is not in the vocabulary", st.LastError)
		}
		if strings.ContainsAny(st.LastError, "/:?=") {
			t.Fatalf("lastError %q carries an address", st.LastError)
		}
	}
	// a bad file config
	e.writeSecret("config.json", `{"version":9,"upstreams":[{"name":"Bad Name","server":"ftp://x"}]}`)
	e.k.pollFiles()
	if st := e.k.status(); st.LastError != "config_invalid" || st.ConfigVersion != 3 {
		t.Fatalf("invalid config: %+v", st)
	}
}

var secretRe = regexp.MustCompile(`s3cret|bee:|sock:`)

func TestNoSecretsInLogsOrStatus(t *testing.T) {
	w := newWorld(t)
	e := newEnv(t, true)
	cfg := w.config(1)
	cfg.Upstreams = append(cfg.Upstreams, Upstream{Name: "s", Server: "socks5://" + w.socksAdr})
	set := bLogin()
	set[loginKey("s")] = `{"username":"sock","password":"s3cret-socks-pass"}`
	e.pushConfig(cfg, set, nil)
	e.call("POST", "/v1/rotate", []byte(`{"to":"b"}`), nil)
	fetch(t, e, "http://"+w.echo+"/")
	e.call("POST", "/v1/rotate", []byte(`{"to":"s"}`), nil)
	resp := e.call("GET", "/v1/status", nil, nil)
	body := readJSON(t, resp)
	sb, _ := json.Marshal(body)
	if secretRe.Match(sb) || secretRe.MatchString(e.logs.String()) {
		t.Fatalf("a secret leaked: status=%s logs=%s", sb, e.logs.String())
	}
	if strings.Contains(e.logs.String(), w.echo) {
		t.Fatal("a target address was logged")
	}
}

func TestPasswordSealRoundTrip(t *testing.T) {
	ks, _ := deriveKeys(testControlKey)
	sealed := base64.StdEncoding.EncodeToString(seal(ks.cfg, []byte("pw-123"), aadPassword))
	if pw, err := unsealPassword(ks, sealed); err != nil || pw != "pw-123" {
		t.Fatalf("unseal: %q %v", pw, err)
	}
	other := base64.StdEncoding.EncodeToString(seal(ks.cfg, []byte("pw-123"), "config|1"))
	if _, err := unsealPassword(ks, other); err == nil {
		t.Fatal("a seal for another purpose opened as a password")
	}
}

func TestIntervalRotation(t *testing.T) {
	w := newWorld(t)
	e := newEnv(t, true)
	cfg := w.config(1)
	cfg.Rotation = Rotation{Mode: "interval", EveryMinutes: 1}
	e.pushConfig(cfg, bLogin(), nil)
	st := e.k.status()
	if st.NextRotationAt == nil || st.NextRotationAt.Sub(time.Now()) > time.Minute+time.Second {
		t.Fatalf("nextRotationAt %v", st.NextRotationAt)
	}
}
