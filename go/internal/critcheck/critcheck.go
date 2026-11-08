// Package critcheck tells a failed acceptance check that could not run as
// written apart from one that ran and found the work wanting.
//
// The planner writes acceptance criteria as commands and the verifier runs
// them literally. Live on 2026-11-08 (issue #291, WorkAF r8) the planner wrote
// `go doc ./ ledger.Ledger.Checksum`. The Go tool rejects that spelling for
// every implementation — "doc: too many periods in symbol specification", exit
// 2 — so the verifier failed a change that built, passed its tests and had the
// method, and no pull request was opened. The fix cycle could not help: no code
// change makes a malformed command well formed.
//
// A CHECK THE TOOL REFUSES TO RUN SAYS NOTHING ABOUT THE WORK. So after the
// verifier answers, every failed criterion that names a command is run again
// here, in code. When the tool's own output is a usage or syntax error (not an
// assertion), the criterion is malformed: it is repaired once where a
// deterministic repair is known (the Go symbol form above becomes
// `go doc ./ Ledger.Checksum`) and judged by the repaired command, and
// otherwise it is marked not counted, with a note saying why. A criterion
// whose command runs and fails stays failed, and so does every criterion this
// package cannot re-run.
//
// The project's own build and test gate (package projectcheck) is never
// touched: its criterion is skipped here, and it runs after this pass.
package critcheck

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"time"
)

// Action is what Reconcile did to one failed criterion.
type Action string

const (
	// Repaired: the command was malformed, the repaired command ran cleanly,
	// and the criterion now passes on the repaired command's evidence.
	Repaired Action = "repaired"
	// RepairedFailed: the repaired command ran and failed for real, so the
	// criterion stays failed with the repaired command's evidence.
	RepairedFailed Action = "repaired_failed"
	// NotCounted: the command was malformed and no repair is known, or it
	// read uncommitted state and the rewritten command's output cannot be
	// judged from the criterion's words; the criterion no longer counts
	// against the run.
	NotCounted Action = "not_counted"
	// Rewritten: the command read the working tree after the work was
	// committed; the base-comparing command ran in its place and passed.
	Rewritten Action = "rewritten"
	// RewrittenFailed: the base-comparing command ran and the work fails it
	// for real, so the criterion stays failed with that evidence.
	RewrittenFailed Action = "rewritten_failed"
)

// Finding is one failed criterion Reconcile found malformed.
type Finding struct {
	Criterion string
	Command   string
	// Problem is the tool's own complaint, one line.
	Problem string
	// Repair is the command run in its place, "" when none was known.
	Repair string
	Action Action
	// WorkingTree is true when the check read uncommitted state (see
	// WorkingTreeRewrite) rather than being refused by its tool.
	WorkingTree bool
}

// workingTreeProblem is the reason a working-tree check proves nothing.
const workingTreeProblem = "it reads uncommitted changes, and the work is committed"

// Note is the sentence a run records for the finding.
func (f Finding) Note() string {
	if f.WorkingTree {
		switch f.Action {
		case Rewritten:
			return fmt.Sprintf("Acceptance check rewritten: `%s` %s; `%s` passed in its place",
				f.Command, workingTreeProblem, f.Repair)
		case RewrittenFailed:
			return fmt.Sprintf("Acceptance check rewritten: `%s` %s; `%s` ran in its place and failed",
				f.Command, workingTreeProblem, f.Repair)
		default:
			return fmt.Sprintf("Acceptance check not counted: `%s` %s", f.Command, workingTreeProblem)
		}
	}
	switch f.Action {
	case Repaired:
		return fmt.Sprintf("Acceptance check repaired: `%s` cannot run as written (%s); `%s` passed in its place",
			f.Command, f.Problem, f.Repair)
	case RepairedFailed:
		return fmt.Sprintf("Acceptance check repaired: `%s` cannot run as written (%s); `%s` ran in its place and failed",
			f.Command, f.Problem, f.Repair)
	default:
		return fmt.Sprintf("Acceptance check not counted: `%s` cannot run as written (%s)", f.Command, f.Problem)
	}
}

// CommandTimeout bounds one re-run.
var CommandTimeout = 2 * time.Minute

// maxCriteria bounds how many failed criteria one pass re-runs, and
// maxCommands how many commands of one criterion.
const (
	maxCriteria = 12
	maxCommands = 3
	outputTail  = 1500
)

// runShell executes one command line in dir. A package variable so tests can
// replace it; the default runs the real process under sh.
var runShell = func(ctx context.Context, dir, line string) (int, string, error) {
	cctx, cancel := context.WithTimeout(ctx, CommandTimeout)
	defer cancel()
	cmd := exec.CommandContext(cctx, "sh", "-c", line)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "CI=1")
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &out
	err := cmd.Run()
	if err == nil {
		return 0, out.String(), nil
	}
	var ee *exec.ExitError
	if errors.As(err, &ee) {
		if cctx.Err() != nil {
			return -1, out.String(), cctx.Err()
		}
		return ee.ExitCode(), out.String(), nil
	}
	return -1, out.String(), err
}

