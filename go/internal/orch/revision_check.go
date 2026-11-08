// This file (revision_check.go) is the deterministic check that a plan revised
// in review actually did what the person asked — in EVERY section, not only in
// the design. Live on 2026-11-07 a revision moved the design ("README.md is
// left alone") while its Must have still said "add Len to README.md": the
// person would have approved a plan that said both. So the person's words are
// read into requests the plan can be held to, and a revision that fails them
// is re-planned once more before anyone is asked to approve it.
//
// It reads two shapes of request, the ones a reviewer writes most:
//
//   - LEAVE A FILE ALONE: a sentence naming a file with "alone", "don't
//     touch", "no changes to", "untouched", "unchanged", "never"… No planned
//     change may touch that file, and no line of the plan may mention it
//     except to say it is left alone.
//   - NAME A THING: "a test named TestX", "call it Y", a `backticked` name or a
//     Go test name. It must appear in a planned change AND in the requirements.
//
// Anything else the person wrote is still sent to the planner; it is simply
// not something this check can hold the plan to, and the plan says so.
package orch

import (
	"fmt"
	"regexp"
	"strings"
)

// reviewRequest is one checkable request read from the person's words.
type reviewRequest struct {
	Sentence string // the person's sentence, as written
	Avoid    string // a file the plan must leave alone
	Require  []string
}

var (
	sentenceSplit = regexp.MustCompile(`[.!?]+(?:\s+|$)|\n+`)
	filePattern   = regexp.MustCompile("`?([A-Za-z0-9_./-]+\\.[A-Za-z0-9]{1,6})`?")
	avoidCue      = regexp.MustCompile(`(?i)\b(alone|untouched|unchanged|don'?t (touch|change|edit|modify)|do not (touch|change|edit|modify)|no (changes|edits) to|never (touch|change|edit)|not (touch|change|edit|modify))\b`)
	negationCue   = regexp.MustCompile(`(?i)\b(alone|untouched|unchanged|not|no|never|without|dropped|drop|leave|left|excluded|out of scope|don'?t|nor|neither)\b`)
	namedPattern  = regexp.MustCompile("(?i)\\b(?:named|called|call it|name it)\\s+`?([A-Za-z_][A-Za-z0-9_]*)`?")
	tickPattern   = regexp.MustCompile("`([A-Za-z_][A-Za-z0-9_.()]*)`")
	goTestPattern = regexp.MustCompile(`\b(Test[A-Z][A-Za-z0-9_]*|Benchmark[A-Z][A-Za-z0-9_]*)\b`)
)

// readReviewRequests reads the checkable requests out of the person's words,
// one per sentence that has any.
func readReviewRequests(feedback string) []reviewRequest {
	var out []reviewRequest
	for _, s := range sentenceSplit.Split(feedback, -1) {
		s = strings.TrimSpace(s)
		if s == "" {
			continue
		}
		r := reviewRequest{Sentence: s}
		if avoidCue.MatchString(s) {
			if m := filePattern.FindStringSubmatch(s); m != nil {
				r.Avoid = m[1]
			}
		}
		seen := map[string]bool{}
		add := func(n string) {
			n = strings.TrimSpace(n)
			if n != "" && !seen[n] && n != r.Avoid {
				seen[n] = true
				r.Require = append(r.Require, n)
			}
		}
		for _, m := range namedPattern.FindAllStringSubmatch(s, -1) {
			add(m[1])
		}
		if r.Avoid == "" {
			for _, m := range tickPattern.FindAllStringSubmatch(s, -1) {
				add(m[1])
			}
		}
		// A Go test name the sentence merely refers to ("the TestLen check")
		// is not a request to create it; only a named or new one is.
		if len(r.Require) == 0 && r.Avoid == "" {
			for _, m := range goTestPattern.FindAllString(s, -1) {
				add(m)
			}
		}
		out = append(out, r)
	}
	return out
}

