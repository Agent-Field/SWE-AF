package fatal

import (
	"errors"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/harness"
)

// TestIsFatalError_EachPatternMatchesCaseInsensitively derives one case per
// fatal pattern (contract: each pattern matches case-insensitively). The inputs
// use mixed/upper case to prove the (?i) flag is honored.
func TestIsFatalError_EachPatternMatchesCaseInsensitively(t *testing.T) {
	cases := []struct {
		name string
		msg  string
	}{
		{"credit_balance", "Your CREDIT Balance Is Too Low to proceed"},
		{"insufficient_credits", "Error: INSUFFICIENT account credits"},
		{"insufficient_credit_singular", "insufficient credit"},
		{"billing_expired", "BILLING subscription has EXPIRED"},
		{"billing_inactive", "billing is inactive"},
		{"billing_suspended", "billing account suspended"},
		{"invalid_api_key", "Invalid API Key provided"},
		{"invalid_apikey_no_sep", "invalid apikey"},
		{"invalid_x_api_key", "Invalid X-API-Key header"},
		{"api_key_not_valid_your", "Your API key is not valid"},
		{"api_key_not_valid_bare", "api key is not valid"},
		{"authentication_failed", "AUTHENTICATION Failed for request"},
		{"account_has_been_disabled", "This ACCOUNT Has Been Disabled"},
		{"account_is_disabled", "account is disabled"},
		{"unauthorized", "HTTP 401 UNAUTHORIZED"},
		{"quota_exceeded", "API QUOTA has been exceeded"},
		{"codex_chatgpt_account", "Model not supported when using codex with a chatgpt account"},
		{"codex_newer_version", "This requires a newer version of codex"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if !IsFatalError(tc.msg) {
				t.Errorf("IsFatalError(%q) = false, want true", tc.msg)
			}
		})
	}
}

// TestIsFatalError_BenignMessages verifies the negative half of the contract: a
// benign message (and the empty string) is not classified as fatal.
func TestIsFatalError_BenignMessages(t *testing.T) {
	benign := []string{
		"",
		"everything is fine, tests passed",
		"rate limited, please retry shortly",
		"connection reset by peer",
		"the coder produced valid output",
	}
	for _, msg := range benign {
		if IsFatalError(msg) {
			t.Errorf("IsFatalError(%q) = true, want false", msg)
		}
	}
}

// TestFatalHarnessError_Message asserts the wrapped message is byte-identical to
// the Python exception's str() form and that OriginalMessage is untouched.
func TestFatalHarnessError_Message(t *testing.T) {
	e := &FatalHarnessError{OriginalMessage: "credit balance is too low"}
	want := "Fatal API error (non-retryable): credit balance is too low"
	if got := e.Error(); got != want {
		t.Errorf("Error() = %q, want %q", got, want)
	}
	if e.OriginalMessage != "credit balance is too low" {
		t.Errorf("OriginalMessage = %q, want %q", e.OriginalMessage, "credit balance is too low")
	}
}

// TestCheckFatalHarnessError covers the three branches: fatal error -> typed
// error via errors.As; error-but-benign -> nil; no error -> nil; nil result ->
// nil.
func TestCheckFatalHarnessError(t *testing.T) {
	t.Run("fatal_error_returns_typed_error", func(t *testing.T) {
		r := &harness.Result{IsError: true, ErrorMessage: "your credit balance is too low"}
		err := CheckFatalHarnessError(r)
		var fhe *FatalHarnessError
		if !errors.As(err, &fhe) {
			t.Fatalf("expected *FatalHarnessError, got %v", err)
		}
		if fhe.OriginalMessage != "your credit balance is too low" {
			t.Errorf("OriginalMessage = %q", fhe.OriginalMessage)
		}
	})

	t.Run("error_but_benign_returns_nil", func(t *testing.T) {
		r := &harness.Result{IsError: true, ErrorMessage: "temporary network blip"}
		if err := CheckFatalHarnessError(r); err != nil {
			t.Errorf("expected nil, got %v", err)
		}
	})

	t.Run("not_an_error_returns_nil", func(t *testing.T) {
		r := &harness.Result{IsError: false, ErrorMessage: "your credit balance is too low"}
		if err := CheckFatalHarnessError(r); err != nil {
			t.Errorf("expected nil (is_error false short-circuits), got %v", err)
		}
	})

	t.Run("nil_result_returns_nil", func(t *testing.T) {
		if err := CheckFatalHarnessError(nil); err != nil {
			t.Errorf("expected nil, got %v", err)
		}
	})
}
