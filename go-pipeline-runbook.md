# Go Pipeline Runbook

Production guide for preparing Go repositories, building Docker environments, and running the 3-stage AI coding pipeline. This is the Go counterpart of the Python pipeline—covering the complete workflow from repo discovery through evaluation.

All commands assume you're in the project root and using `.venv/bin/python`.

---

## Quick Start

**Single repo, step-by-step:**

```bash
# Prepare the Go repo (fork, clone, stub, push)
.venv/bin/python tools/prepare_repo_go.py \
    --repo sourcegraph/conc \
    --clone-dir ./repos_staging \
    --output conc_entries.json \
    --org Zahgon

# Create dataset (verify entries first!)
.venv/bin/python tools/create_dataset_go.py conc_entries.json --output conc_dataset.json

# Setup + Build + Test IDs
.venv/bin/python commit0/cli_go.py setup all --dataset-name ./conc_dataset.json --dataset-split train
.venv/bin/python commit0/cli_go.py build
.venv/bin/python tools/generate_test_ids_go.py conc_dataset.json --docker --install

# Base Code Compilation verification
docker run --rm \
    "commit0.repo.nosurf.<hash>:v0" \
    bash -c 'cd /testbed && go test -list . -count=1 ./...'

# Run 3-stage pipeline
bash run_pipeline_go.sh --model opus --dataset ./conc_dataset.json --max-iteration 3
```

---

## Pipeline Architecture

```
                        COMMIT0 GO PIPELINE
                        ====================

    DISCOVERY                               PREPARATION
    =========                               ===========

    +---------------------+
    | tools/discover_go.py|  Search GitHub for Go repos (language:go),
    | (optional)          |  filter by stars, _test.go count, go.mod
    +----------+----------+
               |
               | go_candidates.json
               v
    +----------+----------+
    | tools/validate_go.py|  Clone, detect go.mod, module path,
    | (optional)          |  go_version, test file count
    +----------+----------+
               |
               | validated.json
               v
    +----------+-----------+
    | tools/prepare_repo_go|
    |   Fork to org        |
    |   Clone locally      |
    |   Go AST-stub        |
    |   (gostubber binary) |
    |   Push branches      |
    |   Generate entries   |
    +----------+-----------+
               |
               | entries.json
               v
    +----------+-----------+
    | tools/create_dataset |
    |   _go.py             |
    |   Validate entries   |
    |   Output dataset.json|
    +----------+-----------+
               |
               | dataset.json
               v
    +----------+-----------+
    | cli_go.py setup      |
    |   Clone fork to repos|
    |   Checkout branch    |
    |   Write .commit0.go  |
    |   .yaml              |
    +----------+-----------+
               |
               v
    +----------+-----------+
    | cli_go.py build      |
    |   Build Go base image|
    |   (commit0.base.go)  |
    |   Build repo images  |
    +----------+-----------+
               |
               v
    +----------+-----------+
    | generate_test_ids_go |
    |   go test -list .    |
     | Save *.bz2        |
    |   Install to commit0 |
    +----------+-----------+
               |
               v

    PIPELINE EXECUTION
    ==================

    +-------------------------------+
    | run_pipeline_go.sh            |  3-stage orchestrator
    |                               |
    |  +-------------------------+  |
    |  | STAGE 1: Draft          |  |  Agent drafts implementations
    |  | run_tests=false         |  |  Test names visible, no results
    |  | use_unit_tests_info=true|  |  No topological sort (Go)
    |  +------------+------------+  |
    |               |               |
    |               v               |
    |       [ cli_go.py evaluate ]  |  Runs go test -json in Docker
    |               |               |
    |               v               |
    |  +-------------------------+  |
    |  | STAGE 2: Lint Refine    |  |  Agent fixes goimports/staticcheck
    |  | use_lint_info=true      |  |  /go vet issues
    |  | run_tests=false         |  |
    |  +------------+------------+  |
    |               |               |
    |               v               |
    |       [ cli_go.py evaluate ]  |
    |               |               |
    |               v               |
    |  +-------------------------+  |
    |  | STAGE 3: Test Refine    |  |  Agent iterates on test failures
    |  | run_tests=true          |  |  go test -json feedback
    |  | use_lint_info=true      |  |  Most impactful stage
    |  +------------+------------+  |
    |               |               |
    |               v               |
    |       [ cli_go.py evaluate ]  |  Final pass rate
    +-------------------------------+
               |
               v
    +-----------+-----------+
    | Results                |  logs/pipeline_*.json
    |   Per-stage pass rates |  Per-repo agent logs
    |   Costs, timings       |  Docker image tarballs
    +------------------------+
```