// revisionProblems holds the plan to the requests and says, in the person's
// terms, every place it falls short. Empty means the plan reflects them all.
func revisionProblems(planResult map[string]any, reqs []reviewRequest) []string {
	prd, _ := mapGet(planResult, "prd", map[string]any{}).(map[string]any)
	arch, _ := mapGet(planResult, "architecture", map[string]any{}).(map[string]any)
	issues := asMapList(mapGet(planResult, "issues", nil))

	type line struct{ where, text string }
	var prdLines, issueLines []line
	for _, sec := range []struct{ key, name string }{
		{"must_have", "Must have"}, {"nice_to_have", "Nice to have"},
		{"acceptance_criteria", "How you will know it is done"},
	} {
		for _, it := range coerceAnyList(prd[sec.key]) {
			if strings.HasPrefix(fmt.Sprint(it), reviewerRequestPrefix) {
				continue
			}
			prdLines = append(prdLines, line{sec.name, fmt.Sprint(it)})
		}
	}
	for _, iss := range issues {
		where := "the change \"" + mapStr(iss, "title", mapStr(iss, "name", "")) + "\""
		for _, k := range []string{"title", "description", "testing_strategy"} {
			if v := mapStr(iss, k, ""); v != "" {
				issueLines = append(issueLines, line{where, v})
			}
		}
		for _, it := range coerceAnyList(iss["acceptance_criteria"]) {
			issueLines = append(issueLines, line{where, fmt.Sprint(it)})
		}
	}

	var problems []string
	for _, r := range reqs {
		if f := r.Avoid; f != "" {
			for _, iss := range issues {
				for _, k := range []string{"files_to_modify", "files_to_create"} {
					for _, p := range coerceAnyList(iss[k]) {
						if strings.EqualFold(strings.TrimSpace(fmt.Sprint(p)), f) {
							problems = append(problems, fmt.Sprintf("the change %q still touches %s",
								mapStr(iss, "title", mapStr(iss, "name", "")), f))
						}
					}
				}
			}
			for _, comp := range asMapList(mapGet(arch, "components", nil)) {
				for _, p := range coerceAnyList(comp["touches_files"]) {
					if strings.EqualFold(strings.TrimSpace(fmt.Sprint(p)), f) {
						problems = append(problems, fmt.Sprintf("the design still has %s touching %s",
							mapStr(comp, "name", "a component"), f))
					}
				}
			}
			for _, l := range append(append([]line{}, prdLines...), issueLines...) {
				if mentionsWithoutLeavingAlone(l.text, f) {
					problems = append(problems, fmt.Sprintf("%s still says: %s", l.where, oneLineCut(l.text, 160)))
				}
			}
		}
		for _, n := range r.Require {
			inIssues, inPRD := false, false
			for _, l := range issueLines {
				if strings.Contains(l.text, n) {
					inIssues = true
				}
			}
			for _, l := range prdLines {
				if l.where != "Nice to have" && strings.Contains(l.text, n) {
					inPRD = true
				}
			}
			if !inIssues {
				problems = append(problems, fmt.Sprintf("no planned change mentions %s", n))
			}
			if !inPRD {
				problems = append(problems, fmt.Sprintf("Must have and the checks do not mention %s", n))
			}
		}
	}
	return problems
}

// howThePlanChanged is one line per sentence the person wrote: what the
// revised plan now does about it, or that it could not be checked.
//
// The note is quoted in full right above this list, so a line does not say it
// again: with one sentence the line is only the outcome, and with several it
// leads with a short handle on the sentence it answers. Repeating the whole
// note as the first bullet read as an echo (job 70, 2026-11-07).
func howThePlanChanged(reqs []reviewRequest, problems []string, listed bool) []string {
	var out []string
	for _, r := range reqs {
		lead := func(outcome string) string {
			if len(reqs) == 1 {
				return "- " + strings.ToUpper(outcome[:1]) + outcome[1:]
			}
			return "- " + requestHandle(r.Sentence) + " — " + outcome
		}
		var bad bool
		for _, p := range problems {
			if (r.Avoid != "" && strings.Contains(p, r.Avoid)) || containsAny(p, r.Require) ||
				(strings.Contains(p, "you asked to add, not to replace") && additiveSentence(r.Sentence)) ||
				(strings.Contains(p, "duplicate") || strings.Contains(p, "check the same thing")) && reshapeCue.MatchString(r.Sentence) {
				bad = true
			}
		}
		switch {
		case bad:
			out = append(out, lead("**not fully reflected yet** (see below)"))
		case r.Avoid != "" && len(r.Require) == 0:
			out = append(out, lead(fmt.Sprintf("no planned change touches `%s`.", r.Avoid)))
		case len(r.Require) > 0:
			out = append(out, lead("the plan now includes "+codeJoin(r.Require)+"."))
		default:
			if listed {
				out = append(out, lead("the changes it made are listed below."))
			} else {
				out = append(out, lead("the plan made no change to its files or tests for this; check the summary."))
			}
		}
	}
	return out
}

