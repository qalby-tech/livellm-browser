package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// The control API on :9300. Every route but GET /healthz and
// POST /v1/session-start is signed (see auth.go).

type server_ struct {
	k      *Keeper
	p      *profileStore
	nonces *nonceCache
	now    func() time.Time
}

const maxJSONBody = 1 << 20

// Cookies come in batches of up to 5 MiB (tenant-api's own limit); its
// re-encoding may add a little, hence the slack.
const maxCookiesBody = 6 << 20

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, err error) {
	var ae *apiErr
	if errors.As(err, &ae) {
		writeJSON(w, ae.status, map[string]string{"code": ae.code, "message": ae.message})
		return
	}
	log.Printf("internal error")
	writeJSON(w, 500, map[string]string{"code": "internal", "message": "Something went wrong."})
}

// signed reads the body (unless streamed) and checks the signature.
func (s *server_) signed(h func(w http.ResponseWriter, r *http.Request, body []byte)) http.HandlerFunc {
	return s.signedOpts(false, maxJSONBody, h)
}

// signedBig is signed with its own body cap.
func (s *server_) signedBig(max int, h func(w http.ResponseWriter, r *http.Request, body []byte)) http.HandlerFunc {
	return s.signedOpts(false, max, h)
}

func (s *server_) signedOpts(stream bool, max int, h func(w http.ResponseWriter, r *http.Request, body []byte)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		ks := s.k.keys.Load()
		if ks == nil {
			writeJSON(w, 503, map[string]string{"code": "not_ready"})
			return
		}
		var body []byte
		hash := unsignedTag
		if !stream {
			b, err := io.ReadAll(io.LimitReader(r.Body, int64(max)+1))
			if err != nil || len(b) > max {
				writeJSON(w, 413, map[string]string{"code": "too_large"})
				return
			}
			body = b
			hash = bodyHashOf(b)
		}
		if !verify(r, ks.auth, hash, s.nonces, s.now()) {
			writeJSON(w, 401, map[string]string{"code": "unauthorized"})
			return
		}
		h(w, r, body)
	}
}

func (s *server_) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("POST /v1/session-start", s.sessionStart)
	mux.HandleFunc("GET /v1/status", s.signed(func(w http.ResponseWriter, r *http.Request, _ []byte) {
		writeJSON(w, 200, s.k.status())
	}))
	mux.HandleFunc("PUT /v1/config", s.signed(s.putConfig))
	mux.HandleFunc("POST /v1/rotate", s.signed(s.rotate))
	mux.HandleFunc("GET /v1/profile", s.signed(func(w http.ResponseWriter, r *http.Request, _ []byte) {
		writeJSON(w, 200, s.p.info(r.Context()))
	}))
	mux.HandleFunc("POST /v1/profile/snapshots", s.signed(s.snapshot))
	mux.HandleFunc("DELETE /v1/profile/snapshots/{id}", s.signed(func(w http.ResponseWriter, r *http.Request, _ []byte) {
		if err := s.p.deleteSnapshot(r.PathValue("id")); err != nil {
			writeErr(w, err)
			return
		}
		writeJSON(w, 200, map[string]bool{"deleted": true})
	}))
	mux.HandleFunc("POST /v1/profile/snapshots/{id}/restore", s.signed(s.restore))
	mux.HandleFunc("POST /v1/profile/export", s.signed(s.export))
	mux.HandleFunc("POST /v1/profile/import", s.signedOpts(true, 0, s.importProfile))
	mux.HandleFunc("POST /v1/cookies", s.signedBig(maxCookiesBody, s.cookies))
	return mux
}

