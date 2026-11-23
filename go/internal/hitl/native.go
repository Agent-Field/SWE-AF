// This file (native.go) is SWE-AF's human-in-the-loop WITHOUT hax: the question
// itself travels to the AgentField control plane on the pause, so any answering
// door the control plane serves (WorkAF's Needs you, the CP UI, the CLI) can
// show it in full and resolve it. Hax stays an optional extra door: when a key
// is present the same pause also carries hax's request id and URL.
//
// The control plane's contract (HITL-CONTRACT.md, CP section) gives a pause four
// optional fields — kind, question (one line), body (full markdown) and option
// labels — and resolves it with approved / request_changes / rejected plus the
// person's words as feedback. A NATIVE PAUSE MUST CARRY EVERYTHING A PERSON
// NEEDS TO JUDGE IT IN ITS BODY: there is no other page to open.
package hitl

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync/atomic"

	"github.com/Agent-Field/agentfield/sdk/go/agent"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// Question kinds the control plane accepts on a pause.
const (
	KindPlanReview = "plan_review"
	KindQuestion   = "question"
)

// ReviewOptions are the three answers every SWE-AF pause offers. The control
// plane maps each label to the decision of the same meaning.
var ReviewOptions = []string{"approve", "request_changes", "reject"}

// EnvNativeHITL turns on pauses without hax for every role that can ask
// (ask_user forms, and the plan gate of a plain build). It is off by default so
// a deployment that never had a person behind its runs does not start blocking
// on questions nobody answers. build_issue — the reasoner WorkAF dispatches —
// asks for plan review by itself and does not need it.
const EnvNativeHITL = "SWE_HITL_NATIVE"

// NativeEnabled reports whether EnvNativeHITL is truthy.
func NativeEnabled() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(EnvNativeHITL))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

// askSeq numbers native ask_user rounds across the process.
var askSeq atomic.Int64

// NativeQuestion is what a native pause shows a person.
type NativeQuestion struct {
	// RequestID names this round of the question. Every round of a revision
	// loop needs a fresh one, because the control plane keeps the answered
	// round until the next pause. Empty lets the control plane mint one.
	RequestID string
	// RequestURL is an optional page that also shows the question (hax).
	RequestURL     string
	Kind           string
	Question       string
	Body           string
	Options        []string
	ExpiresInHours int
	ExecutionID    string
}

// PauseOptions renders the question as the SDK's pause options.
func (q NativeQuestion) PauseOptions() agent.PauseOptions {
	opts := q.Options
	if len(opts) == 0 {
		opts = ReviewOptions
	}
	return agent.PauseOptions{
		ApprovalRequestID:  q.RequestID,
		ApprovalRequestURL: q.RequestURL,
		ExpiresInHours:     q.ExpiresInHours,
		ExecutionID:        q.ExecutionID,
		Kind:               q.Kind,
		Question:           oneLine(q.Question, 480),
		Body:               q.Body,
		Options:            append([]string(nil), opts...),
	}
}

// oneLine collapses s to a single line of at most n bytes (the control plane
// refuses a question over 500), cutting on a rune boundary.
func oneLine(s string, n int) string {
	s = strings.Join(strings.Fields(s), " ")
	if len(s) <= n {
		return s
	}
	cut := n - len("…")
	for cut > 0 && !utf8RuneStart(s[cut]) {
		cut--
	}
	return s[:cut] + "…"
}

func utf8RuneStart(b byte) bool { return b&0xC0 != 0x80 }

// AskUserBody renders an ask_user form as the markdown a person reads: the
// description, then every field with what kind of answer it wants and, for a
// choice, the choices. The answer comes back as the person's words.
//
// THE BODY SPEAKS OF THE CHOICES, NEVER OF THE APPROVAL BUTTONS. A door shows
// a form as its choices plus "answer in your own words" and "skip"; a closing
// sentence about **approve**, **request changes** and **Reject** named
// controls that card does not have (WorkAF r5–r7). A field's default is the
// form's machine value ("stdlib_crc32", "provide_module"), so it is shown as
// the field's `suggested:` property spelled as the label of the choice it
// names; a default that names none of the choices is not shown at all.
func AskUserBody(spec schemas.AskUserForm) string {
	var b strings.Builder
	if spec.Description != nil && strings.TrimSpace(*spec.Description) != "" {
		b.WriteString(strings.TrimSpace(*spec.Description))
		b.WriteString("\n\n")
	}
	if len(spec.Fields) > 0 {
		b.WriteString("## What it needs from you\n\n")
	}
	hasChoices := false
	for _, f := range spec.Fields {
		label := strings.TrimSpace(f.Label)
		if label == "" {
			label = f.ID
		}
		fmt.Fprintf(&b, "- **%s**", label)
		if f.Required {
			b.WriteString(" (required)")
		}
		if f.Description != nil && strings.TrimSpace(*f.Description) != "" {
			b.WriteString(" — " + strings.TrimSpace(*f.Description))
		}
		b.WriteString("\n")
		if len(f.Options) > 0 {
			hasChoices = true
			labels := make([]string, 0, len(f.Options))
			for _, o := range f.Options {
				labels = append(labels, optionLabel(o))
			}
			fmt.Fprintf(&b, "  - choices: %s\n", strings.Join(labels, " · "))
		}
		if s := suggestedOf(f); s != "" {
			fmt.Fprintf(&b, "  - suggested: %s\n", s)
		}
	}
	if hasChoices {
		b.WriteString("\nPick one of the choices, or answer in your own words. Without an answer it goes on with its own judgement.")
	} else {
		b.WriteString("\nAnswer in your own words. Without an answer it goes on with its own judgement.")
	}
	return b.String()
}

