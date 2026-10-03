#!/usr/bin/env bash
# HiveMind worker wrapper. Installed by the master over SSH; hive is not required on the worker.
set -euo pipefail

RUN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$RUN_DIR/job.env"
export GIT_TERMINAL_PROMPT=0

PROMPT_FILE="${PROMPT_FILE:-$RUN_DIR/prompt.txt}"
RESULT_FILE="${RESULT_FILE:-$RUN_DIR/result.json}"
WRAPPER_LOG="${WRAPPER_LOG:-$RUN_DIR/wrapper.log}"
AGENT_LOG="${AGENT_LOG:-$RUN_DIR/agent.log}"
TEST_LOG="${TEST_LOG:-$RUN_DIR/test.log}"
BENCH_LOG="${BENCH_LOG:-$RUN_DIR/bench.log}"
BENCH_JSON="${BENCH_JSON:-$RUN_DIR/bench.json}"

AGENT_EXIT=""
TEST_EXIT=""
BENCH_PATH=""
FINISHED="0"

json_escape() {
    printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()), end="")' 2>/dev/null || {
        local escaped
        escaped=$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')
        printf '"%s"' "$escaped"
    }
}

write_result() {
    local status="$1"
    local error="${2:-}"
    local agent_json="null"
    local test_json="null"
    local bench_json="null"
    local error_json="null"

    if [ -n "$AGENT_EXIT" ]; then
        agent_json="$AGENT_EXIT"
    fi
    if [ -n "$TEST_EXIT" ]; then
        test_json="$TEST_EXIT"
    fi
    if [ -n "$BENCH_PATH" ]; then
        bench_json=$(json_escape "$BENCH_PATH")
    fi
    if [ -n "$error" ]; then
        error_json=$(json_escape "$error")
    fi

    cat > "$RESULT_FILE" <<EOF
{
  "status": $(json_escape "$status"),
  "branch": $(json_escape "$BRANCH"),
  "agent_exit_code": $agent_json,
  "test_exit_code": $test_json,
  "benchmark_path": $bench_json,
  "error": $error_json
}
EOF
}

fail() {
    write_result "failed" "$1"
    FINISHED="1"
    exit 1
}

on_exit() {
    local code=$?
    if [ "$FINISHED" != "1" ]; then
        write_result "failed" "wrapper aborted (exit $code)"
    fi
}
trap on_exit EXIT

if [ -f "$HOME/.profile" ]; then
    set +u
    . "$HOME/.profile" >/dev/null 2>&1 || true
    set -u
fi
if [ -f "$HOME/.bash_profile" ]; then
    set +u
    . "$HOME/.bash_profile" >/dev/null 2>&1 || true
    set -u
fi
if [ -f "$HOME/.bashrc" ]; then
    set +u
    eval "$(grep -E '^[[:space:]]*export[[:space:]]+' "$HOME/.bashrc" 2>/dev/null || true)"
    set -u
fi
if [ -f "$HOME/.env" ]; then
    set +u
    eval "$(grep -E '^[[:space:]]*export[[:space:]]+' "$HOME/.env" 2>/dev/null || true)"
    set -u
fi

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

WORKSPACE="$(eval echo "$WORKSPACE_DIR")"
mkdir -p "$(dirname "$WORKSPACE")"

normalize_git_url() {
    printf '%s' "$1" \
        | sed -e 's|^ssh://git@||' -e 's|^git@github.com:|github.com/|' -e 's|^git@||' \
              -e 's|^https://||' -e 's|^http://||' -e 's|\.git$||' -e 's|/$||' \
        | tr '[:upper:]' '[:lower:]'
}

if [ ! -e "$WORKSPACE" ]; then
    git clone "$REPO_URL" "$WORKSPACE" || fail "git clone failed"
elif [ ! -d "$WORKSPACE" ]; then
    fail "workspace path exists and is not a directory: $WORKSPACE"
elif [ ! -d "$WORKSPACE/.git" ]; then
    leftovers="$(ls -A "$WORKSPACE" 2>/dev/null | grep -Ev '^(\.hive|\.aider.*)$' || true)"
    if [ -n "$leftovers" ]; then
        fail "workspace is not empty and is not a git repo: $WORKSPACE"
    fi
    mkdir -p "$WORKSPACE/.hive"
    clone_tmp="$WORKSPACE/.hive/.origin-clone"
    rm -rf "$clone_tmp"
    git clone "$REPO_URL" "$clone_tmp" || fail "git clone failed"
    mv "$clone_tmp/.git" "$WORKSPACE/.git"
    rm -rf "$clone_tmp"
    git -C "$WORKSPACE" checkout -f HEAD || fail "git checkout after clone failed"
