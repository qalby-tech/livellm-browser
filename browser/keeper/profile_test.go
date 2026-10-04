package main

import (
	"archive/tar"
	"bytes"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"filippo.io/age"
)

func writeFile(t *testing.T, p, content string) {
	t.Helper()
	os.MkdirAll(filepath.Dir(p), 0o700)
	if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

// fakeProfile lays out a small Chrome profile with every kind of entry.
func fakeProfile(t *testing.T, root, cookie string) {
	live := filepath.Join(root, "default")
	writeFile(t, filepath.Join(live, "Local State"), `{"os_crypt":{}}`)
	writeFile(t, filepath.Join(live, "Default", "Network", "Cookies"), "cookie="+cookie)
	writeFile(t, filepath.Join(live, "Default", "Network", "Cookies-journal"), "j")
	writeFile(t, filepath.Join(live, "Default", "Preferences"), `{"a":1}`)
	writeFile(t, filepath.Join(live, "Default", ".livellm-locale"), `{"keys":["intl.accept_languages"],"locale":"ru-RU"}`)
	writeFile(t, filepath.Join(live, "Default", "Extensions", "abcdef", "1.0_0", "manifest.json"), `{}`)
	for _, junk := range []string{
		"Default/Cache/Cache_Data/f_0001", "Default/Code Cache/js/x", "GrShaderCache/data_0",
		"Default/Service Worker/CacheStorage/x", "Default/Service Worker/ScriptCache/y",
		"Crashpad/reports/r", "BrowserMetrics-1.pma", "Default/optimization_guide_hint_cache_store/x",
		"Default/x.tmp", "Safe Browsing/s", "component_crx_cache/c", "segmentation_platform/s",
		"GraphiteDawnCache/g", "ShaderCache/s",
	} {
		writeFile(t, filepath.Join(live, filepath.FromSlash(junk)), "junk")
	}
	writeFile(t, filepath.Join(live, "Default", "Service Worker", "Database", "keep"), "sw-db")
	os.Symlink("/etc/passwd", filepath.Join(live, "Default", "evil-link"))
	os.Symlink("/etc", filepath.Join(live, "Default", "evil-dir"))
	os.Symlink("x", filepath.Join(live, "SingletonLock"))
}

func tarNames(t *testing.T, r io.Reader) []string {
	t.Helper()
	zr, err := newZstdReader(r)
	if err != nil {
		t.Fatal(err)
	}
	tr := tar.NewReader(zr)
	var names []string
	for {
		h, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		names = append(names, h.Name)
	}
	return names
}

func TestArchiveExclusionsAndSymlinks(t *testing.T) {
	root := t.TempDir()
	fakeProfile(t, root, "c1")
	var buf bytes.Buffer
	if err := writeArchive(&buf, filepath.Join(root, "default"), Manifest{ChromeMajor: 154}); err != nil {
		t.Fatal(err)
	}
	names := tarNames(t, bytes.NewReader(buf.Bytes()))
	if names[0] != manifestName {
		t.Fatalf("first entry %q", names[0])
	}
	have := map[string]bool{}
	for _, n := range names {
		have[strings.TrimSuffix(n, "/")] = true
	}
	for _, want := range []string{"profile/Local State", "profile/Default/Network/Cookies", "profile/Default/Network/Cookies-journal",
		"profile/Default/.livellm-locale", "profile/Default/Service Worker/Database/keep", "profile/Default/Extensions/abcdef/1.0_0/manifest.json"} {
		if !have[want] {
			t.Errorf("missing %s", want)
		}
	}
	for _, n := range names {
		for _, bad := range []string{"Cache_Data", "Code Cache", "GrShaderCache", "CacheStorage", "ScriptCache", "Crashpad",
			"BrowserMetrics", "optimization_guide", ".tmp", "Safe Browsing", "component_crx_cache", "segmentation_platform",
			"GraphiteDawnCache", "ShaderCache", "evil", "Singleton"} {
			if strings.Contains(n, bad) {
				t.Errorf("%s should be left out", n)
			}
		}
	}
}

func buildTar(t *testing.T, entries []tar.Header, manifest *Manifest) []byte {
	var buf bytes.Buffer
	zw, _ := newZstdWriter(&buf)
	tw := tar.NewWriter(zw)
	if manifest != nil {
		mb, _ := json.Marshal(manifest)
		tw.WriteHeader(&tar.Header{Name: manifestName, Mode: 0o600, Size: int64(len(mb)), Typeflag: tar.TypeReg})
		tw.Write(mb)
	}
	for _, h := range entries {
		h := h
		body := ""
		if h.Typeflag == tar.TypeReg {
			body = "data"
			h.Size = 4
		}
		tw.WriteHeader(&h)
		tw.Write([]byte(body))
	}
	tw.Close()
	zw.Close()
	return buf.Bytes()
}

func TestExtractRefusesTraversalAndLinks(t *testing.T) {
	m := &Manifest{Format: 1, ChromeMajor: 154}
	cases := map[string][]tar.Header{
		"dotdot":    {{Name: "profile/../x", Typeflag: tar.TypeReg}},
		"deep":      {{Name: "profile/a/../../x", Typeflag: tar.TypeReg}},
		"abs":       {{Name: "/etc/x", Typeflag: tar.TypeReg}},
		"outside":   {{Name: "other/x", Typeflag: tar.TypeReg}},
		"symlink":   {{Name: "profile/l", Typeflag: tar.TypeSymlink, Linkname: "/etc/passwd"}},
		"hardlink":  {{Name: "profile/h", Typeflag: tar.TypeLink, Linkname: "profile/a"}},
		"device":    {{Name: "profile/d", Typeflag: tar.TypeChar}},
		"backslash": {{Name: "profile/..\\x", Typeflag: tar.TypeReg}},
	}
	for name, entries := range cases {
		dst := t.TempDir()
		_, err := extractArchive(bytes.NewReader(buildTar(t, entries, m)), dst, extractOpts{runningMajor: 154})
		if ae, ok := err.(*apiErr); !ok || ae.status != 422 {
			t.Errorf("%s: %v, want 422", name, err)
		}
	}
	// no manifest = not a LiveLLM profile
	_, err := extractArchive(bytes.NewReader(buildTar(t, []tar.Header{{Name: "profile/x", Typeflag: tar.TypeReg}}, nil)), t.TempDir(), extractOpts{})
	if err != errNoManifest {
		t.Fatalf("no manifest: %v", err)
	}
	// newer Chrome: 409 unless forced
	newer := &Manifest{Format: 1, ChromeMajor: 155}
	ok := []tar.Header{{Name: "profile/Default/", Typeflag: tar.TypeDir}, {Name: "profile/Default/Prefs", Typeflag: tar.TypeReg}}
	_, err = extractArchive(bytes.NewReader(buildTar(t, ok, newer)), t.TempDir(), extractOpts{runningMajor: 154})
	if ae, isAE := err.(*apiErr); !isAE || ae.code != "profile_newer" || !strings.Contains(ae.message, "Chrome 155; this browser runs 154") {
		t.Fatalf("newer: %v", err)
	}
	dst := t.TempDir()
	if _, err := extractArchive(bytes.NewReader(buildTar(t, ok, newer)), dst, extractOpts{runningMajor: 154, force: true}); err != nil {
		t.Fatalf("forced: %v", err)
	}
	if b, _ := os.ReadFile(filepath.Join(dst, "Default", "Prefs")); string(b) != "data" {
		t.Fatal("forced import did not write")
	}
	// size cap
	_, err = extractArchive(bytes.NewReader(buildTar(t, ok, m)), t.TempDir(), extractOpts{runningMajor: 154, maxBytes: 2})
	if err != errTooBig {
		t.Fatalf("cap: %v", err)
	}
}

func readCookie(t *testing.T, root string) string {
	b, err := os.ReadFile(filepath.Join(root, "default", "Default", "Network", "Cookies"))
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestSnapshotRestoreAndTamper(t *testing.T) {
	e := newEnv(t, false)
	fakeProfile(t, e.profiles, "first")
	resp := e.call("POST", "/v1/profile/snapshots", []byte(`{"name":"s1"}`), nil)
	snap := readJSON(t, resp)
	if resp.StatusCode != 200 {
		t.Fatalf("snapshot: %d %v", resp.StatusCode, snap)
	}
	id := snap["id"].(string)
	if e.launcher.pauses.Load() != 1 || e.launcher.resumes.Load() != 1 {
		t.Fatalf("pause/resume %d/%d", e.launcher.pauses.Load(), e.launcher.resumes.Load())
	}
	// sealed on disk: age ciphertext, the cookie is not readable
	raw, _ := os.ReadFile(e.p.snapPath(id))
	if !bytes.HasPrefix(raw, []byte(ageMagic)) || bytes.Contains(raw, []byte("first")) {
		t.Fatal("snapshot is not sealed")
	}
	writeFile(t, filepath.Join(e.profiles, "default", "Default", "Network", "Cookies"), "cookie=other")
	resp = e.call("POST", "/v1/profile/snapshots/"+id+"/restore", []byte(`{"keepCurrent":true}`), nil)
	if resp.StatusCode != 200 {
		t.Fatalf("restore: %d %v", resp.StatusCode, readJSON(t, resp))
	}
	if c := readCookie(t, e.profiles); c != "cookie=first" {
		t.Fatalf("after restore: %q", c)
	}
	if _, err := os.Lstat(filepath.Join(e.profiles, "default", "Default", "evil-link")); err == nil {
		t.Fatal("a symlink came back through a snapshot")
	}
	// keepCurrent made a before-… snapshot
	info := readJSON(t, e.call("GET", "/v1/profile", nil, nil))
	snaps := info["snapshots"].([]any)
	if len(snaps) != 2 || !strings.HasPrefix(snaps[1].(map[string]any)["name"].(string), "before-") {
		t.Fatalf("snapshots %v", snaps)
	}
	// tampered: garbage → refused, the live profile untouched
	os.WriteFile(e.p.snapPath(id), []byte("garbage garbage garbage"), 0o600)
	resp = e.call("POST", "/v1/profile/snapshots/"+id+"/restore", nil, nil)
	if resp.StatusCode != 422 {
		t.Fatalf("tampered: %d", resp.StatusCode)
	}
	// flipped bytes inside the payload → refused
	other := snaps[1].(map[string]any)["id"].(string)
	b, _ := os.ReadFile(e.p.snapPath(other))
	b[len(b)-20] ^= 0xff
	os.WriteFile(e.p.snapPath(other), b, 0o600)
	resp = e.call("POST", "/v1/profile/snapshots/"+other+"/restore", nil, nil)
	if resp.StatusCode != 422 {
		t.Fatalf("flipped: %d", resp.StatusCode)
	}
	if c := readCookie(t, e.profiles); c != "cookie=first" {
		t.Fatalf("a refused restore changed the profile: %q", c)
	}
	// delete
	if resp := e.call("DELETE", "/v1/profile/snapshots/"+id, nil, nil); resp.StatusCode != 200 {
		t.Fatalf("delete: %d", resp.StatusCode)
	}
	if resp := e.call("DELETE", "/v1/profile/snapshots/"+id, nil, nil); resp.StatusCode != 404 {
		t.Fatalf("delete again: %d", resp.StatusCode)
	}
}

func TestSnapshotUnderAnotherKeyAndNoRoom(t *testing.T) {
	e := newEnv(t, false)
	fakeProfile(t, e.profiles, "k")
	snap := readJSON(t, e.call("POST", "/v1/profile/snapshots", []byte(`{}`), nil))
	id := snap["id"].(string)
	// the platform key rotated
	nk, _ := deriveKeys(strings.Repeat("cd", 32))
	e.k.keys.Store(nk)
	ks, _ := deriveKeys(strings.Repeat("cd", 32))
	req, _ := http.NewRequest("POST", e.api.URL+"/v1/profile/snapshots/"+id+"/restore", nil)
	signRequest(req, ks.auth, nil, false, timeNow(), randNonce())
	resp, _ := http.DefaultClient.Do(req)
	if m := readJSON(t, resp); resp.StatusCode != 409 || m["code"] != "snapshot_key_changed" {
		t.Fatalf("old key: %d %v", resp.StatusCode, m)
	}
	// no room
	e2 := newEnv(t, false)
	fakeProfile(t, e2.profiles, "k")
	e2.p.freeBytesFn = func(string) int64 { return 1 << 20 }
	resp = e2.call("POST", "/v1/profile/snapshots", []byte(`{}`), nil)
	if m := readJSON(t, resp); resp.StatusCode != 507 || !strings.Contains(m["message"].(string), "Not enough room") {
		t.Fatalf("no room: %d %v", resp.StatusCode, m)
	}
	if e2.launcher.pauses.Load() != 0 {
		t.Fatal("paused although there was no room")
	}
}

func sealedPassword(pw string) string {
	ks, _ := deriveKeys(testControlKey)
	return base64.StdEncoding.EncodeToString(seal(ks.cfg, []byte(pw), aadPassword))
}

func TestExportImportRoundTrip(t *testing.T) {
	e := newEnv(t, false)
	fakeProfile(t, e.profiles, "moved")
	body, _ := json.Marshal(map[string]string{"passwordSealed": sealedPassword("correct horse")})
	resp := e.call("POST", "/v1/profile/export", body, nil)
	if resp.StatusCode != 200 || resp.Header.Get("X-Profile-Extension") != ".llcprofile.age" {
		t.Fatalf("export: %d", resp.StatusCode)
	}
	enc, _ := io.ReadAll(resp.Body)
	if !bytes.HasPrefix(enc, []byte(ageMagic)) {
		t.Fatal("password export is not age")
	}
	// opens with a stock age scrypt identity
	id, _ := age.NewScryptIdentity("correct horse")
	r, err := age.Decrypt(bytes.NewReader(enc), id)
	if err != nil {
		t.Fatalf("stock age: %v", err)
	}
	names := tarNames(t, r)
	if names[0] != manifestName {
		t.Fatalf("first %q", names[0])
	}
	// the temporary snapshot is gone
	if ents, _ := os.ReadDir(e.p.snapDir()); len(ents) != 0 {
		t.Fatalf("export left %d files", len(ents))
	}

	// import into another browser
	e2 := newEnv(t, false)
	fakeProfile(t, e2.profiles, "before")
	resp = e2.call("POST", "/v1/profile/import", enc, nil)
	if m := readJSON(t, resp); resp.StatusCode != 422 || m["code"] != "password_required" {
		t.Fatalf("no password: %d %v", resp.StatusCode, m)
	}
	resp = e2.call("POST", "/v1/profile/import", enc, map[string]string{"X-Profile-Password-Sealed": sealedPassword("wrong")})
	if m := readJSON(t, resp); resp.StatusCode != 422 || m["code"] != "wrong_password" {
		t.Fatalf("wrong password: %d %v", resp.StatusCode, m)
	}
	resp = e2.call("POST", "/v1/profile/import", enc, map[string]string{"X-Profile-Password-Sealed": sealedPassword("correct horse")})
	if resp.StatusCode != 200 {
		t.Fatalf("import: %d %v", resp.StatusCode, readJSON(t, resp))
	}
	if c := readCookie(t, e2.profiles); c != "cookie=moved" {
		t.Fatalf("imported cookie %q", c)
	}

	// a plain export: zstd tar, manifest first, no caches
	resp = e.call("POST", "/v1/profile/export", []byte(`{}`), nil)
	plain, _ := io.ReadAll(resp.Body)
	names = tarNames(t, bytes.NewReader(plain))
	if names[0] != manifestName {
		t.Fatal("manifest not first")
	}
	for _, n := range names {
		if strings.Contains(n, "Cache") && !strings.Contains(n, "Service Worker/Database") {
			t.Fatalf("plain export has %s", n)
		}
	}
	// from a snapshot
	snap := readJSON(t, e.call("POST", "/v1/profile/snapshots", []byte(`{"name":"x"}`), nil))
	sb, _ := json.Marshal(map[string]string{"snapshot": snap["id"].(string)})
	resp = e.call("POST", "/v1/profile/export", sb, nil)
	if resp.StatusCode != 200 {
		t.Fatalf("export snapshot: %d", resp.StatusCode)
	}
	// over the archive cap → 413
	e3 := newEnv(t, false)
	e3.p.maxArchive = 100
	resp = e3.call("POST", "/v1/profile/import", plain, nil)
	if resp.StatusCode != 413 {
		t.Fatalf("over the cap: %d", resp.StatusCode)
	}
}

func TestImportWorkFactorCap(t *testing.T) {
	e := newEnv(t, false)
	var buf bytes.Buffer
	rec, _ := age.NewScryptRecipient("pw")
	rec.SetWorkFactor(17)
	w, _ := age.Encrypt(&buf, rec)
	w.Write(buildTar(t, nil, &Manifest{Format: 1}))
	w.Close()
	resp := e.call("POST", "/v1/profile/import", buf.Bytes(), map[string]string{"X-Profile-Password-Sealed": sealedPassword("pw")})
	if m := readJSON(t, resp); resp.StatusCode != 422 || m["code"] != "password_too_strong" {
		t.Fatalf("wf 17: %d %v", resp.StatusCode, m)
	}
}

func TestAgeIdentityFromKey(t *testing.T) {
	a, _ := deriveKeys(testControlKey)
	b, _ := deriveKeys(testControlKey)
	c, _ := deriveKeys(strings.Repeat("ef", 32))
	if a.snap.Recipient().String() != b.snap.Recipient().String() {
		t.Fatal("identity not deterministic")
	}
	var buf bytes.Buffer
	w, _ := age.Encrypt(&buf, a.snap.Recipient())
	w.Write([]byte("hello"))
	w.Close()
	if r, err := age.Decrypt(bytes.NewReader(buf.Bytes()), b.snap); err != nil {
		t.Fatal(err)
	} else if out, _ := io.ReadAll(r); string(out) != "hello" {
		t.Fatal("round trip")
	}
	if _, err := age.Decrypt(bytes.NewReader(buf.Bytes()), c.snap); err == nil {
		t.Fatal("another key opened it")
	}
}

func TestCookiesForwardedToTheLauncher(t *testing.T) {
	e := newEnv(t, false)
	resp := e.call("POST", "/v1/cookies", []byte(`[{"name":"a","value":"1","domain":"x.test","path":"/"},{"name":"b","value":"2","domain":"x.test","path":"/"}]`), nil)
	if m := readJSON(t, resp); resp.StatusCode != 200 || m["added"].(float64) != 2 {
		t.Fatalf("cookies: %d %v", resp.StatusCode, m)
	}
	if e.launcher.cookies.Load() != 2 {
		t.Fatal("launcher did not get them")
	}
}

func TestExcludedMatcher(t *testing.T) {
	var got []string
	for _, p := range []string{"Default/Cache", "Default/Network", "Default/Service Worker/CacheStorage", "Local State", "SingletonCookie", "x.tmp"} {
		if excluded(p, !strings.Contains(p, ".") && p != "SingletonCookie") {
			got = append(got, p)
		}
	}
	sort.Strings(got)
	want := []string{"Default/Cache", "Default/Service Worker/CacheStorage", "SingletonCookie", "x.tmp"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("excluded %v", got)
	}
}

// tenant-api's export form: the settings sealed to the request's own nonce.
func TestExportSealedEnvelope(t *testing.T) {
	e := newEnv(t, false)
	fakeProfile(t, e.profiles, "env")
	ks, _ := deriveKeys(testControlKey)
	nonce := randNonce()
	plain, _ := json.Marshal(map[string]string{"password": "pw-env"})
	body := seal(ks.cfg, plain, "export|"+nonce)
	req, _ := http.NewRequest("POST", e.api.URL+"/v1/profile/export", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/octet-stream")
	signRequest(req, ks.auth, body, false, timeNow(), nonce)
	resp, err := http.DefaultClient.Do(req)
	if err != nil || resp.StatusCode != 200 || resp.Header.Get("X-Profile-Extension") != ".llcprofile.age" {
		t.Fatalf("sealed export: %v %v", err, resp)
	}
	enc, _ := io.ReadAll(resp.Body)
	id, _ := age.NewScryptIdentity("pw-env")
	if _, err := age.Decrypt(bytes.NewReader(enc), id); err != nil {
		t.Fatalf("export password: %v", err)
	}
	// the same envelope under another request's nonce does not open
	n2 := randNonce()
	req2, _ := http.NewRequest("POST", e.api.URL+"/v1/profile/export", bytes.NewReader(body))
	signRequest(req2, ks.auth, body, false, timeNow(), n2)
	resp2, _ := http.DefaultClient.Do(req2)
	if resp2.StatusCode != 400 {
		t.Fatalf("envelope replayed into another request: %d", resp2.StatusCode)
	}
}