// optionLabel is what a person reads for one choice: its label, else its value.
func optionLabel(o map[string]string) string {
	if l := strings.TrimSpace(o["label"]); l != "" {
		return l
	}
	return strings.TrimSpace(o["value"])
}

// suggestedOf is a field's default as a person reads it. For a field with
// choices it is the label of the choice whose value (or label) the default
// names, and "" when it names none; a free field's default is shown as given.
// Booleans and empty values say nothing a person can use and are left out.
func suggestedOf(f schemas.AskUserFormField) string {
	if f.DefaultValue == nil {
		return ""
	}
	var def string
	switch v := f.DefaultValue.(type) {
	case string:
		def = strings.TrimSpace(v)
	case bool:
		return ""
	default:
		def = strings.TrimSpace(fmt.Sprint(v))
	}
	if def == "" {
		return ""
	}
	if len(f.Options) == 0 {
		return def
	}
	for _, o := range f.Options {
		if strings.EqualFold(strings.TrimSpace(o["value"]), def) || strings.EqualFold(strings.TrimSpace(o["label"]), def) {
			return optionLabel(o)
		}
	}
	return ""
}

// RequestUserInputNative pauses for an ask_user form with no hax. The person's
// words come back as feedback and are handed to the reasoner as the answer to
// every field (Values["answer"]); approving with no words means "use your own
// judgement". It mirrors RequestUserInputAndPause's status table: rejected →
// cancelled, expired → timeout.
func RequestUserInputNative(
	ctx context.Context,
	app App,
	pauser Pauser,
	spec schemas.AskUserForm,
	p RequestUserInputParams,
	round int,
) schemas.AskUserResponse {
	expires := int(p.ExpiresInHours)
	if expires <= 0 {
		expires = 24
	}
	id := ""
	if p.ExecutionID != "" {
		// The round number alone could repeat when one execution runs the
		// ask loop twice, and the control plane refuses a reused id, so a
		// process-wide sequence keeps every round's id unique.
		id = fmt.Sprintf("swe-ask-%s-q%d-%d", p.ExecutionID, round, askSeq.Add(1))
	}
	q := NativeQuestion{
		RequestID:      id,
		Kind:           KindQuestion,
		Question:       spec.Title,
		Body:           AskUserBody(spec),
		ExpiresInHours: expires,
		ExecutionID:    p.ExecutionID,
	}
	noteSafe(ctx, app, fmt.Sprintf("ask_user: asking a person (%q)", spec.Title), "ask_user", "native", "pause")
	result, err := pauser.Pause(ctx, q.PauseOptions())
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) {
			return schemas.AskUserResponse{Status: "timeout", Values: map[string]any{}}
		}
		msg := fmt.Sprintf("pause failed: %v", err)
		noteSafe(ctx, app, "ask_user: "+msg, "ask_user", "native", "error")
		return schemas.AskUserResponse{Status: "error", Values: map[string]any{}, Error: &msg}
	}
	var feedback *string
	if fb := strings.TrimSpace(result.Feedback); fb != "" {
		feedback = &fb
	}
	resp := parseApprovalResult(result.Decision, feedback, result.RawResponse)
	if resp.Status == "submitted" && len(resp.Values) == 0 {
		if feedback != nil {
			resp.Values = map[string]any{"answer": *feedback}
		} else {
			resp.Values = map[string]any{"answer": "No preference — use your own judgement."}
		}
	}
	noteSafe(ctx, app, fmt.Sprintf("ask_user: answered (status=%s)", resp.Status), "ask_user", "native", resp.Status)
	return resp
}
