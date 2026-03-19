#!/bin/bash
# Parallel test validation for commit0 custom dataset repos
# Runs pytest with coverage on each repo, measures runtime
# Usage: bash run_test_validation.sh [max_parallel]

set -uo pipefail

MAX_PARALLEL=${1:-6}
STAGING_DIR="/home/ec2-user/Jaeger2/commit0/repos_staging"
RESULTS_DIR="/home/ec2-user/Jaeger2/commit0/tools/test_results"
PYTHON="/home/ec2-user/.local/share/uv/python/cpython-3.12-linux-aarch64-gnu/bin/python3.12"
UV="/home/ec2-user/.local/bin/uv"
TIMEOUT=1800

mkdir -p "$RESULTS_DIR"

# Repo definitions: dir_name|src_dir|test_dir
REPOS=(
  "nvbn__thefuck|thefuck|tests"
  "Textualize__rich|rich|tests"
  "psf__black|src/black|tests"
  "Textualize__textual|src/textual|tests"
  "tiangolo__typer|typer|tests"
  "aio-libs__aiohttp|aiohttp|tests"
  "Rapptz__discord.py|discord|tests"
  "encode__httpx|httpx|tests"
  "encode__starlette|starlette|tests"
  "arrow-py__arrow|arrow|tests"
  "burnash__gspread|gspread|tests"
  "pallets__werkzeug|src/werkzeug|tests"
  "pytransitions__transitions|transitions|tests"
  "boto__boto3|boto3|tests"
  "boto__botocore|botocore|tests"
  "pypa__pip|src/pip|tests"
  "ManimCommunity__manim|manim|tests"
  "PrefectHQ__prefect|src/prefect|tests"
)

validate_repo() {
  local entry="$1"
  IFS='|' read -r dir_name src_dir test_dir <<< "$entry"
  local repo_dir="$STAGING_DIR/$dir_name"
  local result_file="$RESULTS_DIR/${dir_name}.json"
  local log_file="$RESULTS_DIR/${dir_name}.log"
  local short_name=$(echo "$dir_name" | sed 's/.*__//')

  echo "[START] $short_name"

  if [ ! -d "$repo_dir" ]; then
    echo "{\"repo\": \"$short_name\", \"status\": \"error\", \"error\": \"repo dir not found\"}" > "$result_file"
    echo "[ERROR] $short_name — repo dir not found"
    return
  fi

  local venv_dir="/tmp/test_venv_${short_name}"
  rm -rf "$venv_dir"
  $PYTHON -m venv "$venv_dir" > "$log_file" 2>&1
  
  local PIP="$venv_dir/bin/pip"
  local PYTEST="$venv_dir/bin/python -m pytest"

  echo "[INSTALL] $short_name" 
  cd "$repo_dir"
  $PIP install --quiet --upgrade pip setuptools wheel >> "$log_file" 2>&1
  
  if ! timeout 600 $PIP install --quiet -e ".[dev,test,tests,testing]" >> "$log_file" 2>&1; then
    if ! timeout 600 $PIP install --quiet -e ".[test]" >> "$log_file" 2>&1; then
      if ! timeout 600 $PIP install --quiet -e "." >> "$log_file" 2>&1; then
        echo "{\"repo\": \"$short_name\", \"status\": \"install_failed\", \"error\": \"pip install failed\"}" > "$result_file"
        echo "[FAIL] $short_name — install failed"
        rm -rf "$venv_dir"
        return
      fi
    fi
  fi
  
  $PIP install --quiet pytest pytest-cov >> "$log_file" 2>&1

  local test_count
  test_count=$(timeout 60 $PYTEST "$test_dir" --collect-only -q 2>/dev/null | tail -1 | grep -oP '\d+(?= test)' || echo "0")

  echo "[TEST] $short_name ($test_count tests found)"
  local start_time=$(date +%s)
  
  local pytest_output
  pytest_output=$(timeout $TIMEOUT $PYTEST "$test_dir" \
    --tb=no --no-header -q \
    --cov="$src_dir" --cov-report=term-missing:skip-covered \
    2>&1) || true
  
  local exit_code=$?
  local end_time=$(date +%s)
  local duration=$((end_time - start_time))

  local summary_line=$(echo "$pytest_output" | grep -E "^\d+ passed" | tail -1)
  local passed=$(echo "$summary_line" | grep -oP '\d+(?= passed)' || echo "0")
  local failed=$(echo "$summary_line" | grep -oP '\d+(?= failed)' || echo "0")
  local errors=$(echo "$summary_line" | grep -oP '\d+(?= error)' || echo "0")
  local skipped=$(echo "$summary_line" | grep -oP '\d+(?= skipped)' || echo "0")
  
  local coverage_line=$(echo "$pytest_output" | grep "^TOTAL" | tail -1)
  local coverage_pct=$(echo "$coverage_line" | grep -oP '\d+%' | head -1 || echo "unknown")

  local timed_out="false"
  if [ $duration -ge $TIMEOUT ]; then
    timed_out="true"
  fi

  # Write result
  cat > "$result_file" << EOJSON
{
  "repo": "$short_name",
  "status": "completed",
  "test_count": "$test_count",
  "passed": "$passed",
  "failed": "$failed",
  "errors": "$errors",
  "skipped": "$skipped",
  "coverage": "$coverage_pct",
  "duration_seconds": $duration,
  "timed_out": $timed_out,
  "exit_code": $exit_code,
  "exceeds_30min": $([ $duration -ge 1800 ] && echo "true" || echo "false")
}
EOJSON

  echo "$pytest_output" >> "$log_file"
  
  echo "[DONE] $short_name — ${passed} passed, ${failed} failed, ${duration}s, coverage=${coverage_pct}"

  rm -rf "$venv_dir"
}

export -f validate_repo
export STAGING_DIR RESULTS_DIR TIMEOUT PYTHON UV

echo "Starting parallel test validation (max $MAX_PARALLEL workers)..."
echo "Results: $RESULTS_DIR/"
echo ""

# Run in parallel using xargs
printf '%s\n' "${REPOS[@]}" | xargs -P "$MAX_PARALLEL" -I {} bash -c 'validate_repo "$@"' _ {}

echo ""
echo "=== SUMMARY ==="
echo ""
for f in "$RESULTS_DIR"/*.json; do
  if [ -f "$f" ]; then
    python3 -c "
import json
with open('$f') as fh:
    d = json.load(fh)
    name = d['repo']
    status = d['status']
    if status == 'completed':
        print(f\"{name:20s} | {d['passed']:>5s} passed | {d['failed']:>5s} failed | {d['coverage']:>5s} cov | {d['duration_seconds']:>5d}s | timeout={d['timed_out']}\")
    else:
        print(f\"{name:20s} | {status} — {d.get('error','')}\")
"
  fi
done
