package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"
)

// launcherClient talks to the launcher's pod-local endpoints over loopback.
type launcherClient struct {
	base string
	hc   *http.Client
}

func newLauncherClient(base string) *launcherClient {
	return &launcherClient{base: base, hc: &http.Client{Timeout: 90 * time.Second}}
}

type launcherVersion struct {
	Chrome      string `json:"chrome"`
	ChromeMajor int    `json:"chromeMajor"`
	Image       string `json:"image"`
	Timezone    string `json:"timezone"`
	Locale      string `json:"locale"`
}

func (l *launcherClient) do(ctx context.Context, method, path string, body any, out any) error {
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, l.base+path, rd)
	if err != nil {
		return err
	}
	req.Header.Set("X-Livellm-Keeper", "1")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := l.hc.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("launcher %s: %d", path, resp.StatusCode)
	}
	if out != nil {
		return json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(out)
	}
	return nil
}

func (l *launcherClient) version(ctx context.Context) (launcherVersion, error) {
	var v launcherVersion
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	err := l.do(ctx, http.MethodGet, "/version", nil, &v)
	return v, err
}

func (l *launcherClient) pause(ctx context.Context, maxSeconds int) error {
	return l.do(ctx, http.MethodPost, "/browsers/default/pause", map[string]int{"maxSeconds": maxSeconds}, nil)
}

func (l *launcherClient) resume(ctx context.Context) error {
	var err error
	for i := 0; i < 3; i++ {
		if err = l.do(ctx, http.MethodPost, "/browsers/default/resume", nil, nil); err == nil {
			return nil
		}
		select {
		case <-ctx.Done():
			return err
		case <-time.After(2 * time.Second):
		}
	}
	return err
}

var errLauncher = errors.New("launcher refused")

func (l *launcherClient) cookies(ctx context.Context, raw json.RawMessage) error {
	return l.do(ctx, http.MethodPost, "/browsers/default/cookies", raw, nil)
}
