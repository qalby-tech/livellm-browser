package main

import (
	"archive/tar"
	"bytes"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// archiveText unpacks an archive and returns every file's content joined.
func archiveText(t *testing.T, b []byte) string {
	t.Helper()
	zr, err := newZstdReader(bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	tr := tar.NewReader(zr)
	var sb strings.Builder
	for {
		h, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		c, _ := io.ReadAll(tr)
		sb.WriteString(h.Name + "=" + string(c) + "\n")
	}
	return sb.String()
}

// outsideSecrets makes a directory outside the volume shaped like the
// sidecar's own: state.json and ..data/control-key.
func outsideSecrets(t *testing.T) string {
	d := t.TempDir()
	writeFile(t, filepath.Join(d, "state.json"), "SECRET-STATE")
	writeFile(t, filepath.Join(d, "..data", "control-key"), "SECRET-KEY")
	return d
}

// A directory listed by the archive writer and then swapped for a symlink
// out of the volume (the browser container can do this while Chrome is
// paused) must not bring the outside files into the archive.
func TestArchiveSwappedDirStaysInVolume(t *testing.T) {
	vol := t.TempDir()
	fakeProfile(t, vol, "c1")
	zz := filepath.Join(vol, "default", "zz")
	writeFile(t, filepath.Join(zz, "state.json"), "decoy")
	writeFile(t, filepath.Join(zz, "..data", "control-key"), "decoy")
	out := outsideSecrets(t)
	archiveListed = func() {
		os.Rename(zz, zz+".real")
		if err := os.Symlink(out, zz); err != nil {
			t.Fatal(err)
		}
	}
	defer func() { archiveListed = nil }()
	var buf bytes.Buffer
	if err := writeArchive(&buf, openRoot(t, vol), "default", Manifest{ChromeMajor: 154}); err != nil {
		t.Fatal(err)
	}
	if txt := archiveText(t, buf.Bytes()); strings.Contains(txt, "SECRET") {
		t.Fatalf("outside files in the archive:\n%s", txt)
	}
}

// The whole profile swapped for a symlink out of the volume.
func TestArchiveSwappedProfileStaysInVolume(t *testing.T) {
	vol := t.TempDir()
	fakeProfile(t, vol, "c1")
	out := outsideSecrets(t)
	live := filepath.Join(vol, "default")
	writeFile(t, filepath.Join(live, "state.json"), "decoy")
	archiveListed = func() {
		os.Rename(live, live+".real")
		os.Symlink(out, live)
	}
	defer func() { archiveListed = nil }()
	var buf bytes.Buffer
	writeArchive(&buf, openRoot(t, vol), "default", Manifest{ChromeMajor: 154})
	if buf.Len() > 0 && strings.Contains(archiveText(t, buf.Bytes()), "SECRET") {
		t.Fatal("outside files in the archive")
	}
}

// A listed file swapped for a FIFO is skipped instead of blocking the
// archive (and the profile lock, with Chrome paused) forever.
func TestArchiveSkipsSwappedFifo(t *testing.T) {
	vol := t.TempDir()
	fakeProfile(t, vol, "c1")
	prefs := filepath.Join(vol, "default", "Default", "Preferences")
	archiveListed = func() {
		os.Remove(prefs)
		if err := syscall.Mkfifo(prefs, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	defer func() { archiveListed = nil }()
	done := make(chan error, 1)
	var buf bytes.Buffer
	go func() { done <- writeArchive(&buf, openRoot(t, vol), "default", Manifest{ChromeMajor: 154}) }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("archive blocked on a FIFO")
	}
	if strings.Contains(archiveText(t, buf.Bytes()), "profile/Default/Preferences=") {
		t.Fatal("FIFO archived")
	}
}

// A staging directory whose subdirectory is a symlink out of the volume:
// extraction refuses and writes nothing outside.
func TestExtractStaysInVolume(t *testing.T) {
	vol := t.TempDir()
	out := t.TempDir()
	if err := os.Mkdir(filepath.Join(vol, "stage"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(out, filepath.Join(vol, "stage", "Default")); err != nil {
		t.Fatal(err)
	}
	entries := []tar.Header{{Name: "profile/Default/Preferences", Typeflag: tar.TypeReg}}
	_, err := extractArchive(bytes.NewReader(buildTar(t, entries, &Manifest{Format: 1, ChromeMajor: 154})), openRoot(t, vol), "stage", extractOpts{runningMajor: 154})
	if err == nil {
		t.Fatal("extraction through a symlink out of the volume succeeded")
	}
	if ents, _ := os.ReadDir(out); len(ents) != 0 {
		t.Fatalf("wrote outside the volume: %v", ents)
	}
}

// The manifest's locale marker read through a symlink out of the volume.
func TestManifestMarkerStaysInVolume(t *testing.T) {
	e := newEnv(t, false)
	fakeProfile(t, e.profiles, "c1")
	out := t.TempDir()
	writeFile(t, filepath.Join(out, "m"), `{"locale":"xx-XX"}`)
	marker := filepath.Join(e.profiles, "default", "Default", ".livellm-locale")
	os.Remove(marker)
	if err := os.Symlink(filepath.Join(out, "m"), marker); err != nil {
		t.Fatal(err)
	}
	if m := e.p.manifest(t.Context()); m.Locale == "xx-XX" {
		t.Fatal("manifest read a marker outside the volume")
	}
}
