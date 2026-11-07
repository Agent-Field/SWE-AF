package hitl

import (
	"context"
	"strings"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/agent"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

type nativePauser struct {
	answer agent.ApprovalResult
	seen   []agent.PauseOptions
}

func (n *nativePauser) Pause(_ context.Context, opts agent.PauseOptions) (*agent.ApprovalResult, error) {
	n.seen = append(n.seen, opts)
	r := n.answer
	return &r, nil
}

// With SWE_HITL_NATIVE on and no hax, an ask_user form pauses on the control
// plane with its fields in the body, and the person's words come back as the
// answer the reasoner is re-invoked with.
func TestAskUserWithoutHaxAsksOnTheControlPlane(t *testing.T) {
	t.Setenv(EnvNativeHITL, "1")
	desc := "Two storage options fit."
	form := map[string]any{"title": "Which store?", "description": desc, "fields": []any{
		map[string]any{"id": "store", "type": "radio", "label": "Store", "required": true,
			"options": []any{map[string]any{"label": "SQLite", "value": "sqlite"}, map[string]any{"label": "Files", "value": "files"}}},
	}}
	p := &nativePauser{answer: agent.ApprovalResult{Decision: "request_changes", Feedback: "use SQLite"}}
	calls := 0
	var prior any
	invoke := func(_ context.Context, kw map[string]any) (map[string]any, error) {
		calls++
		if calls == 1 {
			return map[string]any{"ask_user_form": form}, nil
		}
		prior = kw["prior_user_responses"]
		return map[string]any{"ask_user_form": nil, "ok": true}, nil
	}
	out, err := RunWithAskUser(context.Background(), invoke, nil, RunWithAskUserParams{
		App: &silentApp{}, Pauser: p, ExecutionID: "exec-9", Budget: &AskUserBudget{Remaining: 2},
	})
	if err != nil {
		t.Fatal(err)
	}
	if out["ok"] != true || len(p.seen) != 1 {
		t.Fatalf("out=%v pauses=%d", out, len(p.seen))
	}
	got := p.seen[0]
	if got.Kind != KindQuestion || got.Question != "Which store?" || !strings.HasPrefix(got.ApprovalRequestID, "swe-ask-exec-9-") {
		t.Fatalf("pause = %+v", got)
	}
	for _, want := range []string{"Two storage options fit.", "**Store** (required)", "SQLite · Files"} {
		if !strings.Contains(got.Body, want) {
			t.Errorf("body missing %q:\n%s", want, got.Body)
		}
	}
	list := prior.([]any)
	entry := list[0].(map[string]any)
	if entry["status"] != "submitted" || entry["values"].(map[string]any)["answer"] != "use SQLite" {
		t.Fatalf("prior = %v", entry)
	}
}

// Off by default: no hax and no SWE_HITL_NATIVE still skips, as before.
func TestAskUserNativeIsOptIn(t *testing.T) {
	t.Setenv(EnvNativeHITL, "")
	p := &nativePauser{answer: agent.ApprovalResult{Decision: "approved"}}
	invoke := func(context.Context, map[string]any) (map[string]any, error) {
		return map[string]any{"ask_user_form": map[string]any{"title": "q", "fields": []any{}}}, nil
	}
	if _, err := RunWithAskUser(context.Background(), invoke, nil, RunWithAskUserParams{
		App: &silentApp{}, Pauser: p, ExecutionID: "e",
	}); err != nil {
		t.Fatal(err)
	}
	if len(p.seen) != 0 {
		t.Fatal("native ask must be opt-in")
	}
}

func TestOneLineCapsTheQuestion(t *testing.T) {
	q := NativeQuestion{Question: strings.Repeat("é", 400) + "\nsecond"}
	got := q.PauseOptions().Question
	if len(got) > 480 || strings.Contains(got, "\n") {
		t.Fatalf("len=%d %q", len(got), got[:20])
	}
	_ = schemas.AskUserForm{}
}
