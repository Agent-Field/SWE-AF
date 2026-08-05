package main

import (
	"bytes"
	"strings"
	"testing"
)

func TestDialRejectsMissingNamespace(t *testing.T) {
	if err := dial(nil, strings.NewReader(""), &bytes.Buffer{}); err == nil {
		t.Fatal("dial accepted missing namespace")
	}
}

func TestDialRejectsMissingAddress(t *testing.T) {
	t.Setenv("FURROW_DIAL_ADDR", "")
	t.Setenv("FURROW_DIAL_TOKEN", "token")
	if err := dial([]string{"workspace"}, strings.NewReader(""), &bytes.Buffer{}); err == nil {
		t.Fatal("dial accepted missing address")
	}
}
