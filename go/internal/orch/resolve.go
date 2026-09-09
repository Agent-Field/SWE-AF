package orch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/roles/ci"
	"github.com/Agent-Field/SWE-AF/go/internal/workspace"
)

// resolveInput mirrors the Python resolve() signature (param names + defaults).
// ci_failures / review_comments / config are read from the raw input map so the
// list/dict shapes round-trip verbatim; scalars bind through this struct.
type resolveInput struct {
	PRURL             string         `json:"pr_url"`
	PRNumber          int            `json:"pr_number"`
	RepoURL           string         `json:"repo_url"`
	HeadBranch        string         `json:"head_branch"`
	BaseBranch        string         `json:"base_branch"`
	Goal              string         `json:"goal"`
	AdditionalContext string         `json:"additional_context"`
	Config            map[string]any `json:"config"`
}

// ResolveHandler ports resolve() (app.py:1685): update an existing PR — merge
// base, run the PR-resolver agent, fix CI, address review comments, push. The
// node-wiring wave registers this under the exact name "resolve".
//
// Single-repo only (v1). The caller passes the PR's own head_branch; SWE-AF
// clones the repo, checks out the branch, merges base_branch into it (always
// merge, never rebase), hands the tree to run_pr_resolver, pushes, runs the CI
// fix loop, and posts brief replies + resolves threads for addressed comments.
func ResolveHandler(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in := resolveInput{BaseBranch: "main"}
	if raw, err := json.Marshal(input); err == nil {
		_ = json.Unmarshal(raw, &in)
	}

	// ci_failures = ci_failures or []; review_comments = review_comments or [].
	ciFailures := maps0(input["ci_failures"])
	reviewComments := maps0(input["review_comments"])

	// cfg = BuildConfig(**config) if config else BuildConfig()
	cfg, err := config.LoadBuildConfig(in.Config)
	if err != nil {
		return nil, err
	}
	cfg.EnableGithubPR = false // the PR already exists — never create

	if in.PRNumber == 0 || in.HeadBranch == "" || in.RepoURL == "" || in.PRURL == "" {
		return nil, errors.New(
			"resolve requires non-empty pr_url, pr_number, repo_url, head_branch")
	}

	buildID := newBuildID()
	repoName := deriveRepoName(in.RepoURL)
	repoPath := filepath.Join(workspace.Root(), fmt.Sprintf("%s-resolve-%s", repoName, buildID))

	deps.Note(ctx, fmt.Sprintf("Resolve starting (build_id=%s) — PR #%d", buildID, in.PRNumber),
		"resolve", "start")

	// ---- 1. Clone ----------------------------------------------------------
	// Create only the parent; git clone creates the leaf. Pre-creating the leaf
	// makes git refuse it as "already exists and is not an empty directory" on
	// Windows, where it cannot re-open the node-created dir (issue #107).
	_ = os.MkdirAll(filepath.Dir(repoPath), 0o755)
	clone := runGit(ctx, "", "clone", in.RepoURL, repoPath)
	if clone.ExitCode != 0 {
		errMsg := strings.TrimSpace(clone.Stderr)
		deps.Note(ctx, fmt.Sprintf("Resolve clone failed: %s", errMsg), "resolve", "clone", "error")
		return nil, fmt.Errorf("git clone failed: %s", errMsg)
	}

	// ---- 2. Fetch PR head + checkout ---------------------------------------
	fetchPR := runGit(ctx, repoPath, "fetch", "origin",
		fmt.Sprintf("pull/%d/head:%s", in.PRNumber, in.HeadBranch))
	if fetchPR.ExitCode != 0 {
		// Fallback: branch may already be a regular ref on origin (same-repo PR).
		fetchBranch := runGit(ctx, repoPath, "fetch", "origin",
			fmt.Sprintf("%s:%s", in.HeadBranch, in.HeadBranch))
		if fetchBranch.ExitCode != 0 {
			errMsg := strings.TrimSpace(fetchPR.Stderr + "\n" + fetchBranch.Stderr)
			deps.Note(ctx, fmt.Sprintf("Resolve fetch PR head failed: %s", errMsg),
				"resolve", "fetch", "error")
			return nil, fmt.Errorf("git fetch PR head failed: %s", errMsg)
		}
	}

	checkout := runGit(ctx, repoPath, "checkout", in.HeadBranch)
	if checkout.ExitCode != 0 {
		errMsg := strings.TrimSpace(checkout.Stderr)
		deps.Note(ctx, fmt.Sprintf("Resolve checkout failed: %s", errMsg),
			"resolve", "checkout", "error")
		return nil, fmt.Errorf("git checkout %s failed: %s", in.HeadBranch, errMsg)
	}

	// Configure committer identity for any merge / commit we make in this
	// workspace. Match the run_github_pr / repo_finalize bot-identity convention.
	gitEmail := os.Getenv("SWE_AF_GIT_EMAIL")
	if gitEmail == "" {
		gitEmail = "swe-af@users.noreply.github.com"
	}
	gitName := os.Getenv("SWE_AF_GIT_NAME")
	if gitName == "" {
		gitName = "SWE-AF"
	}
	runGit(ctx, repoPath, "config", "user.email", gitEmail)
	runGit(ctx, repoPath, "config", "user.name", gitName)

	// ---- 3. Merge base into head (always merge, never rebase) --------------
	mergeState, conflictedFiles := attemptBaseMerge(ctx, repoPath, in.BaseBranch)
	mergeNote := fmt.Sprintf("Resolve merge state: %s", mergeState)
	if len(conflictedFiles) > 0 {
		mergeNote += fmt.Sprintf(" (%d conflict(s))", len(conflictedFiles))
	}
	deps.Note(ctx, mergeNote, "resolve", "merge", mergeState)

	// ---- 4. Run the PR resolver agent --------------------------------------
	resolvedModels, err := cfg.ResolvedModels()
	if err != nil {
		return nil, err
	}
	// Prefer the ci_fixer slot (sized for code-fix tasks); fall back to coder.
	resolverModel := resolvedModels["ci_fixer_model"]
	if resolverModel == "" {
		resolverModel = resolvedModels["coder_model"]
	}
	if resolverModel == "" {
		resolverModel = "sonnet"
	}

	// Remember where the remote head branch pointed before the agent ran, so a
	// post-run comparison can tell whether anything was actually pushed. Read
	// from the remote (not a local ref) — the agent pushes straight from its own
	// process, which leaves this workspace's remote-tracking refs stale.
	remoteBefore := remoteBranchSHA(ctx, repoPath, in.HeadBranch)

	resolveResult, err := deps.Call(ctx, "run_pr_resolver", map[string]any{
		"repo_path":          repoPath,
		"pr_number":          in.PRNumber,
		"pr_url":             in.PRURL,
		"head_branch":        in.HeadBranch,
		"base_branch":        in.BaseBranch,
		"merge_state":        mergeState,
		"conflicted_files":   conflictedFiles,
		"failed_checks":      ciFailures,
		"review_comments":    reviewComments,
		"goal":               in.Goal,
		"additional_context": in.AdditionalContext,
		"model":              resolverModel,
		"permission_mode":    resolverPermissionMode(cfg.PermissionMode, cfg.AIProvider()),
		"ai_provider":        cfg.AIProvider(),
	}, "run_pr_resolver")
	if err != nil {
		return nil, err
	}

	// ---- 5. Ensure push happened (agent may have skipped on failure) -------
	pushed := asBool(resolveResult["pushed"])
	if !pushed && asBool(resolveResult["commit_shas"]) {
		// Agent committed but didn't push — push for it.
		push := runGit(ctx, repoPath, "push", "origin", in.HeadBranch)
		if push.ExitCode == 0 {
			pushed = true
			resolveResult["pushed"] = true
			deps.Note(ctx, fmt.Sprintf("Resolve: pushed agent's commits to %s", in.HeadBranch),
				"resolve", "push")
		} else {
			deps.Note(ctx, fmt.Sprintf("Resolve push failed: %s", strings.TrimSpace(push.Stderr)),
				"resolve", "push", "error")
		}
	}

	// ---- 5b. Reconcile an unusable agent report against the remote ---------
	// When the harness returned no parseable result, run_pr_resolver hands back
	// a deterministic all-false fallback. That report says nothing about what
	// happened on disk: the agent may well have committed and pushed before the
	// final structured answer failed to parse. Ask the remote instead of the
	// report, and record precisely what the remote can and cannot prove.
	if resolverReportInvalid(resolveResult) {
		// report_invalid marks the agent's own report as untrustworthy. It is
		// set whenever the sentinel is seen, independently of what the remote
		// shows, so consumers never mistake a reconstructed result for a
		// first-hand one.
		resolveResult["report_invalid"] = true

		remoteAfter := remoteBranchSHA(ctx, repoPath, in.HeadBranch)
		localHead := localHeadSHA(ctx, repoPath)
		verdict := classifyRemoteAdvance(remoteBefore, remoteAfter, localHead)

		switch {
		case verdict.Attributed:
			// The remote tip is exactly this workspace's HEAD, so the advance
			// is our agent's work. pushed is now provable — fixed is NOT: a
			// landed commit is not evidence that CI passes or that the review
			// comments were addressed. Leaving fixed false keeps the overall
			// success verdict below (fixed && pushed) false, which is the
			// truthful answer for a run whose agent never reported back.
			resolveResult["pushed"] = true
			pushed = true

			// rev-list / diff need the post-push objects locally; the workspace
			// only has what it cloned plus whatever the agent committed.
			if fetchRemoteBranch(ctx, repoPath, in.HeadBranch) {
				resolveResult["commit_shas"] = remoteCommitSHAs(ctx, repoPath, remoteBefore, remoteAfter)
				resolveResult["files_changed"] = remoteFilesChanged(ctx, repoPath, remoteBefore, remoteAfter)
			} else {
				// verification_partial: the push is confirmed but the commit
				// and file lists could not be reconstructed, so their emptiness
				// means "unknown", not "nothing changed".
				resolveResult["commit_shas"] = []string{}
				resolveResult["files_changed"] = []string{}
				resolveResult["verification_partial"] = true
			}
			resolveResult["summary"] = "agent report invalid; verified this workspace's work was pushed to " + in.HeadBranch
			resolveResult["error_message"] = "agent report invalid; push verified against the remote, fix NOT verified"
			deps.Note(ctx, "Resolve: agent report invalid, but this workspace's HEAD is now the remote tip — push verified, fix unverified",
				"resolve", "report", "warning")

		case verdict.Advanced:
			// The branch moved but the new tip is not our HEAD: someone (or
			// something) else pushed while we ran. Record the observation and
			// attribute nothing — claiming this push would be a lie, and the
			// commits are not ours to describe.
			resolveResult["remote_advanced"] = true
			deps.Note(ctx, fmt.Sprintf(
				"Resolve: agent report invalid and %s moved on the remote, but the new tip is not this workspace's HEAD — not attributing the push",
				in.HeadBranch), "resolve", "report", "warning")

		default:
			deps.Note(ctx, "Resolve: agent report invalid and the remote branch did not move — no work landed",
				"resolve", "report", "warning")
		}
	}

	// Capture the new HEAD SHA after push so the CI watcher can anchor verdicts
	// to this specific commit (avoids the previous HEAD's stale check states).
	headSHA := ""
	if sha := runGit(ctx, repoPath, "rev-parse", "HEAD"); sha.ExitCode == 0 {
		headSHA = strings.TrimSpace(sha.Stdout)
	}

	// ---- 6. Post-push CI watch + fix loop ----------------------------------
	var ciGate map[string]any
	if pushed && cfg.CheckCI && deps.CIGate != nil {
		// Startup grace: GitHub Actions takes a few seconds to register a new
		// workflow run after the push lands. Polling immediately races that
		// registration and can see the previous HEAD's stale state.
		if cfg.CIStartupGraceSeconds > 0 {
			deps.Note(ctx, fmt.Sprintf(
				"CI gate: waiting %ds for GitHub Actions to register the new run",
				cfg.CIStartupGraceSeconds), "resolve", "ci_gate", "grace")
			sleepFn(ctx, time.Duration(cfg.CIStartupGraceSeconds)*time.Second)
		}
		gate, gerr := deps.CIGate(ctx, CIGateRequest{
			Deps:              deps,
			Cfg:               cfg,
			Resolved:          resolvedModels,
			RepoPath:          repoPath,
			PRNumber:          in.PRNumber,
			PRURL:             in.PRURL,
			IntegrationBranch: in.HeadBranch,
			BaseBranch:        in.BaseBranch,
			Goal:              fmt.Sprintf("Resolve PR #%d", in.PRNumber),
			CompletedIssues:   []map[string]any{},
			HeadSHA:           headSHA,
		})
		if gerr != nil {
			deps.Note(ctx, fmt.Sprintf("Resolve CI gate errored (non-fatal): %v", gerr),
				"resolve", "ci_gate", "error")
		} else {
			ciGate = gate
		}
	}

	// ---- 7. Reply + resolveReviewThread for addressed comments -------------
	addressed := []map[string]any{}
	for _, c := range asMapList(resolveResult["addressed_comments"]) {
		if asBool(c["addressed"]) {
			addressed = append(addressed, c)
		}
	}
	threadReplies := []map[string]any{}
	if len(addressed) > 0 {
		threadReplies = postThreadRepliesAndResolve(ctx, repoPath, in.PRNumber, addressed)
	}

	// ---- 8. Workspace cleanup (non-blocking) -------------------------------
	_ = os.RemoveAll(repoPath)

	// fixed && pushed — both must hold. Step 5b can raise pushed on the strength
	// of the remote alone, but it never raises fixed, so a run whose agent never
	// reported back still lands here as success=false.
	success := asBool(resolveResult["fixed"]) && pushed
	summary := fmt.Sprintf(
		"PR #%d: merge=%s, %d file(s) changed, %d/%d comment(s) addressed",
		in.PRNumber, mergeState,
		len(asStrList(resolveResult["files_changed"])),
		len(addressed), len(asMapList(resolveResult["addressed_comments"])),
	)
	if ciGate != nil {
		summary += fmt.Sprintf(", CI=%s", mapStr(ciGate, "final_status", "n/a"))
	}

	resultWord := "completed with issues"
	if success {
		resultWord = "succeeded"
	}
	deps.Note(ctx, fmt.Sprintf("Resolve %s: %s", resultWord, summary), "resolve", "complete")

	// ci_gate is None in Python when the gate was skipped. Store an untyped nil
	// (not a typed-nil map) so callers see a true nil; both marshal to JSON null.
	var ciGateOut any
	if ciGate != nil {
		ciGateOut = ciGate
	}

	return map[string]any{
		"pr_url":         in.PRURL,
		"pr_number":      in.PRNumber,
		"head_branch":    in.HeadBranch,
		"base_branch":    in.BaseBranch,
		"merge_state":    mergeState,
		"resolve_result": resolveResult,
		"ci_gate":        ciGateOut,
		"thread_replies": threadReplies,
		"summary":        summary,
		"success":        success,
	}, nil
}

