# Agent Design: DevOps Repository Auditing Agent

This document is written in ASD-STE100 Simplified Technical English (STE).

This document gives the full logic of the agent. It tells:

- How the agent operates from start to end.
- What the LLM sees in each turn.
- Which tools the agent uses, and what each tool gives.
- How the agent makes sure that each finding is correct.

`PLAN.md` gives the work steps. If this document and `PLAN.md` are different, this document is correct.

## 1. Overview

The agent has four phases:

| Phase | Name | Who controls it | Result |
|---|---|---|---|
| 0 | Setup | Python | A safe local copy and a repository profile |
| 1 | Investigation | The LLM | Candidate violations |
| 2 | Self-review | The LLM | A smaller list of candidate violations |
| 3 | Validation and output | Python | `report.json` and `trace.jsonl` |

Only Phase 1 and Phase 2 use the LLM.
Python does not make findings. Python only collects facts, runs tools, and validates the findings of the LLM.
This division is important. The assignment does not permit a fixed checklist with an LLM summary.

```
            +------------------+
 URL  --->  | Phase 0: Setup   |  clone, profile
            +--------+---------+
                     |
                     v
            +------------------+      tool call       +-----------+
            | Phase 1:         | -------------------> |  Tools    |
            | Investigation    | <------------------- | (Python)  |
            | (LLM loop)       |      tool result     +-----------+
            +--------+---------+
                     | finish
                     v
            +------------------+
            | Phase 2:         |  retract or update weak findings
            | Self-review      |
            +--------+---------+
                     | finish
                     v
            +------------------+
            | Phase 3:         |  validate, deduplicate, redact, write
            | Validation       |
            +--------+---------+
                     |
                     v
              report.json, trace.jsonl
```

## 2. Phase 0: Setup

Python does these steps. The LLM is not used.

### 2.1 Validate the input

1. Make sure that the URL agrees with this pattern: `https://github.com/<owner>/<repo>` with an optional `.git` or `/` at the end.
2. If the URL is not correct, stop with exit code 2.
3. Make sure that `GROQ_API_KEY` has a value. If it has no value, stop with exit code 4.

### 2.2 Clone the repository

Run this command in a new temporary directory:

```
git -c core.hooksPath=<empty dir> -c protocol.file.allow=never \
    clone --filter=blob:none --no-recurse-submodules <url> <tmp>/repo
```

Use these settings:

- Set `GIT_TERMINAL_PROMPT=0`. This prevents a password prompt for a private or missing repository.
- Set a timeout of 120 seconds.
- Use `--filter=blob:none`. The clone gets the full commit history, but it gets old file contents only when a tool needs them. Thus, `git_log` can show old commits, and the clone is fast.
- Do not clone submodules. Submodules can point to other hosts.
- Hooks do not run. The agent does not run code from the repository.

If the clone fails, stop with exit code 3.

### 2.3 Make the repository profile

Python makes a short profile. The profile contains only facts, not judgements.

| Profile item | Example |
|---|---|
| Number of files | 87 |
| Number of commits | 142 |
| Default branch | `main` |
| File types | `.py: 41, .yml: 4, .md: 3` |
| Key files that are present | `pyproject.toml`, `Dockerfile`, `.github/workflows/ci.yml` |
| Top-level directories | `src/`, `tests/`, `docs/` |
| Large directories | `node_modules/ (2,341 files)` |

The list of key files is a fixed list of names, for example `README*`, `.gitignore`, `requirements*.txt`, `pyproject.toml`, `package.json`, `Dockerfile`, `.github/workflows/*`, `.pre-commit-config.yaml`, `.env*`.
The profile only tells which of these files are present. It does not tell if a missing file is a defect. The LLM decides that.

Python also makes a file index. The file index has each file path, its size, and its number of lines.
The tools and the validation use the file index.

## 3. Phase 1: Investigation loop

### 3.1 Agent state

Python keeps the agent state in one object. The LLM can change the state only with tools.

| Field | Content | Changed by |
|---|---|---|
| `profile` | The repository profile | Phase 0 |
| `coverage` | The status of each of the nine inspection areas | `update_coverage` |
| `notes` | Short facts that the LLM wants to keep | `add_note` |
| `candidates` | Candidate violations | `record_violation`, `retract_violation`, `update_violation` |
| `seen_lines` | For each file, the line numbers that a tool showed to the LLM | `read_file`, `search_code`, other tools |
| `history` | All steps, for the trace | The loop |
| `window` | The last three tool calls and their results | The loop |
| `budget` | Steps used, tokens used, time used | The loop |

