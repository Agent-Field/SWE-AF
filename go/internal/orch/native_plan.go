// This file (native_plan.go) renders a plan_result as the plan review a person
// reads on the control-plane pause. It is written for an engineer deciding in a
// minute whether to approve:
//
//   - On a revision, the person's own words come first, then one line per
//     request saying how the plan changed (revision_check.go holds the plan to
//     them), and anything the revision still does not reflect.
//   - Then a SUMMARY of at most summaryCap lines: what changes, every file it
//     touches and why, the tests it adds, what it will not change, and the
//     risks or "none".
//   - Then `## Details`: the full requirements, design, every planned change
//     with its checks, and the order — the substance, unabridged.
//
// THE BODY IS SELF-CONTAINED. Everything in it is text the planner wrote for
// other agents, so personText strips what the person cannot follow: machine
// paths, HTML comments copied from the issue, and references to documents they
// never see (AC numbers, §4, C1, D9, F2, "tech-lead finding"). Revision numbers
// come from ONE counter, the gate's. There is no answer list: every door that
// shows this draws its own answers.
package orch

import (
	"fmt"
	"regexp"
	"sort"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/hitl"
)

// maxPlanBody keeps the body inside the control plane's 256 KiB limit with
// room to spare; a plan that large is cut with a line saying so.
const maxPlanBody = 200 * 1024

// summaryCap is the most lines the Summary may hold.
const summaryCap = 12

