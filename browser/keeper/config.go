package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// Config is config.json in the control Secret: no credentials.
type Config struct {
	Version   int64      `json:"version"`
	Relay     bool       `json:"relay"`
	Upstreams []Upstream `json:"upstreams"`
	Rotation  Rotation   `json:"rotation"`
	CheckURL  string     `json:"checkUrl,omitempty"`
}

type Upstream struct {
	Name               string `json:"name"`
	Server             string `json:"server"`
	ChangeIPMethod     string `json:"changeIpMethod,omitempty"`
	MinChangeIPSeconds int    `json:"minChangeIpSeconds,omitempty"`
}

type Rotation struct {
	Mode         string `json:"mode,omitempty"` // off | session | interval
	EveryMinutes int    `json:"everyMinutes,omitempty"`
	Order        string `json:"order,omitempty"` // sequential | random
}

// Login is upstream.<name>.login.
type Login struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

// Creds are the per-upstream secrets, keyed by Secret key name:
// "upstream.<name>.login" (Login JSON) and "upstream.<name>.change-ip" (URL).
type Creds map[string]string

var nameRe = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$`)

func loginKey(name string) string    { return "upstream." + name + ".login" }
func changeIPKey(name string) string { return "upstream." + name + ".change-ip" }

func (c *Config) normalize() {
	for i := range c.Upstreams {
		u := &c.Upstreams[i]
		u.ChangeIPMethod = strings.ToUpper(u.ChangeIPMethod)
		if u.ChangeIPMethod == "" {
			u.ChangeIPMethod = "GET"
		}
		if u.MinChangeIPSeconds == 0 {
			u.MinChangeIPSeconds = 60
		}
	}
	if c.Rotation.Mode == "" {
		c.Rotation.Mode = "off"
	}
	if c.Rotation.Order == "" {
		c.Rotation.Order = "sequential"
	}
}

var errConfigInvalid = errors.New("config_invalid")

// validate checks shape only; reachability is the relay's business.
func (c *Config) validate() error {
	if len(c.Upstreams) > 20 {
		return errConfigInvalid
	}
	seen := map[string]bool{}
	for _, u := range c.Upstreams {
		if !nameRe.MatchString(u.Name) || seen[u.Name] {
			return errConfigInvalid
		}
		seen[u.Name] = true
		if _, err := parseServer(u.Server); err != nil {
			return errConfigInvalid
		}
		if u.ChangeIPMethod != "GET" && u.ChangeIPMethod != "POST" {
			return errConfigInvalid
		}
		if u.MinChangeIPSeconds < 10 || u.MinChangeIPSeconds > 3600 {
			return errConfigInvalid
		}
	}
	switch c.Rotation.Mode {
	case "off", "session":
	case "interval":
		if c.Rotation.EveryMinutes < 1 || c.Rotation.EveryMinutes > 1440 {
			return errConfigInvalid
		}
	default:
		return errConfigInvalid
	}
	if c.Rotation.Order != "sequential" && c.Rotation.Order != "random" {
		return errConfigInvalid
	}
	if c.CheckURL != "" {
		u, err := url.Parse(c.CheckURL)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
			return errConfigInvalid
		}
	}
	return nil
}

// server is a parsed upstream address.
type server struct {
	scheme string // http | https | socks5
	host   string
	port   string
}

func (s server) addr() string { return net.JoinHostPort(s.host, s.port) }

func parseServer(raw string) (server, error) {
	u, err := url.Parse(raw)
	if err != nil {
		return server{}, err
	}
	if u.User != nil || (u.Path != "" && u.Path != "/") || u.RawQuery != "" || u.Fragment != "" {
		return server{}, errConfigInvalid
	}
	scheme := strings.ToLower(u.Scheme)
	if scheme == "socks5h" {
		scheme = "socks5"
	}
	if scheme != "http" && scheme != "https" && scheme != "socks5" {
		return server{}, errConfigInvalid
	}
	host, port := u.Hostname(), u.Port()
	if host == "" {
		return server{}, errConfigInvalid
	}
	if port == "" {
		port = map[string]string{"http": "80", "https": "443", "socks5": "1080"}[scheme]
	}
	if n, err := strconv.Atoi(port); err != nil || n < 1 || n > 65535 {
		return server{}, errConfigInvalid
	}
	return server{scheme: scheme, host: host, port: port}, nil
}

// login returns the upstream's login, if stored.
func (c Creds) login(name string) *Login {
	raw, ok := c[loginKey(name)]
	if !ok {
		return nil
	}
	var l Login
	if json.Unmarshal([]byte(raw), &l) != nil || l.Username == "" {
		return nil
	}
	return &l
}

func (c Creds) changeIP(name string) string {
	return strings.TrimSpace(c[changeIPKey(name)])
}

// keepOnly drops credentials for upstreams no longer in the config.
func (c Creds) keepOnly(cfg *Config) Creds {
	out := Creds{}
	for _, u := range cfg.Upstreams {
		for _, k := range []string{loginKey(u.Name), changeIPKey(u.Name)} {
			if v, ok := c[k]; ok {
				out[k] = v
			}
		}
	}
	return out
}

// ── files: the mounted control Secret ──

type fileSet struct {
	cfg   *Config // nil when config.json is absent
	creds Creds
}

func readFiles(dir string) (fileSet, error) {
	fs := fileSet{creds: Creds{}}
	b, err := os.ReadFile(filepath.Join(dir, "config.json"))
	if err != nil {
		if os.IsNotExist(err) {
			return fs, nil
		}
		return fs, err
	}
	var c Config
	if err := json.Unmarshal(b, &c); err != nil {
		return fs, errConfigInvalid
	}
	c.normalize()
	fs.cfg = &c
	for _, u := range c.Upstreams {
		for _, k := range []string{loginKey(u.Name), changeIPKey(u.Name)} {
			if v, err := os.ReadFile(filepath.Join(dir, k)); err == nil {
				fs.creds[k] = string(v)
			}
		}
	}
	return fs, nil
}

// ── state: /run/keeper/state.json (memory emptyDir, keeper only) ──

type persisted struct {
	Config       *Config          `json:"config"`
	Creds        Creds            `json:"creds"`
	Current      int              `json:"current"`
	LastChangeIP map[string]int64 `json:"lastChangeIp"`
	FirstApply   int64            `json:"firstApply"`
	LastRotation int64            `json:"lastRotation"`
	Generation   int64            `json:"generation"`
}

func loadState(path string) (*persisted, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var p persisted
	if err := json.Unmarshal(b, &p); err != nil {
		return nil, err
	}
	if p.Config != nil {
		p.Config.normalize()
	}
	return &p, nil
}

func saveState(path string, p *persisted) error {
	if path == "" {
		return nil
	}
	b, err := json.Marshal(p)
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// ── sealed push: PUT /v1/config?version=N ──
//
// body = nonce(12) || AES-256-GCM(K_cfg, plaintext, aad "config|N").
// plaintext = {"version":N,"config":{…},"set":{key:value},"remove":[key]}

type pushBody struct {
	Version int64             `json:"version"`
	Config  *Config           `json:"config"`
	Set     map[string]string `json:"set"`
	Remove  []string          `json:"remove"`
}

func configAAD(version int64) string { return fmt.Sprintf("config|%d", version) }

func openPush(k *keys, version int64, body []byte) (*pushBody, error) {
	plain, err := unseal(k.cfg, body, configAAD(version))
	if err != nil {
		return nil, err
	}
	var p pushBody
	if err := json.Unmarshal(plain, &p); err != nil || p.Config == nil || p.Version != version {
		return nil, errConfigInvalid
	}
	p.Config.normalize()
	if p.Config.Version != 0 && p.Config.Version != version {
		return nil, errConfigInvalid
	}
	p.Config.Version = version
	return &p, nil
}

// merge applies a push to the previous credentials.
func mergeCreds(prev Creds, p *pushBody) Creds {
	out := Creds{}
	for k, v := range prev {
		out[k] = v
	}
	for k, v := range p.Set {
		out[k] = v
	}
	for _, k := range p.Remove {
		delete(out, k)
	}
	return out.keepOnly(p.Config)
}

func nowUnix() int64 { return time.Now().Unix() }
