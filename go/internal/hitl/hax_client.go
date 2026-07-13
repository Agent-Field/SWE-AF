package hitl

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"
)

// HaxCreateRequestTimeout is the hard cap on a single hax create-request call.
//
// Ported from swe_af/hitl/ask_user.py::HAX_CREATE_REQUEST_TIMEOUT_SECONDS (120s)
// and swe_af/app.py::HAX_CREATE_REQUEST_TIMEOUT_SECONDS. The hax REST service is
// synchronous and has occasionally wedged for over an hour in production
// (run_1778512783034_f4985c96); an explicit client-side timeout keeps a hung
// hax from silently chewing through the parent execution's active-time budget.
const HaxCreateRequestTimeout = 120 * time.Second

// DefaultHaxBaseURL mirrors swe_af/hitl/ask_user.py::build_hax_client_from_env,
// which defaults HAX_SDK_URL to http://localhost:3000.
const DefaultHaxBaseURL = "http://localhost:3000"

// HaxClient is a thin REST client for the hax human-input service.
//
// The Python port uses the hax-sdk Python client; Go calls the same REST
// surface directly (design §4.6): POST {BaseURL}/api/v1/requests with a Bearer
// token. SenderName / SenderKey identify the requesting sender when the
// deployment configures one; they are not part of the ask-user create-request
// body (which mirrors the Python ask_user path exactly) and are carried here
// only so callers that need sender attribution can set them.
type HaxClient struct {
	// BaseURL is the hax service origin WITHOUT the /api/v1 suffix (that suffix
	// is appended by CreateRequest). Default: DefaultHaxBaseURL.
	BaseURL string
	// APIKey is the hax API key sent as "Authorization: Bearer <key>".
	APIKey string
	// SenderName / SenderKey optionally identify the requesting sender.
	SenderName string
	SenderKey  string
	// HTTPClient is used for all requests; defaults to a fresh http.Client.
	HTTPClient *http.Client
	// Timeout overrides HaxCreateRequestTimeout when non-zero (tests use this).
	Timeout time.Duration
}

// BuildHaxClientFromEnv constructs a HaxClient from HAX_API_KEY / HAX_SDK_URL.
//
// Returns nil when HAX_API_KEY is unset or blank — callers MUST treat a nil
// client as "HITL disabled" and short-circuit any ask-user logic (mirrors
// swe_af/hitl/ask_user.py::build_hax_client_from_env returning None).
func BuildHaxClientFromEnv() *HaxClient {
	apiKey := strings.TrimSpace(os.Getenv("HAX_API_KEY"))
	if apiKey == "" {
		return nil
	}
	base := strings.TrimSpace(os.Getenv("HAX_SDK_URL"))
	if base == "" {
		base = DefaultHaxBaseURL
	}
	return &HaxClient{
		BaseURL:    strings.TrimRight(base, "/"),
		APIKey:     apiKey,
		SenderName: strings.TrimSpace(os.Getenv("HAX_SENDER_NAME")),
		SenderKey:  strings.TrimSpace(os.Getenv("HAX_SENDER_KEY")),
	}
}

// ApprovalWebhookURL resolves the control-plane webhook the CP calls back when
// a human responds. Mirrors swe_af/hitl/ask_user.py::approval_webhook_url:
// {cp_base}/api/v1/webhooks/approval-response. Returns "" when the server URL
// is empty (Python returns None).
func ApprovalWebhookURL(agentFieldServer string) string {
	base := strings.TrimRight(strings.TrimSpace(agentFieldServer), "/")
	if base == "" {
		return ""
	}
	return base + "/api/v1/webhooks/approval-response"
}

// CreateRequestParams are the inputs to CreateRequest. Only fields the ask-user
// path uses are modeled; each maps to a camelCase body key exactly as the
// hax-sdk Python client builds it (client.py::create_request).
type CreateRequestParams struct {
	Type             string         // -> "type" (e.g. "form-builder")
	Payload          map[string]any // -> "payload"
	Title            string         // -> "title" (omitted when empty)
	Description      *string        // -> "description" (omitted when nil)
	WebhookURL       string         // -> "webhookUrl" (omitted when empty)
	ExpiresInSeconds int            // -> "expiresInSeconds" (omitted when <= 0)
	UserID           string         // -> "userId" (omitted when empty)
	Metadata         map[string]any // -> "metadata" (omitted when nil)
}

// CreatedRequest is the subset of the hax create-request response we consume.
type CreatedRequest struct {
	ID  string `json:"id"`
	URL string `json:"url"`
}

// CreateRequest POSTs {BaseURL}/api/v1/requests with a Bearer token and a
// camelCase JSON body, bounded by a hard timeout (HaxCreateRequestTimeout, or
// HaxClient.Timeout when set). A timeout surfaces as an error rather than a
// silent multi-hour stall.
func (c *HaxClient) CreateRequest(ctx context.Context, p CreateRequestParams) (*CreatedRequest, error) {
	body := map[string]any{
		"type":    p.Type,
		"payload": p.Payload,
	}
	if p.Title != "" {
		body["title"] = p.Title
	}
	if p.Description != nil {
		body["description"] = *p.Description
	}
	if p.WebhookURL != "" {
		body["webhookUrl"] = p.WebhookURL
	}
	if p.ExpiresInSeconds > 0 {
		body["expiresInSeconds"] = p.ExpiresInSeconds
	}
	if p.UserID != "" {
		body["userId"] = p.UserID
	}
	if p.Metadata != nil {
		body["metadata"] = p.Metadata
	}
	// publicKey is intentionally omitted so hax returns plaintext response
	// values end-to-end (design §4.6 note).

	raw, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("hax create_request: marshal body: %w", err)
	}

	timeout := c.Timeout
	if timeout <= 0 {
		timeout = HaxCreateRequestTimeout
	}
	reqCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	url := c.BaseURL + "/api/v1/requests"
	req, err := http.NewRequestWithContext(reqCtx, http.MethodPost, url, bytes.NewReader(raw))
	if err != nil {
		return nil, fmt.Errorf("hax create_request: build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+c.APIKey)

	httpClient := c.HTTPClient
	if httpClient == nil {
		httpClient = http.DefaultClient
	}

	resp, err := httpClient.Do(req)
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) || errors.Is(reqCtx.Err(), context.DeadlineExceeded) {
			return nil, fmt.Errorf(
				"hax create_request (%s) timed out after %s; hax-sdk is likely wedged: %w",
				p.Type, timeout, err,
			)
		}
		return nil, fmt.Errorf("hax create_request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("hax create_request: read response: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("hax create_request: status %d: %s", resp.StatusCode, strings.TrimSpace(string(respBody)))
	}

	var created CreatedRequest
	if err := json.Unmarshal(respBody, &created); err != nil {
		return nil, fmt.Errorf("hax create_request: decode response: %w", err)
	}
	if created.ID == "" {
		return nil, fmt.Errorf("hax create_request: response missing id: %s", strings.TrimSpace(string(respBody)))
	}
	return &created, nil
}
