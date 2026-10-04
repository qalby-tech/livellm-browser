// Command livellm-keeper is the browser pod's control sidecar: the proxy
// relay Chrome talks to, proxy rotation and the exit probe, and profile
// snapshots, export and import. Its Secret and state are mounted into this
// container only.
package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"
)

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC)
	secretDir := envOr("KEEPER_SECRET_DIR", "/etc/livellm/keeper")
	runDir := envOr("KEEPER_RUN_DIR", "/run/keeper")
	profiles := envOr("KEEPER_PROFILES_DIR", "/home/headless/Desktop/app/profiles")
	launcher := envOr("KEEPER_LAUNCHER", "http://127.0.0.1:9000")
	relayRequired := os.Getenv("KEEPER_RELAY") == "required"
	maxSnaps, _ := strconv.Atoi(envOr("KEEPER_MAX_SNAPSHOTS", "10"))
	maxArchive, _ := strconv.ParseInt(envOr("KEEPER_MAX_ARCHIVE_MIB", "2048"), 10, 64)

	k := newKeeper(secretDir, runDir+"/state.json", relayRequired, launcher)
	k.boot()
	p := newProfileStore(profiles, k, maxSnaps, maxArchive)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	if relayRequired {
		rs := &http.Server{Addr: "127.0.0.1:3128", Handler: k.rl, ReadHeaderTimeout: 30 * time.Second, ErrorLog: quietLog()}
		go func() {
			if err := rs.ListenAndServe(); err != nil && err != http.ErrServerClosed {
				log.Fatalf("relay listener failed")
			}
		}()
		log.Printf("relay listening on 127.0.0.1:3128")
	}
	api := &server_{k: k, p: p, nonces: newNonceCache(), now: time.Now}
	srv := &http.Server{Addr: ":9300", Handler: api.routes(), ReadHeaderTimeout: 30 * time.Second, ErrorLog: quietLog()}
	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("control listener failed")
		}
	}()
	log.Printf("control API listening on :9300")
	go k.run(ctx)
	<-ctx.Done()
	sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	srv.Shutdown(sctx)
}

// quietLog drops net/http's own error lines: they can carry addresses.
func quietLog() *log.Logger { return log.New(discard{}, "", 0) }

type discard struct{}

func (discard) Write(p []byte) (int, error) { return len(p), nil }
