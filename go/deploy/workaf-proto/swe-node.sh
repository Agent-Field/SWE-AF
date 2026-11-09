#!/bin/zsh
# SWE-AF's Go node (swe-planner) on $NODE_HOST (dumb), run from spark beside the
# agentfield prototype kit (deploy/workaf-proto in agentfield proto/harness-watch),
# whose env.sh, control plane and node secrets it reuses.
#
# Usage: swe-node.sh build|start|stop|status|logs|clean
#
# THE NODE IS LINK ONLY: it dials the control plane, registers
# http://swe-planner.nodelink and binds only an ephemeral loopback port. No
# tunnel, no public port. Coding runs on the codeaf harness in exec mode, so
# every coding step streams --events and takes --steer:
#   SWE_PRO_ENGINE=0  SWE_DEFAULT_RUNTIME=aforge  AGENTFIELD_AFORGE_COMMAND=exec
# (SWE-AF spells the codeaf harness "aforge"; that is their config word.)
#
# Secrets never touch argv: a wrapper on the node box reads the kit's mode-600
# files into the environment and execs the node.
set -euo pipefail
: ${KIT_DIR:=$HOME/af-proto-link/deploy/workaf-proto}
HERE=$KIT_DIR; source $KIT_DIR/env.sh
SWE_GO=${0:A:h:h:h}                 # SWE-AF/go
ID=swe-planner
STAGE=$LOCAL/stage/darwin-arm64
R="\$HOME/$KIT"; RN="$R/swe-planner"
: ${GH_VERSION:=2.63.2}
remote() { ssh -o BatchMode=yes $NODE_HOST "$@"; }

build() {
  mkdir -p $STAGE
  say "swe-planner ← $SWE_GO (darwin/arm64, SDK via go.mod replace)"
  (cd $SWE_GO && GOOS=darwin GOARCH=arm64 CGO_ENABLED=0 go build -trimpath -o $STAGE/swe-planner ./cmd/swe-planner)
  if [[ ! -x $STAGE/gh ]]; then
    say "gh $GH_VERSION for macOS arm64 (the PR step runs gh pr create)"
    local t=$(mktemp -d); trap "rm -rf $t" EXIT
    curl -fsSL -o $t/gh.zip https://github.com/cli/cli/releases/download/v$GH_VERSION/gh_${GH_VERSION}_macOS_arm64.zip
    (cd $t && unzip -q gh.zip) && cp $t/gh_${GH_VERSION}_macOS_arm64/bin/gh $STAGE/gh && chmod +x $STAGE/gh
  fi
}

# secrets pipes the three keys into mode-600 files on the node box, exactly as
# the kit's node.sh does: never through argv or this terminal.
secrets() {
  remote "mkdir -p $R/secrets && chmod 700 $R/secrets"
  remote "test -s $R/secrets/openrouter" || {
    ssh -o BatchMode=yes $ENGINE_HOST 'python3 -c "import json,os;print(json.load(open(os.path.expanduser(\"~/.codeaf/config.json\")))[\"api_key\"])"' \
      | remote "umask 077; cat > $R/secrets/openrouter"
  }
  remote "test -s $R/secrets/github" || gh auth token | remote "umask 077; cat > $R/secrets/github"
  remote "umask 077; cat > $R/secrets/cpkey" < $LOCAL/cp/secrets/cpkey
  remote "chmod 600 $R/secrets/*"
}

ship() {
  secrets
  [[ -x $STAGE/swe-planner && -x $STAGE/gh && -x $STAGE/codeaf ]] || die "run swe-node.sh build (and the kit's build.sh codeaf)"
  remote "mkdir -p $R/bin $RN/home $RN/work $RN/workspaces $R/logs; test -s $R/secrets/openrouter && test -s $R/secrets/github && test -s $R/secrets/cpkey" \
    || die "kit secrets missing on $NODE_HOST; run the kit's node.sh start once"
  for b in swe-planner gh codeaf; do
    if [[ $(remote "shasum -a 256 $R/bin/$b 2>/dev/null | cut -c1-64") != $(sha256sum $STAGE/$b | cut -c1-64) ]]; then
      say "shipping $b"; scp -q $STAGE/$b $NODE_HOST:$KIT/bin/.$b.new && remote "mv -f $R/bin/.$b.new $R/bin/$b"
    fi
  done
  # The harness's codeaf home: every model row is $MODEL.
  remote "mkdir -p $RN/home/codeaf && chmod 700 $RN/home/codeaf && umask 077 && cat > $RN/home/codeaf/config.json" <<JSON
{"model.talk": "$MODEL", "task.model": "$MODEL", "vision_model": "$MODEL",
 "models.tiers.low": "$MODEL", "models.tiers.reflex": "$MODEL"}
JSON
  remote "umask 077; cat > $RN/run.sh; chmod 700 $RN/run.sh" <<'RUN'
#!/bin/sh
# Reads the secrets into the environment (never argv) and execs the node.
export OPENROUTER_API_KEY="$(cat "$KITR/secrets/openrouter")"
export AGENTFIELD_API_KEY="$(cat "$KITR/secrets/cpkey")"
export GH_TOKEN="$(cat "$KITR/secrets/github")"
exec "$KITR/bin/swe-planner"
RUN
}