---

## Key Differences from Python Pipeline

| Aspect | Python Pipeline | Go Pipeline |
|--------|----------------|-------------|
| CLI entry point | `commit0` (installed script) | `python commit0/cli_go.py` |
| Agent config CLI | `agent/cli.py` | `agent/config_go.py` |
| Pipeline script | `run_pipeline.sh` | `run_pipeline_go.sh` |
| Config file | `.commit0.yaml` | `.commit0.go.yaml` |
| Agent config | `.agent.yaml` | `.agent.go.yaml` |
| Base Docker image | `commit0.base.python3.12:latest` | `commit0.base.go:latest` |
| Test runner | `pytest` | `go test -json -count=1 ./...` |
| Linting | ruff + pyright (pre-commit) | goimports + staticcheck + go vet |
| Stubbing | Python AST (`ast.parse`) | Go AST (`gostubber` binary) |
| Stub marker | `pass` | `"STUB: not implemented"` (string literal) |
| Source filter | `*.py`, skip `__init__`, conftest | `*.go`, skip `*_test.go`, `doc.go`, `vendor/` |
| Test IDs format | `file::class::test` (pytest) | `package/TestName` (go test) |
| Test ID files | `commit0/data/test_ids/<repo>.bz2` | `commit0/data/test_ids/<repo>.bz2` |
| Dependencies | pip_packages, venv, pip install | `go mod download`, `go build ./...` |
| Multiple images needed | Yes (per Python version) | No (single Go image covers all versions) |
| Topological sort | Yes (Python import deps) | No (Go has no equivalent) |
| Coverage | Supported | Not supported |
| Spec PDF | Supported | Not supported |

---

## Step-by-Step: Manual Method

### Step 1: Prepare the Go repo

```bash
.venv/bin/python tools/prepare_repo_go.py \
    --repo <OWNER>/<REPO> \
    --clone-dir ./repos_staging \
    --output <repo>_entries.json \
    --org Zahgon
```

This forks the repo to your org, clones locally, builds `gostubber` (if not built), runs Go AST stubbing on exported functions, and pushes a `commit0_all` branch with:
- `base_commit`: stubbed code (exported function bodies replaced with zero-value returns + `"STUB: not implemented"` string literal)
- `reference_commit`: the original working code

The gostubber binary:
- Stubs only EXPORTED functions (capitalized names)
- Preserves unexported functions, `init()`, and `main()`
- Preserves all `*_test.go` files untouched
- Skips `doc.go`, `vendor/`, `.git/`, `testdata/` directories
- Replaces function bodies with `_ = "STUB: not implemented"` + appropriate zero-value returns

Output: `<repo>_entries.json` with commit SHAs and metadata.

### Step 2: Verify and fix entries JSON

**Always inspect entries before proceeding.** Go-specific checks:

| Field | What to check |
|-------|---------------|
| `src_dir` | Should be `"."` for most Go repos (source at root). Monorepos may use subdirectories. |
| `test.test_cmd` | Default: `"go test -json -count=1 ./..."`. Override for repos needing build tags or specific packages. |
| `setup.install` | Default: `"go mod download && go build ./..."`. Some repos need `go generate` or CGO setup. |
| `setup.pre_install` | System packages for CGO deps (e.g., `["apt-get install -y libsqlite3-dev"]`). |
| `repo` | Fork location: `<org>/<repo>`. Must be the fork, not the upstream. |

