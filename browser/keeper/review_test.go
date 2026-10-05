package main

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
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

// ── A Chrome browser's sidecar answers exactly as before the Camoufox engine ──

// goldenChrome was recorded by goldenChromeScenario against the keeper of
// de91007 (the last commit before the Camoufox engine), KEEPER_ENGINE unset.
const goldenChrome = `{
  "cookies": "200 {\"added\":2}\n",
  "cookies-bad": "400 {\"code\":\"bad_request\"}\n",
  "export-names": "livellm-profile.json\nprofile/Default/\nprofile/Default/.livellm-locale\nprofile/Default/Extensions/\nprofile/Default/Extensions/abcdef/\nprofile/Default/Extensions/abcdef/1.0_0/\nprofile/Default/Extensions/abcdef/1.0_0/manifest.json\nprofile/Default/Network/\nprofile/Default/Network/Cookies\nprofile/Default/Network/Cookies-journal\nprofile/Default/Preferences\nprofile/Default/Service Worker/\nprofile/Default/Service Worker/Database/\nprofile/Default/Service Worker/Database/keep\nprofile/Local State",
  "export-status": "200 .llcprofile",
  "import": "200 {\"chromeVersion\":\"154.0.8037.57\",\"imported\":true,\"sizeBytes\":94}\n",
  "import-format2": "422 {\"code\":\"not_livellm_profile\",\"message\":\"Only profiles exported from LiveLLM browsers can be imported. Import cookies instead.\"}\n",
  "import-newer": "409 {\"code\":\"profile_newer\",\"message\":\"This profile is from Chrome 155; this browser runs 154. Import anyway?\"}\n",
  "import-newer-force": "200 {\"chromeVersion\":\"155.0.1.2\",\"imported\":true,\"sizeBytes\":0}\n",
  "import-no-manifest": "422 {\"code\":\"not_livellm_profile\",\"message\":\"Only profiles exported from LiveLLM browsers can be imported. Import cookies instead.\"}\n",
  "import-traversal": "422 {\"code\":\"invalid_profile\",\"message\":\"This file is not a valid browser profile.\"}\n",
  "manifest": "{\"format\":1,\"chromeVersion\":\"154.0.8037.57\",\"chromeMajor\":154,\"imageVersion\":\"2.3.0\",\"createdAt\":\"X\",\"locale\":\"ru-RU\",\"extensions\":[\"abcdef\"],\"sizeBytes\":94}",
  "profile": "200 {\"sizeBytes\":94,\"chromeVersion\":\"154.0.8037.57\",\"snapshots\":[],\"freeBytes\":10737418240}\n",
  "profile-with-snapshot": "200 {\"sizeBytes\":94,\"chromeVersion\":\"154.0.8037.57\",\"snapshots\":[{\"id\":\"X\",\"name\":\"g1\",\"createdAt\":\"X\",\"sizeBytes\":94,\"chromeVersion\":\"154.0.8037.57\"}],\"freeBytes\":10737418240}\n",
  "snapshot": "200 {\"id\":\"X\",\"name\":\"g1\",\"createdAt\":\"X\",\"sizeBytes\":94,\"chromeVersion\":\"154.0.8037.57\"}\n",
  "status": "200 {\"configVersion\":0,\"mode\":\"direct\",\"relay\":false,\"upstream\":null,\"measuredAt\":null,\"rotatedAt\":null,\"generation\":0,\"nextRotationAt\":null,\"chromeVersion\":\"154.0.8037.57\"}\n"
}`