else
    origin_url="$(git -C "$WORKSPACE" remote get-url origin 2>/dev/null || true)"
    if [ -z "$origin_url" ]; then
        fail "existing workspace has no origin remote"
    fi
    expected="$(normalize_git_url "$REPO_URL")"
    actual="$(normalize_git_url "$origin_url")"
    if [ "$expected" != "$actual" ]; then
        fail "workspace origin '$origin_url' does not match repo_url '$REPO_URL'"
    fi
    git -C "$WORKSPACE" remote set-url origin "$REPO_URL" || true
fi

cd "$WORKSPACE"

git fetch origin || fail "git fetch origin failed"

if git show-ref --verify --quiet refs/remotes/origin/main; then
    BASE_BRANCH="main"
elif git show-ref --verify --quiet refs/remotes/origin/master; then
    BASE_BRANCH="master"
else
    fail "origin has neither main nor master"
fi

dirty="$(git status --porcelain | grep -v '^??' | awk '{print substr($0,4)}' | grep -Ev '^(\.hive|\.aider)' | grep -v '^$' || true)"
if [ -n "$dirty" ]; then
    fail "workspace has a dirty git tree; commit or clean it before dispatch"
fi

git checkout -B "$BASE_BRANCH" "origin/$BASE_BRANCH" || fail "could not checkout $BASE_BRANCH"
git checkout -B "$BRANCH" || fail "could not create branch $BRANCH"

if ! grep -qxF '.hive/' .gitignore 2>/dev/null && ! grep -qxF '.hive' .gitignore 2>/dev/null; then
    printf '\n.hive/\n' >> .gitignore
fi
if ! grep -qF '.aider' .gitignore 2>/dev/null; then
    printf '.aider*\n' >> .gitignore
fi
# agy workspace files
if ! grep -qxF '.agy/' .gitignore 2>/dev/null; then
    printf '.agy/\n' >> .gitignore
fi
if ! grep -qxF '__pycache__/' .gitignore 2>/dev/null; then
    printf '__pycache__/\n*.pyc\n*.pyo\n' >> .gitignore
fi
# Remove any accidentally tracked cache / compiled files before agent runs
git rm -r --cached __pycache__ "*.pyc" "*.pyo" 2>/dev/null || true

export PROMPT_FILE
set +e
eval "$AGENT_COMMAND" >"$AGENT_LOG" 2>&1
AGENT_EXIT=$?
set -e

if [ "$AGENT_EXIT" -ne 0 ]; then
    fail "agent exited with code $AGENT_EXIT"
fi

if [ -n "${TEST_COMMAND:-}" ]; then
    set +e
    eval "$TEST_COMMAND" >"$TEST_LOG" 2>&1
    TEST_EXIT=$?
    set -e
fi

if [ -n "${BENCH_COMMAND:-}" ]; then
    set +e
    eval "$BENCH_COMMAND" >"$BENCH_LOG" 2>&1
    bench_exit=$?
    set -e
    if [ "$bench_exit" -eq 0 ]; then
        if python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$BENCH_LOG" 2>/dev/null; then
            cp "$BENCH_LOG" "$BENCH_JSON"
            BENCH_PATH="$BENCH_JSON"
        else
            BENCH_PATH="$BENCH_LOG"
        fi
    fi
fi

git add -A
if git diff --cached --quiet; then
    if [ -n "${TEST_COMMAND:-}" ] && [ "${TEST_EXIT:-0}" -ne 0 ]; then
        fail "agent made no git changes and tests failed (exit $TEST_EXIT)"
    fi
else
    git -c user.email=hive@hivemind.local -c user.name="HiveMind Worker" \
        commit -m "hive: ${STRATEGY} on ${WORKER_ID} (${RUN_ID})" \
        || fail "git commit failed"
fi

git push -u --force origin "$BRANCH" || fail "git push origin $BRANCH failed"

if [ -n "${TEST_COMMAND:-}" ] && [ "${TEST_EXIT:-0}" -ne 0 ]; then
    write_result "failed" "tests failed (exit $TEST_EXIT)"
    FINISHED="1"
    exit 1
fi

write_result "succeeded" ""
FINISHED="1"
exit 0
