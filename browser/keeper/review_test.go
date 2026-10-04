package main

import (
	"archive/tar"
	"bytes"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func cookieBatch(n, valueLen int) []byte {
	v := strings.Repeat("v", valueLen)
	arr := make([]map[string]any, n)
	for i := range arr {
		arr[i] = map[string]any{"name": fmt.Sprintf("c%d", i), "value": v, "domain": "x.test", "path": "/"}
	}
	b, _ := json.Marshal(arr)
	return b
}

func TestCookiesTakeTheFullBatch(t *testing.T) {
	e := newEnv(t, false)
	// 4000 cookies, about 5 MiB: what tenant-api lets through.
	body := cookieBatch(4000, 1250)
	if len(body) < 5<<20-200<<10 || len(body) > 5<<20 {
		t.Fatalf("batch is %d bytes", len(body))
	}
	resp := e.call("POST", "/v1/cookies", body, nil)
	if m := readJSON(t, resp); resp.StatusCode != 200 || m["added"].(float64) != 4000 {
		t.Fatalf("cookies: %d %v", resp.StatusCode, m)
	}
	// Past the cap: refused before the launcher sees it.
	resp = e.call("POST", "/v1/cookies", cookieBatch(5000, 1400), nil)
	if resp.StatusCode != 413 {
		t.Fatalf("oversized: %d", resp.StatusCode)
	}
	// Other signed routes keep the small cap.
	resp = e.call("POST", "/v1/rotate", bytes.Repeat([]byte(" "), maxJSONBody+1), nil)
	if resp.StatusCode != 413 {
		t.Fatalf("rotate with a big body: %d", resp.StatusCode)
	}
}

func TestExportOfADamagedSnapshotNeverLooksComplete(t *testing.T) {
	e := newEnv(t, false)
	fakeProfile(t, e.profiles, "first")
	snap := readJSON(t, e.call("POST", "/v1/profile/snapshots", []byte(`{"name":"small"}`), nil))
	small := snap["id"].(string)
	b, _ := os.ReadFile(e.p.snapPath(small))
	b[len(b)-20] ^= 0xff
	os.WriteFile(e.p.snapPath(small), b, 0o600)
	// Damage in the first piece: refused outright.
	resp := e.call("POST", "/v1/profile/export", []byte(`{"snapshot":"`+small+`"}`), nil)
	if resp.StatusCode != 422 {
		t.Fatalf("damaged small snapshot exported: %d", resp.StatusCode)
	}
	resp.Body.Close()

	// Damage past the first piece: the answer has started, so the
	// connection is cut and the reader sees an error, not a clean end.
	noise := make([]byte, 2<<20)
	rand.Read(noise)
	writeFile(t, filepath.Join(e.profiles, "default", "Default", "Big"), string(noise))
	snap = readJSON(t, e.call("POST", "/v1/profile/snapshots", []byte(`{"name":"big"}`), nil))
	big := snap["id"].(string)
	b, _ = os.ReadFile(e.p.snapPath(big))
	b[len(b)-100] ^= 0xff
	os.WriteFile(e.p.snapPath(big), b, 0o600)
	resp = e.call("POST", "/v1/profile/export", []byte(`{"snapshot":"`+big+`"}`), nil)
	if resp.StatusCode != 200 {
		t.Fatalf("export: %d", resp.StatusCode)
	}
	got, err := io.ReadAll(resp.Body)
	resp.Body.Close()
	if err == nil {
		t.Fatalf("a damaged export ended cleanly after %d bytes", len(got))
	}
	// The lock was released: the profile is usable again.
	if resp := e.call("POST", "/v1/profile/snapshots", []byte(`{}`), nil); resp.StatusCode != 200 {
		t.Fatalf("snapshot after a cut export: %d", resp.StatusCode)
	}
}

func TestARouteChangeClearsTheOldExit(t *testing.T) {
	w := newWorld(t)
	e := newEnv(t, true)
	dead := "http://" + w.ip + ":1"
	cfg := w.config(1)
	cfg.Upstreams = append(cfg.Upstreams, Upstream{Name: "dead", Server: dead})
	if resp := e.pushConfig(cfg, bLogin(), nil); resp.StatusCode != 200 {
		t.Fatalf("push: %d", resp.StatusCode)
	}
	waitFor(t, "probe", func() bool { return e.k.status().ExitIP == "203.0.113.1" })
	st := readJSON(t, e.call("POST", "/v1/rotate", []byte(`{"to":"dead"}`), nil))
	if st["upstream"].(map[string]any)["name"] != "dead" {
		t.Fatalf("rotate: %v", st)
	}
	if st["exitIp"] != nil || st["country"] != nil || st["measuredAt"] != nil {
		t.Fatalf("the dead upstream shows the previous exit: %v", st)
	}
	if st["lastError"] != "upstream_unreachable" {
		t.Fatalf("lastError %v", st["lastError"])
	}
}

func TestRotationAnswersWithinItsBoundOnASlowCheck(t *testing.T) {
	w := newWorld(t)
	e := newEnv(t, true)
	var slow atomic.Bool
	_, check := listenOn(t, w.ip, http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
		if slow.Load() {
			select {
			case <-r.Context().Done():
				return
			case <-time.After(15 * time.Second):
			}
		}
		echoHandler().ServeHTTP(rw, r)
	}))
	cfg := w.config(1)
	cfg.CheckURL = "http://" + check + "/ip"
	e.pushConfig(cfg, bLogin(), nil)
	waitFor(t, "probe", func() bool { return e.k.status().ExitIP == "203.0.113.1" })
	slow.Store(true)
	start := time.Now()
	resp := e.call("POST", "/v1/rotate", []byte(`{}`), nil)
	st := readJSON(t, resp)
	if took := time.Since(start); took > e.k.rotateProbe+2*time.Second {
		t.Fatalf("rotate took %v with a %v bound", took, e.k.rotateProbe)
	}
	if resp.StatusCode != 200 || st["upstream"].(map[string]any)["name"] != "b" {
		t.Fatalf("rotate: %d %v", resp.StatusCode, st)
	}
	if e.k.rotateProbe > 25*time.Second || newKeeper(t.TempDir(), filepath.Join(t.TempDir(), "s"), false, "http://127.0.0.1:1").rotateProbe > 25*time.Second {
		t.Fatal("a rotation without a change-IP call may wait more than 25 s for the new exit")
	}
}

