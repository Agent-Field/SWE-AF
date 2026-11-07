// This file (native_plan.go) renders a plan_result as the plan review a person
// reads on the control-plane pause: one line to ask, and a markdown body that
// holds EVERYTHING needed to judge the plan — the goal, what it will deliver,
// how anyone will know it is done, the design, every planned change with the
// files it touches and its checks, and the order the changes run in. There is
// no other page behind it when hax is absent, so nothing may be left out.
package orch

import (
	"fmt"
	"sort"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/hitl"
)

// maxPlanBody keeps the body inside the control plane's 256 KiB limit with
// room to spare; a plan that large is cut with a line saying so.
const maxPlanBody = 200 * 1024

// NativePlanQuestion is the plan_review pause for one round of the gate.
// revisionIter is 0 for the first plan; history carries the words that asked
// for each earlier revision.
func NativePlanQuestion(
	req ApprovalRequest,
	planResult map[string]any,
	revisionIter int,
	history []map[string]any,
) hitl.NativeQuestion {
	title := strings.TrimSpace(req.ReviewTitle)
	if title == "" {
		title = firstLine(req.Goal)
	}
	question := fmt.Sprintf("Approve the plan for %s?", title)
	if revisionIter > 0 {
		question = fmt.Sprintf("Approve the revised plan (revision %d) for %s?", revisionIter, title)
	}
	id := ""
	if req.ExecutionID != "" {
		id = fmt.Sprintf("swe-plan-%s-r%d", req.ExecutionID, revisionIter+1)
	}
	expires := 0
	if req.Cfg != nil {
		expires = req.Cfg.ApprovalExpiresInHours
	}
	return hitl.NativeQuestion{
		RequestID:      id,
		Kind:           hitl.KindPlanReview,
		Question:       question,
		Body:           PlanReviewMarkdown(title, req.Goal, planResult, revisionIter, history),
		Options:        hitl.ReviewOptions,
		ExpiresInHours: expires,
		ExecutionID:    req.ExecutionID,
	}
}

// PlanReviewMarkdown renders the full plan as markdown.
func PlanReviewMarkdown(
	title, goal string,
	planResult map[string]any,
	revisionIter int,
	history []map[string]any,
) string {
	var b strings.Builder
	heading := "Plan: " + title
	if revisionIter > 0 {
		heading = fmt.Sprintf("Plan: %s (revision %d)", title, revisionIter)
	}
	b.WriteString("# " + heading + "\n\n")

	if len(history) > 0 {
		last := history[len(history)-1]
		if fb := strings.TrimSpace(fmt.Sprint(last["feedback"])); fb != "" && fb != "<nil>" {
			b.WriteString("## What you asked to change\n\n")
			b.WriteString(quote(fb) + "\n\n")
			if re, _ := last["revision_error"].(string); re != "" {
				b.WriteString("The design could not be fully revised (" + re +
					"), so this is the last complete plan; the changes below may not reflect your words yet.\n\n")
			}
		}
	}

	if g := strings.TrimSpace(goal); g != "" {
		b.WriteString("## Goal\n\n" + g + "\n\n")
	}

	prd, _ := mapGet(planResult, "prd", map[string]any{}).(map[string]any)
	if vd := mapStr(prd, "validated_description", ""); vd != "" {
		b.WriteString("## What it will deliver\n\n" + strings.TrimSpace(vd) + "\n\n")
	}
	bulletSection(&b, "Must have", prd["must_have"])
	bulletSection(&b, "Nice to have", prd["nice_to_have"])
	bulletSection(&b, "Left out on purpose", prd["out_of_scope"])
	bulletSection(&b, "How you will know it is done", prd["acceptance_criteria"])
	bulletSection(&b, "Assumptions", prd["assumptions"])
	bulletSection(&b, "Risks", prd["risks"])

	arch, _ := mapGet(planResult, "architecture", map[string]any{}).(map[string]any)
	if len(arch) > 0 {
		b.WriteString("## Design\n\n")
		if s := mapStr(arch, "summary", ""); s != "" {
			b.WriteString(strings.TrimSpace(s) + "\n\n")
		}
		for _, comp := range asMapList(mapGet(arch, "components", nil)) {
			fmt.Fprintf(&b, "- **%s**: %s", mapStr(comp, "name", "Component"),
				strings.TrimSpace(mapStr(comp, "responsibility", "")))
			if files := codeList(comp["touches_files"]); files != "" {
				b.WriteString(" (" + files + ")")
			}
			b.WriteString("\n")
		}
		if decs := asMapList(mapGet(arch, "decisions", nil)); len(decs) > 0 {
			b.WriteString("\nDecisions:\n\n")
			for _, dec := range decs {
				fmt.Fprintf(&b, "- **%s** — %s\n", strings.TrimSpace(mapStr(dec, "decision", "")),
					strings.TrimSpace(mapStr(dec, "rationale", "")))
			}
		}
		b.WriteString("\n")
	}

	issues := asMapList(mapGet(planResult, "issues", nil))
	order := issueOrder(planResult, issues)
	if len(issues) > 0 {
		fmt.Fprintf(&b, "## Planned changes (%d)\n\n", len(issues))
		for i, iss := range order {
			name := mapStr(iss, "name", "")
			t := strings.TrimSpace(mapStr(iss, "title", name))
			fmt.Fprintf(&b, "### %d. %s\n\n", i+1, t)
			if d := strings.TrimSpace(mapStr(iss, "description", "")); d != "" {
				b.WriteString(d + "\n\n")
			}
			if f := codeList(iss["files_to_modify"]); f != "" {
				b.WriteString("- Changes: " + f + "\n")
			}
			if f := codeList(iss["files_to_create"]); f != "" {
				b.WriteString("- Creates: " + f + "\n")
			}
			if deps := coerceAnyList(iss["depends_on"]); len(deps) > 0 {
				b.WriteString("- After: " + joinAny(deps, ", ") + "\n")
			}
			if ts := strings.TrimSpace(mapStr(iss, "testing_strategy", "")); ts != "" {
				b.WriteString("- Tested by: " + ts + "\n")
			}
			if ac := coerceAnyList(iss["acceptance_criteria"]); len(ac) > 0 {
				b.WriteString("- Done when:\n")
				for _, c := range ac {
					fmt.Fprintf(&b, "  - %v\n", c)
				}
			}
			b.WriteString("\n")
		}
	} else {
		b.WriteString("## Planned changes\n\nThe planner produced no changes. Approving builds nothing.\n\n")
	}

	if files := touchedFiles(issues); len(files) > 0 {
		b.WriteString("## Files it will touch\n\n")
		for _, f := range files {
			b.WriteString("- `" + f + "`\n")
		}
		b.WriteString("\n")
	}

	if levels := levelLines(planResult, issues); len(levels) > 1 {
		b.WriteString("## Order\n\n")
		for _, l := range levels {
			b.WriteString(l + "\n")
		}
		b.WriteString("\n")
	}

	out := b.String()
	if len(out) > maxPlanBody {
		cut := maxPlanBody
		for cut > 0 && out[cut]&0xC0 == 0x80 {
			cut--
		}
		out = out[:cut] + "\n\n… the plan is longer than this page can carry; the rest was cut.\n"
	}
	return out
}

