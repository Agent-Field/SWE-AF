//go:build functional

package functional

import (
	"net/http"
	"testing"
)

// TestHealth asserts both Go nodes serve /health with a 200 (design §11(b),
// T7.1 acceptance). By the time TestMain returns, readiness has already been
// polled, so this is a direct single-shot assertion.
//
// Contract: GET :8005/health -> 200 ; GET :8006/health -> 200.
func TestHealth(t *testing.T) {
	requireStack(t)

	cases := []struct {
		name string
		url  string
	}{
		{"swe-planner-go", plannerBaseURL + "/health"},
		{"swe-fast-go", fastBaseURL + "/health"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			body, status, err := httpGet(tc.url)
			if err != nil {
				t.Fatalf("GET %s: %v", tc.url, err)
			}
			if status != http.StatusOK {
				t.Fatalf("GET %s -> %d, want 200 (body: %s)", tc.url, status, string(body))
			}
		})
	}
}
