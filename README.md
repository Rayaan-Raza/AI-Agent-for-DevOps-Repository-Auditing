# DevOps Repository Auditing Agent

[![CI](https://github.com/Rayaan-Raza/AI-Agent-for-DevOps-Repository-Auditing/actions/workflows/ci.yml/badge.svg)](https://github.com/Rayaan-Raza/AI-Agent-for-DevOps-Repository-Auditing/actions/workflows/ci.yml)

This agent examines a public GitHub repository. It finds DevOps best-practice violations and writes a JSON report.
An LLM selects each inspection tool from the results of the tools before it. The agent does not use a fixed checklist.

The agent does not use an agent framework (no LangChain, LangGraph, or n8n).
The agent loop, the tool dispatch, and the control flow are in Python. The only LLM library is the Groq SDK.

## Contents

1. [Quick start](#1-quick-start)
2. [Requirements](#2-requirements)
3. [Installation](#3-installation)
4. [Setup](#4-setup)
5. [Execution](#5-execution)
6. [Report format](#6-report-format)
7. [What the agent examines](#7-what-the-agent-examines)
8. [Architecture](#8-architecture)
9. [Tools](#9-tools)
10. [Precision controls](#10-precision-controls)
11. [Safety](#11-safety)
12. [Trace file](#12-trace-file)
13. [Tests](#13-tests)
14. [Self-evaluation](#14-self-evaluation)
15. [CI/CD and contribution workflow](#15-cicd-and-contribution-workflow)
16. [Project structure](#16-project-structure)
17. [Troubleshooting](#17-troubleshooting)
18. [Assignment requirements map](#18-assignment-requirements-map)
19. [Limits](#19-limits)

## 1. Quick start

Do these steps for a first audit:

```bash
git clone https://github.com/Rayaan-Raza/AI-Agent-for-DevOps-Repository-Auditing.git
cd AI-Agent-for-DevOps-Repository-Auditing
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # Windows: copy .env.example .env
```

Open `.env` and set `GROQ_API_KEY`. Then run:

```bash
python agent.py --repo https://github.com/<owner>/<repo> --output report.json
```

To get a free Groq API key, sign in at <https://console.groq.com/keys> and select **Create API Key**.

## 2. Requirements

| Item | Version | Note |
|---|---|---|
| Python | 3.11 or later | CI tests Python 3.11, 3.12, and 3.13. |
| `git` | Any recent version | Must be on the `PATH`. The agent clones with `git`. |
| Groq API key | Free or paid plan | <https://console.groq.com/keys> |
| Docker | Optional | Only for `--allow-exec` (test execution in a container). |

The Python dependencies are in `requirements.txt` and `pyproject.toml`:

| Package | Use |
|---|---|
| `groq` | LLM provider SDK (chat completions with tool calls) |
| `python-dotenv` | Loads the `.env` file |
| `PyYAML` | Parses GitHub Actions workflows with line numbers |
| `regex` | Regular-expression search with a timeout |
| `jsonschema` | Validates tool arguments and the report |
| `ruff` | Static lint tool for the `run_ruff` inspection tool |
| `pytest` | Test runner |

## 3. Installation

Do these steps in a terminal:

1. Clone this repository and go into it.

   ```bash
   git clone https://github.com/Rayaan-Raza/AI-Agent-for-DevOps-Repository-Auditing.git
   cd AI-Agent-for-DevOps-Repository-Auditing
   ```

2. Make a virtual environment and activate it.

   ```bash
   python -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   ```

3. Install the dependencies.

   ```bash
   pip install -r requirements.txt
   ```

4. Optional: make sure that the installation is correct. The tests do not need an API key.

   ```bash
   pytest
   ```

## 4. Setup

1. Copy `.env.example` to `.env`.

   ```bash
   cp .env.example .env
   ```

2. Open `.env`. Set `GROQ_API_KEY` to your key.
3. Optional: set the other values. If a value is empty, the agent uses the default.

| Variable | Default | Description |
|---|---|---|
| `GROQ_API_KEY` | (none, mandatory) | Groq API key |
| `LLM_MODEL` | `openai/gpt-oss-120b` | Groq model ID. `openai/gpt-oss-20b` and `qwen/qwen3.8-27b` also work. |
| `LLM_REASONING_EFFORT` | `low` | `low`, `medium`, or `high`. Only for reasoning models. A higher value uses more tokens. |
| `MAX_STEPS` | `35` | Maximum number of LLM steps in the investigation |
| `MAX_SECONDS` | `1800` | Maximum run time in seconds |
| `LLM_MAX_WAIT_SECONDS` | `300` | Longest wait for a rate limit. If Groq asks for a longer wait, the agent stops. Set a larger value for the Groq free tier. |

You can also set these values as environment variables. An environment variable has priority over the `.env` file.
The `.env` file is in `.gitignore`. Do not commit it.

### 4.1 Groq free tier

The Groq free tier has these limits for each model:

| Limit | Value |
|---|---|
| Tokens per minute | 8,000 |
| Tokens per day | 200,000 |
| Requests per day | 1,000 |

One audit uses approximately 60,000 to 120,000 tokens. Thus, the free tier permits approximately two audits for each model each day.
Each model has its own limit. If one model is at its daily limit, set `LLM_MODEL` to a different model.

For an unattended run on the free tier, use a longer rate-limit wait:

```bash
LLM_MAX_WAIT_SECONDS=1800 python agent.py --repo https://github.com/<owner>/<repo> --output report.json --max-seconds 5400
```

## 5. Execution

Run this command:

```bash
python agent.py --repo https://github.com/<owner>/<repo> --output report.json
```

The agent runs without interactive prompts. It writes two files:

- `report.json`: the report.
- `report.trace.jsonl`: one record for each tool call. Section 12 gives the format.

The agent writes progress messages to stderr. Example of the format (shortened):

```text
cloning https://github.com/example/demo ...
cloned: 45 files, 64 commits. model: openai/gpt-oss-120b
[investigation 1] inspect_workflows ok - Examine the CI workflow for triggers and permissions.
[investigation 2] inspect_dependencies ok - Check the dependency constraints and the lockfiles.
rate limit: waiting 28.5 s for tokens
[investigation 3] read_file ok - Check the settings file for hardcoded secrets.
...
investigation ended: finished after 18 steps; 7 candidates
report written: report.json (6 violations, 18 steps, 742 s). trace: report.trace.jsonl
```

### 5.1 Options

| Option | Description |
|---|---|
| `--repo URL` | Mandatory. `https://github.com/<owner>/<repo>`. A trailing `.git` or `/` is permitted. |
| `--output PATH` | Mandatory. Path of the JSON report. The agent makes the directory if it does not exist. |
| `--trace PATH` | Path of the trace file. Default: `<output>.trace.jsonl` |
| `--model ID` | Groq model ID. This value replaces `LLM_MODEL`. |
| `--max-steps N` | Step limit. This value replaces `MAX_STEPS`. |
| `--max-seconds N` | Time limit. This value replaces `MAX_SECONDS`. |
| `--allow-exec` | Let the agent run `pytest` in a Docker container without network access |
| `--keep-low-confidence` | Keep findings that the LLM marked as low confidence |
| `-q`, `--quiet` | Show only errors |

### 5.2 Exit codes

| Code | Meaning | Report written |
|---|---|---|
| 0 | The inspection is complete. The `violations` array can be empty. | Yes |
| 1 | Unexpected error | No |
| 2 | Invalid input (bad URL, missing argument, output path is a directory) | No |
| 3 | The clone failed (the repository does not exist, is private, or the clone timed out) | No |
| 4 | The API key is missing or the API rejected it | No |
| 5 | LLM API failure (rate limit not cleared, server error) | Partial, if violations were recorded |
| 6 | The agent cannot write the report | No |

If the exit code is not 0, the agent writes an error message to stderr.
If the LLM API fails after the agent recorded violations (exit code 5), the agent writes a partial report with the validated violations. The message on stderr says that the report is partial.

### 5.3 Run time

On the Groq free tier, one audit takes approximately 10 to 25 minutes. Most of this time is waiting for the rate limit.
The agent reads the rate-limit headers from each response and waits automatically. With a paid Groq plan, an audit is much faster.

## 6. Report format

```json
{
  "repository": "https://github.com/example/demo-project",
  "violations": [
    {
      "rule_id": "SEC-001",
      "category": "security",
      "severity": "critical",
      "file": "config.py",
      "line": 17,
      "description": "Hardcoded API credential",
      "evidence": "Credential assigned directly in source code",
      "recommendation": "Use a secrets manager or environment variable"
    }
  ]
}
```

| Field | Type | Description |
|---|---|---|
| `repository` | string | The repository URL |
| `violations` | array | The violations. The array is empty if the agent finds no violations. |
| `rule_id` | string | Category prefix, a dash, and three digits. Example: `SEC-001`. Section 7.2 gives the catalog. |
| `category` | string | One of the nine categories in section 7.1 |
| `severity` | string | `critical`, `high`, `medium`, or `low`. Section 7.3 gives the rules. |
| `file` | string or `null` | Path relative to the repository root. `null` if no file applies (for example, a missing file). |
| `line` | integer or `null` | Line number in `file`. `null` if no line applies. |
| `description` | string | One sentence. Maximum 160 characters. |
| `evidence` | string | What a tool showed. It never contains a secret value. |
| `recommendation` | string | One concrete action |

The agent sorts the violations by severity (critical first), then by file, then by line.
Python validates the report against a JSON schema before it writes the file. The agent writes to a temporary file and then renames it. Thus, the report is never half-written.

`sample_report.json` is a real output of the agent. `sample_report.trace.jsonl` is its trace.

## 7. What the agent examines

### 7.1 Inspection areas

| Category (`category`) | Prefix | Example violations |
|---|---|---|
| `repository_hygiene` | `HYG` | Missing README, unsuitable `.gitignore`, tracked temporary or generated files |
| `dependency_management` | `DEP` | Dependencies with no version, inconsistent specifications, a missing lockfile where a tool needs one |
| `code_quality` | `QUA` | No lint or format configuration, clear maintainability problems |
| `testing` | `TST` | No tests, tests not run in CI, tests not run on pull requests |
| `ci` | `CI` | Incorrect triggers, ignored failures (`\|\| true`, `continue-on-error`), skipped validation |
| `security` | `SEC` | Exposed credentials, secrets in git history, unsafe workflow permissions, script injection |
| `configuration` | `CFG` | Machine-specific paths, committed local settings, no environment separation |
| `collaboration` | `COL` | Unsafe pull request automation, no review guidance |
| `build_release` | `BLD` | No build validation, risky release or deploy automation |

### 7.2 Rule catalog

`auditor/rules.py` has the rule IDs. The catalog has names only. It does not run checks.
The LLM selects an ID from the catalog. If no ID applies, the LLM can make a new ID with the correct prefix.

| Rule | Name |
|---|---|
| `HYG-001` | Missing README |
| `HYG-002` | Missing or unsuitable `.gitignore` |
| `HYG-003` | Tracked temporary, cache, or generated files |
| `HYG-004` | Tracked vendored dependencies or virtual environment |
| `HYG-005` | Large binary or data files committed without need |
| `DEP-001` | Dependencies without version constraints in an application |
| `DEP-002` | Missing lockfile where the tool chain needs one |
| `DEP-003` | Inconsistent dependency specifications between files |
| `DEP-004` | Dependency from an unpinned URL or branch |
| `QUA-001` | No linting or formatting configuration or enforcement |
| `QUA-002` | Clear maintainability problem (dead code, wildcard imports, bare `except`) |
| `TST-001` | No automated tests |
| `TST-002` | Tests are not run in CI |
| `TST-003` | Tests do not run on pull requests |
| `TST-004` | Failing tests |
| `CI-001` | Incorrect or missing workflow triggers |
| `CI-002` | Test or check failures ignored (`\|\| true`, `continue-on-error`) |
| `CI-003` | Validation step skipped or disabled |
| `CI-004` | Third-party action pinned to a mutable branch |
| `CI-005` | Invalid workflow file |
| `SEC-001` | Hardcoded credential in source code |
| `SEC-002` | Committed secret file (`.env`, key, certificate) |
| `SEC-003` | Secret in git history |
| `SEC-004` | Excessive workflow token permissions |
| `SEC-005` | `pull_request_target` or `workflow_run` runs untrusted pull request code |
| `SEC-006` | Script injection from untrusted event data in a `run` step |
| `SEC-007` | Secret printed or exposed in workflow logs |
| `SEC-008` | Insecure default (debug on, TLS verification off) |
| `CFG-001` | Machine-specific absolute path |
| `CFG-002` | Committed local IDE or tool settings |
| `CFG-003` | No separation of environment configuration |
| `COL-001` | Unsafe pull request automation (auto-merge without checks) |
| `COL-002` | No contribution or review guidance where relevant |
| `BLD-001` | No build validation |
| `BLD-002` | Risky release or deploy automation (no gate, deploy on every push) |
| `BLD-003` | Docker image uses a mutable or `latest` tag, or runs as root |

### 7.3 Severity rules

The system prompt gives these rules to the LLM:

| Severity | Rule | Examples |
|---|---|---|
| `critical` | An attacker can use it now, or a real secret is exposed. | A real credential or private key in code or history. `pull_request_target` that checks out or runs pull request code with secrets or a write token. |
| `high` | A control that must stop bad changes does not work. | `pytest \|\| true`. `continue-on-error` on tests. `permissions: write-all`. `${{ github.event.* }}` in a `run` step. A committed `.env` with real values. A secret printed in logs. |
| `medium` | A practice that causes failures or risk over time. | No tests, or tests not in CI. Dependencies with no version in an application. Tracked generated files. Machine-specific paths. Third-party action on a branch. Deploy on every push. Docker `latest` or root. Missing `.gitignore` or README. |
| `low` | A small problem with low risk. | No lint configuration. A README without setup steps. Minor hygiene. |

### 7.4 What is not a violation

The agent does not report these items. This decreases false positives.

- An absent optional file when an equivalent solution is present. Examples:

  | Need | Equivalent solutions |
  |---|---|
  | Dependency file | `requirements*.txt`, `pyproject.toml`, `setup.cfg`, `setup.py`, `Pipfile` |
  | Lockfile | Only necessary for a tool that makes one (Poetry, uv, Pipenv, npm, yarn, pnpm). A library does not need one. `requirements.txt` with `==` pins is acceptable. |
  | Linter | `ruff`, `flake8`, `pylint`, `black`, `pre-commit`, or a lint step in CI |
  | CI | GitHub Actions, or another CI file (`.gitlab-ci.yml`, `.circleci/`, `Jenkinsfile`, `.travis.yml`) |
  | Review guidance | `CONTRIBUTING.md`, a pull request template, `CODEOWNERS`, or a README "Contributing" section |

- Version ranges with an upper bound (`>=1,<2`, `~=1.4`, `^1.2`). They are properly constrained.
- Actions pinned to a version tag (`actions/checkout@v4`).
- A missing `permissions` block alone. Only `write-all` or broad write permissions are a finding.
- Branch protection and other remote repository settings. Files cannot show them.
- `.env.example`, sample, and template files with empty or placeholder values.
- A missing LICENSE, CHANGELOG, Dockerfile, or Dependabot configuration.
- Style issues that `ruff` reports. They are not a violation by themselves.

## 8. Architecture

### 8.1 Phases

| Phase | Name | Control | Function |
|---|---|---|---|
| 0 | Setup | Python | Validate the URL. Clone the repository into a temporary directory. Make a file index and a profile of facts (file types, key files that are present or absent). |
| 1 | Investigation | LLM | The LLM selects tools, reads the results, and records violations. |
| 2 | Self-review | LLM | The LLM examines each candidate violation again. It removes weak findings and duplicates, and corrects the severity. |
| 3 | Output | Python | Validate, deduplicate, redact, sort, and write the report. Delete the clone. |

Python does not make findings. Python only collects facts, runs tools, and validates the findings of the LLM.

### 8.2 Agent cycle

```
 observe ──> reason ──> select tool ──> execute tool ──> interpret result ──┐
    ^                                                                       │
    └───────────────────────────────────────────────────────────────────────┘
                                   (until finish) ──> self-review ──> report
```

1. Observe. Python sends an AUDIT STATE message and the newest tool results.
2. Reason. The LLM decides what to examine next.
3. Select tool. The LLM sends one or more tool calls. Each investigation tool takes a `reason` argument: one sentence that tells why the LLM selected the tool.
4. Execute tool. Python validates the arguments against the tool schema and runs the tool on the clone.
5. Interpret results. The LLM reads the result. It records violations, keeps a note, or selects a follow-up tool.
6. Report. The LLM calls `finish`. Python starts the self-review, then validates the findings and writes the report.

Example of dynamic tool selection (from a real run on `repo_b_subtle`, section 14). The reasons are the words of the LLM:

| Step | Tool calls | Reason given by the LLM |
|---|---|---|
| 1 | `inspect_workflows`, `inspect_dependencies`, `git_tracked_ignored` | "Examine all CI/CD workflows for security and configuration issues." |
| 2 | `read_file(".github/workflows/triage.yml")`, `read_file(".github/workflows/release.yml")`, `read_file(".vscode/settings.json")` | "Check triage workflow for unsafe automation patterns." |
| 5 | `record_violations([SEC-005 triage.yml:3, SEC-007 release.yml, ...])` | The files from step 2 showed `pull_request_target` and a secret in an `echo` command. |
| 8 | `record_violations([CFG-001 .vscode/settings.json:1])` | The file from step 2 showed a machine-specific interpreter path. |

The files that the LLM read in step 2 come from the result of step 1. A fixed checklist cannot do this.

### 8.3 Agent state

Python keeps the state. The LLM changes it only with tools.

| Field | Content |
|---|---|
| Profile | File counts, file types, key files present or absent, vendored directories |
| Coverage | The status of each of the nine areas: `pending`, `in_progress`, `examined`, or `not_applicable` |
| Notes | Short facts that the LLM wants to keep (maximum 25) |
| Candidates | The candidate violations, with IDs `V1`, `V2`, ... |
| Seen lines | For each file, the line numbers that a tool showed to the LLM |

### 8.4 Context management

The Groq free tier has a small token limit. Thus, the agent does not send the full history.
For each request, Python makes these messages:

1. The system prompt: the method, the evidence rules, the severity rules, and the rule IDs.
2. The AUDIT STATE:
   - the profile and the complete list of tracked files (for repositories with 150 files or fewer),
   - the coverage of the nine areas,
   - the notes,
   - the files already seen, and the files seen without a verdict,
   - the candidate violations,
   - one line for each older step.
3. The newest turns (tool calls and results) that fit in the token budget (maximum six turns).

Python estimates the tokens of each request. It calibrates the estimate with the real token count from each response.

### 8.5 Loop controls

| Control | Function |
|---|---|
| Repeat detection | If the LLM repeats an identical tool call, Python does not run it again. Python sends the old result with a warning. |
| Loop warning | After two repeated calls, the AUDIT STATE tells the LLM to record its findings and move on. |
| Budget warning | At 60% of `MAX_STEPS`, the AUDIT STATE tells the LLM to record its findings and finish. |
| Finish check | If the LLM calls `finish` with areas still pending, Python tells it one time which areas are pending. |
| Malformed tool calls | If the API rejects a malformed tool call, Python does not keep it in the history. It asks for one valid call. After a second failure, it asks for a simpler call. After four failures, the investigation stops and the agent writes the report with its findings so far. |
| Cut-off completion | If the model uses all its completion tokens before it sends a tool call, Python sends the request again with a larger limit. |
| Step and time limits | `MAX_STEPS` and `MAX_SECONDS` stop the investigation. The agent then writes the report with its findings. |
| Rate limits | Python reads the rate-limit headers and waits before a request when the token budget is low. It waits up to `LLM_MAX_WAIT_SECONDS` for a 429 response. |

## 9. Tools

All tools read the cloned copy only. No tool writes to it.
Each result is redacted (section 11) and cut to approximately 6,000 characters before the LLM sees it.

### 9.1 Investigation tools

| Tool | Arguments | Result |
|---|---|---|
| `list_files` | `path`, `depth` (1 to 6), `pattern` (glob) | Tracked files with size and line count. Vendored directories (`node_modules/`, `venv/`, `dist/`) show as one line with a file count. |
| `read_file` | `path`, `start_line`, `end_line` | Lines with numbers (maximum 200 lines for each call). The result tells the next `start_line` if the file is longer. |
| `search_code` | `pattern` (regex), `path_glob`, `ignore_case`, `max_results` | Tracked file paths that match, then content matches as `file:line: text`. With only `path_glob`, it lists the files that match. |
| `git_log` | `max_count`, `path`, `search`, `show_patch` | Commits with hash, date, author name, subject, and changed files. `search` finds commits that add or remove a text (`git log -S`). Author emails are not shown. |

### 9.2 Analysis tools

These tools give facts in a structured format. They do not give findings.

| Tool | Result |
|---|---|
| `inspect_workflows` | For each GitHub Actions workflow, with line numbers: triggers, top-level and job permissions, jobs, steps, action references with their type (`sha`, `tag`, `branch or other`, `local`), `with` and `env` values, `continue-on-error`, secrets used, and untrusted expressions such as `${{ github.event.* }}`. |
| `inspect_dependencies` | Each dependency with its constraint type (`exact`, `range`, `lower_only`, `upper_only`, `none`, `url_or_path`). Supports `requirements*.txt` (with `-r` includes), `pyproject.toml` (PEP 621 and Poetry), `setup.cfg`, `Pipfile`, and `package.json`. Also lists the lockfiles, the configured tools that make a lockfile, and packages with different constraints in different files. |
| `git_tracked_ignored` | Tracked files that agree with `.gitignore`, and tracked files that agree with common temporary, local, or secret patterns (`*.pyc`, `.env`, `.idea/`, `*.pem`, and more). Example and template files are excluded. |
| `run_ruff` | Runs `ruff check` (static, does not run the code). Gives the number of issues for each rule, the first 20 issues, and tells if the repository has a ruff configuration. |
| `run_tests` | Only with `--allow-exec`. Section 11 gives the isolation. |

### 9.3 Facts added to results

`read_file` and `search_code` add a last line with facts that the LLM must not miss:

```text
[Secret values redacted at: line 4 aws_access_key real-looking; line 5 generic_secret real-looking]
[Absolute user paths at: line 6 C:/Users/jdoe/projects/inventory/data]
```

### 9.4 State tools

| Tool | Function |
|---|---|
| `record_violations` | Records one or more violations (maximum 6 for each call). Python validates each item (section 10). |
| `update_violation` | Changes a recorded violation. Only in the self-review. |
| `retract_violation` | Removes a recorded violation. Only in the self-review. |
| `add_note` | Keeps a short fact after old tool results leave the context |
| `update_coverage` | Sets the status of one or more inspection areas |
| `finish` | Ends the current phase |

## 10. Precision controls

The hidden evaluation measures precision. False positives decrease the grade. Thus, Python validates each finding immediately.
If a check fails, the tool result tells the LLM why. The LLM can then correct the finding.

| Check | Rule |
|---|---|
| Fields | All fields are present. The enums are correct. Text is cut to its maximum length. |
| Rule prefix | The prefix agrees with the category (`SEC-` only for `security`, and so on). |
| File exists | The file is in the repository. For a `history` finding, the file is in the git history. |
| Line exists | The line is in the range of the file. |
| Line seen | The LLM looked at the line through a tool before it reports the line. |
| Absence | For an `absent` finding, no tracked file agrees with the patterns in `absent_globs`. |
| Dependency facts | An "unpinned" finding is rejected if all dependencies in the file have an exact pin or an upper bound. A "missing lockfile" finding is rejected if no tool that makes a lockfile is configured, or if the lockfile is present. |
| Branch protection | A finding about branch protection or required reviews is rejected. Files cannot show these settings. |
| Secrets | Secret-like values in the text are redacted. |
| Duplicates | A finding with the same rule, file, and line as a different finding is rejected. |

After the investigation, the self-review examines each finding again. Then Python removes low-confidence findings, runs the checks again, deduplicates, and validates the report with the JSON schema.

## 11. Safety

| Risk | Control |
|---|---|
| Change to the target repository | The agent clones into a temporary directory. It never pushes. It deletes the clone at the end, also after an error. |
| Code execution during the clone | Git hooks are disabled. Submodules are not cloned. Git LFS downloads are disabled. Password prompts are disabled. |
| Code execution during the audit | No tool runs code from the repository. `run_tests` is the only exception. It runs only with `--allow-exec`, in Docker, with a read-only copy, no network for the tests, and limits for memory (1 GB), processes, and time. |
| Path traversal | Each path is resolved. A path outside the clone, an absolute path, or a path in `.git/` is rejected. Symbolic links that point out of the clone are rejected. |
| Slow regular expressions | `search_code` uses a timeout for each line and a time limit for each search. |
| Secret exposure | `auditor/redact.py` replaces secrets with `<REDACTED:type hint>` in tool results, the trace, stderr, and the report. The LLM never sees a secret value. The API key of the run is also redacted. |
| Tool failure | A tool error goes to the LLM as text. It does not stop the agent. |

Redaction finds these secret types: AWS access keys, GitHub tokens, Slack tokens, Stripe keys, Google API keys, Groq, OpenAI, and Anthropic API keys, JWTs, private key blocks, passwords in URLs, long base64 values, and assignments to names such as `password`, `secret`, `token`, or `api_key`.
Each redaction has a hint: `real-looking` or `placeholder-like`. Thus, the LLM can tell a real secret from a placeholder without seeing the value.

## 12. Trace file

The trace file has one JSON record for each tool call. The evaluator can use it to examine the agent behavior.
Example of the format (one record, shortened):

```json
{"step": 2, "phase": "investigation", "tool": "read_file",
 "args": {"path": ".github/workflows/triage.yml", "reason": "Check the triage workflow for unsafe automation."},
 "ok": true, "result_chars": 812, "result_preview": ".github/workflows/triage.yml  lines 1-17 of 17 ...",
 "model_reasoning": "inspect_workflows showed pull_request_target with a checkout of the PR head ...",
 "tokens": {"prompt": 3638, "completion": 448}, "llm_seconds": 26.5, "elapsed_s": 58.2}
```

| Field | Content |
|---|---|
| `step`, `phase` | Step number and phase (`investigation` or `review`) |
| `tool`, `args` | The tool call. `args.reason` tells why the LLM selected the tool. |
| `ok` | `false` if the tool gave an error or the call was a repeat |
| `result_preview` | The first 300 characters of the result |
| `model_reasoning` | The reasoning text of the model, if the model gives one |
| `tokens`, `llm_seconds`, `elapsed_s` | Token use and time |

Other records have an `event` field: `no_tool_call`, `tool_use_failed` (with the rejected output), and `end` (stop reason, step count, coverage, and retracted findings with their reasons).
All trace records are redacted.

## 13. Tests

Run the tests:

```bash
pytest
```

The tests do not use the Groq API. They need no API key and no network.
A scripted fake LLM tests the loop. The fake LLM also makes sure that each request has the message structure that the Groq API requires.
The tests make small git repositories in a temporary directory.

| Test file | What it examines |
|---|---|
| `tests/test_tools.py` | Each tool, path traversal rejection, regular-expression timeouts, vendored directories, dependency classification, the facts footer, and that the tools do not change the clone |
| `tests/test_redact.py` | Each secret type, placeholder hints, and that code references are not redacted |
| `tests/test_repo.py` | URL validation, clone, file index, profile, clean-up, and clone errors |
| `tests/test_report.py` | Finding validation, dependency guards, deduplication, sorting, the JSON schema, the atomic write, and the tool dispatcher |
| `tests/test_llm.py` | Rate-limit parsing and waiting, Groq error mapping, malformed tool calls, and the retry for cut-off completions |
| `tests/test_loop.py` | Dynamic tool selection, rejected findings, repeat detection, malformed tool calls, step and time limits, self-review, and the token budget |
| `tests/test_cli.py` | Exit codes, an end-to-end run, partial reports, empty reports, and that no secret goes to stderr |

## 14. Self-evaluation

The hidden repositories are not available. Thus, `eval/fixtures.py` has three local test repositories with known defects:

| Repository | Content | Known defects |
|---|---|---|
| `repo_a_obvious` | A small Flask service | 8: no README, no `.gitignore`, tracked `.pyc` files, a hardcoded AWS key, a committed `.env`, unpinned dependencies, no tests, a machine-specific path |
| `repo_b_subtle` | A tidy project with CI | 7: `pytest \|\| true`, `permissions: write-all`, unsafe `pull_request_target`, script injection, a secret printed in logs, a private key in git history, committed IDE settings |
| `repo_c_healthy` | A healthy library | 0. This repository tests false positives. |

Run the evaluation (this uses the Groq API):

```bash
python eval/score.py                         # all three repositories
python eval/score.py --only repo_b_subtle    # one repository
python eval/score.py -- --model openai/gpt-oss-20b   # arguments after -- go to agent.py
```

The script builds the repositories, runs the agent on each one, and calculates the precision and the recall.
The agent does not read these files. No finding is hardcoded in the agent.

Results so far:

| Repository | Model | Precision | Recall | Note |
|---|---|---|---|---|
| `repo_a_obvious` | `qwen/qwen3.8-27b` | 0.88 | 0.88 | 7 of 8 defects found in 17 steps |
| `repo_b_subtle` | several | (incomplete) | (incomplete) | The free-tier daily token limit stopped the runs. |
| `repo_c_healthy` | `openai/gpt-oss-20b` | (incomplete) | (incomplete) | No false positives in the steps that completed. |

The live runs found these problems. Each one has a fix in the code:

| Problem in a live run | Fix |
|---|---|
| The LLM read the same file many times. | Repeat detection, the files-seen list, and the loop warning |
| The LLM searched for file names with a content search. | The tracked file list in the AUDIT STATE, and path matches in `search_code` |
| The LLM saw a violation but did not record it. | `record_violations` takes a list. The "files seen without a verdict" list. |
| The LLM could not tell a real secret from a placeholder. | The `real-looking` and `placeholder-like` hints |
| The LLM reported version ranges as unpinned and a missing lockfile for a library. | The dependency guards in the validation |
| The daily token limit stopped a run and the findings were lost. | The partial report |

## 15. CI/CD and contribution workflow

GitHub Actions runs `.github/workflows/ci.yml` on each pull request to `main` and on each push to `main`:

| Job | Steps |
|---|---|
| Lint | `ruff check .` and `ruff format --check .` |
| Test | `pytest` on Python 3.11, 3.12, and 3.13 |
| CLI smoke test | `agent.py --help`, and the exit codes for an invalid URL (2) and a missing API key (4) |

The workflow has read-only permissions and does not need an API key.

Other files:

| File | Function |
|---|---|
| `.github/pull_request_template.md` | A checklist for each pull request |
| `.github/CODEOWNERS` | Requests a review from the repository owner |
| `.github/dependabot.yml` | Weekly update pull requests for pip packages and GitHub Actions |
| `CONTRIBUTING.md` | Branch names, local checks, pull request steps, and commit messages |

Do not push directly to `main`. Make a branch, open a pull request, and merge when CI passes. `CONTRIBUTING.md` gives the steps.

## 16. Project structure

```text
.
├── agent.py                  CLI entry point and exit codes
├── tools.py                  Tool functions, schemas, argument validation, dispatch
├── auditor/
│   ├── repo.py               URL validation, safe clone, file index, profile
│   ├── loop.py               Investigation loop, self-review, repeat detection, trace
│   ├── context.py            Messages for each turn within the token budget
│   ├── llm.py                Groq client, rate limiter, error mapping
│   ├── prompts.py            System prompt, review prompt, reminders
│   ├── report.py             Finding validation, guards, deduplication, JSON schema, atomic write
│   ├── redact.py             Secret redaction
│   ├── state.py              Agent state
│   └── rules.py              Rule ID catalog (names only, no checks)
├── tests/                    Automated tests (no API key needed)
├── eval/
│   ├── fixtures.py           Three test repositories with known defects
│   └── score.py              Runs the agent and calculates precision and recall
├── docs/
│   ├── PLAN.md               Work plan
│   ├── AGENT_DESIGN.md       Full design
│   └── Assignment_2_...pdf   Assignment brief
├── sample_report.json        Real output of the agent
├── sample_report.trace.jsonl Trace of the sample run
├── .env.example              Configuration names, no values
├── requirements.txt          Dependencies for pip
├── pyproject.toml            Package metadata and tool settings
├── CONTRIBUTING.md           Contribution workflow
└── .github/                  CI workflow, PR template, CODEOWNERS, Dependabot
```

## 17. Troubleshooting

| Message or problem | Cause | Action |
|---|---|---|
| `GROQ_API_KEY is not set` (exit 4) | No key in `.env` or the environment | Copy `.env.example` to `.env` and set the key. |
| `Groq rejected the API key (401)` (exit 4) | The key is wrong or deleted | Make a new key at <https://console.groq.com/keys>. |
| `Invalid repository URL` (exit 2) | The URL is not `https://github.com/<owner>/<repo>` | Use the repository URL, not a URL to a file or a branch. |
| `Clone failed` (exit 3) | The repository does not exist, is private, or the network failed | Make sure that the repository is public and the URL is correct. |
| `retry after ... s. The daily token or request limit is probably used up` (exit 5) | The Groq daily limit for the model is used up | Wait, set a different `LLM_MODEL`, or set `LLM_MAX_WAIT_SECONDS` to a larger value. |
| `Model ... is not available on this Groq account (404)` | The model is not available for the key | Set `LLM_MODEL` to `openai/gpt-oss-120b` or `openai/gpt-oss-20b`. |
| Many `rate limit: waiting ...` messages | The 8,000 tokens per minute limit of the free tier | This is normal. The agent continues automatically. |
| `investigation stopped early (max_steps)` | The step limit stopped the investigation | The report has the findings so far. Set a larger `--max-steps` if necessary. |

## 18. Assignment requirements map

| Requirement | Where |
|---|---|
| Explicit Python agent/tool loop, no framework | `auditor/loop.py`, `tools.py` |
| LLM selects tools from observations, with follow-up calls | Section 8.2, `reason` in each trace record |
| `list_files`, `read_file`, `search_code`, `git_log` | `tools.py` (section 9.1) |
| Optional tools (ruff, tests, git status, GitHub Actions inspection) | `run_ruff`, `run_tests`, `git_tracked_ignored`, `inspect_workflows` |
| Acceptable alternatives are not violations | Section 7.4, system prompt, dependency guards |
| No branch protection claims | Validation rule in `auditor/report.py` |
| `python agent.py --repo <url> --output report.json`, unattended | `agent.py` |
| Valid JSON, empty array if no violations, all fields, allowed severities, `null` for file and line | `auditor/report.py` (JSON schema) |
| Non-zero exit with a useful message on fatal errors | Section 5.2 |
| Never expose secrets | `auditor/redact.py` |
| Do not modify targets or run untrusted code without isolation | Section 11 |
| Handle missing files, API errors, invalid input, timeouts | Sections 5.2, 8.5, 11 |
| `requirements.txt`, `.env.example`, `README.md`, `tests/`, `sample_report.json` | Repository root |

## 19. Limits

- The agent supports public GitHub repositories only.
- The agent does not examine remote settings, for example branch protection.
- `run_tests` needs Docker and `--allow-exec`. Without it, the agent does not report failed tests.
- For repositories with more than 150 files, the AUDIT STATE does not list all files. The LLM uses `list_files` instead.
- On the Groq free tier, the daily token limit permits approximately two audits for each model each day.
- The quality of the findings depends on the LLM. The validation rejects unsupported findings, but it cannot prove that each finding is correct.