// goldenChromeScenario drives the keeper as a Chrome browser's sidecar
// (KEEPER_ENGINE unset) through every answer the Camoufox work must leave
// byte-identical, and returns "<name>" -> "<status> <body>" with the
// time- and random-dependent values replaced.
func goldenChromeScenario(t *testing.T) map[string]string {
	t.Helper()
	out := map[string]string{}
	norm := regexp.MustCompile(`"(id|createdAt|CreatedAt)":"[^"]*"`)
	normName := regexp.MustCompile(`"name":"(s\d{8}t\d{6}-[0-9a-f]{6}|before-[0-9-]+)"`)
	rec := func(name string, resp *http.Response) []byte {
		t.Helper()
		b, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		s := norm.ReplaceAllString(string(b), `"$1":"X"`)
		s = normName.ReplaceAllString(s, `"name":"X"`)
		out[name] = fmt.Sprintf("%d %s", resp.StatusCode, s)
		return b
	}
	e := newEnv(t, false)
	e.k.refreshChromeVersion(context.Background())
	rec("status", e.call("GET", "/v1/status", nil, nil))
	fakeProfile(t, e.profiles, "golden")
	rec("profile", e.call("GET", "/v1/profile", nil, nil))
	rec("snapshot", e.call("POST", "/v1/profile/snapshots", []byte(`{"name":"g1"}`), nil))
	rec("profile-with-snapshot", e.call("GET", "/v1/profile", nil, nil))

	resp := e.call("POST", "/v1/profile/export", []byte(`{}`), nil)
	plain, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	out["export-status"] = fmt.Sprintf("%d %s", resp.StatusCode, resp.Header.Get("X-Profile-Extension"))
	zr, _ := newZstdReader(bytes.NewReader(plain))
	tr := tar.NewReader(zr)
	var names []string
	for i := 0; ; i++ {
		h, err := tr.Next()
		if err != nil {
			break
		}
		names = append(names, h.Name)
		if i == 0 {
			mb, _ := io.ReadAll(tr)
			out["manifest"] = norm.ReplaceAllString(string(mb), `"$1":"X"`)
		}
	}
	out["export-names"] = strings.Join(names, "\n")

	e2 := newEnv(t, false)
	fakeProfile(t, e2.profiles, "before")
	rec("import", e2.call("POST", "/v1/profile/import", plain, nil))

	m2 := &Manifest{Format: 2, ChromeMajor: 154}
	rec("import-format2", e2.call("POST", "/v1/profile/import", buildTar(t, nil, m2), nil))
	rec("import-no-manifest", e2.call("POST", "/v1/profile/import", buildTar(t, []tar.Header{{Name: "profile/a", Typeflag: tar.TypeReg}}, nil), nil))
	newer := &Manifest{Format: 1, ChromeVersion: "155.0.1.2", ChromeMajor: 155}
	rec("import-newer", e2.call("POST", "/v1/profile/import", buildTar(t, []tar.Header{{Name: "profile/Default/x", Typeflag: tar.TypeReg}}, newer), nil))
	rec("import-newer-force", e2.call("POST", "/v1/profile/import?force=1", buildTar(t, []tar.Header{{Name: "profile/Default/x", Typeflag: tar.TypeReg}}, newer), nil))
	rec("import-traversal", e2.call("POST", "/v1/profile/import", buildTar(t, []tar.Header{{Name: "profile/../x", Typeflag: tar.TypeReg}}, &Manifest{Format: 1}), nil))
	rec("cookies", e.call("POST", "/v1/cookies", []byte(`[{"name":"a","value":"1","domain":"x.test","path":"/"},{"name":"b","value":"2","domain":"x.test","path":"/"}]`), nil))
	rec("cookies-bad", e.call("POST", "/v1/cookies", []byte(`{}`), nil))
	return out
}

func TestChromeAnswersAreByteIdenticalToBeforeCamoufox(t *testing.T) {
	var want map[string]string
	if err := json.Unmarshal([]byte(goldenChrome), &want); err != nil {
		t.Fatal(err)
	}
	got := goldenChromeScenario(t)
	if len(got) != len(want) {
		t.Errorf("answers: got %d, want %d", len(got), len(want))
	}
	for k, w := range want {
		if got[k] != w {
			t.Errorf("%s changed:\n got: %q\nwant: %q", k, got[k], w)
		}
	}
}

// ── Camoufox (KEEPER_ENGINE=camoufox) ──

// cfLauncher is a Camoufox launcher's pod-local API.
type cfLauncher struct {
	version   string
	major     int
	cookies   atomic.Int64
	dropEvery int // the launcher drops every n-th cookie (Firefox refusing it)
	srv       *httptest.Server
}

