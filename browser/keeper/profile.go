package main

import (
	"archive/tar"
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"filippo.io/age"
	"github.com/klauspost/compress/zstd"
)

// Profile archives (.llcprofile): zstd(tar). The first entry is
// livellm-profile.json, then profile/… . With a password the whole stream is
// wrapped in age scrypt (.llcprofile.age, opens with a stock `age -d`).
// Snapshots on the browser's disk are the same stream sealed with age to the
// browser's own X25519 identity (K_snap).

const (
	manifestName     = "livellm-profile.json"
	profilePrefix    = "profile/"
	archiveFormat    = 1
	headroom         = 64 << 20
	maxEntries       = 200_000
	exportWorkFactor = 16
	ageMagic         = "age-encryption.org/v1"
)

type Manifest struct {
	Format        int      `json:"format"`
	ChromeVersion string   `json:"chromeVersion"`
	ChromeMajor   int      `json:"chromeMajor"`
	ImageVersion  string   `json:"imageVersion"`
	CreatedAt     string   `json:"createdAt"`
	Locale        string   `json:"locale,omitempty"`
	Timezone      string   `json:"timezone,omitempty"`
	Extensions    []string `json:"extensions"`
	SizeBytes     int64    `json:"sizeBytes"`
}

type SnapshotMeta struct {
	ID            string `json:"id"`
	Name          string `json:"name"`
	CreatedAt     string `json:"createdAt"`
	SizeBytes     int64  `json:"sizeBytes"`
	ChromeVersion string `json:"chromeVersion"`
}