```bash
# Quick sanity check
cat <repo>_entries.json | python -m json.tool | grep -E '"src_dir"|"test_cmd"|"install"|"go_version"'

# Verify go.mod exists in the repo
cat repos_staging/<owner>__<repo>/go.mod | head -3
```

### Step 3: Create the dataset

```bash
.venv/bin/python tools/create_dataset_go.py <repo>_entries.json --output <repo>_dataset.json
```

Validates all entries (required fields, `language == "go"` constraint, commit SHA format) and writes the dataset JSON.

### Step 4: Setup

```bash
.venv/bin/python commit0/cli_go.py setup all \
    --dataset-name ./<repo>_dataset.json \
    --dataset-split train
```

Clones the fork into `repos/<repo>/`, checks out the `commit0_all` branch, creates a `commit0` branch, adds `.gitignore` entries (`.aider*`, `logs/`, `vendor/`), and writes `.commit0.go.yaml`.

### Step 5: Build Docker images

```bash
.venv/bin/python commit0/cli_go.py build
```

Builds two images:
- `commit0.base.go:latest`: Ubuntu 22.04 with Go 1.25.0 toolchain, staticcheck, goimports, `GOTOOLCHAIN=local`
- `commit0.repo.<repo>.<hash>:v0`: Clones the repo inside the container, runs `go mod download` + `go build ./...` at `reference_commit`, then resets to `base_commit` (stubbed code)

The base image is multi-arch (supports arm64 and amd64 via TARGETARCH).

### Step 6: Generate and install test IDs

```bash
.venv/bin/python tools/generate_test_ids_go.py <repo>_dataset.json --docker --install
```

Runs `go test -list . -count=1 ./...` at the `reference_commit` inside Docker to discover all test names. Saves them as `commit0/data/test_ids/<repo>.bz2`.

Test ID format: `package/TestName` (e.g., `github.com/sourcegraph/conc/pool/TestPool_Go`).

### Step 7: Validate stubbed code

```bash
docker run --rm \
    "commit0.repo.<repo>.<hash>:v0" \
    bash -c 'cd /testbed && go test -list . -count=1 ./...'
```

The test count from this validation must match Step 6. If it shows 0 tests, either:
- The stubs broke compilation (check `go build ./...`)
- The module path or imports are broken
- There's a CGO dependency not in `pre_install`

### Step 8: Run the 3-stage pipeline

```bash
bash run_pipeline_go.sh \
    --model opus \
    --dataset ./<repo>_dataset.json \
    --max-iteration 3 2>&1 | tee logs/<repo>_run.log
```

This runs: Stage 1 (Draft) → Evaluate → Stage 2 (Lint: goimports+staticcheck+go vet) → Evaluate → Stage 3 (Test: go test -json feedback) → Evaluate.

### Step 9: Check results

```bash
cat logs/pipeline_*_results.json | python -m json.tool
```

Results include per-stage pass rates, costs, and timing breakdowns.

---

## Dataset JSON Schema (Go)

The dataset file is a JSON array. Every downstream tool (setup, build, evaluate, pipeline) reads this format.

```json
[
  {
    "instance_id": "conc_go",
    "repo": "Zahgon/conc",
    "original_repo": "sourcegraph/conc",
    "base_commit": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
    "reference_commit": "f6e5d4c3b2a1f6e5d4c3b2a1f6e5d4c3b2a1f6e5",
    "setup": {
      "install": "go mod download && go build ./...",
      "packages": "",
      "pip_packages": "",
      "pre_install": [],
      "go_version": "1.23",
      "specification": ""
    },
    "test": {
      "test_cmd": "go test -json -count=1 ./...",
      "test_dir": "."
    },
    "src_dir": ".",
    "language": "go"
  }
]
```

### Field reference