// requestHandle is a short, whole-word handle on one of the person's
// sentences: its first clause, quoted, so a reader can find it in the note.
func requestHandle(s string) string {
	h := strings.TrimRight(phrase(s, 60), " ,;:")
	if h == "" {
		w := strings.Fields(s)
		if len(w) > 8 {
			w = append(w[:8:8], "…")
		}
		h = strings.Join(w, " ")
	}
	return "“" + h + "”"
}

var clauseSplit = regexp.MustCompile(`[,;:()—–]|\. `)

// mentionsWithoutLeavingAlone reports a line that would CHANGE file: it names
// the file beside an editing verb and nowhere says the file is left alone.
//
// Two live misreadings shaped it. "Add Len to the method list in README.md
// without altering the CLI example" must be caught although it holds a
// negation ("without" negates the example, so a whole-line "not" is not
// enough: the strong state words below are). And "README.md, cmd/ledger and
// every other file untouched", or a check like `git diff -- README.md` is
// empty, must NOT be caught: they say the file is left alone, in a list or a
// command, where no word sits right beside it.
func mentionsWithoutLeavingAlone(text, file string) bool {
	lower := strings.ToLower(text)
	lf := strings.ToLower(strings.Trim(file, "`"))
	if !strings.Contains(lower, lf) || strongLeftAlone.MatchString(lower) {
		return false
	}
	words := strings.Fields(lower)
	for i, w := range words {
		if !strings.Contains(w, lf) {
			continue
		}
		left, edits := false, false
		for j := max0(i - 8); j < i; j++ {
			ww := strings.Trim(words[j], "`'\",.:;()")
			if beforeNegation[ww] {
				left = true
			}
			if editVerb[ww] {
				edits = true
			}
		}
		for j := i + 1; j < len(words) && j <= i+3; j++ {
			if afterState[strings.Trim(words[j], "`'\",.:;()")] {
				left = true
			}
		}
		if edits && !left {
			return true
		}
	}
	return false
}

// strongLeftAlone is a line that says, somewhere, that files are left alone.
var strongLeftAlone = regexp.MustCompile(`\b(untouched|unchanged|left alone|stays? alone|not (be )?(touched|modified|edited|changed)|no changes? to|is not modified|never (touch|edit|modify)|neither modified|out of scope|left as-is)\b`)

// editVerb is a word that, before a file, means the plan would change it.
var editVerb = map[string]bool{"add": true, "adds": true, "adding": true, "update": true, "updates": true,
	"updating": true, "edit": true, "edits": true, "modify": true, "modifies": true, "change": true,
	"changes": true, "document": true, "documents": true, "mention": true, "mentions": true, "amend": true,
	"append": true, "write": true, "writes": true, "extend": true, "list": true, "lists": true, "touch": true,
	"touches": true, "rewrite": true, "insert": true}

var beforeNegation = map[string]bool{"not": true, "no": true, "never": true, "leave": true, "leaves": true,
	"don't": true, "dont": true, "without": true, "neither": true, "nor": true, "excluding": true, "except": true, "untouched:": true}

var afterState = map[string]bool{"alone": true, "untouched": true, "unchanged": true, "left": true, "stays": true,
	"remains": true, "excluded": true, "dropped": true, "neither": true, "not": true, "never": true, "is": false}

func max0(i int) int {
	if i < 0 {
		return 0
	}
	return i
}