// resolverReportInvalid reports whether run_pr_resolver returned its
// deterministic "the harness gave me nothing parseable" fallback rather than a
// real report. Matches ci.InvalidResolverReport in either field the fallback
// sets, so a future change to only one of them still trips the check.
func resolverReportInvalid(result map[string]any) bool {
	return mapStr(result, "error_message", "") == ci.InvalidResolverReport ||
		mapStr(result, "summary", "") == ci.InvalidResolverReport
}

// pushVerdict is what the remote branch can prove about a run whose agent
// report is unusable. The two flags are deliberately separate: a branch that
// moved is not the same claim as a branch that moved *because of us*.
type pushVerdict struct {
	// Advanced: the remote head branch tip changed while the resolver ran.
	Advanced bool
	// Attributed: the new remote tip is this workspace's HEAD, so the advance
	// is the work this run produced and may be reported as our push. False
	// alongside Advanced means a third party moved the branch.
	Attributed bool
}

// classifyRemoteAdvance decides what may be claimed from three SHAs: the remote
// tip before the agent ran, the remote tip after, and this workspace's local
// HEAD. An unknown ("") before/after SHA proves nothing, so it yields the zero
// verdict — silence is preferable to a guess.
func classifyRemoteAdvance(remoteBefore, remoteAfter, localHead string) pushVerdict {
	if remoteBefore == "" || remoteAfter == "" || remoteBefore == remoteAfter {
		return pushVerdict{}
	}
	return pushVerdict{
		Advanced:   true,
		Attributed: localHead != "" && localHead == remoteAfter,
	}
}