// NativePlanQuestion is the plan_review pause for one round of the gate.
// revisionIter is 0 for the first plan; history carries the words that asked
// for each earlier revision and what the check found.
func NativePlanQuestion(
	req ApprovalRequest,
	planResult map[string]any,
	revisionIter int,
	history []map[string]any,
) hitl.NativeQuestion {
	title := personText(strings.TrimSpace(req.ReviewTitle), revisionIter)
	if title == "" {
		title = personText(firstLine(req.Goal), revisionIter)
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

// PlanReviewMarkdown renders the plan as markdown: summary first, details below.
func PlanReviewMarkdown(
	title, goal string,
	planResult map[string]any,
	revisionIter int,
	history []map[string]any,
) string {
	t := func(s string) string { return personText(s, revisionIter) }
	var b strings.Builder
	heading := "Plan: " + title
	if revisionIter > 0 {
		heading = fmt.Sprintf("Plan: %s (revision %d)", title, revisionIter)
	}
	b.WriteString("# " + heading + "\n\n")

	prd, _ := mapGet(planResult, "prd", map[string]any{}).(map[string]any)
	arch, _ := mapGet(planResult, "architecture", map[string]any{}).(map[string]any)
	issues := asMapList(mapGet(planResult, "issues", nil))
	order := issueOrder(planResult, issues)

	// --- the person's words, and how the plan changed -------------------------
	var avoided []string
	if len(history) > 0 {
		last := history[len(history)-1]
		if fb, _ := last["feedback"].(string); strings.TrimSpace(fb) != "" {
			reqs := readReviewRequests(fb)
			problems := revisionProblems(planResult, reqs)
			b.WriteString("## What you asked to change\n\n" + quote(strings.TrimSpace(fb)) + "\n\n")
			b.WriteString("## How the plan changed\n\n")
			for _, l := range howThePlanChanged(reqs, problems) {
				b.WriteString(l + "\n")
			}
			b.WriteString("\n")
			if len(problems) > 0 {
				b.WriteString("## Not reflected yet\n\nThe plan was revised twice and still says this; approve only if you accept it:\n\n")
				for _, p := range problems {
					b.WriteString("- " + t(p) + "\n")
				}
				b.WriteString("\n")
			}
			if re, _ := last["revision_error"].(string); re != "" {
				b.WriteString("The design could not be fully revised, so this is the last complete design.\n\n")
			}
			for _, r := range reqs {
				if r.Avoid != "" {
					avoided = append(avoided, r.Avoid)
				}
			}
		}
	}

	// --- summary ---------------------------------------------------------------
	b.WriteString("## Summary\n\n")
	for _, l := range summaryLines(prd, order, avoided, t) {
		b.WriteString(l + "\n")
	}
	b.WriteString("\n")

	// --- details ---------------------------------------------------------------
	b.WriteString("## Details\n\n")
	if g := t(goal); g != "" {
		b.WriteString("### Goal\n\n" + g + "\n\n")
	}
	if vd := t(mapStr(prd, "validated_description", "")); vd != "" {
		b.WriteString("### What it will deliver\n\n" + demoteHeadings(vd) + "\n\n")
	}
	bulletSection(&b, "Must have", prd["must_have"], t)
	bulletSection(&b, "Nice to have", prd["nice_to_have"], t)
	bulletSection(&b, "Left out on purpose", prd["out_of_scope"], t)
	numberedSection(&b, "How you will know it is done", prd["acceptance_criteria"], t)
	bulletSection(&b, "Assumptions", prd["assumptions"], t)
	bulletSection(&b, "Risks", prd["risks"], t)

	if len(arch) > 0 {
		b.WriteString("### Design\n\n")
		if s := t(mapStr(arch, "summary", "")); s != "" {
			b.WriteString(demoteHeadings(s) + "\n\n")
		}
		for _, comp := range asMapList(mapGet(arch, "components", nil)) {
			fmt.Fprintf(&b, "- **%s**: %s", t(mapStr(comp, "name", "Component")), t(mapStr(comp, "responsibility", "")))
			if files := codeList(comp["touches_files"]); files != "" {
				b.WriteString(" (" + files + ")")
			}
			b.WriteString("\n")
		}
		if decs := asMapList(mapGet(arch, "decisions", nil)); len(decs) > 0 {
			b.WriteString("\nDecisions:\n\n")
			for _, dec := range decs {
				fmt.Fprintf(&b, "- **%s** — %s\n", t(mapStr(dec, "decision", "")), t(mapStr(dec, "rationale", "")))
			}
		}
		b.WriteString("\n")
	}

	if len(issues) > 0 {
		fmt.Fprintf(&b, "### Planned changes (%d)\n\n", len(issues))
		for i, iss := range order {
			fmt.Fprintf(&b, "#### %d. %s\n\n", i+1, t(mapStr(iss, "title", mapStr(iss, "name", ""))))
			if d := t(mapStr(iss, "description", "")); d != "" {
				b.WriteString(demoteHeadings(d) + "\n\n")
			}
			if f := codeList(iss["files_to_modify"]); f != "" {
				b.WriteString("- Changes: " + f + "\n")
			}
			if f := codeList(iss["files_to_create"]); f != "" {
				b.WriteString("- Creates: " + f + "\n")
			}
			if deps := coerceAnyList(iss["depends_on"]); len(deps) > 0 {
				b.WriteString("- After: " + strings.Join(titlesFor(deps, issues), ", ") + "\n")
			}
			if ts := t(mapStr(iss, "testing_strategy", "")); ts != "" {
				b.WriteString("- Tested by: " + ts + "\n")
			}
			if ac := coerceAnyList(iss["acceptance_criteria"]); len(ac) > 0 {
				b.WriteString("- Done when:\n")
				for _, c := range ac {
					b.WriteString("  - " + t(fmt.Sprint(c)) + "\n")
				}
			}
			b.WriteString("\n")
		}
	} else {
		b.WriteString("### Planned changes\n\nThe planner produced no changes. Approving builds nothing.\n\n")
	}

	if levels := levelLines(planResult, issues); len(levels) > 1 {
		b.WriteString("### Order\n\n")
		for _, l := range levels {
			b.WriteString(t(l) + "\n")
		}
		b.WriteString("\n")
	}

	out := strings.TrimRight(b.String(), "\n") + "\n"
	if len(out) > maxPlanBody {
		cut := maxPlanBody
		for cut > 0 && out[cut]&0xC0 == 0x80 {
			cut--
		}
		out = out[:cut] + "\n\n… the plan is longer than this page can carry; the rest was cut.\n"
	}
	return out
}

// summaryLines is the Summary: at most summaryCap lines.
func summaryLines(prd map[string]any, order []map[string]any, avoided []string, t func(string) string) []string {
	var lines []string
	what := firstSentences(t(stripMarkdownHeadings(mapStr(prd, "validated_description", ""))), 2, 320)
	if what == "" && len(order) > 0 {
		what = t(mapStr(order[0], "title", ""))
	}
	if what != "" {
		lines = append(lines, "**What changes:** "+what)
	}

	// Each file once, with the title of the change that touches it first.
	purposes := map[string]string{}
	var files []string
	for _, iss := range order {
		for _, k := range []string{"files_to_modify", "files_to_create"} {
			for _, f := range coerceAnyList(iss[k]) {
				name := strings.TrimSpace(fmt.Sprint(f))
				if name == "" {
					continue
				}
				if _, ok := purposes[name]; !ok {
					files = append(files, name)
					verb := ""
					if k == "files_to_create" {
						verb = "new — "
					}
					purposes[name] = verb + t(mapStr(iss, "title", ""))
				}
			}
		}
	}
	sort.SliceStable(files, func(i, j int) bool { return isTestFile(files[i]) == false && isTestFile(files[j]) })
	if len(files) > 0 {
		lines = append(lines, "**Files:**")
		max := 5
		for i, f := range files {
			if i == max {
				lines = append(lines, fmt.Sprintf("- and %d more (see Details)", len(files)-max))
				break
			}
			lines = append(lines, fmt.Sprintf("- `%s` — %s", f, oneLineCut(purposes[f], 90)))
		}
	}

	if tests := testNames(order); len(tests) > 0 {
		lines = append(lines, "**Tests it adds:** "+codeJoin(tests))
	}

	var not []string
	for _, f := range avoided {
		not = append(not, "`"+f+"`")
	}
	for _, it := range coerceAnyList(prd["out_of_scope"]) {
		if len(not) >= 3 {
			break
		}
		not = append(not, oneLineCut(t(fmt.Sprint(it)), 80))
	}
	if len(not) > 0 {
		lines = append(lines, "**Will not change:** "+strings.Join(not, "; "))
	}

	risk := "none"
	if rs := coerceAnyList(prd["risks"]); len(rs) > 0 {
		risk = oneLineCut(t(fmt.Sprint(rs[0])), 160)
		if len(rs) > 1 {
			risk += fmt.Sprintf(" (+%d more in Details)", len(rs)-1)
		}
	}
	lines = append(lines, "**Risks:** "+risk)

	if len(lines) > summaryCap {
		lines = lines[:summaryCap]
	}
	return lines
}

func isTestFile(f string) bool {
	return strings.HasSuffix(f, "_test.go") || strings.Contains(f, "test")
}

// testNames lists the Go test functions the planned changes name.
func testNames(issues []map[string]any) []string {
	seen := map[string]bool{}
	var out []string
	for _, iss := range issues {
		text := mapStr(iss, "description", "") + " " + mapStr(iss, "testing_strategy", "") + " " +
			mapStr(iss, "title", "")
		for _, c := range coerceAnyList(iss["acceptance_criteria"]) {
			text += " " + fmt.Sprint(c)
		}
		for _, m := range goTestPattern.FindAllString(text, -1) {
			if !seen[m] {
				seen[m] = true
				out = append(out, m)
			}
		}
	}
	if len(out) > 6 {
		out = out[:6]
	}
	return out
}

// ---------------------------------------------------------------------------
// personText — what the person can read.
// ---------------------------------------------------------------------------

var (
	htmlComment   = regexp.MustCompile(`(?s)<!--.*?-->`)
	absWorktree   = regexp.MustCompile(`(?:/Users|/home|/private|/tmp|/var|/root)/\S*?/(?:\.worktrees|workspaces)/[^/\s` + "`" + `]+/?`)
	absPath       = regexp.MustCompile("(?:/Users|/home|/private|/tmp|/var|/root)/[^\\s`'\")]+")
	refParen      = regexp.MustCompile(`\s*\((?:[^()]*?\b(?:AC\s?\d+|§\s?\d|[CDFR]\d{1,2}\b|[Rr]ev(?:ision)?\.?\s?\d|finding)[^()]*)\)`)
	refAC         = regexp.MustCompile(`\b(?:PRD\s+)?AC\s?\d+(?:\s*(?:[-–,/]|and|to)\s*(?:AC\s?)?\d+)*\b`)
	refSection    = regexp.MustCompile(`(?:\b(?:architecture|PRD|design)\s+)?§\s?\d+(?:\.\d+)*`)
	refCode       = regexp.MustCompile(`\b(?:review\s+)?[CDFR]\d{1,2}\b`)
	refFinding    = regexp.MustCompile(`(?i)\btech[- ]lead(?:'s)?\s+finding(?:s)?(?:\s*\([a-z0-9]\))?`)
	refArchRev    = regexp.MustCompile(`(?i)\barchitecture\s+(?:rev(?:ision)?\.?\s*\d+(?:\.\d+)*|\d+(?:\.\d+)+)`)
	refRevision   = regexp.MustCompile(`(?i)\brev(?:ision)?\.?\s*\d+\b`)
	refMerger     = regexp.MustCompile(`(?i)\bmerger agent\b`)
	refScopeRule  = regexp.MustCompile(`(?i)\bthe scope rule\b`)
	refPRD        = regexp.MustCompile(`\bPRD\b`)
	emptyParens   = regexp.MustCompile(`\((?:\s+|\s*[,;/&–-]+\s*(?:and\s*)?)\)`)
	spaceBeforeP  = regexp.MustCompile(`[ \t]+([,.;:])`)
	doublePunct   = regexp.MustCompile(`([,;])\s*[,;]+`)
	manySpaces    = regexp.MustCompile(`[ \t]{2,}`)
	mdHeading     = regexp.MustCompile(`(?m)^(#{1,6})\s`)
	sentenceBreak = regexp.MustCompile(`([.!?])\s+`)
)

// personText rewrites planner text so a person can read it with nothing else
// open. revision is the gate's one counter: any revision number the planner
// wrote is replaced by it.
func personText(s string, revision int) string {
	if s == "" {
		return s
	}
	s = htmlComment.ReplaceAllString(s, "")
	s = absWorktree.ReplaceAllString(s, "")
	s = absPath.ReplaceAllString(s, "the repo")
	s = refParen.ReplaceAllString(s, "")
	s = refFinding.ReplaceAllString(s, "your review")
	s = refArchRev.ReplaceAllString(s, "the design")
	s = refAC.ReplaceAllString(s, "")
	s = refSection.ReplaceAllString(s, "")
	s = refCode.ReplaceAllString(s, "")
	if revision > 0 {
		s = refRevision.ReplaceAllString(s, fmt.Sprintf("revision %d", revision))
	} else {
		s = refRevision.ReplaceAllString(s, "this plan")
	}
	s = refMerger.ReplaceAllString(s, "the merge step")
	s = refScopeRule.ReplaceAllString(s, "the keep-it-small guideline")
	s = refPRD.ReplaceAllString(s, "requirements")
	s = emptyParens.ReplaceAllString(s, "")
	s = doublePunct.ReplaceAllString(s, "$1")
	s = spaceBeforeP.ReplaceAllString(s, "$1")
	s = manySpaces.ReplaceAllString(s, " ")
	return strings.TrimSpace(s)
}

// demoteHeadings keeps planner markdown headings below the body's own levels.
func demoteHeadings(s string) string {
	return mdHeading.ReplaceAllString(s, "#####$1 ")
}

func stripMarkdownHeadings(s string) string {
	var keep []string
	for _, l := range strings.Split(s, "\n") {
		if !strings.HasPrefix(strings.TrimSpace(l), "#") {
			keep = append(keep, l)
		}
	}
	return strings.Join(keep, " ")
}

// firstSentences is the first n sentences of s, at most max runes.
func firstSentences(s string, n, max int) string {
	s = strings.Join(strings.Fields(s), " ")
	idx := sentenceBreak.FindAllStringIndex(s, -1)
	if len(idx) >= n {
		s = s[:idx[n-1][0]+1]
	}
	return oneLineCut(s, max)
}

// ---------------------------------------------------------------------------
// Structure helpers.
// ---------------------------------------------------------------------------

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
	if raw, ok := planResult["levels"].([][]string); ok {
		return raw
	}
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
	return out
}

// levelLines says which changes run together, by title.
func levelLines(planResult map[string]any, issues []map[string]any) []string {
	var out []string
	for i, lvl := range levelsOf(planResult) {
		anyLvl := make([]any, len(lvl))
		for j, n := range lvl {
			anyLvl[j] = n
		}
		names := titlesFor(anyLvl, issues)
		line := fmt.Sprintf("%d. %s", i+1, strings.Join(names, "; "))
		if len(names) > 1 {
			line += " (together)"
		}
		out = append(out, line)
	}
	return out
}

// titlesFor maps issue names to their titles; the kebab name is machinery.
func titlesFor(names []any, issues []map[string]any) []string {
	titles := map[string]string{}
	for _, iss := range issues {
		n := mapStr(iss, "name", "")
		titles[n] = strings.TrimSpace(mapStr(iss, "title", n))
	}
	out := make([]string, 0, len(names))
	for _, n := range names {
		k := fmt.Sprint(n)
		if t := titles[k]; t != "" {
			out = append(out, t)
		} else {
			out = append(out, k)
		}
	}
	return out
}

func bulletSection(b *strings.Builder, heading string, v any, t func(string) string) {
	items := coerceAnyList(v)
	if len(items) == 0 {
		return
	}
	b.WriteString("### " + heading + "\n\n")
	for _, it := range items {
		b.WriteString("- " + t(fmt.Sprint(it)) + "\n")
	}
	b.WriteString("\n")
}

func numberedSection(b *strings.Builder, heading string, v any, t func(string) string) {
	items := coerceAnyList(v)
	if len(items) == 0 {
		return
	}
	b.WriteString("### " + heading + "\n\n")
	for i, it := range items {
		fmt.Fprintf(b, "%d. %s\n", i+1, t(fmt.Sprint(it)))
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