func containsAny(s string, needles []string) bool {
	for _, n := range needles {
		if strings.Contains(s, n) {
			return true
		}
	}
	return false
}

func codeJoin(items []string) string {
	q := make([]string, len(items))
	for i, it := range items {
		q[i] = "`" + it + "`"
	}
	return strings.Join(q, ", ")
}

func oneLineCut(s string, n int) string {
	s = strings.Join(strings.Fields(s), " ")
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n-1]) + "…"
}

// An ADDITIVE request keeps everything and adds one thing ("also add a table
// test"); a RESHAPING request makes an old form go away ("make the
// zero-allocation check its own test named X", "rename", "move", "split",
// "replace"). Live on 2026-11-07 "Also add a table test" was read as replace
// and dropped two tests the person had seen, so an additive request is held
// to keeping them. But the same day "Also make the TestLen zero-allocation
// check its own test named TestLenDoesNotAllocate" was a MOVE, and holding
// it to "keep everything" would defend the very leftover the person asked to
// reshape: the planner kept TestLenNoAllocations beside the new test and
// wrote a rule protecting the duplicate. So "also" alone is not additive; a
// sentence is additive only when it ADDS (add, include, cover, write…) and
// reshapes nothing, and a test a reshaping sentence names is never held.
var (
	additiveCue = regexp.MustCompile(`(?i)\b(also|as well|in addition|additionally|too|on top of)\b`)
	addVerb     = regexp.MustCompile(`(?i)\b(add|adds|adding|include|including|cover|covering|write|keep|plus)\b`)
	reshapeCue  = regexp.MustCompile(`(?i)\b(its own|their own|own test|separate|split|move|moved|rename|renamed|replace|replaces|replacing|instead|rather than|turn (it|them) into|drop|remove|delete|merge|combine|fold|consolidate|only)\b`)
)

// additiveSentence is one the person wrote to add something and change
// nothing else.
func additiveSentence(s string) bool {
	return additiveCue.MatchString(s) && addVerb.MatchString(s) && !reshapeCue.MatchString(s)
}

// additiveProblems names every test the plan sent back would have added that
// the revision no longer adds, when the person asked only to ADD — except a
// test a reshaping sentence of theirs names or stems from ("TestLen…" when
// they asked to move a TestLen check out), which they asked to go away.
func additiveProblems(feedback string, beforeTests []string, revised map[string]any) []string {
	var additive bool
	var reshaped []string
	for _, sent := range sentenceSplit.Split(feedback, -1) {
		if additiveSentence(sent) {
			additive = true
		}
		if reshapeCue.MatchString(sent) {
			reshaped = append(reshaped, goTestPattern.FindAllString(sent, -1)...)
		}
	}
	if !additive {
		return nil
	}
	now := map[string]bool{}
	for _, n := range planTests(revised) {
		now[n] = true
	}
	// A test the person gave a new name is renamed, not dropped — that test
	// exactly, never everything that shares its stem: "TestRefunds" renamed
	// does not excuse dropping TestRefundsEmpty. A name whose test cannot be
	// told excuses ONE of the tests it could be, once the new name is there.
	renamed := map[string]bool{}
	spare := 0
	for _, r := range readRenames(personWords(feedback), beforeTests) {
		switch {
		case r.From != "":
			renamed[r.From] = true
		case now[r.To]:
			spare++
		}
	}
	var out []string
	for _, n := range beforeTests {
		if now[n] || renamed[n] || stemsFrom(n, reshaped) {
			continue
		}
		if spare > 0 {
			spare--
			continue
		}
		out = append(out, fmt.Sprintf("the revision no longer adds %s, but you asked to add, not to replace", n))
	}
	return out
}

func stemsFrom(name string, roots []string) bool {
	for _, r := range roots {
		if strings.HasPrefix(name, r) {
			return true
		}
	}
	return false
}