running() { remote "kill -0 \$(cat $RN/node.pid 2>/dev/null) 2>/dev/null"; }

start() {
  ship
  if running; then say "$ID already up pid $(remote "cat $RN/node.pid")"; return; fi
  remote "cd $RN/work; env -i HOME=\$HOME USER=\$USER KITR=$R \
    PATH=$R/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin \
    NODE_ID=$ID AGENTFIELD_NODE_LINK_ONLY=1 AGENTFIELD_NODE_HOST=$NODE_HOST AGENTFIELD_SERVER=$CP_URL \
    SWE_PRO_ENGINE=0 SWE_DEFAULT_RUNTIME=aforge AGENTFIELD_AFORGE_COMMAND=exec SWE_HITL_NATIVE=1 SWE_FENCE=1 \
    SWE_DEFAULT_MODEL=openrouter/$MODEL CODEAF_BIN=$R/bin/codeaf AGENTFIELD_HOME=$RN/home \
    SWE_AF_GIT_NAME=santoshkumarradha SWE_AF_GIT_EMAIL=santoshkumarradha@users.noreply.github.com \
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=credential.https://github.com.helper 'GIT_CONFIG_VALUE_0=!$R/bin/gh auth git-credential' \
    SWE_WORKSPACE_ROOT=$RN/workspaces TMPDIR=$RN/work GOCACHE=$RN/gocache GOPATH=$RN/gopath \
    nohup $RN/run.sh > $R/logs/swe-planner.log 2>&1 < /dev/null & echo \$! > $RN/node.pid"
  local key=$(<$LOCAL/cp/secrets/cpkey)
  for i in {1..60}; do
    curl -fsS -H "X-API-Key: $key" http://127.0.0.1:$CP_PORT/api/v1/nodes/$ID 2>/dev/null | grep -q '"id"' && break; sleep 0.5
  done
  curl -fsS -H "X-API-Key: $key" http://127.0.0.1:$CP_PORT/api/v1/nodes/$ID | jq -c '{id, base_url, health_status, reasoners: ([.reasoners[]?.id] | length)}'
  say "listeners held by $ID on $NODE_HOST:"
  remote "lsof -nP -a -iTCP -sTCP:LISTEN -p \$(cat $RN/node.pid) 2>/dev/null | sed 's/^/    /' || true"
}

stop() {
  remote "p=\$(cat $RN/node.pid 2>/dev/null) && kill \$p 2>/dev/null; rm -f $RN/node.pid; \
    for i in 1 2 3 4 5 6 7 8 9 10; do pgrep -f '$KIT/bin/swe-planner' >/dev/null || break; sleep 1; done; \
    pkill -9 -f '$KIT/bin/swe-planner' 2>/dev/null; pkill -f '$KIT/swe-planner/' 2>/dev/null; true"
  say "$ID stopped on $NODE_HOST"
}

case ${1:-} in
  build) build;; start) start;; stop) stop;; restart) stop; start;;
  status) running && say "$ID up" || say "$ID down";;
  logs) remote "tail -n ${2:-80} $R/logs/swe-planner.log";;
  clean) stop; remote "chmod -R u+w $RN 2>/dev/null; rm -rf $RN $R/bin/swe-planner $R/bin/gh $R/bin/codeaf $R/secrets $R/logs/swe-planner.log; rmdir $R/bin $R/logs $R 2>/dev/null; true"; rm -f $STAGE/swe-planner $STAGE/gh; say "removed swe-planner state, its binaries and its key files on $NODE_HOST";;
  *) die "usage: swe-node.sh build|start|stop|restart|status|logs|clean";;
esac