### 3.2 Messages for each turn

The agent does not send the full history in each request.
The Groq limit of 8,000 tokens per minute makes a full history too large.
Python makes the messages again for each turn:

```
[system]     System prompt (static, approximately 1,800 tokens)
[user]       Audit state (made by Python, approximately 600 to 1,200 tokens)
[assistant]  Tool call, step n-2          \
[tool]       Tool result, step n-2         |  window: the last 3 steps,
[assistant]  Tool call, step n-1           |  full text
[tool]       Tool result, step n-1         |
[assistant]  Tool call, step n             |
[tool]       Tool result, step n          /
```

The audit state message has this format:

```
AUDIT STATE  (step 14 of 40, 61% of time budget left)
Repository: https://github.com/example/demo
Profile: 87 files, 142 commits, Python. Key files: pyproject.toml, Dockerfile,
  .github/workflows/ci.yml, .gitignore. Missing from key list: README*.

Coverage:
  repository_hygiene    examined
  dependency_management examined
  code_quality          pending
  testing               in_progress
  ci                    examined
  security              in_progress
  configuration         pending
  collaboration         pending
  build_release         not_applicable (no build or release files)

Notes:
  - CI runs on push to main only, not on pull_request. (ci.yml:3-5)
  - Tests are in tests/, 12 files, pytest.

Candidate violations (3):
  V1 SEC-001 critical config.py:17   Hardcoded API credential
  V2 CI-002  high     ci.yml:24      Test failures ignored with "|| true"
  V3 HYG-001 medium   (none)         Missing README

Older steps (summary):
  s1  list_files(".", depth=2)            -> 64 entries
  s2  read_file("pyproject.toml")         -> 48 lines
  ...
```

Each older step has one summary line. Python makes these lines. The LLM does not make them.
The LLM must keep important facts with `add_note`, because the full text of old results goes away.
The system prompt tells the LLM this rule.

Python limits the notes to 25 lines. Python limits each note to 200 characters.

### 3.3 One turn

Each turn has these steps:

1. Python makes the messages (section 3.2).
2. Python sends the request to Groq with `tools` and `tool_choice: "auto"`.
3. The LLM sends one or more tool calls.
4. For each tool call, Python does these steps:
   1. Make sure that the tool name is in the tool list.
   2. Parse the JSON arguments. Validate them against the schema.
   3. If the arguments are not correct, give the error text as the tool result.
   4. Run the tool with a time limit.
   5. Redact secrets in the result.
   6. Cut the result to the size limit. Add a line that tells how to get more.
   7. Update `seen_lines`.
   8. Add the step to `history` and to the trace file.
5. Python moves the oldest step out of the window if the window has more than three steps.
6. Python examines the stop conditions (section 3.6).

If the LLM sends text and no tool call, Python adds this message in the next turn:
"Send a tool call. If the audit is complete, call `finish`."
If this occurs three times in sequence, the loop goes to Phase 2.

### 3.4 The `reason` argument

Each investigation tool has a mandatory `reason` argument.
The LLM writes one sentence in it. The sentence tells why the LLM selected this tool.

Example:

```json
{
  "name": "read_file",
  "arguments": {
    "path": ".github/workflows/ci.yml",
    "reason": "The profile shows one workflow. I must find its triggers and its test step."
  }
}
```

The `reason` text goes into the trace.
The evaluator can read the trace. The trace shows that the LLM selected each tool from its observations.
The `reason` text also makes the LLM think before it calls a tool.

### 3.5 Method that the LLM uses

The system prompt tells the LLM to use this method:

1. Look at the profile. Decide which inspection areas apply to this repository. Mark the other areas as `not_applicable` with a reason.
2. Start with the files that have the most risk: workflows, configuration files, dependency manifests, and files with names like `config`, `settings`, `secrets`.
3. For each area, make a hypothesis. Example: "The CI does not run the tests on pull requests."
4. Use a tool to get evidence for or against the hypothesis.
5. If the evidence shows a violation, read the exact lines. Then call `record_violation`.
6. If the evidence shows an acceptable alternative, do not record a violation. Add a note.
7. Use follow-up tools when a result shows a new risk. Example: a workflow uses `secrets.DEPLOY_KEY`. Then search for where the deploy step runs.
8. When an area is complete, call `update_coverage` with `examined`.
9. When all areas are `examined` or `not_applicable`, call `finish`.