func useCamoufox(t *testing.T, e *env) *cfLauncher {
	t.Helper()
	cl := &cfLauncher{version: "156.0.1-beta.34", major: 156}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /version", func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{
			"engine": "camoufox", "browserVersion": cl.version, "browserMajor": cl.major, "playwright": "1.62.0",
			"image": "camoufox-1.0.0", "timezone": "Europe/Moscow", "locale": "ru-RU", "paused": false,
		})
	})
	mux.HandleFunc("POST /browsers/default/pause", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte(`{"status":"paused"}`)) })
	mux.HandleFunc("POST /browsers/default/resume", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte(`{"status":"ok"}`)) })
	mux.HandleFunc("POST /browsers/default/cookies", func(w http.ResponseWriter, r *http.Request) {
		var arr []any
		json.NewDecoder(r.Body).Decode(&arr)
		dropped := 0
		if cl.dropEvery > 0 {
			dropped = len(arr) / cl.dropEvery
		}
		cl.cookies.Add(int64(len(arr) - dropped))
		json.NewEncoder(w).Encode(map[string]int{"added": len(arr) - dropped, "dropped": dropped})
	})
	cl.srv = httptest.NewServer(mux)
	t.Cleanup(cl.srv.Close)
	e.k.launcher = newLauncherClient(cl.srv.URL)
	e.k.engine = engineCamoufox
	return cl
}

// firefoxProfile lays out a small Camoufox profile (profiles/default is the
// Firefox profile root) with what an archive keeps and what it leaves out.
func firefoxProfile(t *testing.T, root, cookie string) {
	live := filepath.Join(root, "default")
	writeFile(t, filepath.Join(live, "cookies.sqlite"), "cookie="+cookie)
	writeFile(t, filepath.Join(live, "prefs.js"), `user_pref("a", 1);`)
	writeFile(t, filepath.Join(live, "livellm-identity.json"), `{"format":1}`)
	writeFile(t, filepath.Join(live, ".livellm-session-cookies.json"), `{"version":1}`)
	writeFile(t, filepath.Join(live, "storage", "default", "https+++a.test", "idb", "1.sqlite"), "idb")
	writeFile(t, filepath.Join(live, "storage", "permanent", "chrome", "x"), "p")
	writeFile(t, filepath.Join(live, "compatibility.ini"), "[Compatibility]\nLastVersion=156.0.1_20261003/20261003\n")
	for _, junk := range []string{
		"cache2/entries/AB", "startupCache/startupCache.8.little", "thumbnails/t.png", "shader-cache/s",
		"crashes/store.json.mozlz4", "minidumps/m.dmp", "datareporting/state.json", "saved-telemetry-pings/p",
		"safebrowsing/google4/x", "safebrowsing-updating/y", "storage/temporary/https+++b.test/x",
		"storage/to-be-removed/z", ".parentlock", "x.tmp", "storage/default/https+++a.test/y.tmp",
		downgradeMarker,
	} {
		writeFile(t, filepath.Join(live, filepath.FromSlash(junk)), "junk")
	}
	os.Symlink("127.0.0.1:+1234", filepath.Join(live, "lock"))
	os.Symlink("/etc/passwd", filepath.Join(live, "evil-link"))
}

func exportPlain(t *testing.T, e *env) []byte {
	t.Helper()
	resp := e.call("POST", "/v1/profile/export", []byte(`{}`), nil)
	b, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("export: %d %s", resp.StatusCode, b)
	}
	return b
}

func firstManifest(t *testing.T, archive []byte) string {
	t.Helper()
	zr, err := newZstdReader(bytes.NewReader(archive))
	if err != nil {
		t.Fatal(err)
	}
	tr := tar.NewReader(zr)
	h, err := tr.Next()
	if err != nil || h.Name != manifestName {
		t.Fatalf("first entry %v %v", h, err)
	}
	b, _ := io.ReadAll(tr)
	return string(b)
}