// remoteBranchSHA returns the SHA origin currently has for branch, or "" when
// the branch is absent or the query failed.
func remoteBranchSHA(ctx context.Context, repoPath, branch string) string {
	r := runGit(ctx, repoPath, "ls-remote", "origin", "refs/heads/"+branch)
	if r.ExitCode != 0 {
		return ""
	}
	fields := strings.Fields(r.Stdout)
	if len(fields) == 0 {
		return ""
	}
	return fields[0]
}

// localHeadSHA returns the workspace's current HEAD SHA, or "" if unreadable.
func localHeadSHA(ctx context.Context, repoPath string) string {
	r := runGit(ctx, repoPath, "rev-parse", "HEAD")
	if r.ExitCode != 0 {
		return ""
	}
	return strings.TrimSpace(r.Stdout)
}

// fetchRemoteBranch pulls the branch's current objects into the workspace so
// rev-list / diff can resolve the post-push SHA locally. Reports success.
func fetchRemoteBranch(ctx context.Context, repoPath, branch string) bool {
	return runGit(ctx, repoPath, "fetch", "origin", branch).ExitCode == 0
}

func remoteCommitSHAs(ctx context.Context, repoPath, before, after string) []string {
	r := runGit(ctx, repoPath, "rev-list", "--reverse", before+".."+after)
	if r.ExitCode != 0 {
		return []string{}
	}
	return nonEmptyLines(r.Stdout)
}