| Field | Type | Description |
|-------|------|-------------|
| `instance_id` | string | Unique ID (e.g., `"conc_go"`) |
| `repo` | string | Fork location: `<org>/<repo_name>` |
| `original_repo` | string | Upstream repo: `<owner>/<repo_name>` |
| `base_commit` | string | SHA of the stubbed commit (exported function bodies replaced with stubs) |
| `reference_commit` | string | SHA of the original working code |
| `setup.install` | string | Install command run inside Docker (typically `"go mod download && go build ./..."`) |
| `setup.packages` | string | System packages to apt-get install (space-separated, usually empty for Go) |
| `setup.pip_packages` | string | Additional pip packages (usually empty for Go, kept for schema compatibility) |
| `setup.pre_install` | list[str] | Shell commands to run before install (apt-get for CGO deps, etc.) |
| `setup.go_version` | string | Go version from `go.mod` (informational — single Docker image serves all) |
| `setup.specification` | string | URL to documentation (usually empty for Go — no spec PDF support yet) |
| `test.test_cmd` | string | Test command (default: `"go test -json -count=1 ./..."`) |
| `test.test_dir` | string | Relative path to test root (usually `"."` for Go) |
| `src_dir` | string | Source directory (usually `"."` for Go — source at root by convention) |
| `language` | string | Must be `"go"` |

---

## File Reference

### tools/discover_go.py (~175 lines)

Discovers candidate Go repos from GitHub. Searches by star count, filters by Go percentage, checks for `_test.go` files and `go.mod`.

| Function | What it does |
|----------|-------------|
| `_search_go_repos(min_stars, max_results, token, ...)` | GitHub API search with `language:go` filter and star-range pagination |
| `_check_go_test_files(full_name, token)` | Checks if repo has `_test.go` files via GitHub tree API |
| `_get_go_version(full_name, branch, token)` | Reads `go.mod` from GitHub to extract Go version |
| `main()` | CLI orchestrator: search → filter → enrich → save |

### tools/validate_go.py (~191 lines)

Validates candidate repos by cloning and analyzing their structure.

| Function | What it does |
|----------|-------------|
| `clone_repo(full_name, clone_dir, branch)` | Clones a repo locally for analysis |
| `detect_go_structure(repo_dir)` | Detects go.mod, go_version, module_path, test file count |
| `validate_candidate(candidate, clone_dir, run_tests)` | Full validation orchestrator: clone → detect → optional test run |
| `main()` | CLI entry: reads candidates JSON, iterates validation |

### tools/prepare_repo_go.py (~430 lines)

Prepares Go repos for the dataset: forks, clones, Go AST-stubs via gostubber, pushes branches.

| Function | What it does |
|----------|-------------|
| `fork_repo(full_name, org)` | Forks via `gh` CLI to the target organization |
| `full_clone(full_name, clone_dir, branch)` | Deep clone with optional branch checkout |
| `detect_go_module(repo_dir)` | Reads `go.mod` for module path and Go version |
| `create_stubbed_branch(repo_dir, ...)` | Calls gostubber on all `.go` files, commits result |
| `build_setup_dict(repo_dir, go_info)` | Generates install/pre_install/go_version config |
| `build_test_dict(repo_dir)` | Generates test_cmd and test_dir |

### tools/stub_go.py (~133 lines)

Python wrapper invoking the `gostubber` binary.

| Function | What it does |
|----------|-------------|
| `_ensure_gostubber()` | Builds the Go binary if not found at `tools/gostubber/gostubber` |
| `stub_go_repo(src_dir, out_dir, dry_run, verbose)` | Copies `src_dir` to `out_dir`, stubs all `.go` files using gostubber |

### tools/gostubber/ (Go binary)

Go AST-based stubber. Replaces exported function bodies with zero-value returns + `"STUB: not implemented"`.

| File | What it does |
|------|-------------|
| `main.go` | CLI entry: `--dir`, `--skip-tests`, `--skip-vendor`, `--json` flags |
| `stubber.go` | AST transformer: parses Go, stubs exported funcs only, preserves unexported/init/main |
| `go.mod` | Module definition (stdlib-only, no external deps) |

### tools/create_dataset_go.py

Creates dataset JSON from prepared entries.

| Function | What it does |
|----------|-------------|
| `validate_entry(entry, index)` | Schema validation with `language == "go"` constraint |
| `generate_go_split_constants(entries, split_name)` | Generates `GO_SPLIT` dict code |