// duplicateProblems holds a revision to the person's reshaping: two new tests
// that check the same thing, and any line of the plan whose only job is to
// protect such a leftover ("renaming, removing or merging either allocation
// test is out of scope"; "a later tidy-up must not delete one of them").
func duplicateProblems(plan map[string]any) []string {
	var out []string
	tests := planTests(plan)
	var dupNames []string
	for i := 0; i < len(tests); i++ {
		for j := i + 1; j < len(tests); j++ {
			if sameCheck(tests[i], tests[j]) {
				out = append(out, fmt.Sprintf("the plan adds both %s and %s, which check the same thing; keep one", tests[i], tests[j]))
				dupNames = append(dupNames, tests[i], tests[j])
			}
		}
	}
	prd, _ := mapGet(plan, "prd", map[string]any{}).(map[string]any)
	var lines []string
	for _, k := range []string{"out_of_scope", "risks", "assumptions", "must_have", "acceptance_criteria"} {
		for _, it := range coerceAnyList(prd[k]) {
			lines = append(lines, fmt.Sprint(it))
		}
	}
	for _, it := range coerceAnyList(approverSummary(plan)["risks"]) {
		lines = append(lines, fmt.Sprint(it))
	}
	for _, it := range coerceAnyList(approverSummary(plan)["will_not_change"]) {
		lines = append(lines, fmt.Sprint(it))
	}
	seen := map[string]bool{}
	for _, l := range lines {
		if strings.HasPrefix(l, reviewerRequestPrefix) || seen[l] {
			continue
		}
		if protectsLeftover(l, dupNames) {
			seen[l] = true
			out = append(out, "the plan protects a duplicate instead of removing it: "+oneLineCut(l, 160))
		}
	}
	return out
}

var (
	protectCue   = regexp.MustCompile(`(?i)\b(must not|do not|don'?t|never|not)\s+(be\s+)?(delete|deleted|remove|removed|merge|merged|rename|renamed|consolidate|consolidated|collapse|collapsed|tidy)\b|\b(renaming|removing|merging|consolidating|deleting)\b[^.]{0,40}\b(either|both|one of)\b|\bnear-identical\b|\bduplicate\w*\b|\bon purpose\b`)
	testWordCue  = regexp.MustCompile(`(?i)\btests?\b|\bTest[A-Z]`)
	camelSplit   = regexp.MustCompile(`[A-Z][a-z0-9]*`)
	fillerTokens = map[string]bool{"test": true, "does": true, "not": true, "no": true, "is": true, "it": true, "has": true, "should": true, "zero": true}
)

// protectsLeftover is a line that guards tests against being removed or
// merged, or names a duplicate pair, rather than describing the change.
func protectsLeftover(line string, dupNames []string) bool {
	if !testWordCue.MatchString(line) || !protectCue.MatchString(line) {
		return false
	}
	if len(dupNames) == 0 {
		// Without a duplicate pair in the tests, only words that defend
		// a duplicate by name count ("near-identical", "on purpose").
		return regexp.MustCompile(`(?i)near-identical|duplicate|on purpose|either allocation|both allocation`).MatchString(line)
	}
	return true
}

// sameCheck: two test names that, past their shared subject, name the same
// check — TestLenNoAllocations and TestLenDoesNotAllocate both say "Len,
// alloc"; TestLen and TestLenEmpty do not.
func sameCheck(a, b string) bool {
	sa, sb := checkStems(a), checkStems(b)
	if len(sa) < 2 || len(sa) != len(sb) {
		return false
	}
	for k := range sa {
		if !sb[k] {
			return false
		}
	}
	return true
}

func checkStems(name string) map[string]bool {
	out := map[string]bool{}
	for _, tok := range camelSplit.FindAllString(strings.TrimPrefix(name, "Test"), -1) {
		t := strings.ToLower(tok)
		if fillerTokens[t] {
			continue
		}
		if len(t) > 5 {
			t = t[:5]
		}
		out[t] = true
	}
	return out
}

// planTests is the new tests a plan names: the planner's own list when it
// gave one, else every Go test name its planned changes mention.
func planTests(plan map[string]any) []string {
	if as := approverSummary(plan); len(coerceAnyList(as["tests_added"])) > 0 {
		var out []string
		for _, it := range coerceAnyList(as["tests_added"]) {
			if n := strings.Trim(strings.TrimSpace(fmt.Sprint(it)), "`"); goTestPattern.MatchString(n) {
				out = append(out, n)
			}
		}
		return out
	}
	issues := asMapList(mapGet(plan, "issues", nil))
	return testNames(issueOrder(plan, issues), nil)
}