func remoteFilesChanged(ctx context.Context, repoPath, before, after string) []string {
	r := runGit(ctx, repoPath, "diff", "--name-only", before, after)
	if r.ExitCode != 0 {
		return []string{}
	}
	return nonEmptyLines(r.Stdout)
}

func nonEmptyLines(s string) []string {
	out := []string{}
	for _, line := range strings.Split(s, "\n") {
		if line = strings.TrimSpace(line); line != "" {
			out = append(out, line)
		}
	}
	return out
}

// resolverPermissionMode picks the permission mode handed to run_pr_resolver.
//
// An explicitly configured mode always wins. When none is configured the
// default is provider-dependent, because the SDK harnesses disagree on what an
// empty permission mode means (sdk/go/harness):
//
//   - claude: an empty mode omits --permission-mode entirely, so the CLI falls
//     back to its "prompting" default. Under `claude --print` there is nobody to
//     answer the prompt, so every write is denied and the resolver silently
//     produces no commits. "auto" maps to bypassPermissions, which is what the
//     resolver actually needs — it owns a throwaway clone.
//   - codex: an empty mode already yields `--sandbox workspace-write`, i.e. the
//     workspace is writable. "auto" would escalate to
//     --dangerously-bypass-approvals-and-sandbox, dropping the sandbox around
//     the *whole machine* for no benefit. Leave it empty.
//   - opencode: the provider never reads PermissionMode, so the value is inert.
//     Leave it empty rather than implying a guarantee we do not make.
//
// Hence the "auto" default is gated to the claude provider only.
func resolverPermissionMode(configured, provider string) string {
	if configured != "" {
		return configured
	}
	if provider == "claude" {
		return "auto"
	}
	return ""
}