// ProjectCheckField marks the criterion package projectcheck adds; it is never
// reconsidered here.
const ProjectCheckField = "project_check"

// Reconcile is ReconcileFrom without a base commit: only checks the tool
// itself refused are reconsidered.
func Reconcile(ctx context.Context, repo string, verification map[string]any) []Finding {
	return ReconcileFrom(ctx, repo, "", verification)
}

// ReconcileFrom re-runs the commands of verification's failed criteria in
// repo and rewrites the ones the tool itself refused (see the package
// comment). With base (the commit the run started from) it also rewrites the
// checks that read uncommitted working-tree state once the work is committed
// (see WorkingTreeRewrite). When it changed something and no failed criterion
// remains, verification passes. It returns what it found, in criterion order.
func ReconcileFrom(ctx context.Context, repo, base string, verification map[string]any) []Finding {
	if verification == nil || strings.TrimSpace(repo) == "" {
		return nil
	}
	if _, err := exec.LookPath("sh"); err != nil {
		return nil
	}
	list := criteriaList(verification["criteria_results"])
	committed := strings.TrimSpace(base) != "" && workIsCommitted(ctx, repo, base)
	var findings []Finding
	tried := 0
	for _, c := range list {
		if tried >= maxCriteria {
			break
		}
		if passedOf(c) || isProjectCheck(c) {
			continue
		}
		crit, _ := c["criterion"].(string)
		cmds := Commands(crit)
		if len(cmds) == 0 {
			evidence, _ := c["evidence"].(string)
			cmds = backticked(evidence)
		}
		cmds = runnable(cmds)
		if len(cmds) == 0 {
			continue
		}
		tried++
		if committed {
			if f, ok := reconcileWorkingTree(ctx, repo, base, c, crit, cmds); ok {
				findings = append(findings, f)
				continue
			}
		}
		if f, ok := reconcileOne(ctx, repo, c, crit, cmds); ok {
			findings = append(findings, f)
		}
	}
	if len(findings) == 0 {
		return nil
	}
	remaining := 0
	for _, c := range list {
		if !passedOf(c) {
			remaining++
		}
	}
	lead := make([]string, 0, len(findings))
	for _, f := range findings {
		lead = append(lead, f.Note())
	}
	summary, _ := verification["summary"].(string)
	if remaining == 0 {
		verification["passed"] = true
		summary = "Every acceptance check that could run passed. " + summary
	}
	verification["summary"] = strings.TrimSpace(strings.Join(lead, ". ") + ". " + summary)
	return findings
}

// reconcileOne re-runs a criterion's commands and, on the first one the tool
// refuses, rewrites the criterion in place.
func reconcileOne(ctx context.Context, repo string, c map[string]any, crit string, cmds []string) (Finding, bool) {
	for i, line := range cmds {
		if i >= maxCommands {
			break
		}
		_, out, err := runShell(ctx, repo, line)
		if err != nil {
			continue
		}
		problem := Malformed(out)
		if problem == "" {
			continue
		}
		f := Finding{Criterion: crit, Command: line, Problem: problem, Action: NotCounted}
		original, _ := c["evidence"].(string)
		if fixed := Repair(line, out); fixed != "" {
			rcode, rout, rerr := runShell(ctx, repo, fixed)
			if rerr == nil && Malformed(rout) == "" {
				f.Repair = fixed
				if rcode == 0 {
					f.Action = Repaired
					c["passed"] = true
				} else {
					f.Action = RepairedFailed
				}
				c["evidence"] = fmt.Sprintf("%s.\n`%s` exited %d:\n%s", f.Note(), fixed, rcode, tail(rout))
				c["repaired_command"] = fixed
				return f, true
			}
		}
		c["passed"] = true
		c["not_counted"] = true
		c["evidence"] = strings.TrimSpace(fmt.Sprintf("%s. The verifier said: %s", f.Note(), original))
		return f, true
	}
	return Finding{}, false
}