### tools/generate_test_ids_go.py (~367 lines)

Generates Go test ID `.bz2` files.

| Function | What it does |
|----------|-------------|
| `collect_test_ids_local(repo_dir, ...)` | Runs `go test -list . -count=1 ./...` locally |
| `collect_test_ids_docker(repo_name, image_name, reference_commit, timeout)` | Docker SDK-based test discovery |
| `save_test_ids(test_ids, name, output_dir)` | Saves as `<name>.bz2` |
| `install_test_ids(source_dir, repo_names)` | Copies to `commit0/data/test_ids/` |
| `generate_for_dataset(dataset_path, output_dir, ...)` | Main orchestrator: iterates entries, collects IDs |

### commit0/harness/spec_go.py (120 lines)

Go-specific Spec subclass. Inherits from `Spec(ABC, dataclass)`.

| Property/Method | What it does |
|-----------------|-------------|
| `base_image_key` | Returns `"commit0.base.go:latest"` |
| `base_dockerfile` | Reads `Dockerfile.go` template |
| `repo_dockerfile` | FROM + proxy ARGs + COPY setup.sh + RUN + WORKDIR |
| `make_repo_script_list()` | git clone → fetch → reset → go mod download → go build → reset to base |
| `make_eval_script_list()` | cd → reset → git apply → goimports -w . → test_cmd → echo $? |
| `make_go_spec()` | Factory function creating Commit0GoSpec from instance dict |

### commit0/harness/evaluate_go.py (~290 lines)

Runs evaluation: applies git diff as patch, runs `go test -json` in Docker, reports pass rates.

| Function | What it does |
|----------|-------------|
| `main(...)` | Iterates repos, applies patches, runs tests, reports per-repo pass rates |

### commit0/harness/go_test_parser.py (~133 lines)

Parses `go test -json` output (test2json protocol).

| Function | What it does |
|----------|-------------|
| `parse_go_test_json(raw_output)` | Returns `Dict[str, TestStatus]` from JSON test output |
| `parse_go_test_json_with_durations(raw_output)` | Same + per-test duration data |
| `parse_go_test_plain(raw_output)` | Fallback parser for `go test -v` output |
| `compute_go_pass_rate(results, expected_tests)` | Calculates pass rate |

### commit0/harness/lint_go.py (~136 lines)

Runs Go static analysis inside Docker.

| Function | What it does |
|----------|-------------|
| `main(...)` | Runs `goimports -d .`, `staticcheck ./...`, `go vet ./...` in Docker container |

### commit0/cli_go.py (~378 lines)

Typer CLI providing all Go subcommands: `setup`, `build`, `test`, `evaluate`, `lint`, `save`, `get-tests`.

### run_pipeline_go.sh (~566 lines)

3-stage pipeline orchestrator for Go.

| Function | What it does |
|----------|-------------|
| `resolve_model()` | Maps presets (opus, kimi, glm5, minimax, gpt54) to full model IDs |
| `resolve_dataset()` | Resolves dataset name, JSON path, or GO_SPLIT key |
| `preflight()` | Checks dependencies: jq, bc, python, venv, repos dir |
| `run_go_setup()` | Calls `cli_go.py setup` |
| `run_go_build()` | Calls `cli_go.py build` |
| `run_go_evaluate()` | Calls `cli_go.py evaluate`, parses CSV output |
| `run_go_agent()` | Launches agent with stage-specific YAML via `config_go.py run` |
| Stage 1 (draft) | Agent drafts Go implementations. `run_tests=false`, `use_unit_tests_info=true`. |
| Stage 2 (lint) | Agent fixes goimports/staticcheck/go vet issues. `use_lint_info=true`. |
| Stage 3 (test) | Agent iterates on `go test` failures. `run_tests=true`, `use_lint_info=true`. |

---

## Configuration

### .commit0.go.yaml

Written by `cli_go.py setup`. All Go downstream commands read this file.

