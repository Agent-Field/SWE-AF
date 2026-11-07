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
func howThePlanChanged(reqs []reviewRequest, problems []string) []string {
	var out []string
	for _, r := range reqs {
		var bad bool
		for _, p := range problems {
			if (r.Avoid != "" && strings.Contains(p, r.Avoid)) || containsAny(p, r.Require) {
				bad = true
			}
		}
		switch {
		case bad:
			out = append(out, fmt.Sprintf("- %s — **not fully reflected yet** (see below)", r.Sentence))
		case r.Avoid != "" && len(r.Require) == 0:
			out = append(out, fmt.Sprintf("- %s — no planned change touches `%s`.", r.Sentence, r.Avoid))
		case len(r.Require) > 0:
			out = append(out, fmt.Sprintf("- %s — the plan now includes %s.", r.Sentence, codeJoin(r.Require)))
		default:
			out = append(out, fmt.Sprintf("- %s — sent to the planner; check the summary below.", r.Sentence))
		}
	}
	return out
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
