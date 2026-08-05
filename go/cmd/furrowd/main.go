package main

import (
	"bufio"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/subtle"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"log"
	"math/big"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/Agent-Field/SWE-AF/go/internal/furrow"
)

const (
	defaultAddr        = ":8802"
	defaultRemotesRoot = "/var/lib/swe-af/furrow/remotes"
	defaultMaxConns    = 32
)

type config struct {
	addr, root, cert, key, furrowBin string
	maxConns                         int
}

type server struct {
	cfg config
	sem chan struct{}
	wg  sync.WaitGroup
}

func main() {
	cfg, err := configFromEnv()
	if err != nil {
		log.Fatalf("furrowd: %v", err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, cfg, nil); err != nil && !errors.Is(err, context.Canceled) {
		log.Fatalf("furrowd: %v", err)
	}
}

func configFromEnv() (config, error) {
	c := config{addr: envOr("FURROWD_ADDR", defaultAddr), root: envOr("FURROWD_REMOTES_ROOT", defaultRemotesRoot), maxConns: defaultMaxConns}
	c.cert, c.key = os.Getenv("FURROWD_TLS_CERT"), os.Getenv("FURROWD_TLS_KEY")
	if (c.cert == "") != (c.key == "") {
		return c, errors.New("FURROWD_TLS_CERT and FURROWD_TLS_KEY must be set together")
	}
	if c.cert == "" {
		c.cert, c.key = filepath.Join(c.root, "furrowd.crt"), filepath.Join(c.root, "furrowd.key")
	}
	if value := os.Getenv("FURROWD_MAX_CONNECTIONS"); value != "" {
		n, err := strconv.Atoi(value)
		if err != nil || n < 1 {
			return c, fmt.Errorf("invalid FURROWD_MAX_CONNECTIONS %q", value)
		}
		c.maxConns = n
	}
	var ok bool
	c.furrowBin, ok = resolveFurrowBin()
	if !ok {
		return c, fmt.Errorf("no runnable furrow binary at %s", c.furrowBin)
	}
	return c, nil
}

func resolveFurrowBin() (string, bool) {
	if value := os.Getenv("SWE_FURROW_BIN"); value != "" {
		return value, runnable(value)
	}
	const defaultBin = "/usr/local/bin/furrow"
	if runnable(defaultBin) {
		return defaultBin, true
	}
	if executable, err := os.Executable(); err == nil {
		for _, name := range []string{"furrow-" + runtime.GOOS + "-" + runtime.GOARCH, "furrow"} {
			path := filepath.Join(filepath.Dir(executable), name)
			if runnable(path) {
				return path, true
			}
		}
	}
	return defaultBin, false
}

func runnable(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir() && info.Mode().Perm()&0o111 != 0
}

func run(ctx context.Context, cfg config, ready chan<- net.Addr) error {
	certificate, err := loadOrCreateCertificate(cfg.cert, cfg.key)
	if err != nil {
		return fmt.Errorf("prepare TLS certificate: %w", err)
	}
	listener, err := tls.Listen("tcp", cfg.addr, &tls.Config{Certificates: []tls.Certificate{certificate}, MinVersion: tls.VersionTLS12})
	if err != nil {
		return fmt.Errorf("listen on %s: %w", cfg.addr, err)
	}
	defer listener.Close()
	if ready != nil {
		ready <- listener.Addr()
	}
	s := &server{cfg: cfg, sem: make(chan struct{}, cfg.maxConns)}
	go func() {
		<-ctx.Done()
		_ = listener.Close()
	}()
	for {
		conn, err := listener.Accept()
		if err != nil {
			if ctx.Err() != nil {
				s.wg.Wait()
				return ctx.Err()
			}
			return fmt.Errorf("accept connection: %w", err)
		}
		select {
		case s.sem <- struct{}{}:
			s.wg.Add(1)
			go s.serveSafely(conn)
		default:
			log.Printf("furrowd: warning: connection rejected")
			_ = conn.Close()
		}
	}
}

func (s *server) serveSafely(conn net.Conn) {
	defer s.wg.Done()
	defer func() { <-s.sem }()
	defer conn.Close()
	defer func() {
		if recovered := recover(); recovered != nil {
			log.Printf("furrowd: warning: recovered serving connection")
		}
	}()
	if err := s.serve(conn); err != nil {
		log.Printf("furrowd: warning: connection rejected")
	}
}

func (s *server) serve(conn net.Conn) error {
	if err := conn.SetDeadline(time.Now().Add(10 * time.Second)); err != nil {
		return err
	}
	if tlsConn, ok := conn.(*tls.Conn); ok {
		if err := tlsConn.Handshake(); err != nil {
			return err
		}
	}
	reader := bufio.NewReaderSize(conn, 513)
	lineBytes, err := reader.ReadSlice('\n')
	if err != nil || len(lineBytes) > 512 {
		return errors.New("invalid authentication")
	}
	line := string(lineBytes)
	parts := strings.Split(strings.TrimSuffix(line, "\n"), " ")
	if len(parts) != 3 || parts[0] != "AUTH" || parts[1] == "" || parts[2] == "" {
		return errors.New("invalid authentication")
	}
	entry, ok := lookupEntry(filepath.Join(s.cfg.root, "registry.json"), parts[1])
	if !ok || !validNamespace(parts[2]) {
		return errors.New("invalid authentication")
	}
	// The namespace on the wire is furrow's *blinded* name — a keyed BLAKE3
	// digest of the human one, which never leaves the client — so it cannot be
	// compared against the registry's namespace. It does not need to be: the
	// token already pins this connection to one run's data root, and the
	// namespace only selects a directory beneath it. Charset validation above is
	// what keeps that selection inside the root.
	if err := conn.SetDeadline(time.Time{}); err != nil {
		return err
	}
	if _, err := io.WriteString(conn, "OK\n"); err != nil {
		return err
	}
	return s.runChild(conn, reader, entry, parts[2])
}

// validNamespace mirrors furrow's own namespace rule: [A-Za-z0-9._-], at most 96
// bytes, and never a path traversal.
func validNamespace(namespace string) bool {
	if namespace == "" || len(namespace) > 96 || namespace == "." || namespace == ".." {
		return false
	}
	for i := 0; i < len(namespace); i++ {
		c := namespace[i]
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9':
		case c == '.', c == '_', c == '-':
		default:
			return false
		}
	}
	return true
}

