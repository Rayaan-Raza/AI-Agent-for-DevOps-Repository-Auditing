# Plan: DevOps Repository Auditing Agent

This document is written in ASD-STE100 Simplified Technical English (STE).

## 1. Purpose

This plan tells how to make the agent for Assignment 2.
The agent examines a public GitHub repository.
The agent finds DevOps violations and writes a JSON report.
An LLM selects the tools. The LLM also selects the next tool from the result of the last tool.

## 2. Rules from the assignment

These rules are mandatory:

1. Do not use LangChain, LangGraph, n8n, or other agent frameworks.
2. You can use a basic LLM provider SDK.
3. Write the agent loop, the tool dispatch, and the control flow in Python.
4. Do not change the target repository.
5. Do not run code from the target repository without isolation.
6. Do not show secret values in logs or in the report.
7. Do not put findings for a specific repository into the code.
8. Do not report a defect if a check is not available.
9. Do not report remote branch protection settings. Files cannot show these settings.
10. Do not report a missing optional file if an equivalent solution is in the repository.

## 3. Decisions

| Item | Decision | Reason |
|---|---|---|
| LLM provider | Groq Python SDK (`groq`). The API key is in `GROQ_API_KEY`. | The user has a Groq API key. The grade does not change with the provider. |
| Default model | `openai/gpt-oss-120b`. Set a different model with the `LLM_MODEL` variable. | It is a production model. It supports tool calls and has a 131,072-token context. |
| Fallback model | `llama-3.3-70b-versatile` | It is a production model with tool calls. |
| Provider interface | Put all LLM calls in one module (`llm.py`). | You can change the provider in one location. |
| Packaging | `pyproject.toml` and `requirements.txt` | The evaluator can use `pip install -r requirements.txt`. |
| Clone method | `git clone` into a temporary directory | The agent reads a copy. The remote repository does not change. |
| Test execution | Off by default. Use `--allow-exec` with Docker to run it. | This prevents the execution of untrusted code on the host. |
| Linter | `ruff` as a tool. `ruff` reads files but does not run them. | This is safe without isolation. |

Do not use Groq preview models (for example `qwen/qwen3.8-27b`). Groq can remove preview models at short notice.

## 3A. Groq specifics

This section gives the Groq facts that change the design.
Data source: Groq documentation, read on 2026-10-24. Examine the values again in step 6.

### 3A.1 API format

The Groq API uses the OpenAI chat format.
Tools are JSON Schema objects in a `tools` array.
The model sends a `tool_calls` array. Each call has an `id`, a function name, and a JSON string of arguments.
The agent sends the result in a message with `role: "tool"` and the same `tool_call_id`.

### 3A.2 Rate limits

The base limits for `openai/gpt-oss-120b` are:

| Limit | Value |
|---|---|
| Requests per minute | 30 |
| Requests per day | 1,000 |
| Tokens per minute | 8,000 |
| Tokens per day | 200,000 |

The free plan limits can be lower. Look at the limits page in the Groq console for your account.

The limit of 8,000 tokens per minute is the most important constraint.
Each request sends the full message history. Thus, a long history uses the limit quickly.
The limit of 200,000 tokens per day permits only a small number of full runs each day.

### 3A.3 Design changes for the rate limits

1. Context compaction. Keep the full text of only the last three tool results. Replace older tool results with a short note of 1 to 3 lines.
2. Small tool outputs. Limit each tool result to approximately 1,500 tokens. Tell the LLM how to get the next part, for example with `start_line`.
3. Short system prompt. Keep the system prompt and the tool schemas below approximately 2,000 tokens.
4. Rate limiter. Read the `x-ratelimit-remaining-tokens` and `retry-after` headers. Wait before the next request if the remaining tokens are not sufficient.
5. Token log. Record the tokens for each request in the trace. Use the log to tune the compaction.
6. Self-evaluation budget. Run the evaluation on one test repository at a time. Use `llama-3.1-8b-instant` for fast tests of the loop only. Do not use it to measure precision and recall.

### 3A.4 Tool call failures

Groq reports some tool call problems:

- The API can return HTTP 400 with the code `tool_use_failed`. This occurs when the model makes a malformed tool call.
- The API rejects a call to a tool that is not in the request.
- If a bad tool call stays in the message history, the next requests can also fail.
- If `tool_choice` is `required`, the model sometimes does not call a tool. Then the API returns an error.

The agent does these steps for these problems:

1. Use `tool_choice: "auto"`. Do not use `required`.
2. Catch `BadRequestError` with the code `tool_use_failed`.
3. Do not add the failed message to the history.
4. Add a short user message: "Your last tool call was malformed. Send one valid tool call."
5. Send the request again. Do this a maximum of three times. Then stop with exit code 5.
6. If the model sends text and no tool call, tell it to call a tool or `finish`.
7. Validate the tool arguments with the schema in Python. Send validation errors to the LLM as the tool result.

### 3A.5 Parallel tool calls

The `gpt-oss` models do not support parallel tool calls. The `llama` models support them.
The loop must accept one or more tool calls in each response.
The loop runs the tool calls in sequence.

### 3A.6 SDK settings

- Install with `pip install groq`. The SDK needs Python 3.10 or later.
- The SDK reads `GROQ_API_KEY` from the environment.
- Use `python-dotenv` to load the `.env` file.
- Set `timeout=60` and `max_retries=3` on the client. The SDK retries 408, 409, 429, and 5xx errors with backoff.
- The SDK error classes are `AuthenticationError` (401), `RateLimitError` (429), `BadRequestError` (400), `APITimeoutError`, and `APIConnectionError`.

## 4. File structure

```
agent.py              CLI entry point
tools.py              Inspection tools
auditor/
  __init__.py
  llm.py              Groq client, rate limiter, tool_use_failed recovery
  state.py            Agent state (coverage, notes, candidates, seen lines)
  context.py          Makes the messages for each turn
  loop.py             Agent loop (observe, reason, select, execute, interpret)
  prompts.py          System prompt and evidence rules
  report.py           Violation model, validation, deduplication, JSON output
  redact.py           Secret redaction for tool output, logs, and report
  repo.py             Clone, URL validation, clean-up
  rules.py            Rule ID catalog (taxonomy only, not a checklist)
tests/
  test_tools.py
  test_loop.py        Uses a scripted fake LLM
  test_report.py
  test_redact.py
  test_cli.py
  fixtures/           Small test repositories with known defects
eval/
  repos/              Three local test repositories (A, B, C)
  expected/           Known defects for each test repository
  score.py            Calculates precision and recall
requirements.txt
pyproject.toml
.env.example
.gitignore
README.md
sample_report.json
```

## 5. Architecture

### 5.1 Agent cycle

The agent does this cycle:

1. Observe. The agent gets the file list and the last tool result.
2. Reason. The LLM decides what to examine next.
3. Select tool. The LLM sends a tool call.
4. Execute tool. Python runs the tool in the cloned copy.
5. Interpret results. The LLM reads the result. The LLM can record a violation or select a follow-up tool.
6. Report. The LLM calls `finish`. Python validates the violations and writes the JSON file.

### 5.2 Loop limits

The loop stops when one of these conditions occurs:

- The LLM calls `finish`.
- The step count is more than `MAX_STEPS` (default 40).
- The total time is more than `MAX_SECONDS` (default 900). This time includes the waits for the Groq rate limits.

If the loop stops at a limit, the agent writes the violations that it has.
The agent also writes a warning to stderr.

### 5.3 Trace

The agent records each step in `trace.jsonl`.
Each record has the step number, the tool name, the arguments, and a short result.
The redaction module removes secrets before the agent writes the record.
The evaluator can use the trace to examine the agent behavior.

## 6. Tools

### 6.1 Mandatory tools

| Tool | Function | Safety |
|---|---|---|
| `list_files(path=".", max_depth)` | Gives the file tree. Ignores `.git/`. | Stays in the repository root. |
| `read_file(path, start_line, end_line)` | Gives the file text with line numbers. | Rejects paths outside the root. Limits the size. Rejects binary files. Redacts secrets. |
| `search_code(pattern, glob)` | Gives the matched lines with file and line number. | Uses Python `re` with a timeout. Limits the number of results. Redacts secrets. |
| `git_log(max_count, path)` | Gives commits, authors, dates, and changed files. | Read-only git command. |