// planFiles is every file a plan's changes touch, in order, once.
func planFiles(plan map[string]any) []string {
	seen := map[string]bool{}
	var out []string
	for _, iss := range asMapList(mapGet(plan, "issues", nil)) {
		for _, k := range []string{"files_to_modify", "files_to_create"} {
			for _, f := range coerceAnyList(iss[k]) {
				if n := strings.TrimSpace(fmt.Sprint(f)); n != "" && !seen[n] {
					seen[n] = true
					out = append(out, n)
				}
			}
		}
	}
	return out
}

// planDiff is what changed between the plan sent back and the revision, in
// the person's terms: tests and files gained and lost.
func planDiff(beforeTests, beforeFiles []string, revised map[string]any, existing map[string]bool) []string {
	var out []string
	gained, lost := setDiff(beforeTests, planTests(revised))
	gained, lost = withoutExisting(gained, existing), withoutExisting(lost, existing)
	if len(gained) > 0 {
		out = append(out, "- Tests now added: "+codeJoin(gained)+".")
	}
	if len(lost) > 0 {
		out = append(out, "- Tests no longer added: "+codeJoin(lost)+".")
	}
	fg, fl := setDiff(beforeFiles, planFiles(revised))
	if len(fg) > 0 {
		out = append(out, "- Files now changed: "+codeJoin(fg)+".")
	}
	if len(fl) > 0 {
		out = append(out, "- Files no longer changed: "+codeJoin(fl)+".")
	}
	return out
}

func setDiff(before, after []string) (gained, lost []string) {
	b, a := map[string]bool{}, map[string]bool{}
	for _, x := range before {
		b[x] = true
	}
	for _, x := range after {
		a[x] = true
		if !b[x] {
			gained = append(gained, x)
		}
	}
	for _, x := range before {
		if !a[x] {
			lost = append(lost, x)
		}
	}
	return gained, lost
}

func withoutExisting(names []string, existing map[string]bool) []string {
	var out []string
	for _, n := range names {
		if !existing[n] {
			out = append(out, n)
		}
	}
	return out
}

// stringList reads a []string that may have come back through JSON as []any.
func stringList(v any) []string {
	var out []string
	for _, it := range coerceAnyList(v) {
		out = append(out, fmt.Sprint(it))
	}
	return out
}

// revisionChecks is every way a revision can fall short of the person's
// words: the requests read from them, an additive request kept, and no
// duplicate left behind or protected.
func revisionChecks(plan map[string]any, reqs []reviewRequest, feedback string, beforeTests []string) []string {
	out := revisionProblems(plan, reqs)
	out = append(out, additiveProblems(feedback, beforeTests, plan)...)
	out = append(out, renameProblems(personWords(feedback), beforeTests, plan)...)
	return append(out, duplicateProblems(plan)...)
}

// renameTarget is a sentence that gives a test the plan already adds a NAME:
// "name the test TestX", "call the new test TestX", "rename it TestX",
// "rename TestA to TestX". Job 70 (2026-11-07) asked "name the test
// TestNegativesCountsRefunds" of a plan that added one test, TestNegatives,
// and the planner kept TestNegatives and added a second test beside it,
// labelled "reviewer-requested". A name for "the test" is a rename.
var (
	renameTheTest = regexp.MustCompile(`(?i)\b(?:name|call|rename|retitle)\s+(?:the|that|this|your|its|it(?:s)?)\s*(?:new\s+|added\s+|one\s+)?(?:test\s+)?(?:function\s+)?(?:to\s+|as\s+)?` + "`?" + `(Test[A-Z][A-Za-z0-9_]*)`)
	renameFromTo  = regexp.MustCompile(`(?i)\brename\s+` + "`?" + `(Test[A-Z][A-Za-z0-9_]*)` + "`?" + `\s+(?:to|as)\s+` + "`?" + `(Test[A-Z][A-Za-z0-9_]*)`)
)