The system prompt does not give a fixed order of tools.
The LLM selects the order from the profile and from the results.

### 3.6 Stop conditions

| Condition | Action |
|---|---|
| The LLM calls `finish` and all areas are `examined` or `not_applicable` | Go to Phase 2. |
| The LLM calls `finish`, but some areas are `pending` | Python tells the LLM which areas are pending. This occurs one time only. If the LLM calls `finish` again, go to Phase 2. |
| Steps are at 75% of `MAX_STEPS` | Python adds this line to the audit state: "Budget low. Examine the pending areas or call finish." |
| Steps are equal to `MAX_STEPS` (40) | Go to Phase 2. Write a warning to stderr. |
| Time is more than `MAX_SECONDS` (900) | Go to Phase 3 immediately. Write a warning to stderr. |

## 4. Tools

### 4.1 Rules for all tools

- Each tool reads only from the cloned copy. No tool writes to it.
- Each path argument is relative to the repository root.
- Python resolves each path. If the resolved path is outside the root, the tool gives an error. This includes symbolic links.
- Each result has a size limit of approximately 6,000 characters (approximately 1,500 tokens).
- Each result is redacted (section 6).
- A tool error does not stop the agent. The error text goes to the LLM as the tool result.
- Each tool has a time limit of 30 seconds.

### 4.2 Investigation tools

#### `list_files`

| Argument | Type | Default | Description |
|---|---|---|---|
| `path` | string | `"."` | Directory to list |
| `depth` | integer | 2 | Maximum depth, 1 to 5 |
| `pattern` | string | none | Glob filter, for example `*.yml` |
| `reason` | string | mandatory | Why the LLM calls this tool |

Result: One line for each file, with the size and the number of lines.
Hidden files (for example `.env`, `.github/`) are included.
A directory with more than 50 files shows as one line with the file count, for example `node_modules/ (2,341 files)`.
The maximum is 300 lines.

Example result:

```
.github/workflows/ci.yml        1.2 KB   41 lines
.gitignore                      0.1 KB    6 lines
config.py                       0.8 KB   29 lines
src/ (23 files)
tests/ (12 files)
```

#### `read_file`

| Argument | Type | Default | Description |
|---|---|---|---|
| `path` | string | mandatory | File to read |
| `start_line` | integer | 1 | First line |
| `end_line` | integer | `start_line + 199` | Last line |
| `reason` | string | mandatory | Why the LLM calls this tool |

Result: The lines with line numbers. The first line tells the range and the total.

```
config.py  lines 1-29 of 29
 1  import os
 2
...
17  API_KEY = "<REDACTED:generic_api_key>"
```

The tool rejects binary files and files larger than 1 MB.
If the range is larger than the size limit, the tool shows a part. The last line tells the next `start_line`.
Python adds all shown lines to `seen_lines`.

#### `search_code`

| Argument | Type | Default | Description |
|---|---|---|---|
| `pattern` | string | mandatory | Regular expression, maximum 200 characters |
| `path_glob` | string | all files | Glob filter, for example `**/*.py` |
| `ignore_case` | boolean | false | Case-insensitive search |
| `max_results` | integer | 40 | 1 to 100 |
| `reason` | string | mandatory | Why the LLM calls this tool |

Result: One line for each match, in the format `file:line: text`. Each text is cut at 200 characters.
The last line tells the total number of matches.
The search does not look in `.git/`, binary files, or files larger than 1 MB.
The search does not look in vendored directories (`node_modules/`, `venv/`, `.venv/`, `dist/`, `build/`) unless `path_glob` names them.
The search uses the `regex` package with a timeout of 5 seconds for each file. This prevents a slow pattern from stopping the agent.
Python adds all match lines to `seen_lines`.

#### `git_log`

| Argument | Type | Default | Description |
|---|---|---|---|
| `max_count` | integer | 20 | 1 to 100 |
| `path` | string | none | Show only commits that changed this path |
| `search` | string | none | Show only commits whose changes add or remove this text (`git log -S`) |
| `show_patch` | boolean | false | Show the changes (diff) for each commit |
| `reason` | string | mandatory | Why the LLM calls this tool |

Result: For each commit, the short hash, the date, the author name, the subject, and the changed files.
With `show_patch`, the result also has the diff. The diff is redacted and cut to the size limit.