func lookupEntry(path, token string) (furrow.Entry, bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return furrow.Entry{}, false
	}
	entries := make(map[string]furrow.Entry)
	if json.Unmarshal(data, &entries) != nil {
		return furrow.Entry{}, false
	}
	var match furrow.Entry
	found := 0
	for runID, entry := range entries {
		if subtle.ConstantTimeCompare([]byte(token), []byte(entry.Token)) == 1 {
			entry.RunID = runID
			match = entry
			found = 1
		}
	}
	return match, found == 1
}

func (s *server) runChild(conn net.Conn, input io.Reader, entry furrow.Entry, namespace string) error {
	cmd := exec.Command(s.cfg.furrowBin, "__remote", namespace)
	cmd.Env = append(os.Environ(), "FURROW_REMOTE_DATA_DIR="+filepath.Join(s.cfg.root, entry.RunID))
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return fmt.Errorf("create child stdin: %w", err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return fmt.Errorf("create child stdout: %w", err)
	}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start furrow: %w", err)
	}
	done := make(chan error, 1)
	go func() { _, err := io.Copy(conn, stdout); done <- err }()
	_, copyErr := io.Copy(stdin, input)
	_ = stdin.Close()
	wait := make(chan error, 1)
	go func() { wait <- cmd.Wait() }()
	var waitErr error
	select {
	case waitErr = <-wait:
	case <-time.After(time.Second):
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		waitErr = <-wait
	}
	outputErr := <-done
	if waitErr != nil {
		log.Printf("furrowd: furrow child exited non-zero: %v", waitErr)
	}
	if copyErr != nil {
		return fmt.Errorf("copy client input: %w", copyErr)
	}
	if outputErr != nil && !errors.Is(outputErr, net.ErrClosed) {
		return fmt.Errorf("copy child output: %w", outputErr)
	}
	return nil
}

func loadOrCreateCertificate(certPath, keyPath string) (tls.Certificate, error) {
	certificate, err := tls.LoadX509KeyPair(certPath, keyPath)
	if err == nil {
		return certificate, nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return tls.Certificate{}, err
	}
	if err := os.MkdirAll(filepath.Dir(certPath), 0o700); err != nil {
		return tls.Certificate{}, err
	}
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		return tls.Certificate{}, err
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		return tls.Certificate{}, err
	}
	now := time.Now()
	template := x509.Certificate{SerialNumber: serial, Subject: pkix.Name{CommonName: "furrowd"}, NotBefore: now.Add(-time.Minute), NotAfter: now.AddDate(10, 0, 0), KeyUsage: x509.KeyUsageKeyEncipherment | x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, DNSNames: []string{"localhost"}, IPAddresses: []net.IP{net.ParseIP("127.0.0.1"), net.ParseIP("::1")}}
	der, err := x509.CreateCertificate(rand.Reader, &template, &template, &key.PublicKey, key)
	if err != nil {
		return tls.Certificate{}, err
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})
	if err := os.WriteFile(certPath, certPEM, 0o600); err != nil {
		return tls.Certificate{}, err
	}
	if err := os.WriteFile(keyPath, keyPEM, 0o600); err != nil {
		return tls.Certificate{}, err
	}
	return tls.X509KeyPair(certPEM, keyPEM)
}

func envOr(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}