```yaml
base_dir: repos
dataset_name: ./conc_dataset.json
dataset_split: test
repo_split: conc_go
```

### .env file

Same as Python pipeline. `run_pipeline_go.sh` sources this automatically if present.

```bash
# For Bedrock models
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1

# For Bedrock with bearer token auth
AWS_BEARER_TOKEN_BEDROCK=...

# For OpenAI models
OPENAI_API_KEY=sk-...
```

### Agent config YAML (Go)

Generated by `run_pipeline_go.sh` for each stage:

```yaml
agent_name: aider
model_name: bedrock/converse/arn:aws:bedrock:us-east-1:...
model_short: opus4.6
use_user_prompt: false
user_prompt: 'You need to complete the implementations for all stubbed functions
  (those containing "STUB: not implemented") and pass the unit tests.
  Do not change the names or signatures of existing functions.
  IMPORTANT: You must NEVER modify, edit, or delete any test files
  (files matching *_test.go). Test files are read-only.'
use_topo_sort_dependencies: false        # Not applicable for Go
add_import_module_to_context: false
use_repo_info: false
use_unit_tests_info: true                # true in stage 1 only
use_spec_info: false                     # No spec PDF support for Go
use_lint_info: false                     # true in stages 2 and 3
run_entire_dir_lint: true
pre_commit_config_path: ''
run_tests: false                         # true in stage 3
max_iteration: 3
record_test_for_each_commit: false
cache_prompts: true
max_test_output_length: 15000
capture_thinking: true
trajectory_md: true
output_jsonl: true
```

### Model presets

| Preset | Model ID | Short name | Cache prompts |
|--------|----------|------------|---------------|
| `opus` | `bedrock/converse/arn:aws:bedrock:us-east-1:...:4w7tmk1iplxi` | opus4.6 | true |
| `kimi` | `bedrock/converse/arn:aws:bedrock:us-east-1:...:5m69567zugvx` | kimi-k2.5 | false |
| `glm5` | `bedrock/converse/arn:aws:bedrock:us-east-1:...:8lzlkxguk85a` | glm-5 | false |
| `minimax` | `bedrock/converse/arn:aws:bedrock:us-east-1:...:6oaav7wbxid4` | minimax-m2.5 | false |
| `gpt54` | `openai/gpt-5.4` | gpt-5.4 | false |

### run_pipeline_go.sh CLI options

| Flag | Description | Default |
|------|-------------|---------|
| `--model` | Model preset or full model string | (required) |
| `--dataset` | Dataset name, GO_SPLIT key, or JSON path | (required) |
| `--branch` | Override auto-generated branch name | auto |
| `--repo-split` | Override repo_split | derived from dataset |
| `--max-iteration` | Agent iterations per stage | 3 |
| `--stage-timeout` | Hard stage timeout in seconds | 0 (disabled) |
| `--eval-timeout` | Eval timeout in seconds | 3600 |
| `--backend` | Backend: docker or modal | docker |
| `--inactivity-timeout` | Kill agent if no activity for N seconds | 900 |
| `--max-wall-time` | Absolute per-stage wall-time cap | 86400 |
| `--num-samples` | Number of independent runs | 1 |

---

## Troubleshooting

### gostubber build failure

**Symptom**: `prepare_repo_go.py` or `stub_go.py` fails with "gostubber binary not found" or Go compilation errors.

**Cause**: The gostubber binary needs Go installed on the host (Go 1.21+ required).

**Fix**:
```bash
# Build manually
cd tools/gostubber && go build -o gostubber . && cd -

# Verify
tools/gostubber/gostubber --help
```

### Docker base image build failure

**Symptom**: `cli_go.py build` fails building `commit0.base.go:latest`.

**Cause**: The Dockerfile.go downloads Go 1.25.0 from `go.dev/dl/`. Network issues, proxy config, or missing MITM CA cert.