The LLM uses `git_log` for these tasks:

- Find secrets that a later commit removed. Example: `search: "AWS_SECRET"`, `show_patch: true`.
- Find generated files that the repository tracks. Example: commits that add `dist/` files.
- Find direct commits that change the workflows.

Note: The author email does not show. This keeps personal data out of the output.

### 4.3 Analysis tools

These tools give facts in a structured format. They do not give findings.

#### `inspect_workflows`

| Argument | Type | Default | Description |
|---|---|---|---|
| `path` | string | all files in `.github/workflows/` | One workflow file |
| `reason` | string | mandatory | Why the LLM calls this tool |

Python parses each workflow with a YAML loader that keeps line numbers.
Python does not run the workflow.
Result for each workflow:

```
.github/workflows/ci.yml
  triggers: push (branches: main)                                  [line 3]
  top-level permissions: (not set)
  job "test" runs-on: ubuntu-latest                                [line 8]
    permissions: (not set)
    step 1  uses actions/checkout@v4          ref type: tag        [line 11]
    step 2  uses actions/setup-python@main    ref type: branch     [line 12]
    step 3  run: pip install -r requirements.txt                   [line 15]
    step 4  run: pytest || true                                    [line 17]
    step 5  continue-on-error: true                                [line 19]
  secrets used: secrets.PYPI_TOKEN                                 [line 22]
  expressions in run steps: ${{ github.event.pull_request.title }} [line 25]
```

The ref type is one of `sha`, `tag`, `branch`, or `local`.
The tool also shows each use of `${{ github.event.* }}` inside a `run` step. This is a common injection risk.
The tool does not say "this is a violation". The LLM decides.

Python adds the shown line numbers to `seen_lines`.

#### `inspect_dependencies`

| Argument | Type | Default | Description |
|---|---|---|---|
| `reason` | string | mandatory | Why the LLM calls this tool |

Python finds and parses the dependency files. It supports these files:

- Python: `requirements*.txt`, `pyproject.toml` (PEP 621 and Poetry), `setup.cfg`, `Pipfile`.
- Lockfiles: `poetry.lock`, `uv.lock`, `Pipfile.lock`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`.
- JavaScript: `package.json`.

Result: For each manifest, each dependency with its constraint type.
The constraint types are `exact` (`==1.2.3`), `range` (`>=1.2,<2`), `lower_only` (`>=1.2`), `none` (no version), and `url_or_path`.
The result also lists the lockfiles that are present.
The result also lists packages that have different constraints in two manifests.

#### `git_tracked_ignored`

| Argument | Type | Default | Description |
|---|---|---|---|
| `reason` | string | mandatory | Why the LLM calls this tool |

Python runs `git ls-files -c -i --exclude-standard`.
Result: The tracked files that agree with a pattern in `.gitignore`.
These files are in the repository, but `.gitignore` says they must not be.

Python also gives the tracked files that agree with a fixed list of common temporary or local patterns.
Examples: `*.pyc`, `__pycache__/`, `.DS_Store`, `*.log`, `.env`, `.idea/`, `.vscode/settings.json`, `*.sqlite3`.
The LLM decides if each file is a violation. For example, `.env.example` is acceptable.

#### `run_ruff`

| Argument | Type | Default | Description |
|---|---|---|---|
| `path` | string | `"."` | Directory or file |
| `reason` | string | mandatory | Why the LLM calls this tool |

Python runs `ruff check --no-fix --output-format json --exit-zero`.
`ruff` reads the files. It does not run them. Thus, this tool is safe without isolation.
If the repository has a `ruff` configuration, `ruff` uses it. If not, `ruff` uses its default rules.
Result: The number of issues for each rule code, and the first 20 issues with file and line.
If the repository has no Python files, the tool tells this.

The system prompt tells the LLM this rule: "Ruff output is not a violation by itself. Report a code-quality violation only for a clear maintainability problem, or if the repository has no lint configuration at all."

#### `run_tests` (optional)

This tool is available only if the user starts the agent with `--allow-exec`.
Python runs `pytest` in a Docker container with these settings:

- `--network none`
- A read-only copy of the repository
- Limits for memory (1 GB) and time (180 seconds)

Result: The number of tests that passed, failed, and had errors, and the names of the failed tests.
If `--allow-exec` is not set, the tool is not in the tool list. The LLM does not see it.
If Docker is not available, the tool gives this result: "Test execution is not available." The LLM must not record a violation about failed tests.

### 4.4 State tools

| Tool | Arguments | Function |
|---|---|---|
| `add_note` | `text` | Keeps a fact in the audit state. |
| `update_coverage` | `area`, `status`, `reason` | Changes the status of an inspection area. The status is `in_progress`, `examined`, or `not_applicable`. |
| `record_violation` | See section 5.1 | Adds a candidate violation. |
| `update_violation` | `id`, fields to change | Changes a candidate violation. |
| `retract_violation` | `id`, `reason` | Removes a candidate violation. The reason goes into the trace. |
| `finish` | `summary` | Ends the current phase. |

## 5. Violations

### 5.1 `record_violation` arguments

| Argument | Type | Description |
|---|---|---|
| `rule_id` | string | For example `SEC-001`. The prefix must agree with the category. |
| `category` | enum | One of the nine categories |
| `severity` | enum | `critical`, `high`, `medium`, `low` |
| `file` | string or null | Path relative to the root |
| `line` | integer or null | Line number |
| `description` | string | Maximum 120 characters |
| `evidence` | string | Maximum 300 characters. Tells what the LLM saw. |
| `recommendation` | string | Maximum 300 characters. Tells what to do. |
| `kind` | enum | `present` (something bad is in the repository) or `absent` (something necessary is missing) |
| `absent_globs` | list of strings | Only for `absent`. The file patterns that are missing, for example `["README*"]`. |
| `confidence` | enum | `high`, `medium`, `low` |

`kind`, `absent_globs`, and `confidence` are internal. They do not go into the report.

### 5.2 Immediate validation

Python validates each `record_violation` call immediately.
If a check fails, the tool result gives the error. The LLM can then correct the call.

| Check | Rule |
|---|---|
| Fields | All fields are present. The enums are correct. |
| Prefix | `SEC-` is only for `security`, `CI-` is only for `ci`, and so on. |
| File exists | If `file` is not null, the file is in the file index. |
| Line range | If `line` is not null, the line is in the range 1 to the number of lines. |
| Line seen | If `line` is not null, the line is in `seen_lines`. The LLM must look at a line before it reports it. |
| Absence | If `kind` is `absent`, no file in the file index agrees with any `absent_globs` pattern. |
| Secrets | `description`, `evidence`, and `recommendation` contain no secret. If they do, Python redacts them and tells the LLM. |
| Branch protection | If the text says "branch protection", the tool rejects the call. The rule is in section 2 of `PLAN.md`. |

Example of a rejected call:

```
ERROR: line 52 is not in config.py (29 lines). Read the file and correct the line number.
```

### 5.3 Severity rules

The system prompt gives these rules:

| Severity | Rule | Examples |
|---|---|---|
| `critical` | An attacker can use it now, or a real secret is exposed. | A live credential in a source file or in the history. `pull_request_target` with a checkout of the PR code and access to secrets. |
| `high` | A control that must stop bad changes does not work. | `pytest \|\| true`. `continue-on-error: true` on tests. `permissions: write-all`. A committed `.env` file with values. Script injection with `${{ github.event.* }}` in `run`. |
| `medium` | A practice that causes failures or risk over time. | No tests run in CI. Dependencies with no versions in an application. Tracked generated files. Machine-specific paths. Third-party actions on a branch ref. |
| `low` | A small problem with low risk. | No lint configuration. A README without install steps. Actions on a tag ref, not a SHA. |

### 5.4 Equivalent solutions

The system prompt gives this list. The LLM must not report a missing item if an equivalent is present.

| Need | Equivalent solutions |
|---|---|
| Dependency file | `requirements.txt`, `pyproject.toml`, `setup.cfg`, `setup.py`, `Pipfile`, `environment.yml` |
| Lockfile | `poetry.lock`, `uv.lock`, `Pipfile.lock`, `requirements.txt` with `==` versions, `pip-tools` output. A library does not need a lockfile. |
| Linter | `ruff`, `flake8`, `pylint`, configuration in `pyproject.toml`, `setup.cfg`, `tox.ini`, or `.pre-commit-config.yaml` |
| Formatter | `black`, `ruff format`, `autopep8`, `yapf` |
| Test runner | `pytest`, `unittest`, `tox`, `nox`, `make test` |
| CI | GitHub Actions. Other CI files (`.gitlab-ci.yml`, `.circleci/`, `Jenkinsfile`, `azure-pipelines.yml`) are also CI. |
| Review guidance | `CONTRIBUTING.md`, `.github/PULL_REQUEST_TEMPLATE.md`, `CODEOWNERS`, a section in the README |
| Secret handling | Environment variables, `secrets.*` in workflows, `.env.example` with no values |

## 6. Secret redaction

`redact.py` finds secrets with patterns. It replaces each secret with `<REDACTED:type>`.

| Type | Pattern summary |
|---|---|
| `aws_access_key` | `AKIA` or `ASIA` and 16 uppercase characters or digits |
| `github_token` | `ghp_`, `gho_`, `ghs_`, `ghu_`, `github_pat_` and the token characters |
| `private_key` | `-----BEGIN ... PRIVATE KEY-----` to the END line |
| `slack_token` | `xox[abprs]-...` |
| `stripe_key` | `sk_live_...`, `rk_live_...` |
| `jwt` | Three base64url parts with dots, starting with `eyJ` |
| `generic_assignment` | A name with `key`, `secret`, `token`, `password`, or `passwd`, then `=` or `:`, then a quoted value of 8 or more characters |
| `url_credentials` | `scheme://user:password@host` |