func TestSweepRemovesWhatARestartLeftBehind(t *testing.T) {
	e := newEnv(t, false)
	fakeProfile(t, e.profiles, "kept")
	keep := readJSON(t, e.call("POST", "/v1/profile/snapshots", []byte(`{"name":"keep"}`), nil))["id"].(string)
	meta := filepath.Join(e.profiles, ".livellm")
	leftovers := []string{
		filepath.Join(meta, "trash", "1700000000", "Default", "Preferences"),
		filepath.Join(meta, "staging-123", "Default", "Preferences"),
		filepath.Join(meta, "snapshots", "s1.llcprofile.age.partial"),
		filepath.Join(meta, "snapshots", "tmp-export-20261004t120000-abcdef.llcprofile.age"),
		filepath.Join(meta, "snapshots", "sorphan.llcprofile.age"),
	}
	for _, p := range leftovers {
		writeFile(t, p, "x")
	}
	e.p.startSweep()
	waitFor(t, "sweep", func() bool {
		if !e.p.tryLock() {
			return false
		}
		e.p.busy.Unlock()
		return true
	})
	for _, p := range leftovers {
		if _, err := os.Lstat(p); err == nil {
			t.Fatalf("left behind: %s", p)
		}
	}
	for _, p := range []string{filepath.Join(meta, "trash"), filepath.Join(meta, "staging-123")} {
		if _, err := os.Lstat(p); err == nil {
			t.Fatalf("left behind: %s", p)
		}
	}
	if _, err := os.Lstat(e.p.snapPath(keep)); err != nil {
		t.Fatal("the sweep removed a listed snapshot")
	}
	if c := readCookie(t, e.profiles); c != "cookie=kept" {
		t.Fatalf("the sweep touched the live profile: %q", c)
	}
}

func TestManifestCarriesTheBrowsersTimezoneAndLocale(t *testing.T) {
	e := newEnv(t, false)
	e.launcher.tz, e.launcher.locale = "Europe/Moscow", "ru-RU"
	fakeProfile(t, e.profiles, "tz")
	os.Remove(filepath.Join(e.profiles, "default", "Default", ".livellm-locale"))
	resp := e.call("POST", "/v1/profile/export", nil, nil)
	if resp.StatusCode != 200 {
		t.Fatalf("export: %d", resp.StatusCode)
	}
	defer resp.Body.Close()
	zr, err := newZstdReader(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	tr := tar.NewReader(zr)
	if h, err := tr.Next(); err != nil || h.Name != manifestName {
		t.Fatalf("first entry: %v %v", h, err)
	}
	var m Manifest
	if err := json.NewDecoder(tr).Decode(&m); err != nil {
		t.Fatal(err)
	}
	if m.Timezone != "Europe/Moscow" || m.Locale != "ru-RU" {
		t.Fatalf("manifest %+v", m)
	}
}

func TestAPauseTheBrowserRefusesIsBusy(t *testing.T) {
	e := newEnv(t, false)
	fakeProfile(t, e.profiles, "p")
	e.launcher.failPause.Store(true)
	resp := e.call("POST", "/v1/profile/snapshots", []byte(`{}`), nil)
	m := readJSON(t, resp)
	if resp.StatusCode != 409 || m["code"] != "browser_busy" {
		t.Fatalf("failed pause: %d %v", resp.StatusCode, m)
	}
}