func TestCamoufoxArchiveIsFormat2WithoutFirefoxCaches(t *testing.T) {
	e := newEnv(t, false)
	useCamoufox(t, e)
	firefoxProfile(t, e.profiles, "cf")
	archive := exportPlain(t, e)
	names := tarNames(t, bytes.NewReader(archive))
	if names[0] != manifestName {
		t.Fatalf("manifest not first: %v", names)
	}
	want := map[string]bool{
		"profile/cookies.sqlite": true, "profile/prefs.js": true, "profile/livellm-identity.json": true,
		"profile/.livellm-session-cookies.json": true, "profile/storage/default/https+++a.test/idb/1.sqlite": true,
		"profile/storage/permanent/chrome/x": true, "profile/compatibility.ini": true,
	}
	for _, n := range names[1:] {
		if strings.HasSuffix(n, "/") {
			continue
		}
		if !want[n] {
			t.Errorf("archive has %s", n)
		}
		delete(want, n)
	}
	for n := range want {
		t.Errorf("archive lacks %s", n)
	}
	for _, n := range names {
		for _, dir := range []string{"cache2", "startupCache", "thumbnails", "shader-cache", "crashes", "minidumps", "datareporting", "saved-telemetry-pings", "safebrowsing", "storage/temporary", "storage/to-be-removed"} {
			if strings.HasPrefix(n, "profile/"+dir) {
				t.Errorf("archive has %s", n)
			}
		}
	}
	m := regexp.MustCompile(`"createdAt":"[^"]+"`).ReplaceAllString(firstManifest(t, archive), `"createdAt":"X"`)
	wantM := `{"format":2,"engine":"camoufox","browserVersion":"156.0.1-beta.34","browserMajor":156,"imageVersion":"camoufox-1.0.0","createdAt":"X","locale":"ru-RU","timezone":"Europe/Moscow","extensions":[],"sizeBytes":` + regexp.MustCompile(`"sizeBytes":(\d+)`).FindStringSubmatch(m)[1] + `}`
	if m != wantM {
		t.Fatalf("manifest\n got %s\nwant %s", m, wantM)
	}
}

func TestCamoufoxStatusProfileAndSnapshotAnswers(t *testing.T) {
	e := newEnv(t, false)
	useCamoufox(t, e)
	e.k.refreshChromeVersion(context.Background())
	st := readJSON(t, e.call("GET", "/v1/status", nil, nil))
	if st["engine"] != "camoufox" || st["browserVersion"] != "156.0.1-beta.34" || st["playwright"] != "1.62.0" {
		t.Fatalf("status %v", st)
	}
	if _, ok := st["chromeVersion"]; ok {
		t.Fatalf("status has chromeVersion: %v", st)
	}
	firefoxProfile(t, e.profiles, "cf")
	snap := readJSON(t, e.call("POST", "/v1/profile/snapshots", []byte(`{"name":"one"}`), nil))
	if snap["engine"] != "camoufox" || snap["browserVersion"] != "156.0.1-beta.34" {
		t.Fatalf("snapshot %v", snap)
	}
	if _, ok := snap["chromeVersion"]; ok {
		t.Fatalf("snapshot has chromeVersion: %v", snap)
	}
	info := readJSON(t, e.call("GET", "/v1/profile", nil, nil))
	if info["engine"] != "camoufox" || info["browserVersion"] != "156.0.1-beta.34" {
		t.Fatalf("profile %v", info)
	}
	if _, ok := info["chromeVersion"]; ok {
		t.Fatalf("profile has chromeVersion: %v", info)
	}
	snaps := info["snapshots"].([]any)
	if len(snaps) != 1 || snaps[0].(map[string]any)["engine"] != "camoufox" {
		t.Fatalf("snapshots %v", snaps)
	}
	// a snapshot restores (format 2 round trip through the sealed store)
	writeFile(t, filepath.Join(e.profiles, "default", "cookies.sqlite"), "cookie=later")
	id := snap["id"].(string)
	if resp := e.call("POST", "/v1/profile/snapshots/"+id+"/restore", nil, nil); resp.StatusCode != 200 {
		t.Fatalf("restore: %d %v", resp.StatusCode, readJSON(t, resp))
	}
	if b, _ := os.ReadFile(filepath.Join(e.profiles, "default", "cookies.sqlite")); string(b) != "cookie=cf" {
		t.Fatalf("after restore %q", b)
	}
}