Python applies redaction to:

- Each tool result, before the LLM sees it.
- Each trace record.
- Each field of the report.
- Each message to stderr.

The redacted text still shows that a secret was there. Thus, the LLM can still report the violation.
The LLM never sees the secret value. Thus, the LLM cannot put it in the report.

Note: Redaction can also hide placeholder values, for example `password = "changeme"`. The LLM must decide if a value is a real secret or a placeholder. Python adds a hint to the redaction: `<REDACTED:generic_assignment len=8 placeholder-like>`. Python uses a short list of placeholder words, for example `changeme`, `example`, `your_`, `xxx`, `<...>`.

## 7. Phase 2: Self-review

When Phase 1 ends, Python starts one review turn.
Python sends a new message with all candidate violations. Each candidate shows its evidence and the exact file lines.

The review message tells the LLM to examine each candidate with these questions:

1. Is the evidence direct? Did a tool show the problem?
2. Is an equivalent solution present (section 5.4)?
3. Is the severity correct (section 5.3)?
4. Is this a duplicate of a different candidate?
5. Is this about something that files cannot show, for example branch protection?

The LLM can use `retract_violation`, `update_violation`, and the read tools.
The LLM can do a maximum of 8 steps in this phase. Then it calls `finish`.

## 8. Phase 3: Validation and output

Python does these steps. The LLM is not used.

1. Remove each candidate with `confidence: low`. The trace keeps it. (Tune this rule in the self-evaluation.)
2. Run the checks in section 5.2 again on each candidate.
3. Deduplicate. Two candidates are duplicates if they have the same `rule_id`, the same `file`, and the same `line`. Keep the one with the higher severity.
4. Redact each field again.
5. Sort by severity (critical first), then by file, then by line.
6. Make the report with only the eight fields from the assignment.
7. Validate the report with a JSON schema.
8. Write the report to a temporary file. Then rename it to the output path. This prevents a half-written file.
9. Delete the temporary clone.
10. Exit with code 0.

If the time limit stops the agent in Phase 1, Python does these steps on the candidates that it has.

## 9. Trace file

The agent writes `trace.jsonl` next to the report. Each line is one JSON record:

```json
{"step": 7, "phase": "investigation", "tool": "inspect_workflows",
 "args": {"reason": "The profile shows one workflow. I must find its triggers."},
 "result_chars": 1430, "result_preview": ".github/workflows/ci.yml\n  triggers: push ...",
 "tokens": {"prompt": 3120, "completion": 88}, "elapsed_s": 41.2}
```

The trace shows the sequence of tools and the reason for each tool.
The trace is the evidence for the rubric item "dynamic tool selection".

## 10. Example run

This example shows how the agent investigates a small repository.
The repository has a committed `.env` file, a hardcoded key, and a weak CI.