// issueOrder returns the issues in the order they run: by level, then the
// order the planner listed them. Issues no level names keep their place at
// the end.
func issueOrder(planResult map[string]any, issues []map[string]any) []map[string]any {
	byName := map[string]map[string]any{}
	for _, iss := range issues {
		byName[mapStr(iss, "name", "")] = iss
	}
	seen := map[string]bool{}
	var out []map[string]any
	for _, lvl := range levelsOf(planResult) {
		for _, n := range lvl {
			if iss, ok := byName[n]; ok && !seen[n] {
				seen[n] = true
				out = append(out, iss)
			}
		}
	}
	for _, iss := range issues {
		if n := mapStr(iss, "name", ""); !seen[n] {
			seen[n] = true
			out = append(out, iss)
		}
	}
	return out
}

func levelsOf(planResult map[string]any) [][]string {
	var out [][]string
	for _, lvl := range coerceAnyList(planResult["levels"]) {
		var names []string
		switch t := lvl.(type) {
		case []any:
			for _, n := range t {
				names = append(names, fmt.Sprint(n))
			}
		case []string:
			names = append(names, t...)
		}
		if len(names) > 0 {
			out = append(out, names)
		}
	}
	if raw, ok := planResult["levels"].([][]string); ok && len(out) == 0 {
		out = raw
	}
	return out
}

// levelLines says which changes run together, by title.
func levelLines(planResult map[string]any, issues []map[string]any) []string {
	titles := map[string]string{}
	for _, iss := range issues {
		n := mapStr(iss, "name", "")
		titles[n] = strings.TrimSpace(mapStr(iss, "title", n))
	}
	var out []string
	for i, lvl := range levelsOf(planResult) {
		names := make([]string, 0, len(lvl))
		for _, n := range lvl {
			if t := titles[n]; t != "" {
				names = append(names, t)
			} else {
				names = append(names, n)
			}
		}
		line := fmt.Sprintf("%d. %s", i+1, strings.Join(names, "; "))
		if len(names) > 1 {
			line += " (together)"
		}
		out = append(out, line)
	}
	return out
}

func touchedFiles(issues []map[string]any) []string {
	set := map[string]bool{}
	for _, iss := range issues {
		for _, k := range []string{"files_to_modify", "files_to_create"} {
			for _, f := range coerceAnyList(iss[k]) {
				if s := strings.TrimSpace(fmt.Sprint(f)); s != "" {
					set[s] = true
				}
			}
		}
	}
	out := make([]string, 0, len(set))
	for f := range set {
		out = append(out, f)
	}
	sort.Strings(out)
	return out
}

func bulletSection(b *strings.Builder, heading string, v any) {
	items := coerceAnyList(v)
	if len(items) == 0 {
		return
	}
	b.WriteString("## " + heading + "\n\n")
	for _, it := range items {
		fmt.Fprintf(b, "- %v\n", it)
	}
	b.WriteString("\n")
}

func codeList(v any) string {
	items := coerceAnyList(v)
	parts := make([]string, 0, len(items))
	for _, it := range items {
		if s := strings.TrimSpace(fmt.Sprint(it)); s != "" {
			parts = append(parts, "`"+s+"`")
		}
	}
	return strings.Join(parts, ", ")
}

func joinAny(items []any, sep string) string {
	parts := make([]string, len(items))
	for i, it := range items {
		parts[i] = fmt.Sprint(it)
	}
	return strings.Join(parts, sep)
}

func quote(s string) string {
	lines := strings.Split(s, "\n")
	for i, l := range lines {
		lines[i] = "> " + l
	}
	return strings.Join(lines, "\n")
}

func firstLine(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		s = s[:i]
	}
	return strings.TrimSpace(s)
}