// attemptBaseMerge fetches base_branch and merges it into the current branch.
// Ports _attempt_base_merge (app.py:1960). Returns (merge_state, conflicted)
// where merge_state is "clean" (already up to date), "merged" (merge succeeded),
// "conflict" (merge in progress with unresolved conflicts), or "skipped"
// (couldn't fetch base). Always uses git merge — never rebase — to preserve PR
// history.
func attemptBaseMerge(ctx context.Context, repoPath, baseBranch string) (string, []string) {
	if fetch := runGit(ctx, repoPath, "fetch", "origin", baseBranch); fetch.ExitCode != 0 {
		return "skipped", []string{}
	}

	// Already up to date? — check if base is an ancestor of HEAD.
	ancestor := runGit(ctx, repoPath, "merge-base", "--is-ancestor",
		"origin/"+baseBranch, "HEAD")
	if ancestor.ExitCode == 0 {
		return "clean", []string{}
	}

	merge := runGit(ctx, repoPath, "merge", "--no-edit", "--no-ff", "origin/"+baseBranch)
	if merge.ExitCode == 0 {
		return "merged", []string{}
	}

	// Merge produced conflicts — list them and leave the merge in progress for
	// the resolver agent to finish.
	diff := runGit(ctx, repoPath, "diff", "--name-only", "--diff-filter=U")
	conflicted := []string{}
	for _, line := range strings.Split(diff.Stdout, "\n") {
		if s := strings.TrimSpace(line); s != "" {
			conflicted = append(conflicted, s)
		}
	}
	return "conflict", conflicted
}