### 6.2 Optional tools

`AGENT_DESIGN.md` section 4 gives the full specification of each tool.

| Tool | Function |
|---|---|
| `git_tracked_ignored()` | Shows tracked files that agree with `.gitignore` or with common temporary file patterns. (`git status` is always clean in a new clone. Thus, this tool replaces `git_status`.) |
| `inspect_workflows()` | Gives a summary of `.github/workflows/*.yml` with line numbers: triggers, jobs, permissions, steps, action refs, secrets. |
| `inspect_dependencies()` | Gives each dependency and its constraint type. Lists the lockfiles. |
| `run_ruff()` | Runs `ruff check` on the copy. Gives a count for each rule. |
| `run_tests()` | Only with `--allow-exec`. Runs `pytest` in a Docker container without network access. |

### 6.3 Agent control tools

| Tool | Function |
|---|---|
| `record_violation(...)` | Records one violation with all report fields. |
| `update_violation(id, ...)` | Changes a recorded violation. |
| `retract_violation(id, reason)` | Removes a recorded violation. |
| `add_note(text)` | Keeps a fact after old tool results go away. |
| `update_coverage(area, status, reason)` | Changes the status of an inspection area. |
| `finish(summary)` | Ends the current phase. |

`record_violation` validates each violation immediately.
If the validation fails, the tool sends the error to the LLM. The LLM can then correct the violation.

## 7. Precision controls

The hidden evaluation measures precision and recall.
False positives decrease the precision. These controls decrease false positives:

1. Evidence validation. If a violation has a file and a line, the agent must have read or searched that file before. The line must be in the file.
2. Equivalent solutions. The system prompt gives a list of equivalent solutions. Examples:
   - `requirements.txt`, `pyproject.toml`, `Pipfile`, or `setup.cfg` for dependencies.
   - `ruff`, `flake8`, `pylint`, or `black` for lint and format.
   - `pre-commit` hooks or CI steps for lint.
3. Absent files. The agent must not report an absent optional file. It reports an absent file only if the repository needs it. Example: a lockfile is mandatory only for an application that uses Poetry or npm.
4. Branch protection. The system prompt prohibits findings about remote branch protection.
5. Deduplication. Python merges violations that have the same rule ID, file, and line.
6. Review step. Before `finish`, the LLM reads its list again. The LLM removes weak findings.

## 8. Recall controls

These controls increase recall:

1. The system prompt gives the nine inspection areas from the assignment.
2. The prompt tells the LLM to examine each area that applies to the repository.
3. The prompt does not give a fixed order. The LLM selects the order from the file list.
4. The prompt tells the LLM to examine CI workflows carefully. Subtle defects are frequently in CI. Examples:
   - `pull_request_target` with a checkout of the PR head.
   - `permissions: write-all` or no `permissions` block.
   - `continue-on-error: true` on test steps.
   - `|| true` after a test command.
   - Tests that do not run on `pull_request`.
   - Unpinned third-party actions.
   - Secrets in `echo` commands.
5. The prompt tells the LLM to use `git_log` to find secrets that a later commit removed.

## 9. Rule ID catalog

`rules.py` gives rule IDs for each category. Examples:

| Prefix | Category |
|---|---|
| `HYG-` | Repository hygiene |
| `DEP-` | Dependency management |
| `QUA-` | Code quality |
| `TST-` | Testing |
| `CI-` | GitHub Actions / CI |
| `SEC-` | Security |
| `CFG-` | Configuration |
| `COL-` | Collaboration |
| `BLD-` | Build and release |

The catalog gives names only. The catalog does not run checks.
The LLM can use a new ID with the correct prefix if no ID in the catalog applies.

## 10. Report contract

The report has this structure:

```json
{
  "repository": "<url>",
  "violations": [
    {
      "rule_id": "SEC-001",
      "category": "security",
      "severity": "critical",
      "file": "config.py",
      "line": 17,
      "description": "...",
      "evidence": "...",
      "recommendation": "..."
    }
  ]
}
```

`report.py` makes sure of these conditions:

- Each violation has all eight fields.
- `severity` is `critical`, `high`, `medium`, or `low`.
- `file` and `line` are JSON `null` if they do not apply.
- `line` is an integer or `null`.
- No field contains a secret value.
- The `violations` array is empty if the agent finds no violations.

## 11. Error handling

| Condition | Agent action | Exit code |
|---|---|---|
| Invalid URL | Write an error message to stderr. | 2 |
| Clone failure or timeout | Write an error message to stderr. | 3 |
| No `GROQ_API_KEY`, or Groq returns 401 | Write an error message to stderr. Do not show the key. | 4 |
| Groq 429, 5xx, or timeout | The SDK tries again three times with backoff. Then stop. | 5 |
| Groq 400 `tool_use_failed` | Do the steps in section 3A.4. | 5 if the failures continue |
| Tool error | Send the error text to the LLM. Continue the loop. | Not applicable |
| Malformed tool arguments | Send the error text to the LLM. Continue the loop. | Not applicable |
| Report complete | Write the JSON file. | 0 |

The agent deletes the temporary clone at the end, also after an error.

## 12. Tests

Use `pytest`. The tests do not use the real LLM API.

1. `test_tools.py`: Make small git repositories in `tmp_path`. Examine each tool. Examine path traversal rejection.
2. `test_loop.py`: Use a fake LLM that sends a fixed sequence of tool calls in the Groq response format. Make sure that the loop runs the tools, records violations, and stops at `finish` and at `MAX_STEPS`. Make sure that the loop recovers from a `tool_use_failed` error. Make sure that the context compaction keeps the history small.
3. `test_report.py`: Make sure that the validation rejects bad severity values and missing fields. Make sure that deduplication works.
4. `test_redact.py`: Make sure that the redaction removes AWS keys, GitHub tokens, private keys, and passwords.
5. `test_cli.py`: Make sure that the exit codes are correct for invalid input.

## 13. Self-evaluation

The hidden repositories are not available. Thus, make three local test repositories:

- Repository A: Obvious defects. Example: no README, a committed `.env` file, a hardcoded key.
- Repository B: Subtle defects. Example: unsafe CI triggers, `|| true` after tests, write permissions.
- Repository C: A healthy repository. The correct report has zero or very few violations.

`eval/score.py` runs the agent on each repository.
It compares the report with the known defects. It then calculates precision and recall.
Use the results to improve the system prompt.
Do not put the known defects into the prompt.

## 14. Work steps

| Step | Task | Output |
|---|---|---|
| 1 | Make the project structure, `pyproject.toml`, `requirements.txt`, `.gitignore`, `.env.example`. | Installable project |
| 2 | Write `repo.py` and `redact.py`. | Safe clone and redaction |
| 3 | Write `tools.py` with the mandatory and optional tools. | Tools |
| 4 | Write `report.py` and `rules.py`. | Validated report |
| 5 | Write `test_tools.py`, `test_report.py`, `test_redact.py`. | Tests pass |
| 6 | Write `llm.py` (Groq client, rate limiter, `tool_use_failed` recovery) and `prompts.py`. Examine the Groq limits for your account. | LLM client |
| 7 | Write `loop.py` and `agent.py`. | Working CLI |
| 8 | Write `test_loop.py` and `test_cli.py`. | Tests pass |
| 9 | Make the three test repositories and `score.py`. | Precision and recall values |
| 10 | Tune the prompt. Do the evaluation again. | Better scores |
| 11 | Run the agent on a real public repository. Save the output. | `sample_report.json` |
| 12 | Write `README.md` in STE. | Documentation |
| 13 | Push to a public GitHub repository. Clone it again in a new directory. Install it and run it. | Submission URL |

## 15. Rubric map

| Rubric item | Weight | Plan sections |
|---|---|---|
| Agent architecture and dynamic tool selection | 20% | 5, 6.3, 8 |
| Inspection tool implementation | 15% | 6 |
| Hidden repository detection | 30% | 7, 8, 13 |
| Evidence, severity, recommendations | 15% | 7, 10 |
| Reliability, error handling, safe execution | 10% | 2, 5.2, 11 |
| JSON compliance, tests, documentation | 10% | 10, 12, 14 |