var (
	excludedDirs = map[string]bool{
		"Cache": true, "Code Cache": true, "GPUCache": true, "DawnGraphiteCache": true,
		"DawnWebGPUCache": true, "GrShaderCache": true, "GraphiteDawnCache": true,
		"ShaderCache": true, "Crashpad": true, "component_crx_cache": true,
		"extensions_crx_cache": true, "Safe Browsing": true, "segmentation_platform": true,
	}
	excludedPaths = []string{"Service Worker/CacheStorage", "Service Worker/ScriptCache"}
	excludedGlobs = []string{"BrowserMetrics*", "optimization_guide_*", "Singleton*", "*.tmp"}
	snapshotIDRe  = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,63}$`)
)

// excluded reports whether a profile-relative path (slash form) is left out.
func excluded(rel string, isDir bool) bool {
	base := path.Base(rel)
	for _, g := range excludedGlobs {
		if ok, _ := path.Match(g, base); ok {
			return true
		}
	}
	if isDir && excludedDirs[base] {
		return true
	}
	for _, p := range excludedPaths {
		if rel == p || strings.HasSuffix(rel, "/"+p) {
			return true
		}
	}
	return false
}

type fileEntry struct {
	rel  string
	dir  bool
	size int64
	mode fs.FileMode
	mod  time.Time
}

// Every keeper file operation on the profile volume goes through one
// *os.Root opened at the volume's mount. The browser container can write
// anywhere on that volume, so it can plant a symlink in any path component,
// also between a listing and the open that follows it; O_NOFOLLOW only covers
// the last one. An os.Root resolves every component itself and refuses any
// that leads outside the volume, so nothing the keeper reads into an archive
// or writes out of one can be the sidecar's own Secret or state, which live
// outside it. Paths below are slash-separated and relative to that root.

// openNoBlock opens name in r for reading without following a final symlink
// and without blocking: a FIFO swapped in for a listed file would otherwise
// hold the open (and the profile lock, with Chrome paused) forever. Anything
// but a regular file is refused.
func openNoBlock(r *os.Root, name string) (*os.File, os.FileInfo, error) {
	f, err := r.OpenFile(name, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, nil, err
	}
	st, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, nil, err
	}
	if !st.Mode().IsRegular() {
		f.Close()
		return nil, nil, errors.New("not a regular file")
	}
	return f, st, nil
}

// readSmall reads a regular file of at most max bytes.
func readSmall(r *os.Root, name string, max int64) ([]byte, error) {
	f, st, err := openNoBlock(r, name)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	if st.Size() > max {
		return nil, errors.New("too large")
	}
	return io.ReadAll(io.LimitReader(f, max))
}

// listProfile walks dir (relative to r) without following symlinks;
// symlinks, devices, fifos and sockets are skipped.
func listProfile(r *os.Root, dir string) ([]fileEntry, int64, error) {
	var out []fileEntry
	var total int64
	err := fs.WalkDir(r.FS(), dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			if p == dir {
				return err
			}
			return nil // vanished or unreadable: skip
		}
		if p == dir {
			return nil
		}
		rel := strings.TrimPrefix(p, dir+"/")
		t := d.Type()
		switch {
		case t.IsDir():
			if excluded(rel, true) {
				return filepath.SkipDir
			}
			info, err := d.Info()
			if err != nil {
				return nil
			}
			out = append(out, fileEntry{rel: rel, dir: true, mode: info.Mode().Perm(), mod: info.ModTime()})
		case t.IsRegular():
			if excluded(rel, false) {
				return nil
			}
			info, err := d.Info()
			if err != nil {
				return nil
			}
			out = append(out, fileEntry{rel: rel, size: info.Size(), mode: info.Mode().Perm(), mod: info.ModTime()})
			total += info.Size()
		}
		return nil
	})
	return out, total, err
}

func newZstdWriter(w io.Writer) (*zstd.Encoder, error) {
	return zstd.NewWriter(w, zstd.WithEncoderConcurrency(1), zstd.WithWindowSize(8<<20), zstd.WithEncoderLevel(zstd.SpeedDefault))
}

func newZstdReader(r io.Reader) (*zstd.Decoder, error) {
	return zstd.NewReader(r, zstd.WithDecoderConcurrency(1), zstd.WithDecoderMaxWindow(64<<20), zstd.WithDecoderMaxMemory(64<<20))
}

// archiveListed, when set (tests only), runs between writeArchive's listing
// and its first open: where a swap in the browser container would land.
var archiveListed func()

// writeArchive writes zstd(tar) of the profile at dir (relative to r),
// manifest first.
func writeArchive(w io.Writer, r *os.Root, dir string, m Manifest) error {
	entries, total, err := listProfile(r, dir)
	if err != nil {
		return err
	}
	if archiveListed != nil {
		archiveListed()
	}
	m.Format = archiveFormat
	m.SizeBytes = total
	if m.Extensions == nil {
		m.Extensions = []string{}
	}
	zw, err := newZstdWriter(w)
	if err != nil {
		return err
	}
	tw := tar.NewWriter(zw)
	mb, _ := json.Marshal(m)
	now := time.Now()
	if err := tw.WriteHeader(&tar.Header{Name: manifestName, Mode: 0o600, Size: int64(len(mb)), ModTime: now, Typeflag: tar.TypeReg, Format: tar.FormatPAX}); err != nil {
		return err
	}
	if _, err := tw.Write(mb); err != nil {
		return err
	}
	for _, e := range entries {
		name := profilePrefix + e.rel
		if e.dir {
			if err := tw.WriteHeader(&tar.Header{Name: name + "/", Mode: int64(e.mode | 0o700), ModTime: e.mod, Typeflag: tar.TypeDir, Format: tar.FormatPAX}); err != nil {
				return err
			}
			continue
		}
		f, st, err := openNoBlock(r, path.Join(dir, e.rel))
		if err != nil {
			continue // vanished, replaced or outside the volume: skip
		}
		hdr := &tar.Header{Name: name, Mode: int64(e.mode | 0o600), Size: st.Size(), ModTime: st.ModTime(), Typeflag: tar.TypeReg, Format: tar.FormatPAX}
		if err := tw.WriteHeader(hdr); err != nil {
			f.Close()
			return err
		}
		_, err = io.CopyN(tw, f, st.Size())
		f.Close()
		if err != nil {
			return err
		}
	}
	if err := tw.Close(); err != nil {
		return err
	}
	return zw.Close()
}

// ── extraction with validation ──

var (
	errNoManifest    = &apiErr{422, "not_livellm_profile", "Only profiles exported from LiveLLM browsers can be imported. Import cookies instead."}
	errBadArchive    = &apiErr{422, "invalid_profile", "This file is not a valid browser profile."}
	errTooBig        = &apiErr{413, "too_large", "This profile is too large."}
	errNoRoom        = &apiErr{507, "no_room", "Not enough room in this browser's storage. Grow it or delete a snapshot."}
	errBusy          = &apiErr{409, "busy", "Another profile change is running on this browser. Try again shortly."}
	errKeyChanged    = &apiErr{409, "snapshot_key_changed", "This snapshot was sealed under an older key and can't be restored."}
	errNeedsPassword = &apiErr{422, "password_required", "This profile file is password protected. Send its password."}
	errWrongPassword = &apiErr{422, "wrong_password", "The password does not open this profile file."}
	errStrongWF      = &apiErr{422, "password_too_strong", "This file's password protection is stronger than LiveLLM makes. Export it again from LiveLLM."}
	errNotFound      = &apiErr{404, "not_found", "No such snapshot."}
	errSnapLimit     = &apiErr{409, "snapshot_limit", "This browser has the most snapshots it can keep. Delete one first."}
	errNotReady      = &apiErr{503, "not_ready", "not ready"}
)

type extractOpts struct {
	runningMajor int
	force        bool
	maxBytes     int64 // uncompressed cap
}

func profileNewer(n, m int) *apiErr {
	return &apiErr{409, "profile_newer", fmt.Sprintf("This profile is from Chrome %d; this browser runs %d. Import anyway?", n, m)}
}

func isNoSpace(err error) bool { return errors.Is(err, syscall.ENOSPC) }

// extractArchive validates and unpacks zstd(tar) into dst, a fresh dir
// relative to fsr.
func extractArchive(r io.Reader, fsr *os.Root, dst string, o extractOpts) (*Manifest, error) {
	zr, err := newZstdReader(r)
	if err != nil {
		return nil, errBadArchive
	}
	defer zr.Close()
	tr := tar.NewReader(zr)
	hdr, err := tr.Next()
	if err != nil {
		if isNoSpace(err) {
			return nil, errNoRoom
		}
		return nil, errNoManifest
	}
	if hdr.Name != manifestName || hdr.Typeflag != tar.TypeReg || hdr.Size > 64<<10 {
		return nil, errNoManifest
	}
	mb, err := io.ReadAll(io.LimitReader(tr, 64<<10))
	if err != nil {
		return nil, errNoManifest
	}
	var m Manifest
	if json.Unmarshal(mb, &m) != nil || m.Format != archiveFormat {
		return nil, errNoManifest
	}
	if o.runningMajor > 0 && m.ChromeMajor > o.runningMajor && !o.force {
		return nil, profileNewer(m.ChromeMajor, o.runningMajor)
	}
	var total int64
	count := 0
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			if isNoSpace(err) {
				return nil, errNoRoom
			}
			return nil, errBadArchive
		}
		count++
		if count > maxEntries {
			return nil, errTooBig
		}
		rel, ok := cleanEntry(hdr.Name)
		if !ok {
			return nil, errBadArchive
		}
		target := path.Join(dst, rel)
		switch hdr.Typeflag {
		case tar.TypeDir:
			if err := mkdirIn(fsr, target); err != nil {
				if isNoSpace(err) {
					return nil, errNoRoom
				}
				return nil, errBadArchive
			}
		case tar.TypeReg:
			total += hdr.Size
			if hdr.Size < 0 || (o.maxBytes > 0 && total > o.maxBytes) {
				return nil, errTooBig
			}
			if dir := path.Dir(rel); dir != "." {
				if err := mkdirIn(fsr, path.Join(dst, dir)); err != nil {
					if isNoSpace(err) {
						return nil, errNoRoom
					}
					return nil, errBadArchive
				}
			}
			f, err := fsr.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o600)
			if err != nil {
				if isNoSpace(err) {
					return nil, errNoRoom
				}
				return nil, errBadArchive
			}
			_, err = io.CopyN(f, tr, hdr.Size)
			cerr := f.Close()
			if err == nil {
				err = cerr
			}
			if err != nil {
				if isNoSpace(err) {
					return nil, errNoRoom
				}
				return nil, errBadArchive
			}
			fsr.Chtimes(target, hdr.ModTime, hdr.ModTime)
		default:
			// Symlinks, hard links, devices, fifos: never.
			return nil, errBadArchive
		}
	}
	return &m, nil
}

// cleanEntry maps "profile/a/b" to "a/b"; anything outside profile/ or with
// "..", an absolute path or a backslash is refused.
func cleanEntry(name string) (string, bool) {
	if !strings.HasPrefix(name, profilePrefix) || strings.Contains(name, "\\") || strings.ContainsRune(name, 0) {
		return "", false
	}
	rel := strings.TrimSuffix(strings.TrimPrefix(name, profilePrefix), "/")
	if rel == "" || strings.HasPrefix(rel, "/") {
		return "", false
	}
	for _, part := range strings.Split(rel, "/") {
		if part == ".." || part == "." || part == "" {
			return "", false
		}
	}
	return rel, true
}

// mkdirIn creates dir (relative to r) and its parents; an existing
// non-directory on the way is an error.
func mkdirIn(r *os.Root, dir string) error {
	return r.MkdirAll(dir, 0o700)
}

// ── the profile store ──

// Paths on the profile volume, relative to its root.
const (
	liveRel  = "default"
	metaRel  = ".livellm"
	snapRel  = ".livellm/snapshots"
	trashRel = ".livellm/trash"
)

type profileStore struct {
	root        string // …/profiles, the volume's mount
	k           *Keeper
	maxSnaps    int
	maxArchive  int64
	pauseFor    int // seconds asked of the launcher
	busy        sync.Mutex
	freeBytesFn func(string) int64

	fsMu sync.Mutex
	fsr  *os.Root
}

func newProfileStore(root string, k *Keeper, maxSnaps int, maxArchiveMiB int64) *profileStore {
	return &profileStore{
		root: root, k: k, maxSnaps: maxSnaps, maxArchive: maxArchiveMiB << 20, pauseFor: 300, freeBytesFn: freeBytes,
	}
}

// vol returns the os.Root of the profile volume, opened once. The mount
// path's parents are the sidecar's own read-only image, so the browser
// container can't redirect the open itself.
func (s *profileStore) vol() (*os.Root, error) {
	s.fsMu.Lock()
	defer s.fsMu.Unlock()
	if s.fsr != nil {
		return s.fsr, nil
	}
	r, err := os.OpenRoot(s.root)
	if err != nil {
		return nil, err
	}
	s.fsr = r
	return r, nil
}

// sweep removes what a profile change cut short by a keeper or pod restart
// left on the disk: the replaced profile in trash/, unpacked staging dirs,
// half-written files, temporary export snapshots and sealed snapshots whose
// listing entry was never written. Run once at start, before any change.
func (s *profileStore) sweep() {
	r, err := s.vol()
	if err != nil {
		return
	}
	if ents, err := fs.ReadDir(r.FS(), metaRel); err == nil {
		for _, e := range ents {
			name := e.Name()
			if (name == "trash" && e.IsDir()) || strings.HasPrefix(name, "staging-") {
				r.RemoveAll(path.Join(metaRel, name))
			}
		}
	}
	ents, err := fs.ReadDir(r.FS(), snapRel)
	if err != nil {
		return
	}
	for _, e := range ents {
		name := e.Name()
		p := path.Join(snapRel, name)
		switch {
		case !e.Type().IsRegular():
		case strings.HasSuffix(name, ".partial"), strings.HasPrefix(name, "tmp-export-"):
			r.Remove(p)
		case strings.HasSuffix(name, ".llcprofile.age"):
			id := strings.TrimSuffix(name, ".llcprofile.age")
			if _, err := r.Lstat(path.Join(snapRel, id+".json")); os.IsNotExist(err) {
				r.Remove(p)
			}
		}
	}
}

// startSweep holds the profile lock while sweep runs in the background, so
// a big leftover never holds up the keeper's start and no change races it.
func (s *profileStore) startSweep() {
	s.busy.Lock()
	go func() {
		defer s.busy.Unlock()
		s.sweep()
	}()
}

func freeBytes(p string) int64 {
	var st syscall.Statfs_t
	if err := syscall.Statfs(p, &st); err != nil {
		return 0
	}
	return int64(st.Bavail) * int64(st.Bsize)
}

func snapFile(id string) string { return path.Join(snapRel, id+".llcprofile.age") }
func metaFile(id string) string { return path.Join(snapRel, id+".json") }

func (s *profileStore) ensureDirs() (*os.Root, error) {
	r, err := s.vol()
	if err != nil {
		return nil, err
	}
	if err := mkdirIn(r, snapRel); err != nil {
		return nil, err
	}
	return r, nil
}

func (s *profileStore) tryLock() bool { return s.busy.TryLock() }

func (s *profileStore) manifest(ctx context.Context) Manifest {
	m := Manifest{CreatedAt: time.Now().UTC().Format(time.RFC3339Nano)}
	if v, err := s.k.launcher.version(ctx); err == nil {
		m.ChromeVersion, m.ChromeMajor, m.ImageVersion = v.Chrome, v.ChromeMajor, v.Image
		// The browser container's settings (keeper's own env has none of them).
		m.Timezone, m.Locale = v.Timezone, v.Locale
	}
	r, err := s.vol()
	if err != nil {
		return m
	}
	if b, err := readSmall(r, path.Join(liveRel, "Default", ".livellm-locale"), 64<<10); err == nil {
		var mk struct {
			Locale string `json:"locale"`
		}
		if json.Unmarshal(b, &mk) == nil && mk.Locale != "" {
			m.Locale = mk.Locale
		}
	}
	if ents, err := fs.ReadDir(r.FS(), path.Join(liveRel, "Default", "Extensions")); err == nil {
		for _, e := range ents {
			if e.IsDir() && !strings.HasPrefix(e.Name(), ".") {
				m.Extensions = append(m.Extensions, e.Name())
			}
		}
		sort.Strings(m.Extensions)
	}
	return m
}

func (s *profileStore) runningMajor(ctx context.Context) int {
	v, err := s.k.launcher.version(ctx)
	if err != nil {
		return 0
	}
	return v.ChromeMajor
}

func (s *profileStore) listSnapshots() []SnapshotMeta {
	out := []SnapshotMeta{}
	r, err := s.vol()
	if err != nil {
		return out
	}
	ents, err := fs.ReadDir(r.FS(), snapRel)
	if err != nil {
		return out
	}
	for _, e := range ents {
		if !strings.HasSuffix(e.Name(), ".json") || !e.Type().IsRegular() {
			continue
		}
		id := strings.TrimSuffix(e.Name(), ".json")
		if !snapshotIDRe.MatchString(id) {
			continue
		}
		if _, err := r.Lstat(snapFile(id)); err != nil {
			continue
		}
		b, err := readSmall(r, metaFile(id), 64<<10)
		if err != nil {
			continue
		}
		var m SnapshotMeta
		if json.Unmarshal(b, &m) != nil {
			continue
		}
		m.ID = id
		out = append(out, m)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt < out[j].CreatedAt })
	return out
}

func newSnapshotID() string {
	b := make([]byte, 3)
	rand.Read(b)
	return "s" + strings.ToLower(time.Now().UTC().Format("20060102t150405")) + "-" + hex.EncodeToString(b)
}

type ProfileInfo struct {
	SizeBytes     int64          `json:"sizeBytes"`
	ChromeVersion string         `json:"chromeVersion"`
	Snapshots     []SnapshotMeta `json:"snapshots"`
	FreeBytes     int64          `json:"freeBytes"`
}

func (s *profileStore) info(ctx context.Context) ProfileInfo {
	pi := ProfileInfo{Snapshots: s.listSnapshots(), FreeBytes: s.freeBytesFn(s.root)}
	if r, err := s.vol(); err == nil {
		_, pi.SizeBytes, _ = listProfile(r, liveRel)
	}
	if v, err := s.k.launcher.version(ctx); err == nil {
		pi.ChromeVersion = v.Chrome
	}
	return pi
}

// sealLive writes the live profile (Chrome paused) as an age-sealed archive
// at dst (relative to the volume).
func (s *profileStore) sealLive(ctx context.Context, dst string, m Manifest) (int64, error) {
	ks := s.k.keys.Load()
	if ks == nil {
		return 0, errNotReady
	}
	r, err := s.vol()
	if err != nil {
		return 0, err
	}
	tmp := dst + ".partial"
	f, err := r.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return 0, err
	}
	bw := bufio.NewWriterSize(f, 256<<10)
	aw, err := age.Encrypt(bw, ks.snap.Recipient())
	if err == nil {
		err = writeArchive(aw, r, liveRel, m)
	}
	if err == nil {
		err = aw.Close()
	}
	if err == nil {
		err = bw.Flush()
	}
	if err == nil {
		err = f.Sync()
	}
	cerr := f.Close()
	if err == nil {
		err = cerr
	}
	if err != nil {
		r.Remove(tmp)
		if isNoSpace(err) {
			return 0, errNoRoom
		}
		return 0, err
	}
	if err := r.Rename(tmp, dst); err != nil {
		r.Remove(tmp)
		return 0, err
	}
	_, size, _ := listProfile(r, liveRel)
	return size, nil
}

// withPause runs fn with Chrome closed and always resumes it.
func (s *profileStore) withPause(ctx context.Context, fn func() error) error {
	if err := s.k.launcher.pause(ctx, s.pauseFor); err != nil {
		// 409 like any other "busy right now": the caller retries shortly.
		return &apiErr{409, "browser_busy", "The browser could not be paused. Try again shortly."}
	}
	defer func() {
		rctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
		defer cancel()
		if err := s.k.launcher.resume(rctx); err != nil {
			log.Printf("browser resume failed; its own timer resumes it")
		}
	}()
	return fn()
}

func (s *profileStore) roomFor() (int64, error) {
	r, err := s.vol()
	if err != nil {
		return 0, err
	}
	_, size, err := listProfile(r, liveRel)
	if err != nil && !os.IsNotExist(err) {
		return 0, err
	}
	if s.freeBytesFn(s.root) < size+headroom {
		return size, errNoRoom
	}
	return size, nil
}

func (s *profileStore) writeMeta(m SnapshotMeta) error {
	r, err := s.vol()
	if err != nil {
		return err
	}
	b, _ := json.Marshal(m)
	p := metaFile(m.ID)
	tmp := p + ".partial"
	f, err := r.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return err
	}
	_, err = f.Write(b)
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		r.Remove(tmp)
		return err
	}
	return r.Rename(tmp, p)
}

// snapshot takes a snapshot of the live profile.
func (s *profileStore) snapshot(ctx context.Context, name string) (*SnapshotMeta, error) {
	if !s.tryLock() {
		return nil, errBusy
	}
	defer s.busy.Unlock()
	return s.snapshotLocked(ctx, name, true)
}

func (s *profileStore) snapshotLocked(ctx context.Context, name string, pause bool) (*SnapshotMeta, error) {
	r, err := s.ensureDirs()
	if err != nil {
		return nil, err
	}
	if len(s.listSnapshots()) >= s.maxSnaps {
		return nil, errSnapLimit
	}
	if _, err := s.roomFor(); err != nil {
		return nil, err
	}
	m := s.manifest(ctx)
	id := newSnapshotID()
	if strings.TrimSpace(name) == "" {
		name = id
	}
	var size int64
	take := func() error {
		var err error
		size, err = s.sealLive(ctx, snapFile(id), m)
		return err
	}
	if pause {
		err = s.withPause(ctx, take)
	} else {
		err = take()
	}
	if err != nil {
		return nil, err
	}
	meta := SnapshotMeta{ID: id, Name: truncate(name, 100), CreatedAt: m.CreatedAt, SizeBytes: size, ChromeVersion: m.ChromeVersion}
	if err := s.writeMeta(meta); err != nil {
		r.Remove(snapFile(id))
		return nil, err
	}
	return &meta, nil
}

func truncate(s string, n int) string {
	if len([]rune(s)) <= n {
		return s
	}
	return string([]rune(s)[:n])
}

func (s *profileStore) deleteSnapshot(id string) error {
	if !snapshotIDRe.MatchString(id) {
		return errNotFound
	}
	if !s.tryLock() {
		return errBusy
	}
	defer s.busy.Unlock()
	r, err := s.vol()
	if err != nil {
		return err
	}
	if _, err := r.Lstat(snapFile(id)); err != nil {
		return errNotFound
	}
	r.Remove(snapFile(id))
	r.Remove(metaFile(id))
	return nil
}

// openSnapshot decrypts a snapshot stream (zstd(tar)).
func (s *profileStore) openSnapshot(id string) (io.Reader, io.Closer, error) {
	if !snapshotIDRe.MatchString(id) {
		return nil, nil, errNotFound
	}
	ks := s.k.keys.Load()
	if ks == nil {
		return nil, nil, errNotReady
	}
	r, err := s.vol()
	if err != nil {
		return nil, nil, err
	}
	f, _, err := openNoBlock(r, snapFile(id))
	if err != nil {
		return nil, nil, errNotFound
	}
	dr, err := age.Decrypt(bufio.NewReaderSize(f, 256<<10), ks.snap)
	if err != nil {
		f.Close()
		var nim *age.NoIdentityMatchError
		if errors.As(err, &nim) {
			return nil, nil, errKeyChanged
		}
		return nil, nil, errBadArchive
	}
	return dr, f, nil
}

// stage extracts an archive stream into a fresh staging directory and
// returns its path relative to the volume.
func (s *profileStore) stage(ctx context.Context, rd io.Reader, force bool) (string, *Manifest, error) {
	r, err := s.ensureDirs()
	if err != nil {
		return "", nil, err
	}
	var dst string
	for i := 0; ; i++ {
		b := make([]byte, 8)
		rand.Read(b)
		dst = path.Join(metaRel, "staging-"+hex.EncodeToString(b))
		err = r.Mkdir(dst, 0o700)
		if err == nil {
			break
		}
		if isNoSpace(err) {
			return "", nil, errNoRoom
		}
		if !os.IsExist(err) || i >= 5 {
			return "", nil, err
		}
	}
	limit := s.freeBytesFn(s.root) - headroom
	if s.maxArchive > 0 && 4*s.maxArchive < limit {
		limit = 4 * s.maxArchive
	}
	if limit <= 0 {
		r.RemoveAll(dst)
		return "", nil, errNoRoom
	}
	m, err := extractArchive(rd, r, dst, extractOpts{runningMajor: s.runningMajor(ctx), force: force, maxBytes: limit})
	if err != nil {
		r.RemoveAll(dst)
		return "", nil, err
	}
	return dst, m, nil
}

// discard removes a staged directory.
func (s *profileStore) discard(staged string) {
	if staged == "" {
		return
	}
	if r, err := s.vol(); err == nil {
		r.RemoveAll(staged)
	}
}

// swapIn replaces the live profile with a staged one (Chrome paused).
func (s *profileStore) swapIn(ctx context.Context, staged string, keepCurrent bool) error {
	r, err := s.vol()
	if err != nil {
		return err
	}
	return s.withPause(ctx, func() error {
		if keepCurrent {
			if _, err := s.snapshotLocked(ctx, "before-"+time.Now().UTC().Format("20060102-150405"), false); err != nil {
				return err
			}
		}
		if err := mkdirIn(r, trashRel); err != nil {
			return err
		}
		trash := path.Join(trashRel, fmt.Sprintf("%d", time.Now().UnixNano()))
		hadLive := true
		if err := r.Rename(liveRel, trash); err != nil {
			if !os.IsNotExist(err) {
				return err
			}
			hadLive = false
		}
		if err := r.Rename(staged, liveRel); err != nil {
			if hadLive {
				r.Rename(trash, liveRel) // roll back
			}
			return err
		}
		go r.RemoveAll(trash)
		return nil
	})
}

// restore switches back to a snapshot.
func (s *profileStore) restore(ctx context.Context, id string, keepCurrent bool) error {
	if !s.tryLock() {
		return errBusy
	}
	defer s.busy.Unlock()
	rd, c, err := s.openSnapshot(id)
	if err != nil {
		return err
	}
	staged, _, err := s.stage(ctx, rd, true)
	c.Close()
	if err != nil {
		return err
	}
	if err := s.swapIn(ctx, staged, keepCurrent); err != nil {
		s.discard(staged)
		return err
	}
	return nil
}

// importArchive replaces the live profile with an uploaded archive.
func (s *profileStore) importArchive(ctx context.Context, body io.Reader, password string, force bool) (*Manifest, error) {
	if !s.tryLock() {
		return nil, errBusy
	}
	defer s.busy.Unlock()
	counted := &limitedReader{r: body, n: s.maxArchive}
	br := bufio.NewReaderSize(counted, 64<<10)
	head, _ := br.Peek(len(ageMagic))
	var stream io.Reader = br
	if bytes.Equal(head, []byte(ageMagic)) {
		if password == "" {
			return nil, errNeedsPassword
		}
		id, err := age.NewScryptIdentity(password)
		if err != nil {
			return nil, errWrongPassword
		}
		id.SetMaxWorkFactor(exportWorkFactor)
		dr, err := age.Decrypt(br, id)
		if err != nil {
			if counted.over {
				return nil, errTooBig
			}
			var nim *age.NoIdentityMatchError
			if errors.As(err, &nim) {
				return nil, errWrongPassword
			}
			if strings.Contains(err.Error(), "work factor") {
				return nil, errStrongWF
			}
			return nil, errBadArchive
		}
		stream = dr
	}
	staged, m, err := s.stage(ctx, stream, force)
	if counted.over {
		s.discard(staged)
		return nil, errTooBig
	}
	if err != nil {
		return nil, err
	}
	if err := s.swapIn(ctx, staged, false); err != nil {
		s.discard(staged)
		return nil, err
	}
	return m, nil
}

type limitedReader struct {
	r    io.Reader
	n    int64
	read int64
	over bool
}

func (l *limitedReader) Read(p []byte) (int, error) {
	n, err := l.r.Read(p)
	l.read += int64(n)
	if l.n > 0 && l.read > l.n {
		l.over = true
		return 0, errors.New("too large")
	}
	return n, err
}

// export streams an archive (plain or password-wrapped) to w. Without a
// snapshot id it takes a temporary snapshot first (Chrome paused briefly).
// prepare is called once the stream is ready, before the first byte.
func (s *profileStore) export(ctx context.Context, w io.Writer, snapshot, password string, prepare func(name string)) error {
	if !s.tryLock() {
		return errBusy
	}
	defer s.busy.Unlock()
	id := snapshot
	if id == "" {
		r, err := s.ensureDirs()
		if err != nil {
			return err
		}
		if _, err := s.roomFor(); err != nil {
			return err
		}
		id = "tmp-export-" + strings.TrimPrefix(newSnapshotID(), "s")
		m := s.manifest(ctx)
		if err := s.withPause(ctx, func() error {
			_, err := s.sealLive(ctx, snapFile(id), m)
			return err
		}); err != nil {
			return err
		}
		defer r.Remove(snapFile(id))
	}
	r, c, err := s.openSnapshot(id)
	if err != nil {
		return err
	}
	defer c.Close()
	// The first piece is opened before the answer starts, so a damaged
	// snapshot is refused instead of sent as an empty or cut file.
	first := make([]byte, 64<<10)
	n, err := io.ReadAtLeast(r, first, 1)
	if err != nil {
		return errBadArchive
	}
	r = io.MultiReader(bytes.NewReader(first[:n]), r)
	ext := ".llcprofile"
	if password != "" {
		ext += ".age"
	}
	prepare(ext)
	if password == "" {
		_, err = io.Copy(w, r)
		return err
	}
	rec, err := age.NewScryptRecipient(password)
	if err != nil {
		return err
	}
	rec.SetWorkFactor(exportWorkFactor)
	aw, err := age.Encrypt(w, rec)
	if err != nil {
		return err
	}
	if _, err := io.Copy(aw, r); err != nil {
		return err
	}
	return aw.Close()
}