// postThreadRepliesAndResolve posts a brief reply and resolves the thread for
// each addressed comment. Ports _post_thread_replies_and_resolve (app.py:2003).
// Uses the gh CLI so the GraphQL resolveReviewThread mutation runs under the
// same identity as the push. Replies use the agent's note verbatim, capped at
// 500 chars. Failures are non-fatal — the push has already landed.
func postThreadRepliesAndResolve(ctx context.Context, repoPath string, prNumber int, addressed []map[string]any) []map[string]any {
	results := []map[string]any{}
	for _, entry := range addressed {
		commentID := asInt(mapGet(entry, "comment_id", 0))
		threadID := strings.TrimSpace(mapStr(entry, "thread_id", ""))
		note := truncateNote(mapStr(entry, "note", ""))

		replied := false
		resolved := false
		replyError := ""
		resolveError := ""

		// Inline review-thread reply (REST). Skipped for non-review comments
		// (comment_id == 0), e.g. PR-conversation comments with no per-line thread.
		if commentID != 0 {
			replyPath := fmt.Sprintf(
				"repos/:owner/:repo/pulls/%d/comments/%d/replies", prNumber, commentID)
			reply := runGH(ctx, repoPath, "api", "-X", "POST", replyPath,
				"-f", "body="+note)
			if reply.ExitCode == 0 {
				replied = true
			} else {
				replyError = trunc(strings.TrimSpace(reply.Stderr), 300)
			}
		}

		// Thread resolution (GraphQL). Skipped when no thread id is known.
		if threadID != "" {
			mutation := "mutation($id:ID!){resolveReviewThread(input:{threadId:$id})" +
				"{thread{isResolved}}}"
			res := runGH(ctx, repoPath, "api", "graphql",
				"-f", "query="+mutation,
				"-f", "id="+threadID)
			if res.ExitCode == 0 {
				resolved = true
			} else {
				resolveError = trunc(strings.TrimSpace(res.Stderr), 300)
			}
		}

		results = append(results, map[string]any{
			"comment_id":    commentID,
			"thread_id":     threadID,
			"replied":       replied,
			"resolved":      resolved,
			"reply_error":   replyError,
			"resolve_error": resolveError,
		})
	}
	return results
}

// truncateNote ports `(entry.get("note") or "Addressed.").strip()[:500] or
// "Addressed."` — the reply body: the agent's note trimmed and capped at 500
// chars, defaulting to "Addressed." when empty.
func truncateNote(note string) string {
	s := strings.TrimSpace(note)
	if s == "" {
		return "Addressed."
	}
	s = trunc(s, 500)
	if s == "" {
		return "Addressed."
	}
	return s
}

// trunc returns the first n runes of s (Python's s[:n] slicing semantics).
func trunc(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}