// testRename is one rename the person asked for: From is the test the plan
// sent back added, To the name they gave it. A name whose test cannot be told
// has no From; Among is then every test it could be.
type testRename struct {
	From, To string
	Among    []string
}

// readRenames reads the renames out of the person's words, against the tests
// the plan they sent back would have added. "The test" is the one test that
// plan added; with several, it is the test the new name EXTENDS — the
// longest one the name starts with, as "TestRefundsSumsAbsoluteValues"
// extends TestRefunds and not TestRefundsEmpty. Job 76 (2026-11-07) asked
// "name the test TestRefundsSumsAbsoluteValues" of a plan adding TestRefunds,
// TestRefundsEmpty and TestRefundsNone; reading nothing there, the gate then
// held the revision to "add, not replace" and the plan grew a fourth test.
// When no test is extended, which one is meant cannot be told, and the
// rename carries Among instead: one of them takes the name, none is added.
func readRenames(feedback string, beforeTests []string) []testRename {
	var out []testRename
	for _, s := range sentenceSplit.Split(feedback, -1) {
		if m := renameFromTo.FindStringSubmatch(s); m != nil {
			out = append(out, testRename{From: m[1], To: m[2]})
			continue
		}
		m := renameTheTest.FindStringSubmatch(s)
		if m == nil {
			continue
		}
		var others []string
		for _, b := range beforeTests {
			if b != m[1] {
				others = append(others, b)
			}
		}
		if len(others) != len(beforeTests) || len(others) == 0 {
			// The name is already one the plan adds, or there was no test
			// to rename: nothing to read.
			continue
		}
		if len(others) == 1 {
			out = append(out, testRename{From: others[0], To: m[1]})
			continue
		}
		extended := ""
		for _, o := range others {
			if strings.HasPrefix(m[1], o) && len(o) > len(extended) {
				extended = o
			}
		}
		if extended != "" {
			out = append(out, testRename{From: extended, To: m[1]})
			continue
		}
		out = append(out, testRename{To: m[1], Among: others})
	}
	return out
}

// renameReading is what the gate adds to the person's words before the first
// revision so the planners read a name as a rename, not as one more test.
func renameReading(feedback string, beforeTests []string) string {
	var lines []string
	for _, r := range readRenames(feedback, beforeTests) {
		if r.From == "" {
			lines = append(lines, fmt.Sprintf("%s is a new name for ONE of %s, the one it describes: rename that test to %s and keep the same number of tests; do not add %s beside all of them.",
				r.To, strings.Join(r.Among, ", "), r.To, r.To))
			continue
		}
		lines = append(lines, fmt.Sprintf("%s is a new name for %s: rename %s to %s everywhere and keep ONE test; do not add a second test beside it.",
			r.To, r.From, r.From, r.To))
	}
	if len(lines) == 0 {
		return ""
	}
	return readingMarker + strings.Join(lines, "\n- ")
}

// renameProblems holds a revision to a rename: the old name must be gone from
// the tests it adds once the new one is there — or, for a name whose test
// could not be told, one of the tests it could be.
func renameProblems(feedback string, beforeTests []string, revised map[string]any) []string {
	now := map[string]bool{}
	for _, n := range planTests(revised) {
		now[n] = true
	}
	var out []string
	for _, r := range readRenames(feedback, beforeTests) {
		if !now[r.To] {
			continue
		}
		if r.From != "" {
			if now[r.From] {
				out = append(out, fmt.Sprintf("you asked to name the test %s, not to add a second one, but the plan still adds %s beside it; rename %s to %s",
					r.To, r.From, r.From, r.To))
			}
			continue
		}
		kept := 0
		for _, a := range r.Among {
			if now[a] {
				kept++
			}
		}
		if kept == len(r.Among) {
			out = append(out, fmt.Sprintf("you asked to name a test %s, not to add one, but the plan adds it beside %s; rename the one it describes",
				r.To, strings.Join(r.Among, ", ")))
		}
	}
	return out
}
