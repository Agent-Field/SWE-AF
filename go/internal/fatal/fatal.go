// Package fatal detects non-retryable harness failures (billing exhaustion,
// invalid credentials, disabled accounts, codex model/auth mismatches).
//
// It is a 1:1 port of swe_af/execution/fatal_error.py. When the underlying API
// returns a fatal error, retrying is pointless and wastes time, so
// FatalHarnessError is a distinct error type that short-circuits every retry
// layer. CheckFatalHarnessError inspects a *harness.Result and returns a
// FatalHarnessError immediately on match, so the real error message surfaces
// instead of a misleading generic one.
//
// Ref: https://github.com/Agent-Field/SWE-AF/issues/49
package fatal

import (
	"fmt"
	"regexp"

	"github.com/Agent-Field/agentfield/sdk/go/harness"
)

// fatalPatterns lists the patterns that indicate a non-retryable API failure.
// They are matched case-insensitively (the (?i) flag mirrors Python's
// re.IGNORECASE) against error_message strings. The pattern set is a verbatim
// port of _FATAL_PATTERNS in fatal_error.py — keep it byte-for-byte identical.
var fatalPatterns = compilePatterns(
	`credit balance is too low`,
	`insufficient.{0,20}credits?`,
	`billing.{0,20}(expired|inactive|suspended)`,
	`invalid.{0,10}api.?key`,
	`invalid.{0,10}x-api-key`,
	`(your )?api key is not valid`,
	`authentication failed`,
	`account has been disabled`,
	`account.{0,10}is disabled`,
	`unauthorized`,
	`quota.{0,20}exceeded`,
	// Codex model/auth mismatches: retrying with the same model + auth mode
	// fails identically. Treating these as fatal surfaces the real reason
	// (instead of a silent empty build that burns the retry cap) — e.g. the
	// default `-codex` model under ChatGPT-account auth (#82 Gap 3).
	`not supported when using codex with a chatgpt account`,
	`requires a newer version of codex`,
)

// compilePatterns compiles each pattern once with the case-insensitive flag.
func compilePatterns(patterns ...string) []*regexp.Regexp {
	compiled := make([]*regexp.Regexp, len(patterns))
	for i, p := range patterns {
		compiled[i] = regexp.MustCompile(`(?i)` + p)
	}
	return compiled
}

// FatalHarnessError is returned when the harness encounters a non-retryable API
// error. It is designed to propagate through all retry layers (schema retries,
// SDK execution retries, pipeline retries) without being swallowed by generic
// error handling that would otherwise silently retry.
//
// It mirrors the Python FatalHarnessError(RuntimeError): Error() reproduces the
// exact "Fatal API error (non-retryable): <message>" text, and OriginalMessage
// carries the untouched underlying message (Python's .original_message).
type FatalHarnessError struct {
	OriginalMessage string
}

// Error returns the wrapped message, byte-identical to the Python exception's
// str() form.
func (e *FatalHarnessError) Error() string {
	return fmt.Sprintf("Fatal API error (non-retryable): %s", e.OriginalMessage)
}

// IsFatalError reports whether errorMessage matches a known fatal API error
// pattern. An empty string is never fatal (mirrors the Python guard).
func IsFatalError(errorMessage string) bool {
	if errorMessage == "" {
		return false
	}
	for _, p := range fatalPatterns {
		if p.MatchString(errorMessage) {
			return true
		}
	}
	return false
}

// CheckFatalHarnessError inspects a harness result and returns a
// *FatalHarnessError if it indicates a non-retryable API failure, or nil
// otherwise.
//
// It should be called immediately after the harness returns, before any
// Result.Parsed == nil check, so the real error message surfaces instead of a
// misleading generic one. It reads Result.IsError and Result.ErrorMessage (the
// Go equivalents of the Python HarnessResult's is_error / error_message).
func CheckFatalHarnessError(result *harness.Result) error {
	if result == nil || !result.IsError {
		return nil
	}
	msg := result.ErrorMessage
	if IsFatalError(msg) {
		return &FatalHarnessError{OriginalMessage: msg}
	}
	return nil
}