func (s *server_) sessionStart(w http.ResponseWriter, r *http.Request) {
	var body struct {
		OpenSessions int `json:"openSessions"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&body); err != nil || body.OpenSessions < 0 {
		writeJSON(w, 400, map[string]string{"code": "bad_request"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 120*time.Second)
	defer cancel()
	out, e := s.k.sessionStart(ctx, body.OpenSessions)
	if e != nil {
		writeErr(w, e)
		return
	}
	writeJSON(w, 200, out)
}

func (s *server_) putConfig(w http.ResponseWriter, r *http.Request, body []byte) {
	v, err := strconv.ParseInt(r.URL.Query().Get("version"), 10, 64)
	if err != nil || v <= 0 {
		writeJSON(w, 400, map[string]string{"code": "config_invalid"})
		return
	}
	if e := s.k.push(v, body); e != nil {
		writeErr(w, e)
		return
	}
	writeJSON(w, 200, s.k.status())
}

func (s *server_) rotate(w http.ResponseWriter, r *http.Request, body []byte) {
	var req struct {
		To string `json:"to"`
	}
	if len(strings.TrimSpace(string(body))) > 0 && json.Unmarshal(body, &req) != nil {
		writeJSON(w, 400, map[string]string{"code": "bad_request"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 140*time.Second)
	defer cancel()
	st, e := s.k.rotate(ctx, req.To)
	if e != nil {
		if e.code == "change_ip_failed" {
			writeJSON(w, e.status, map[string]any{"code": e.code, "message": e.message, "status": st})
			return
		}
		writeErr(w, e)
		return
	}
	writeJSON(w, 200, st)
}

func (s *server_) snapshot(w http.ResponseWriter, r *http.Request, body []byte) {
	var req struct {
		Name string `json:"name"`
	}
	if len(body) > 0 && json.Unmarshal(body, &req) != nil {
		writeJSON(w, 400, map[string]string{"code": "bad_request"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Minute)
	defer cancel()
	m, err := s.p.snapshot(ctx, req.Name)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, m)
}

func (s *server_) restore(w http.ResponseWriter, r *http.Request, body []byte) {
	var req struct {
		KeepCurrent bool `json:"keepCurrent"`
	}
	if len(body) > 0 && json.Unmarshal(body, &req) != nil {
		writeJSON(w, 400, map[string]string{"code": "bad_request"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Minute)
	defer cancel()
	if err := s.p.restore(ctx, r.PathValue("id"), req.KeepCurrent); err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]bool{"restored": true})
}

// requestNonce is the (already verified) auth nonce of a request.
func requestNonce(r *http.Request) string {
	a, _ := parseAuth(r.Header.Get("Authorization"))
	return a.nonce
}

type exportReq struct {
	Snapshot       string `json:"snapshot"`
	Password       string `json:"password"`       // only inside the sealed form
	PasswordSealed string `json:"passwordSealed"` // the plain-JSON form
}

// Export body: application/octet-stream seal({"snapshot"?,"password"?},
// AAD "export|<the request's auth nonce>") as tenant-api sends it, or plain
// JSON {"snapshot"?,"passwordSealed"?}.
func (s *server_) export(w http.ResponseWriter, r *http.Request, body []byte) {
	var req exportReq
	nonce := requestNonce(r)
	trimmed := strings.TrimSpace(string(body))
	switch {
	case trimmed == "":
	case strings.HasPrefix(trimmed, "{"):
		if json.Unmarshal(body, &req) != nil {
			writeJSON(w, 400, map[string]string{"code": "bad_request"})
			return
		}
		req.Password = ""
		if req.PasswordSealed != "" {
			pw, err := unsealPassword(s.k.keys.Load(), req.PasswordSealed, nonce)
			if err != nil || pw == "" {
				writeJSON(w, 400, map[string]string{"code": "bad_password_seal"})
				return
			}
			req.Password = pw
		}
	default:
		plain, err := unseal(s.k.keys.Load().cfg, body, "export|"+nonce)
		if err != nil || json.Unmarshal(plain, &req) != nil {
			writeJSON(w, 400, map[string]string{"code": "bad_request"})
			return
		}
	}
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Minute)
	defer cancel()
	started := false
	err := s.p.export(ctx, w, req.Snapshot, req.Password, func(ext string) {
		w.Header().Set("Content-Type", "application/octet-stream")
		w.Header().Set("X-Profile-Extension", ext)
		w.WriteHeader(200)
		started = true
	})
	if err != nil {
		if !started {
			writeErr(w, err)
			return
		}
		// The 200 is out: a clean end would pass a cut or empty file off as
		// the whole profile. Reset the connection so the caller sees a failure.
		panic(http.ErrAbortHandler)
	}
}

func (s *server_) importProfile(w http.ResponseWriter, r *http.Request, _ []byte) {
	password := ""
	if h := r.Header.Get("X-Profile-Password-Sealed"); h != "" {
		pw, err := unsealPassword(s.k.keys.Load(), h, requestNonce(r))
		if err != nil {
			writeJSON(w, 400, map[string]string{"code": "bad_password_seal"})
			return
		}
		password = pw
	}
	force := r.URL.Query().Get("force") == "1"
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Minute)
	defer cancel()
	m, err := s.p.importArchive(ctx, r.Body, password, force)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"imported": true, "chromeVersion": m.ChromeVersion, "sizeBytes": m.SizeBytes})
}

func (s *server_) cookies(w http.ResponseWriter, r *http.Request, body []byte) {
	var arr []json.RawMessage
	if err := json.Unmarshal(body, &arr); err != nil || len(arr) > 5000 {
		writeJSON(w, 400, map[string]string{"code": "bad_request"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()
	if err := s.k.launcher.cookies(ctx, body); err != nil {
		writeJSON(w, 502, map[string]string{"code": "browser_unreachable", "message": "The browser did not take the cookies."})
		return
	}
	writeJSON(w, 200, map[string]int{"added": len(arr)})
}