func TestCamoufoxImportRules(t *testing.T) {
	src := newEnv(t, false)
	useCamoufox(t, src)
	firefoxProfile(t, src.profiles, "moved")
	archive := exportPlain(t, src)

	dst := newEnv(t, false)
	useCamoufox(t, dst)
	firefoxProfile(t, dst.profiles, "before")
	live := filepath.Join(dst.profiles, "default")

	// same version: imported, identity and IndexedDB carried, no marker
	resp := dst.call("POST", "/v1/profile/import", archive, nil)
	m := readJSON(t, resp)
	if resp.StatusCode != 200 || m["engine"] != "camoufox" || m["browserVersion"] != "156.0.1-beta.34" || m["imported"] != true {
		t.Fatalf("import: %d %v", resp.StatusCode, m)
	}
	if _, ok := m["chromeVersion"]; ok {
		t.Fatalf("import answer has chromeVersion: %v", m)
	}
	for _, f := range []string{"livellm-identity.json", "storage/default/https+++a.test/idb/1.sqlite"} {
		if _, err := os.Stat(filepath.Join(live, filepath.FromSlash(f))); err != nil {
			t.Errorf("not imported: %s", f)
		}
	}
	if _, err := os.Stat(filepath.Join(live, downgradeMarker)); err == nil {
		t.Fatal("a same-version import left the downgrade marker")
	}

	// a Chrome (format 1) archive is refused by engine
	chromeEnv := newEnv(t, false)
	fakeProfile(t, chromeEnv.profiles, "chrome")
	chromeArchive := exportPlain(t, chromeEnv)
	resp = dst.call("POST", "/v1/profile/import", chromeArchive, nil)
	if m := readJSON(t, resp); resp.StatusCode != 422 || m["code"] != "profile_engine" ||
		m["message"] != "This profile is from a Chrome browser; this browser runs Camoufox. Profiles move only between browsers of one engine — import its cookies instead." {
		t.Fatalf("chrome archive: %d %v", resp.StatusCode, m)
	}
	// ... and a Chrome browser refuses a Camoufox archive as not its own
	resp = chromeEnv.call("POST", "/v1/profile/import", archive, nil)
	if m := readJSON(t, resp); resp.StatusCode != 422 || m["code"] != "not_livellm_profile" {
		t.Fatalf("camoufox archive into chrome: %d %v", resp.StatusCode, m)
	}

	// a newer Camoufox (major, or same major with a newer build): 409, then force
	for _, newer := range []string{"157.0-beta.1", "156.0.1-beta.35", "156.0.2-beta.1"} {
		nums, _ := camoufoxVersion(newer)
		hi := newEnv(t, false)
		hl := useCamoufox(t, hi)
		hl.version, hl.major = newer, nums[0]
		firefoxProfile(t, hi.profiles, "newer")
		newerArchive := exportPlain(t, hi)

		resp = dst.call("POST", "/v1/profile/import", newerArchive, nil)
		want := "This profile is from Camoufox " + newer + "; this browser runs 156.0.1-beta.34. Import anyway?"
		if m := readJSON(t, resp); resp.StatusCode != 409 || m["code"] != "profile_newer" || m["message"] != want {
			t.Fatalf("newer %s: %d %v", newer, resp.StatusCode, m)
		}
		resp = dst.call("POST", "/v1/profile/import?force=1", newerArchive, nil)
		if resp.StatusCode != 200 {
			t.Fatalf("forced %s: %d %v", newer, resp.StatusCode, readJSON(t, resp))
		}
		resp.Body.Close()
		if b, err := os.ReadFile(filepath.Join(live, downgradeMarker)); err != nil || strings.TrimSpace(string(b)) != newer {
			t.Fatalf("forced %s: marker %q %v", newer, b, err)
		}
		os.Remove(filepath.Join(live, downgradeMarker))
	}

	// an older Camoufox is imported without asking
	lo := newEnv(t, false)
	ll := useCamoufox(t, lo)
	ll.version, ll.major = "156.0.1-beta.30", 156
	firefoxProfile(t, lo.profiles, "older")
	resp = dst.call("POST", "/v1/profile/import", exportPlain(t, lo), nil)
	if resp.StatusCode != 200 {
		t.Fatalf("older: %d %v", resp.StatusCode, readJSON(t, resp))
	}
	resp.Body.Close()

	// an archive can't plant the downgrade marker itself
	planted := buildTar(t, []tar.Header{{Name: "profile/" + downgradeMarker, Typeflag: tar.TypeReg}, {Name: "profile/prefs.js", Typeflag: tar.TypeReg}},
		&Manifest{Format: 2, Engine: "camoufox", BrowserVersion: "156.0.1-beta.34", BrowserMajor: 156})
	if resp := dst.call("POST", "/v1/profile/import", planted, nil); resp.StatusCode != 200 {
		t.Fatalf("planted: %d %v", resp.StatusCode, readJSON(t, resp))
	}
	if _, err := os.Stat(filepath.Join(live, downgradeMarker)); err == nil {
		t.Fatal("the archive planted the downgrade marker")
	}
	// format 2 of another engine, or no manifest: not a LiveLLM profile
	for _, bad := range [][]byte{
		buildTar(t, nil, &Manifest{Format: 2, Engine: "other"}),
		buildTar(t, []tar.Header{{Name: "profile/a", Typeflag: tar.TypeReg}}, nil),
	} {
		resp := dst.call("POST", "/v1/profile/import", bad, nil)
		if m := readJSON(t, resp); resp.StatusCode != 422 || m["code"] != "not_livellm_profile" {
			t.Fatalf("bad archive: %d %v", resp.StatusCode, m)
		}
	}
	// traversal is refused as for Chrome
	trav := buildTar(t, []tar.Header{{Name: "profile/../x", Typeflag: tar.TypeReg}}, &Manifest{Format: 2, Engine: "camoufox", BrowserVersion: "156.0.1-beta.34"})
	if m := readJSON(t, dst.call("POST", "/v1/profile/import", trav, nil)); m["code"] != "invalid_profile" {
		t.Fatalf("traversal: %v", m)
	}
}

