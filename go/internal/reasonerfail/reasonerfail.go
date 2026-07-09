// Package reasonerfail replicates Python's ReasonerFailed(message, result=...)
// carrier without modifying the Go SDK.
//
// A Go reasoner handler that simply returns a value — even one whose payload
// says success=false — is recorded by the SDK's async handler as `succeeded`
// (it only distinguishes "returned" from "raised", never inspecting the
// result). To surface a genuinely failed outcome as `failed` while still
// preserving the structured result on the execution record, the handler first
// POSTs status=failed together with the result to the control plane's
// execution-status endpoint, then returns a plain error(message).
//
// The control plane persists `result` regardless of status, and the SDK's own
// subsequent resultless failed-status POST (see agent.sendExecutionStatus)
// leaves our result intact because the CP only overwrites result when the new
// payload carries one. Net record: status=failed, result=<obj>, error=message —
// byte-identical to the Python ReasonerFailed path (design §4.5).
package reasonerfail

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/Agent-Field/agentfield/sdk/go/agent"
)

// PosterConfig is the minimal seam PostFailedWithResult needs to reach the
// control plane: the base URL, the auth token, and an HTTP client. It is a
// small struct rather than *agent.Agent (whose config is unexported) so tests
// can point it at an httptest server. HTTPClient may be nil, in which case
// http.DefaultClient is used.
type PosterConfig struct {
	AgentFieldURL string
	Token         string
	HTTPClient    *http.Client
}

// executionIDFromCtx is a seam over agent.ExecutionContextFrom so tests can
// inject an execution id — the SDK exposes no exported setter for the
// execution context carried in a context.Context. Production reads the
// ExecutionID exactly as the design specifies.
var executionIDFromCtx = func(ctx context.Context) string {
	return agent.ExecutionContextFrom(ctx).ExecutionID
}

// statusBody is the exact status-update payload posted to the control plane.
// The CP status request binds status/result/error/completed_at (among others);
// these four keys are all it needs to record a failed-with-result outcome.
type statusBody struct {
	Status      string          `json:"status"`
	Result      json.RawMessage `json:"result"`
	Error       string          `json:"error"`
	CompletedAt string          `json:"completed_at"`
}

// PostFailedWithResult posts status=failed plus result to the control plane's
// execution-status endpoint and returns errors.New(message).
//
// The returned error always carries message (parity with raising
// ReasonerFailed(message, result=...)); the POST itself is best-effort — any
// transport/HTTP error is swallowed. result must marshal to a JSON object (the
// CP binds it into a map); a non-object result is not posted (no garbage on the
// wire) but the error is still returned. If the execution id is empty (no
// execution to attach to) the POST is skipped and the error is still returned.
func PostFailedWithResult(ctx context.Context, cfg PosterConfig, result any, message string) error {
	// The contract: return errors.New(message) regardless of what happens
	// with the best-effort POST below.
	failure := errors.New(message)

	// Guard: result must marshal to a JSON object, else there is nothing the
	// CP can persist — skip the POST rather than send garbage.
	body, ok := buildBody(result, message)
	if !ok {
		return failure
	}

	execID := executionIDFromCtx(ctx)
	if execID == "" {
		return failure
	}

	if strings.TrimSpace(cfg.AgentFieldURL) == "" {
		return failure
	}

	postStatus(ctx, cfg, execID, body)
	return failure
}

// buildBody marshals result and verifies it is a JSON object, then wraps it in
// the status payload. Returns ok=false when result is not a JSON object.
func buildBody(result any, message string) ([]byte, bool) {
	raw, err := json.Marshal(result)
	if err != nil {
		return nil, false
	}
	// Must be a JSON object. Reject strings/numbers/arrays and, importantly,
	// `null` (nil result → "null", which json.Unmarshal accepts into a map
	// without error). A JSON object marshals to text beginning with '{'.
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 || trimmed[0] != '{' {
		return nil, false
	}
	var probe map[string]json.RawMessage
	if err := json.Unmarshal(trimmed, &probe); err != nil {
		return nil, false
	}
	body, err := json.Marshal(statusBody{
		Status:      "failed",
		Result:      json.RawMessage(raw),
		Error:       message,
		CompletedAt: time.Now().UTC().Format(time.RFC3339),
	})
	if err != nil {
		return nil, false
	}
	return body, true
}

// postStatus performs the best-effort status POST. All errors are swallowed;
// the caller returns the message error regardless.
func postStatus(ctx context.Context, cfg PosterConfig, execID string, body []byte) {
	callbackURL := strings.TrimSuffix(strings.TrimSpace(cfg.AgentFieldURL), "/") +
		"/api/v1/executions/" + url.PathEscape(execID) + "/status"

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, callbackURL, bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	if cfg.Token != "" {
		req.Header.Set("Authorization", "Bearer "+cfg.Token)
	}

	client := cfg.HTTPClient
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return
	}
	_ = resp.Body.Close()
}