// usageSignatures are the tools' own complaints about how they were called.
// Each names a malformed invocation, never a missing symbol, file or test:
// `go doc ./ Ledger.Missing` ("no method or field") and a grep that matches
// nothing are assertions and are not here.
var usageSignatures = []*regexp.Regexp{
	regexp.MustCompile(`too many periods in symbol specification`),
	regexp.MustCompile(`flag provided but not defined`),
	regexp.MustCompile(`flag needs an argument`),
	regexp.MustCompile(`(?m)^Usage of \S+`),
	regexp.MustCompile(`(?m)^(?:go|cargo|npm): unknown (?:command|subcommand)`),
	regexp.MustCompile(`(?m)^go \w+: unknown flag`),
	regexp.MustCompile(`(?m)^error: (?:unexpected argument|unrecognized subcommand|no such command|unexpected value)`),
	regexp.MustCompile(`(?m)^(?:e?grep|rg): (?:invalid option|unrecognized option|Unmatched|Invalid (?:regular expression|preceding|range|back reference|character class)|Trailing backslash|regex parse error)`),
	regexp.MustCompile(`syntax error near unexpected token`),
	regexp.MustCompile(`unexpected EOF while looking for matching`),
	regexp.MustCompile(`(?m)^sh: \d+: Syntax error`),
	regexp.MustCompile(`(?:test|\[): .*(?:unary|binary|integer expression|argument) (?:operator )?expected`),
}

// assertionMarkers mean a command ran and something it checked failed; output
// carrying one is never malformed, whatever else it prints.
var assertionMarkers = regexp.MustCompile(`(?m)^(?:--- FAIL|FAIL\s|panic: )|^test result: FAILED|AssertionError`)

// Malformed returns the tool's usage complaint in out (its first line), or ""
// when out reads as an ordinary run.
func Malformed(out string) string {
	if assertionMarkers.MatchString(out) {
		return ""
	}
	for _, re := range usageSignatures {
		if loc := re.FindStringIndex(out); loc != nil {
			start := strings.LastIndex(out[:loc[0]], "\n") + 1
			end := strings.Index(out[loc[0]:], "\n")
			if end < 0 {
				end = len(out)
			} else {
				end += loc[0]
			}
			return strings.TrimSpace(out[start:end])
		}
	}
	return ""
}

// goDocPeriods matches the two-argument `go doc <pkg> <sym>` form; the symbol's
// leading segment is the package name when it carries two dots.
var goDocPeriods = regexp.MustCompile(`^(.*\bgo\s+doc\s+(?:-\S+\s+)*[^-\s]\S*\s+)([A-Za-z_]\w*)\.([A-Za-z_]\w*\.[A-Za-z_]\w*)(\s.*)?$`)

// Repair returns line rewritten into the form its tool accepts, or "" when no
// deterministic repair is known for the complaint in out.
//
// `go doc <pkg> <sym>[.<method>]` takes at most one dot in the symbol once a
// package is named, so `go doc ./ ledger.Ledger.Checksum` repairs to
// `go doc ./ Ledger.Checksum`: the leading segment repeats the package.
func Repair(line, out string) string {
	if strings.Contains(out, "too many periods in symbol specification") {
		if m := goDocPeriods.FindStringSubmatch(line); m != nil {
			return m[1] + m[3] + m[4]
		}
	}
	return ""
}

// Rewrite is one acceptance criterion FixPlan respelled.
type Rewrite struct{ From, To string }

// FixPlan respells, in place, the acceptance criteria anywhere in a plan
// (the PRD's, each issue's, each fast task's) whose command is a form its
// tool is known to refuse, before any agent works to them. Only the
// deterministic repairs Repair knows are made, without running anything;
// what this cannot see, Reconcile catches after the verifier.
func FixPlan(plan any) []Rewrite {
	var out []Rewrite
	var walk func(v any)
	walk = func(v any) {
		switch x := v.(type) {
		case map[string]any:
			for k, child := range x {
				if k == "acceptance_criteria" {
					out = append(out, fixList(child)...)
					continue
				}
				walk(child)
			}
		case []any:
			for _, child := range x {
				walk(child)
			}
		case []map[string]any:
			for _, child := range x {
				walk(child)
			}
		}
	}
	walk(plan)
	return out
}

func fixList(v any) []Rewrite {
	var out []Rewrite
	fix := func(crit string) string {
		fixed := crit
		for _, cmd := range Commands(crit) {
			if r := Repair(cmd, "too many periods in symbol specification"); r != "" {
				fixed = strings.Replace(fixed, cmd, r, 1)
			}
		}
		if fixed != crit {
			out = append(out, Rewrite{From: crit, To: fixed})
		}
		return fixed
	}
	switch l := v.(type) {
	case []any:
		for i, x := range l {
			if s, ok := x.(string); ok {
				l[i] = fix(s)
			}
		}
	case []string:
		for i, s := range l {
			l[i] = fix(s)
		}
	}
	return out
}

// backtickSpan is one inline code span.
var backtickSpan = regexp.MustCompile("`([^`\n]+)`")

// inlineCommand is a command spelled in prose without backticks, as the
// planner wrote #291's: "go doc ./ ledger.Ledger.Checksum (run from the repo
// root) prints …". It ends at a parenthesis, a verb describing the result,
// or punctuation.
var inlineCommand = regexp.MustCompile(`(?:^|[\s:])((?:go|cargo|git|grep|egrep|rg|test|npm|pytest)\s[^\n]*?)(?:\s+\(|\s+(?:prints|outputs|shows|exits|returns|passes|succeeds|fails|reports|lists|must|should|yields|produces|contains)\b|[;,]\s|\.\s+[A-Z]|$)`)