func TestCamoufoxCookiesAnswerAddedAndDropped(t *testing.T) {
	e := newEnv(t, false)
	cl := useCamoufox(t, e)
	cl.dropEvery = 2
	resp := e.call("POST", "/v1/cookies", []byte(`[{"name":"a","value":"1","domain":"x.test","path":"/"},{"name":"b","value":"2","domain":"x.test","path":"/","sameSite":"None"}]`), nil)
	b, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 || string(b) != "{\"added\":1,\"dropped\":1}\n" {
		t.Fatalf("cookies: %d %s", resp.StatusCode, b)
	}
}

func TestCamoufoxVersionOrder(t *testing.T) {
	for _, c := range []struct {
		a, r  string
		newer bool
	}{
		{"156.0.1-beta.34", "156.0.1-beta.34", false},
		{"156.0.1-beta.35", "156.0.1-beta.34", true},
		{"156.0.1-beta.33", "156.0.1-beta.34", false},
		{"157.0-beta.1", "156.0.1-beta.34", true},
		{"155.0-beta.99", "156.0.1-beta.34", false},
		{"156.0.2-beta.1", "156.0.1-beta.34", true},
		{"156.0.1", "156.0.1-beta.34", false},
	} {
		if got := camoufoxNewer(0, c.a, 0, c.r); got != c.newer {
			t.Errorf("%s vs %s: newer=%v", c.a, c.r, got)
		}
	}
}

func TestEngineFromEnv(t *testing.T) {
	for v, want := range map[string]string{"": "", "chrome": "", "camoufox": engineCamoufox, " camoufox ": engineCamoufox, "firefox": ""} {
		if got := engineFromEnv(v); got != want {
			t.Errorf("%q -> %q", v, got)
		}
	}
}
