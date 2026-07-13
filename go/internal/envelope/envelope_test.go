package envelope

import (
	"errors"
	"reflect"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/fatal"
)

// TestUnwrapCallResult_FailedFatalStatus: an envelope with status "failed" and a
// fatal error_message yields a *fatal.FatalHarnessError, reachable via errors.As.
func TestUnwrapCallResult_FailedFatalStatus(t *testing.T) {
	raw := map[string]any{
		"status":        "failed",
		"error_message": "credit balance is too low",
	}
	out, err := UnwrapCallResult(raw, "run_coder")
	if out != nil {
		t.Errorf("expected nil result on failure, got %v", out)
	}
	var fhe *fatal.FatalHarnessError
	if !errors.As(err, &fhe) {
		t.Fatalf("expected *fatal.FatalHarnessError, got %T: %v", err, err)
	}
	if fhe.OriginalMessage != "credit balance is too low" {
		t.Errorf("OriginalMessage = %q", fhe.OriginalMessage)
	}
}

// TestUnwrapCallResult_FailedNonFatalStatus: a terminal-failure status with a
// non-fatal error yields a plain error whose message mirrors Python's
// RuntimeError verbatim.
func TestUnwrapCallResult_FailedNonFatalStatus(t *testing.T) {
	cases := []struct {
		name string
		raw  map[string]any
		want string
	}{
		{
			name: "error_message_used",
			raw:  map[string]any{"status": "error", "error_message": "something broke"},
			want: "run_qa failed (status=error): something broke",
		},
		{
			name: "falls_back_to_error_key",
			raw:  map[string]any{"status": "timeout", "error": "deadline exceeded"},
			want: "run_qa failed (status=timeout): deadline exceeded",
		},
		{
			name: "falls_back_to_unknown",
			raw:  map[string]any{"status": "cancelled"},
			want: "run_qa failed (status=cancelled): unknown",
		},
		{
			name: "empty_error_message_falls_through",
			raw:  map[string]any{"status": "failed", "error_message": "", "error": "real cause"},
			want: "run_qa failed (status=failed): real cause",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			out, err := UnwrapCallResult(tc.raw, "run_qa")
			if out != nil {
				t.Errorf("expected nil result, got %v", out)
			}
			var fhe *fatal.FatalHarnessError
			if errors.As(err, &fhe) {
				t.Fatalf("expected plain error, got FatalHarnessError")
			}
			if err == nil || err.Error() != tc.want {
				t.Errorf("err = %v, want %q", err, tc.want)
			}
		})
	}
}

// TestUnwrapCallResult_PlainDictPassesThrough: a map with no envelope keys is
// already unwrapped and passes through unchanged.
func TestUnwrapCallResult_PlainDictPassesThrough(t *testing.T) {
	raw := map[string]any{"complete": true, "summary": "did the thing", "files": []any{"a.go"}}
	out, err := UnwrapCallResult(raw, "run_coder")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !reflect.DeepEqual(out, raw) {
		t.Errorf("out = %v, want unchanged %v", out, raw)
	}
}

// TestUnwrapCallResult_ResultExtracted: a non-failure envelope with an object
// result returns the inner result.
func TestUnwrapCallResult_ResultExtracted(t *testing.T) {
	inner := map[string]any{"complete": true, "summary": "ok"}
	raw := map[string]any{
		"execution_id": "e-1",
		"status":       "succeeded",
		"result":       inner,
	}
	out, err := UnwrapCallResult(raw, "run_coder")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !reflect.DeepEqual(out, inner) {
		t.Errorf("out = %v, want inner %v", out, inner)
	}
}

// TestUnwrapCallResult_EnvelopeWithNilResult: an envelope whose result is nil and
// whose status is not a failure returns the envelope as-is for the caller to
// validate.
func TestUnwrapCallResult_EnvelopeWithNilResult(t *testing.T) {
	raw := map[string]any{
		"execution_id": "e-2",
		"status":       "succeeded",
		"result":       nil,
	}
	out, err := UnwrapCallResult(raw, "run_coder")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !reflect.DeepEqual(out, raw) {
		t.Errorf("out = %v, want envelope %v", out, raw)
	}
}

// TestUnwrapCallResult_Nil: nil input returns nil without error.
func TestUnwrapCallResult_Nil(t *testing.T) {
	out, err := UnwrapCallResult(nil, "call")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if out != nil {
		t.Errorf("out = %v, want nil", out)
	}
}
