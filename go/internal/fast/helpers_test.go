package fast

import (
	"context"
	"encoding/json"
	"sync"

	"github.com/Agent-Field/agentfield/sdk/go/harness"
)

// ---------------------------------------------------------------------------
// Shared test doubles
// ---------------------------------------------------------------------------

// recordedNote captures a single note() call for assertions.
type recordedNote struct {
	message string
	tags    []string
}

// noteRecorder is the Noter seam. It records every note so tests can assert the
// verbatim message/tag strings the Python reasoners emit.
type noteRecorder struct {
	notes []recordedNote
}

func (n *noteRecorder) Note(_ context.Context, message string, tags ...string) {
	n.notes = append(n.notes, recordedNote{message: message, tags: tags})
}

func (n *noteRecorder) hasTag(tag string) bool {
	for _, r := range n.notes {
		for _, t := range r.tags {
			if t == tag {
				return true
			}
		}
	}
	return false
}

// mockHarness is the harnessx.HarnessCaller seam — the Go equivalent of patching
// router.harness. fn receives the dest pointer so it can populate structured
// output, and returns the (*harness.Result, error) reply.
type mockHarness struct {
	fn     func(dest any) (*harness.Result, error)
	called bool
}

func (m *mockHarness) Harness(_ context.Context, _ string, _ map[string]any, dest any, _ harness.Options) (*harness.Result, error) {
	m.called = true
	return m.fn(dest)
}

// parsedResult builds a mockHarness fn that unmarshals bodyJSON into dest and
// returns a success Result whose Parsed points at dest (so harnessx.Run treats
// it as a clean parse).
func parsedResult(bodyJSON string) func(dest any) (*harness.Result, error) {
	return func(dest any) (*harness.Result, error) {
		if err := json.Unmarshal([]byte(bodyJSON), dest); err != nil {
			return nil, err
		}
		return &harness.Result{Parsed: dest}, nil
	}
}

// nilParsedResult builds a mockHarness fn returning a Result with Parsed==nil
// (the harness ran but produced no parseable structured output).
func nilParsedResult(errMsg string) func(dest any) (*harness.Result, error) {
	return func(_ any) (*harness.Result, error) {
		return &harness.Result{Parsed: nil, IsError: true, ErrorMessage: errMsg}, nil
	}
}

// callRecord captures one CallFn invocation.
type callRecord struct {
	target string
	kwargs map[string]any
}

// callScripter records CallFn invocations and dispatches to a scripted fn. The
// executor invokes call() from worker goroutines, and callWithTimeout can leak a
// still-running worker past a timeout (executor.go), so the append and every
// read of calls are guarded by mu to keep `go test -race` clean.
type callScripter struct {
	mu    sync.Mutex
	calls []callRecord
	fn    func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error)
}

func (c *callScripter) call(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
	c.mu.Lock()
	c.calls = append(c.calls, callRecord{target: target, kwargs: kwargs})
	c.mu.Unlock()
	return c.fn(ctx, target, kwargs)
}

// targets returns the ordered list of call targets, for order assertions.
func (c *callScripter) targets() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]string, len(c.calls))
	for i, r := range c.calls {
		out[i] = r.target
	}
	return out
}

// snapshot returns a copy of the recorded calls under lock — the safe way for
// assertions to read individual records without racing a leaked worker.
func (c *callScripter) snapshot() []callRecord {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]callRecord(nil), c.calls...)
}

// count returns the number of recorded calls under lock.
func (c *callScripter) count() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.calls)
}

// byTargetSuffix dispatches based on a substring match of the target, mirroring
// the Python tests' `if key in target` pattern. Unmatched targets return {}.
func byTargetSuffix(responses map[string]map[string]any) func(context.Context, string, map[string]any) (map[string]any, error) {
	return func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
		for key, value := range responses {
			if containsSub(target, key) {
				return value, nil
			}
		}
		return map[string]any{}, nil
	}
}

func containsSub(s, sub string) bool {
	return len(sub) == 0 || (len(s) >= len(sub) && indexOf(s, sub) >= 0)
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
