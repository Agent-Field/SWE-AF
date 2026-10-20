// furrow-dial is a FURROW_SSH_COMMAND shim. With FURROW_DIAL_INSECURE=1 TLS
// certificate verification is disabled; furrow's encrypted payload remains
// the confidentiality boundary, but transport authentication is then by token.
package main

import (
	"bufio"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"strings"
)

func main() {
	if err := dial(os.Args[1:], os.Stdin, os.Stdout); err != nil {
		fmt.Fprintf(os.Stderr, "furrow-dial: %v\n", err)
		os.Exit(1)
	}
}

func dial(args []string, stdin io.Reader, stdout io.Writer) error {
	if len(args) == 0 {
		return errors.New("missing namespace")
	}
	namespace := args[len(args)-1]
	addr := os.Getenv("FURROW_DIAL_ADDR")
	if addr == "" {
		for i, arg := range args {
			if arg == "--" && i+1 < len(args) {
				addr = args[i+1]
				break
			}
		}
	}
	if addr == "" {
		return errors.New("FURROW_DIAL_ADDR is unset and argv has no host")
	}
	token := os.Getenv("FURROW_DIAL_TOKEN")
	if token == "" || strings.ContainsAny(token, " \r\n") || strings.ContainsAny(namespace, " \r\n") {
		return errors.New("missing or invalid authentication parameters")
	}
	insecure := os.Getenv("FURROW_DIAL_INSECURE") == "1"
	if insecure {
		fmt.Fprintln(os.Stderr, "furrow-dial: warning: TLS certificate verification disabled; relying on furrow payload encryption")
	}
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return fmt.Errorf("invalid address: %w", err)
	}
	conn, err := tls.Dial("tcp", addr, &tls.Config{ServerName: host, InsecureSkipVerify: insecure, MinVersion: tls.VersionTLS12}) //nolint:gosec -- explicitly operator-controlled for the generated self-signed certificate.
	if err != nil {
		return fmt.Errorf("connect: %w", err)
	}
	defer conn.Close()
	if _, err := fmt.Fprintf(conn, "AUTH %s %s\n", token, namespace); err != nil {
		return fmt.Errorf("authenticate: %w", err)
	}
	reader := bufio.NewReader(conn)
	line, err := reader.ReadString('\n')
	if err != nil || line != "OK\n" {
		return errors.New("authentication failed")
	}
	inputDone := make(chan error, 1)
	go func() {
		_, err := io.Copy(conn, stdin)
		if tcp, ok := conn.NetConn().(*net.TCPConn); ok {
			_ = tcp.CloseWrite()
		}
		inputDone <- err
	}()
	outputDone := make(chan error, 1)
	go func() {
		_, err := io.Copy(stdout, reader)
		outputDone <- err
	}()
	var inputErr, outputErr error
	select {
	case outputErr = <-outputDone:
		// The remote side ended first. Closing the connection makes a pending
		// socket write fail; a goroutine blocked reading an interactive stdin
		// is harmless because process exit releases it.
		_ = conn.Close()
		return copyError("receive output", outputErr)
	case inputErr = <-inputDone:
		// A stdin EOF is a half-close: retain the read side so the remote can
		// flush its final protocol response before it exits.
		outputErr = <-outputDone
	}
	if inputErr != nil && !errors.Is(inputErr, net.ErrClosed) {
		return fmt.Errorf("send input: %w", inputErr)
	}
	return copyError("receive output", outputErr)
}

func copyError(operation string, err error) error {
	if err != nil && !errors.Is(err, net.ErrClosed) {
		return fmt.Errorf("%s: %w", operation, err)
	}
	return nil
}
