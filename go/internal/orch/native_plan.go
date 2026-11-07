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
	"io/fs"
	"os"
	"path/filepath"
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
		Body:           planReviewMarkdown(title, req.Goal, planResult, revisionIter, history, existingTestNames(req.RepoPath)),
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
	return planReviewMarkdown(title, goal, planResult, revisionIter, history, nil)
}

// planReviewMarkdown is PlanReviewMarkdown knowing which tests the repository
// already has, so the summary lists only the tests the plan ADDS.
func planReviewMarkdown(
	title, goal string,
	planResult map[string]any,
	revisionIter int,
	history []map[string]any,
	existingTests map[string]bool,
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
	for _, l := range summaryLines(prd, approverSummary(planResult), order, avoided, existingTests, t) {
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
//
// The sprint planner writes it for the approver as its own field
// (approver_summary): one or two plain sentences of what changes, each file
// with ITS OWN purpose, the new tests, and short phrases for what is left
// alone and what could go wrong. A plan without that field (an older planner,
// a model that skipped it) falls back to what the plan's other parts say. In
// either case every entry is a COMPLETE phrase: an entry too long for the
// summary is shortened to its first sentence or clause, or left out for
// Details to carry, and is never cut mid-word (owner review, 2026-11-07).
func summaryLines(prd, as map[string]any, order []map[string]any, avoided []string, existingTests map[string]bool, t func(string) string) []string {
	var lines []string
	avoid := map[string]bool{}
	for _, f := range avoided {
		avoid[strings.ToLower(strings.Trim(f, "`"))] = true
	}

	what := phrase(t(mapStr(as, "what_changes", "")), 280)
	if what == "" {
		what = phrase(t(stripMarkdownHeadings(mapStr(prd, "validated_description", ""))), 280)
	}
	if what == "" && len(order) > 0 {
		what = phrase(t(mapStr(order[0], "title", "")), 160)
	}
	if what != "" {
		lines = append(lines, "**What changes:** "+what)
	}

	// Files: the planner's own list and purposes first, then any file a
	// planned change touches that the list forgot, with a purpose derived
	// from that change.
	purposes := map[string]string{}
	var files []string
	addFile := func(name, purpose string) {
		name = strings.Trim(strings.TrimSpace(name), "`")
		if name == "" || avoid[strings.ToLower(name)] {
			return
		}
		if _, ok := purposes[name]; ok {
			if purposes[name] == "" {
				purposes[name] = purpose
			}
			return
		}
		files = append(files, name)
		purposes[name] = purpose
	}
	for _, fp := range asMapList(as["files"]) {
		addFile(mapStr(fp, "path", ""), phrase(t(mapStr(fp, "purpose", "")), 90))
	}
	for _, iss := range order {
		for _, k := range []string{"files_to_modify", "files_to_create"} {
			for _, f := range coerceAnyList(iss[k]) {
				name := strings.TrimSpace(fmt.Sprint(f))
				addFile(name, derivedPurpose(name, k == "files_to_create", iss, existingTests, t))
			}
		}
	}
	sort.SliceStable(files, func(i, j int) bool { return !isTestFile(files[i]) && isTestFile(files[j]) })
	if len(files) > 0 {
		lines = append(lines, "**Files:**")
		const max = 5
		for i, f := range files {
			if i == max {
				lines = append(lines, fmt.Sprintf("- and %d more (see Details)", len(files)-max))
				break
			}
			if p := purposes[f]; p != "" {
				lines = append(lines, fmt.Sprintf("- `%s` — %s", f, p))
			} else {
				lines = append(lines, fmt.Sprintf("- `%s`", f))
			}
		}
	}

	// Tests: the planner's list, held to the same law as the derived one —
	// a test the repository already has is not one the plan adds.
	var tests []string
	seen := map[string]bool{}
	for _, it := range coerceAnyList(as["tests_added"]) {
		n := strings.Trim(strings.TrimSpace(fmt.Sprint(it)), "`")
		if goTestPattern.MatchString(n) && !existingTests[n] && !seen[n] {
			seen[n] = true
			tests = append(tests, n)
		}
	}
	if len(tests) == 0 {
		tests = testNames(order, existingTests)
	}
	if len(tests) > 0 {
		lines = append(lines, "**Tests it adds:** "+codeJoin(tests))
	}

	var not []string
	for _, f := range avoided {
		not = append(not, "`"+f+"`")
	}
	notSource := coerceAnyList(as["will_not_change"])
	if len(notSource) == 0 {
		notSource = coerceAnyList(prd["out_of_scope"])
	}
	for _, it := range notSource {
		if len(not) >= 3 {
			break
		}
		p := phrase(t(fmt.Sprint(it)), 90)
		if p == "" || mentionsAny(p, avoided) {
			continue
		}
		not = append(not, p)
	}
	if len(not) > 0 {
		lines = append(lines, "**Will not change:** "+strings.Join(not, "; "))
	}

	risks := coerceAnyList(as["risks"])
	_, plannerSpoke := as["risks"]
	if len(risks) == 0 && !plannerSpoke {
		risks = coerceAnyList(prd["risks"])
	}
	var rs []string
	for _, it := range risks {
		if p := phrase(t(fmt.Sprint(it)), 120); p != "" {
			rs = append(rs, p)
		}
	}
	risk := "none"
	switch {
	case len(rs) == 0 && len(risks) > 0:
		risk = "listed in Details"
	case len(rs) == 1:
		risk = rs[0]
	case len(rs) >= 2:
		risk = rs[0] + "; " + rs[1]
		if len(rs) > 2 {
			risk += fmt.Sprintf(" (+%d more in Details)", len(rs)-2)
		}
	}
	lines = append(lines, "**Risks:** "+risk)

	if len(lines) > summaryCap {
		lines = lines[:summaryCap]
	}
	return lines
}

// derivedPurpose is a file's purpose when the planner gave none: a test file
// names the new tests its change adds; any other file is the change's title.
func derivedPurpose(file string, created bool, iss map[string]any, existing map[string]bool, t func(string) string) string {
	if isTestFile(file) {
		if names := testNames([]map[string]any{iss}, existing); len(names) > 0 {
			return strings.Join(names, ", ")
		}
	}
	p := phrase(t(mapStr(iss, "title", "")), 90)
	if created && p != "" {
		p = "new: " + p
	}
	return p
}

func mentionsAny(s string, needles []string) bool {
	ls := strings.ToLower(s)
	for _, n := range needles {
		if n != "" && strings.Contains(ls, strings.ToLower(strings.Trim(n, "`"))) {
			return true
		}
	}
	return false
}

// phrase is s as one COMPLETE phrase of at most max runes: whole if it fits,
// else its first sentence, else its first clause, else nothing. It never
// cuts a word, because a summary line that ends "Do not mo…" tells the
// approver less than no line at all.
func phrase(s string, max int) string {
	s = strings.TrimSpace(strings.Join(strings.Fields(s), " "))
	if s == "" {
		return ""
	}
	fits := func(x string) bool { return len([]rune(x)) <= max }
	if fits(s) {
		return s
	}
	if loc := sentenceBreak.FindStringIndex(s); loc != nil {
		if first := strings.TrimSpace(s[:loc[0]+1]); fits(first) {
			return first
		}
	}
	// A bare comma is not a clause boundary here: cutting "do not modify A,
	// B or C" at its first comma would tell the approver only A is safe.
	for _, sep := range []string{"; ", " — ", ": ", " (", ", because ", ", since ", ", so ", ", which ", ", but "} {
		if i := strings.Index(s, sep); i > 0 {
			if first := strings.TrimRight(strings.TrimSpace(s[:i]), ",;:"); fits(first) && len([]rune(first)) >= 12 {
				return first
			}
		}
	}
	return ""
}

// approverSummary is the planner's approver_summary, or an empty map.
func approverSummary(planResult map[string]any) map[string]any {
	if m, ok := planResult["approver_summary"].(map[string]any); ok {
		return m
	}
	return map[string]any{}
}

func isTestFile(f string) bool {
	return strings.HasSuffix(f, "_test.go") || strings.Contains(f, "test")
}

// testNames lists the Go test functions the planned changes name.
func testNames(issues []map[string]any, existing map[string]bool) []string {
	seen := map[string]bool{}
	var out []string
	for _, iss := range issues {
		text := mapStr(iss, "description", "") + " " + mapStr(iss, "testing_strategy", "") + " " +
			mapStr(iss, "title", "")
		for _, c := range coerceAnyList(iss["acceptance_criteria"]) {
			text += " " + fmt.Sprint(c)
		}
		for _, m := range goTestPattern.FindAllString(text, -1) {
			if !seen[m] && !existing[m] {
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

var testFuncDecl = regexp.MustCompile(`(?m)^func (Test[A-Z]\w*|Benchmark[A-Z]\w*)\(`)

// existingTestNames reads the test functions already in the repository's
// _test.go files, skipping the build's own worktrees and artifacts.
func existingTestNames(repoPath string) map[string]bool {
	out := map[string]bool{}
	if strings.TrimSpace(repoPath) == "" {
		return out
	}
	_ = filepath.WalkDir(repoPath, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			switch d.Name() {
			case ".git", ".worktrees", ".artifacts", "vendor", "node_modules":
				return filepath.SkipDir
			}
			return nil
		}
		if strings.HasSuffix(path, "_test.go") {
			if raw, rerr := os.ReadFile(path); rerr == nil {
				for _, m := range testFuncDecl.FindAllStringSubmatch(string(raw), -1) {
					out[m[1]] = true
				}
			}
		}
		return nil
	})
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
	spaceBeforeP  = regexp.MustCompile(`[ \t]+([,;:])(\s|$)`)
	refLabel      = regexp.MustCompile(`(?m)\b(?:PRD\s+)?(?:AC|[CDFR])\s?\d{1,2}\s*[:)]\s*`)
	leadingPunct  = regexp.MustCompile(`(?m)^\s*[:;,]\s*`)
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
	// Code spans are left exactly as written: a command must still run.
	parts := strings.Split(s, "`")
	for i := 0; i < len(parts); i += 2 {
		parts[i] = proseText(parts[i], revision, i == 0)
	}
	return strings.TrimSpace(manySpaces.ReplaceAllString(strings.Join(parts, "`"), " "))
}

// proseText strips references from text outside code spans.
// Only the text's first stretch loses leading punctuation: after a code span,
// a leading comma belongs to the sentence ("`Len()`, an O(1) count").
func proseText(s string, revision int, first bool) string {
	s = refLabel.ReplaceAllString(s, "")
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
	s = spaceBeforeP.ReplaceAllString(s, "$1$2")
	if first {
		s = leadingPunct.ReplaceAllString(s, "")
	}
	return manySpaces.ReplaceAllString(s, " ")
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
