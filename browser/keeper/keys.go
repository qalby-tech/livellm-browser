package main

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"strings"

	"filippo.io/age"
	"golang.org/x/crypto/hkdf"
)

// keys are derived from the browser's control key (Secret key control-key,
// hex) with HKDF-SHA256, no salt.
type keys struct {
	auth []byte // request signatures
	cfg  []byte // AES-256-GCM for config pushes and passwords
	snap *age.X25519Identity
	raw  string // the control key as read, to notice a change
}

const (
	infoAuth = "livellm-keeper auth v1"
	infoCfg  = "livellm-keeper config v1"
	infoSnap = "livellm-keeper snapshots v1"
)

var errNoKey = errors.New("no control key")

func deriveKeys(controlKey string) (*keys, error) {
	controlKey = strings.TrimSpace(controlKey)
	if controlKey == "" {
		return nil, errNoKey
	}
	secret, err := hex.DecodeString(controlKey)
	if err != nil || len(secret) < 16 {
		return nil, errors.New("control key is not hex")
	}
	k := &keys{raw: controlKey}
	k.auth = hkdfBytes(secret, infoAuth, 32)
	k.cfg = hkdfBytes(secret, infoCfg, 32)
	id, err := x25519Identity(hkdfBytes(secret, infoSnap, 32))
	if err != nil {
		return nil, err
	}
	k.snap = id
	return k, nil
}

func hkdfBytes(secret []byte, info string, n int) []byte {
	out := make([]byte, n)
	r := hkdf.New(sha256.New, secret, nil, []byte(info))
	if _, err := io.ReadFull(r, out); err != nil {
		panic(err)
	}
	return out
}

func loadKeys(path string) (*keys, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, errNoKey
		}
		return nil, err
	}
	return deriveKeys(string(b))
}

// x25519Identity turns 32 bytes into an age identity. age only parses the
// Bech32 text form, so encode the scalar as AGE-SECRET-KEY-1….
func x25519Identity(scalar []byte) (*age.X25519Identity, error) {
	s, err := bech32Encode("age-secret-key-", scalar)
	if err != nil {
		return nil, err
	}
	return age.ParseX25519Identity(strings.ToUpper(s))
}

// ── AES-256-GCM seals: nonce(12) || ciphertext+tag ──

func seal(key, plaintext []byte, aad string) []byte {
	block, err := aes.NewCipher(key)
	if err != nil {
		panic(err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		panic(err)
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		panic(err)
	}
	return gcm.Seal(nonce, nonce, plaintext, []byte(aad))
}

var errSeal = errors.New("sealed value does not open")

func unseal(key, sealed []byte, aad string) ([]byte, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	if len(sealed) < gcm.NonceSize()+gcm.Overhead() {
		return nil, errSeal
	}
	out, err := gcm.Open(nil, sealed[:gcm.NonceSize()], sealed[gcm.NonceSize():], []byte(aad))
	if err != nil {
		return nil, errSeal
	}
	return out, nil
}

// Profile passwords travel as base64(seal(K_cfg, password, "profile-password")).
const aadPassword = "profile-password"

func unsealPassword(k *keys, b64 string) (string, error) {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(b64))
	if err != nil {
		return "", errSeal
	}
	pw, err := unseal(k.cfg, raw, aadPassword)
	if err != nil {
		return "", err
	}
	return string(pw), nil
}

// ── Bech32 (BIP 173), only the encoder age needs ──

const bech32Charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

func bech32Polymod(values []byte) uint32 {
	gen := [5]uint32{0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3}
	chk := uint32(1)
	for _, v := range values {
		b := chk >> 25
		chk = (chk&0x1ffffff)<<5 ^ uint32(v)
		for i := 0; i < 5; i++ {
			if (b>>uint(i))&1 == 1 {
				chk ^= gen[i]
			}
		}
	}
	return chk
}

func bech32Encode(hrp string, data []byte) (string, error) {
	// 8-bit to 5-bit groups.
	var conv []byte
	acc, bits := uint32(0), uint(0)
	for _, b := range data {
		acc = acc<<8 | uint32(b)
		bits += 8
		for bits >= 5 {
			bits -= 5
			conv = append(conv, byte(acc>>bits)&31)
		}
	}
	if bits > 0 {
		conv = append(conv, byte(acc<<(5-bits))&31)
	}
	var expand []byte
	for i := 0; i < len(hrp); i++ {
		expand = append(expand, hrp[i]>>5)
	}
	expand = append(expand, 0)
	for i := 0; i < len(hrp); i++ {
		expand = append(expand, hrp[i]&31)
	}
	values := append(append(expand, conv...), 0, 0, 0, 0, 0, 0)
	mod := bech32Polymod(values) ^ 1
	var sb strings.Builder
	sb.WriteString(hrp)
	sb.WriteByte('1')
	for _, c := range conv {
		sb.WriteByte(bech32Charset[c])
	}
	for i := 0; i < 6; i++ {
		sb.WriteByte(bech32Charset[(mod>>uint(5*(5-i)))&31])
	}
	return sb.String(), nil
}