**Fix**:
```bash
# Check Docker is running
docker info

# Build manually for debugging
docker buildx build \
    -f commit0/harness/dockerfiles/Dockerfile.go \
    -t commit0.base.go:latest \
    --load .

# If behind corporate proxy, set proxy ARGs
docker buildx build \
    --build-arg http_proxy=$http_proxy \
    --build-arg https_proxy=$https_proxy \
    -f commit0/harness/dockerfiles/Dockerfile.go \
    -t commit0.base.go:latest \
    --load .
```

### "Cannot resolve dataset" error

**Symptom**: `run_pipeline_go.sh` exits with "Error: Cannot resolve dataset 'X'".

**Cause**: The `--dataset` value must be one of:
1. A path to a `.json` file (e.g., `./conc_dataset.json`)
2. A path containing `/` that resolves to a file (e.g., `datasets/conc.json`)
3. A `GO_SPLIT` key exactly (currently: `conc_go` or `Zahgon/conc`)

**Fix**: Pass the full path to your dataset JSON:
```bash
bash run_pipeline_go.sh --model opus --dataset ./conc_dataset.json
```

### HuggingFace dataset not found (404)

**Symptom**: `setup_go.main()` crashes trying to load `wentingzhao/commit0_go` from HuggingFace.

**Cause**: The HuggingFace Go dataset doesn't exist yet. Only local JSON datasets work currently.

**Fix**: Always use a local JSON file with `--dataset ./your_file.json`. Do not rely on the default `wentingzhao/commit0_go` HF dataset.

### 0 test IDs collected

**Symptom**: `generate_test_ids_go.py` reports 0 tests or the `.bz2` file is empty.

**Causes and fixes**:

1. **Compilation failure**: Stubs broke the build. Check:
   ```bash
   docker run --rm "commit0.repo.<repo>.<hash>:v0" bash -c 'cd /testbed && go build ./...'
   ```

2. **No exported test functions**: Go tests must be named `TestXxx`, `BenchmarkXxx`, `ExampleXxx`, or `FuzzXxx` (exported, capitalized). Internal test helpers don't appear in `-list` output.

3. **Build tags required**: Some repos need build tags:
   ```json
   "test_cmd": "go test -json -count=1 -tags integration ./..."
   ```

4. **CGO dependencies missing**: If tests need C libraries, add them to `setup.pre_install`:
   ```json
   "pre_install": ["apt-get update && apt-get install -y libsqlite3-dev"]
   ```

### Platform mismatches (arm64 vs amd64)

**Symptom**: Docker images build but `go test` fails with `exec format error`.

**Fix**: The Go Dockerfile uses `TARGETARCH` + `dpkg --print-architecture` for multi-arch support. Ensure Docker buildx is configured:
```bash
# Check current platform
docker buildx inspect --bootstrap

# On Apple Silicon, ensure linux/arm64 is used
docker run --platform linux/arm64 --rm "commit0.base.go:latest" go version
```

### Stage 2 regression (lint damaging code)

**Symptom**: Pass rate drops after Stage 2 compared to Stage 1.

**Cause**: `goimports` can reorder imports causing compilation failures if the repo uses `//go:generate` or init() side effects from import ordering. `staticcheck` suggestions are generally safe but may remove code the agent needs.

**Mitigation**: Stage 3 (test feedback) usually recovers. If lint is consistently harmful for a specific repo, reduce `max_iteration` for Stage 2 or skip it by running stages individually.

### go.mod toolchain directive conflict

**Symptom**: Build fails with "go: toolchain not available" or "go: module requires Go 1.XX".

**Cause**: The Docker image uses `GOTOOLCHAIN=local` and Go 1.25.0. If a repo's `go.mod` declares `go 1.26` or higher, the build fails.

**Fix**: Either update the `GO_VERSION` in `Dockerfile.go` to the required version, or downgrade the repo's `go` directive. The single-image approach requires the Docker image's Go version to be >= the highest `go` directive across all target repos.

---

## Go-Specific Gotchas Checklist

Before running the pipeline, verify each item:

- [ ] `go.mod` exists at repo root (module-aware repo required)
- [ ] `src_dir` is `"."` unless it's a monorepo with code in a subdirectory
- [ ] `test_cmd` defaults to `"go test -json -count=1 ./..."` — override only if needed
- [ ] No `vendor/` directory committed (or add `"go mod vendor"` to pre_install)
- [ ] No CGO deps without `pre_install` apt packages
- [ ] Test IDs file is non-empty: `bzcat commit0/data/test_ids/<repo>.bz2 | wc -l`
- [ ] Stubbed code still compiles: `go build ./...` inside Docker shows no errors
- [ ] Docker image exists: `docker images | grep commit0.base.go`
- [ ] `.commit0.go.yaml` exists and points to the correct dataset JSON
- [ ] gostubber binary is built: `ls tools/gostubber/gostubber`
- [ ] `gh auth status` shows authenticated with repo scope (for forking)
- [ ] `language` field is `"go"` in all dataset entries
- [ ] Enough disk space for Docker images (~500MB base + ~200MB per repo image)

---

## Reference Example: sourcegraph/conc

This section documents a complete run against `sourcegraph/conc`, a Go concurrency utilities library.

### Repo profile

| Property | Value |
|----------|-------|
| Repo | `sourcegraph/conc` |
| Description | Better structured concurrency for Go |
| Language | 100% Go |
| Layout | Standard Go (source at root) |
| Source files | ~15 `.go` files (exported functions to stub) |
| Test files | `*_test.go` alongside source (Go convention) |
| go.mod version | `go 1.21` |
| Runtime deps | `golang.org/x/sync` |
| Test deps | None external (stdlib `testing`) |
| Fork | `Zahgon/conc` |

### Commands run

```bash
# 1. Prepare
.venv/bin/python tools/prepare_repo_go.py \
    --repo sourcegraph/conc \
    --clone-dir ./repos_staging \
    --output conc_entries.json \
    --org Zahgon

# 2. Verify entries (check src_dir, test_cmd, install)
cat conc_entries.json | python -m json.tool

# 3. Create dataset
.venv/bin/python tools/create_dataset_go.py conc_entries.json \
    --output conc_dataset.json

# 4. Setup
.venv/bin/python commit0/cli_go.py setup all \
    --dataset-name ./conc_dataset.json --dataset-split train

# 5. Build
.venv/bin/python commit0/cli_go.py build

# 6. Test IDs
.venv/bin/python tools/generate_test_ids_go.py conc_dataset.json \
    --docker --install

# 7. Validate
docker run --rm \
    "commit0.repo.conc.<hash>:v0" \
    bash -c 'cd /testbed && go test -list . -count=1 ./...'

# 8. Run pipeline
bash run_pipeline_go.sh \
    --model opus \
    --dataset ./conc_dataset.json \
    --max-iteration 3 2>&1 | tee logs/opus_conc.log
```

### Expected dataset JSON

```json
[
  {
    "instance_id": "conc_go",
    "repo": "Zahgon/conc",
    "original_repo": "sourcegraph/conc",
    "base_commit": "<sha of stubbed commit>",
    "reference_commit": "<sha of original working code>",
    "setup": {
      "install": "go mod download && go build ./...",
      "packages": "",
      "pip_packages": "",
      "pre_install": [],
      "go_version": "1.21",
      "specification": ""
    },
    "test": {
      "test_cmd": "go test -json -count=1 ./...",
      "test_dir": "."
    },
    "src_dir": ".",
    "language": "go"
  }
]
```

### Output file locations

| What | Path |
|------|------|
| Entries JSON | `conc_entries.json` |
| Dataset JSON | `conc_dataset.json` |
| Results JSON | `logs/pipeline_*_results.json` |
| Pipeline log | `logs/opus_conc.log` |
| Stage logs | `logs/agent/conc_dataset/opus4.6/run_1/stage{1,2,3}_*/` |
| Test IDs | `commit0/data/test_ids/conc.bz2` |
| Staged repo | `repos_staging/sourcegraph__conc/` |
| Working repo | `repos/conc/` |
| Commit0 config | `.commit0.go.yaml` |
| Agent config | `.agent_*.yaml` (per-run, auto-cleaned) |