// Commands returns the commands a criterion names: its backticked spans, or
// failing those, a command spelled inline.
func Commands(criterion string) []string {
	if spans := backticked(criterion); len(spans) > 0 {
		return spans
	}
	var out []string
	for _, m := range inlineCommand.FindAllStringSubmatch(criterion, -1) {
		cmd := strings.TrimSpace(m[1])
		if strings.HasSuffix(cmd, ".") && !strings.HasSuffix(cmd, "..") {
			cmd = strings.TrimSuffix(cmd, ".") // the sentence's full stop
		}
		out = append(out, cmd)
	}
	return out
}

func backticked(s string) []string {
	var out []string
	for _, m := range backtickSpan.FindAllStringSubmatch(s, -1) {
		out = append(out, strings.TrimSpace(m[1]))
	}
	return out
}

// checkPrograms are the programs a re-run may start, with the subcommands
// allowed for those that have them. They read the tree; none deploys,
// installs or writes outside the build cache. Anything else is left to the
// verifier's verdict.
var checkPrograms = map[string][]string{
	"go":     {"doc", "vet", "build", "test", "list"},
	"cargo":  {"build", "test", "check", "doc"},
	"npm":    {"test", "run"},
	"grep":   nil,
	"egrep":  nil,
	"rg":     nil,
	"test":   nil,
	"[":      nil,
	"pytest": nil,
	// git only to read what changed: status and diff are the working-tree
	// checks WorkingTreeRewrite turns into base comparisons.
	"git": {"status", "diff"},
}

// segmentSplit separates the parts of a compound command line.
var segmentSplit = regexp.MustCompile(`\s*(?:&&|\|\||\||;)\s*`)

// runnable keeps the commands whose every part starts an allowed program,
// with no redirection, substitution or background job.
func runnable(cmds []string) []string {
	var out []string
	for _, line := range cmds {
		if strings.ContainsAny(line, "<>`$") || strings.Contains(strings.ReplaceAll(line, "&&", ""), "&") {
			continue
		}
		ok := true
		for _, seg := range segmentSplit.Split(line, -1) {
			fields := strings.Fields(strings.TrimPrefix(strings.TrimSpace(seg), "! "))
			if len(fields) == 0 {
				ok = false
				break
			}
			subs, known := checkPrograms[fields[0]]
			if !known {
				ok = false
				break
			}
			if subs != nil && (len(fields) < 2 || !contains(subs, fields[1])) {
				ok = false
				break
			}
		}
		if ok {
			out = append(out, line)
		}
	}
	return out
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

func criteriaList(v any) []map[string]any {
	var out []map[string]any
	switch l := v.(type) {
	case []any:
		for _, x := range l {
			if m, ok := x.(map[string]any); ok {
				out = append(out, m)
			}
		}
	case []map[string]any:
		out = l
	}
	return out
}

func passedOf(c map[string]any) bool {
	v, ok := c["passed"]
	if !ok {
		return true
	}
	b, _ := v.(bool)
	return b
}

func isProjectCheck(c map[string]any) bool {
	if b, _ := c[ProjectCheckField].(bool); b {
		return true
	}
	crit, _ := c["criterion"].(string)
	return strings.HasPrefix(crit, "The project builds and its tests pass")
}

func tail(s string) string {
	s = strings.TrimSpace(s)
	if len(s) > outputTail {
		return "…" + s[len(s)-outputTail:]
	}
	return s
}

// PlannerRule is the line every planner prompt carries about command checks,
// so the planner writes a command its tool accepts in the first place; the
// pass above is the backstop when it does not.
const PlannerRule = "- **Command checks must be valid invocations**: a criterion's command is run\n" +
	"  literally, so a command its own tool rejects (a usage or syntax error) fails\n" +
	"  for every implementation and proves nothing. Spell a Go symbol check\n" +
	"  `go doc <pkg> <Type>.<Method>`: `go doc ./ Ledger.Checksum` or\n" +
	"  `go doc . Ledger.Checksum`, never `go doc ./ ledger.Ledger.Checksum` (after a\n" +
	"  package path the symbol takes at most one dot). Prefer a test that exercises\n" +
	"  the behavior (`go test ./... -run TestName`) over a documentation lookup.\n" +
	"- **Never check the working tree**: the work is committed before it is\n" +
	"  verified, so `git status` and a bare `git diff` (or `git diff --cached`)\n" +
	"  print nothing for every implementation. To check which files changed,\n" +
	"  compare with the base branch, naming it: `git diff --name-only main...HEAD`.\n"