| Step | LLM tool call | Reason (short) | Result (short) | LLM action after the result |
|---|---|---|---|---|
| 1 | `update_coverage(build_release, not_applicable)` | No Dockerfile, no release workflow | OK | Starts with the risk files |
| 2 | `inspect_workflows()` | One workflow in the profile | Trigger is `push` only. Step `pytest \|\| true` at line 17. | Makes 2 hypotheses: no PR tests, failures ignored |
| 3 | `read_file(ci.yml, 1, 41)` | Look at the exact lines | Lines 3-5 and 17 shown | Records `CI-002` high at line 17 |
| 4 | `record_violation(CI-002, ...)` | | Accepted as V1 | |
| 5 | `record_violation(TST-003, ..., line 3)` | | Accepted as V2 | Marks `ci` as examined |
| 6 | `git_tracked_ignored()` | Look for tracked local files | `.env` is tracked. `.gitignore` has `.env`. | New hypothesis: secrets in `.env` |
| 7 | `read_file(.env)` | Look for secret values | `DB_PASSWORD=<REDACTED:generic_assignment>` at line 2 | Records `SEC-002` high |
| 8 | `search_code("(?i)(api_key\|secret\|token)\\s*=", "**/*.py")` | Look for more secrets in code | `config.py:17` | Follow-up: read the line |
| 9 | `read_file(config.py, 10, 25)` | Look at the context | Line 17 is redacted. It is not from `os.environ`. | Records `SEC-001` critical |
| 10 | `git_log(search: "API_KEY", show_patch: true)` | Was the key changed in the history? | 1 commit added it | Adds a note. No new violation (same secret). |
| 11 | `inspect_dependencies()` | Dependencies in the profile | `requirements.txt`: 6 packages, 4 with `none` | Thinks: is this an application? |
| 12 | `list_files("src", 1)` | Look for an entry point | `src/app.py`, `Dockerfile` absent | Records `DEP-001` medium |
| ... | ... | ... | ... | ... |
| 20 | `finish(...)` | All areas done | Phase 2 starts | |
| 21 | `retract_violation(V5, ...)` | `CONTRIBUTING.md` is present | | Removes a false positive |
| 22 | `finish(...)` | | Phase 3 starts | |

Each step in the example comes from the result of the step before it.
A fixed checklist cannot do steps 6 to 10, because each one depends on the result before it.

## 11. System prompt structure

`prompts.py` has the system prompt. The prompt has these parts, in this order:

1. Role. "You are a DevOps auditor. You examine one repository with tools."
2. Scope. Software engineering and DevOps only. No ML training, datasets, or experiments.
3. Method. Section 3.5 in short form.
4. Memory rule. "Old tool results go away. Use `add_note` to keep important facts."
5. Evidence rules. A violation needs direct evidence from a tool. Do not report branch protection. Do not report a missing optional file when an equivalent is present (section 5.4).
6. Severity rules. Section 5.3.
7. Rule ID prefixes. The nine prefixes and examples of IDs.
8. CI risk patterns. The list from section 8 of `PLAN.md`.
9. Output rules. Use one tool call at a time. Never write a secret value. Write short evidence.

The prompt must be shorter than approximately 1,800 tokens. Use a token count test to make sure of this.

## 12. Module responsibilities

| Module | Responsibility |
|---|---|
| `agent.py` | Parse arguments. Load `.env`. Call the phases. Map errors to exit codes. |
| `auditor/repo.py` | Validate the URL. Clone. Make the profile and the file index. Delete the clone. |
| `tools.py` | All tool functions, the tool schemas, and the dispatch table. |
| `auditor/redact.py` | Secret patterns and redaction. |
| `auditor/state.py` | The agent state (section 3.1). |
| `auditor/llm.py` | Groq client, rate limiter, `tool_use_failed` recovery. |
| `auditor/context.py` | Makes the messages for each turn (section 3.2). |
| `auditor/loop.py` | Phase 1 and Phase 2. Stop conditions. Trace. |
| `auditor/report.py` | Violation checks, deduplication, JSON schema, output. |
| `auditor/prompts.py` | System prompt, review prompt, reminder texts. |
| `auditor/rules.py` | Rule ID catalog and category prefixes. |

## 13. Items to tune during the self-evaluation

| Item | Start value | Effect |
|---|---|---|
| Window size | 3 steps | A larger window gives more context but uses more tokens. |
| `MAX_STEPS` | 40 | More steps can increase recall but use more tokens. |
| Remove `low` confidence | Yes | Increases precision. Can decrease recall. |
| Size limit for each result | 6,000 characters | A smaller limit saves tokens but needs more steps. |
| Severity rules | Section 5.3 | Change only if the hidden test examples show a different scale. |
