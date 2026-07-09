package reasonerfail

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// stubExecID overrides the execution-id seam for the duration of a test and
// restores it afterwards.
func stubExecID(t *testing.T, id string) {
	t.Helper()
	orig := executionIDFromCtx
	executionIDFromCtx = func(context.Context) string { return id }
	t.Cleanup(func() { executionIDFromCtx = orig })
}

// captured records what a stub control-plane server received.
type captured struct {
	called      bool
	method      string
	path        string
	contentType string
	authz       string
	body        map[string]any
}

// newCPServer returns an httptest server that records the first request it
// receives into c.
func newCPServer(t *testing.T, c *captured) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c.called = true
		c.method = r.Method
		c.path = r.URL.Path
		c.contentType = r.Header.Get("Content-Type")
		c.authz = r.Header.Get("Authorization")
		_ = json.NewDecoder(r.Body).Decode(&c.body)
		w.WriteHeader(http.StatusOK)
	}))
}

// Contract: POSTs the exact body shape to the exact path, and returns a
// non-nil error carrying the message.
func TestPostFailedWithResult_PostsExactBodyToExactPath(t *testing.T) {
	stubExecID(t, "exec-123")

	var c captured
	srv := newCPServer(t, &c)
	defer srv.Close()

	cfg := PosterConfig{AgentFieldURL: srv.URL, Token: "tok-abc", HTTPClient: srv.Client()}
	result := map[string]any{"success": false, "summary": "0/3 issues completed"}

	err := PostFailedWithResult(context.Background(), cfg, result, "Build failed: 0/3 issues completed, no branches merged")

	// Non-nil error carrying the message.
	if err == nil {
		t.Fatal("expected non-nil error")
	}
	if err.Error() != "Build failed: 0/3 issues completed, no branches merged" {
		t.Fatalf("error message = %q, want the build-failed message", err.Error())
	}

	// Do() blocks until the handler responds, so the request is recorded by now.
	if !c.called {
		t.Fatal("expected the control plane to be called")
	}
	if c.method != http.MethodPost {
		t.Errorf("method = %q, want POST", c.method)
	}
	if c.path != "/api/v1/executions/exec-123/status" {
		t.Errorf("path = %q, want /api/v1/executions/exec-123/status", c.path)
	}
	if c.contentType != "application/json" {
		t.Errorf("content-type = %q, want application/json", c.contentType)
	}
	if c.authz != "Bearer tok-abc" {
		t.Errorf("authorization = %q, want Bearer tok-abc", c.authz)
	}

	// Exact body shape: status/result/error/completed_at, and only those keys.
	if got := c.body["status"]; got != "failed" {
		t.Errorf("body.status = %v, want failed", got)
	}
	if got := c.body["error"]; got != "Build failed: 0/3 issues completed, no branches merged" {
		t.Errorf("body.error = %v, want the message", got)
	}
	res, ok := c.body["result"].(map[string]any)
	if !ok {
		t.Fatalf("body.result is not an object: %T", c.body["result"])
	}
	if res["success"] != false {
		t.Errorf("body.result.success = %v, want false", res["success"])
	}
	if res["summary"] != "0/3 issues completed" {
		t.Errorf("body.result.summary = %v", res["summary"])
	}
	completedAt, ok := c.body["completed_at"].(string)
	if !ok || completedAt == "" {
		t.Fatalf("body.completed_at missing/empty: %v", c.body["completed_at"])
	}
	if _, perr := time.Parse(time.RFC3339, completedAt); perr != nil {
		t.Errorf("body.completed_at %q is not RFC3339: %v", completedAt, perr)
	}
	for k := range c.body {
		switch k {
		case "status", "result", "error", "completed_at":
		default:
			t.Errorf("unexpected body key %q", k)
		}
	}
}

// Contract: empty ExecutionID → no POST, error still returned.
func TestPostFailedWithResult_EmptyExecutionIDSkipsPost(t *testing.T) {
	stubExecID(t, "")

	var c captured
	srv := newCPServer(t, &c)
	defer srv.Close()

	cfg := PosterConfig{AgentFieldURL: srv.URL, HTTPClient: srv.Client()}
	err := PostFailedWithResult(context.Background(), cfg, map[string]any{"a": 1}, "boom")

	if err == nil || err.Error() != "boom" {
		t.Fatalf("error = %v, want non-nil carrying %q", err, "boom")
	}
	if c.called {
		t.Error("expected no POST when ExecutionID is empty")
	}
}

// Contract: a non-object result → returns error without POSTing garbage.
func TestPostFailedWithResult_NonObjectResultSkipsPost(t *testing.T) {
	// Non-empty exec id so it is the object-guard, not a missing id, that
	// prevents the POST.
	stubExecID(t, "exec-9")

	cases := []struct {
		name   string
		result any
	}{
		{"string", "just a string"},
		{"number", 42},
		{"bool", true},
		{"slice", []int{1, 2, 3}},
		{"nil", nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var c captured
			srv := newCPServer(t, &c)
			defer srv.Close()

			cfg := PosterConfig{AgentFieldURL: srv.URL, HTTPClient: srv.Client()}
			err := PostFailedWithResult(context.Background(), cfg, tc.result, "nope")

			if err == nil || err.Error() != "nope" {
				t.Fatalf("error = %v, want non-nil carrying %q", err, "nope")
			}
			if c.called {
				t.Errorf("expected no POST for non-object result %v", tc.result)
			}
		})
	}
}

// Contract: an empty object is a valid object and is posted.
func TestPostFailedWithResult_EmptyObjectIsPosted(t *testing.T) {
	stubExecID(t, "exec-e")

	var c captured
	srv := newCPServer(t, &c)
	defer srv.Close()

	cfg := PosterConfig{AgentFieldURL: srv.URL, HTTPClient: srv.Client()}
	err := PostFailedWithResult(context.Background(), cfg, map[string]any{}, "empty")

	if err == nil || err.Error() != "empty" {
		t.Fatalf("error = %v, want non-nil carrying %q", err, "empty")
	}
	if !c.called {
		t.Fatal("expected a POST for an empty-object result")
	}
	if _, ok := c.body["result"].(map[string]any); !ok {
		t.Errorf("body.result should be an (empty) object, got %T", c.body["result"])
	}
}

// The best-effort POST must never surface a transport error; the message error
// is returned even when the control plane is unreachable.
func TestPostFailedWithResult_BestEffortSwallowsTransportError(t *testing.T) {
	stubExecID(t, "exec-x")

	// Closed server → connection refused.
	srv := newCPServer(t, &captured{})
	url := srv.URL
	srv.Close()

	cfg := PosterConfig{AgentFieldURL: url}
	err := PostFailedWithResult(context.Background(), cfg, map[string]any{"a": 1}, "still fails")
	if err == nil || err.Error() != "still fails" {
		t.Fatalf("error = %v, want non-nil carrying %q despite transport failure", err, "still fails")
	}
}
