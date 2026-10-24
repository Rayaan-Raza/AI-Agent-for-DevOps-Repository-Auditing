# DevOps Repository Auditing Agent

This document is written in ASD-STE100 Simplified Technical English (STE).

This agent examines a public GitHub repository. It finds DevOps best-practice violations and writes a JSON report.
An LLM selects each inspection tool from the results of the tools before it. The agent does not use a fixed checklist.

The agent does not use an agent framework (no LangChain, LangGraph, or n8n).
The agent loop, the tool dispatch, and the control flow are in Python. The only LLM library is the Groq SDK.

## 1. Requirements

- Python 3.11 or later
- `git` on the `PATH`
- A Groq API key (<https://console.groq.com/keys>)
- Optional: Docker, only for `--allow-exec`

## 2. Installation

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

## 3. Setup

1. Copy `.env.example` to `.env`.

   ```bash
   cp .env.example .env
   ```

2. Open `.env`. Set `GROQ_API_KEY` to your key.
3. Optional: set the other values. If a value is empty, the agent uses the default.

| Variable | Default | Description |
|---|---|---|
| `GROQ_API_KEY` | (none, mandatory) | Groq API key |
| `LLM_MODEL` | `openai/gpt-oss-120b` | Groq model ID. `openai/gpt-oss-20b` is faster. |
| `LLM_REASONING_EFFORT` | `low` | `low`, `medium`, or `high`. Only for reasoning models. A higher value uses more tokens. |
| `MAX_STEPS` | `35` | Maximum number of LLM steps in the investigation |
| `MAX_SECONDS` | `1800` | Maximum run time in seconds |

You can also set these values as environment variables. The `.env` file is in `.gitignore`. Do not commit it.

## 4. Execution

Run this command:

```bash
python agent.py --repo https://github.com/<owner>/<repo> --output report.json
```

The agent runs without interactive prompts. It writes two files:

- `report.json`: the report.
- `report.trace.jsonl`: one record for each tool call. Each record has the tool, the arguments, the reason of the LLM for the call, and a part of the result.

### 4.1 Options

| Option | Description |
|---|---|
| `--repo URL` | Mandatory. `https://github.com/<owner>/<repo>` |
| `--output PATH` | Mandatory. Path of the JSON report |
| `--trace PATH` | Path of the trace file. Default: `<output>.trace.jsonl` |
| `--model ID` | Groq model ID. This value replaces `LLM_MODEL`. |
| `--max-steps N` | Step limit. This value replaces `MAX_STEPS`. |
| `--max-seconds N` | Time limit. This value replaces `MAX_SECONDS`. |
| `--allow-exec` | Let the agent run `pytest` in a Docker container without network access |
| `--keep-low-confidence` | Keep findings that the LLM marked as low confidence |
| `-q`, `--quiet` | Show only errors |

### 4.2 Exit codes

| Code | Meaning |
|---|---|
| 0 | The inspection is complete. The report is written. The `violations` array can be empty. |
| 1 | Unexpected error |
| 2 | Invalid input (bad URL, missing argument, output path is a directory) |
| 3 | The clone failed (the repository does not exist, is private, or the clone timed out) |
| 4 | The API key is missing or the API rejected it |
| 5 | LLM API failure (rate limit not cleared, server error, malformed tool calls continued) |
| 6 | The agent cannot write the report |

If the exit code is not 0, the agent writes an error message to stderr. It does not write a report.

### 4.3 Run time

The Groq free tier gives approximately 8,000 tokens per minute. Thus, one audit takes approximately 10 to 25 minutes.
The agent reads the rate-limit headers and waits automatically. With a paid Groq plan, an audit is much faster.

## 5. Report format

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

- `severity` is `critical`, `high`, `medium`, or `low`.
- `file` and `line` are `null` if they do not apply.
- The report never contains a secret value.
- `category` is one of: `repository_hygiene`, `dependency_management`, `code_quality`, `testing`, `ci`, `security`, `configuration`, `collaboration`, `build_release`.

`sample_report.json` is a real output of the agent.

## 6. Architecture

### 6.1 Phases

| Phase | Name | Control | Function |
|---|---|---|---|
| 0 | Setup | Python | Validate the URL. Clone the repository into a temporary directory. Make a profile of facts (file types, key files that are present or absent). |
| 1 | Investigation | LLM | The LLM selects tools, reads the results, and records violations. |
| 2 | Self-review | LLM | The LLM examines each candidate violation again. It removes weak findings or duplicates. |
| 3 | Output | Python | Validate, deduplicate, redact, sort, and write the report. Delete the clone. |

### 6.2 Agent cycle

```
 observe ──> reason ──> select tool ──> execute tool ──> interpret result ──┐
    ^                                                                       │
    └───────────────────────────────────────────────────────────────────────┘
                                   (until finish) ──> self-review ──> report
```

1. Observe. Python sends an AUDIT STATE message and the newest tool results.
2. Reason. The LLM decides what to examine next.
3. Select tool. The LLM sends a tool call. Each investigation tool has a mandatory `reason` argument.
4. Execute tool. Python validates the arguments and runs the tool on the clone.
5. Interpret results. The LLM reads the result. It records a violation, keeps a note, or selects a follow-up tool.
6. Report. The LLM calls `finish`. Python validates the findings and writes the report.

### 6.3 Context management

The Groq free tier has a small token limit. Thus, the agent does not send the full history.
For each request, Python makes these messages:

- The system prompt (method, evidence rules, severity rules, rule IDs).
- The AUDIT STATE: the profile, the status of the nine areas, the notes, the files already seen, the candidate violations, and one line for each older step.
- The newest turns that fit in the token budget (maximum six).

If the LLM repeats an identical tool call, Python does not run it again. Python sends the old result with a warning.

### 6.4 Tools

| Tool | Function |
|---|---|
| `list_files` | List tracked files with size and line count |
| `read_file` | Read a file with line numbers (secrets are redacted) |
| `search_code` | Regular-expression search with a timeout for each line |
| `git_log` | Commit history. It can find commits that add or remove a text (`git log -S`). |
| `inspect_workflows` | Facts about GitHub Actions workflows with line numbers: triggers, permissions, steps, action refs, secrets, untrusted expressions |
| `inspect_dependencies` | Each dependency with its constraint type, the lockfiles, and conflicts between files |
| `git_tracked_ignored` | Tracked files that agree with `.gitignore` or with common temporary or secret patterns |
| `run_ruff` | Static lint. It does not run the code. |
| `run_tests` | Only with `--allow-exec`: `pytest` in Docker without network access |
| `add_note`, `update_coverage` | Agent memory and area status |
| `record_violation`, `update_violation`, `retract_violation` | Candidate violations |
| `finish` | End the current phase |

The analysis tools give facts only. They do not decide that something is a violation. The LLM decides.

### 6.5 Precision controls

`record_violation` validates each finding immediately. It rejects a finding if:

- The file is not in the repository.
- The line is not in the file.
- The LLM did not see the line through a tool.
- The finding says that a file is absent, but a file agrees with the given pattern.
- The text is about branch protection. Files cannot show this setting.
- It is a duplicate of a different finding.

The system prompt also gives equivalent solutions. For example, `pyproject.toml` is equivalent to `requirements.txt`.
Thus, the agent does not report a missing optional file when an equivalent is present.

### 6.6 Safety

- The agent clones into a temporary directory. It never changes the remote repository.
- Git hooks, submodules, and Git LFS downloads are disabled during the clone.
- The tools only read files. No tool runs code from the repository. `run_tests` is the only exception. It runs only with `--allow-exec`, in Docker, with no network, and with limits for memory and time.
- All paths are resolved and must stay in the clone. Symbolic links that point out of the clone are rejected.
- `redact.py` removes secrets from tool results, the trace, stderr, and the report. The LLM never sees a secret value.
- Each tool has a time limit. A tool error goes to the LLM as text. It does not stop the agent.

### 6.7 Modules

| File | Function |
|---|---|
| `agent.py` | CLI entry point and exit codes |
| `tools.py` | Tool functions, tool schemas, argument validation, and dispatch |
| `auditor/repo.py` | URL validation, safe clone, file index, profile |
| `auditor/loop.py` | Investigation loop, self-review, repeat detection, trace |
| `auditor/context.py` | Messages for each turn within the token budget |
| `auditor/llm.py` | Groq client, rate limiter, error mapping |
| `auditor/prompts.py` | System prompt and review prompt |
| `auditor/report.py` | Finding validation, deduplication, JSON schema, atomic write |
| `auditor/redact.py` | Secret redaction |
| `auditor/state.py` | Agent state |
| `auditor/rules.py` | Rule ID catalog (names only, no checks) |

`docs/AGENT_DESIGN.md` gives the full design. `docs/PLAN.md` gives the work plan.

## 7. Tests

Run the tests:

```bash
pytest
```

The tests do not use the Groq API. A scripted fake LLM tests the loop.
The tests make small git repositories in a temporary directory. They examine:

- Each tool, path traversal rejection, and regular-expression timeouts.
- Secret redaction.
- Finding validation, deduplication, and the JSON schema.
- The Groq error mapping and the rate limiter.
- The loop: dynamic tool selection, rejected findings, repeat detection, malformed tool calls, step and time limits, and self-review.
- The CLI: exit codes, an end-to-end run, and that the clone source does not change.

## 8. Self-evaluation

`eval/fixtures.py` has three test repositories with known defects:

- `repo_a_obvious`: obvious defects.
- `repo_b_subtle`: subtle CI, security, and configuration defects.
- `repo_c_healthy`: a healthy repository. It tests false positives.

Run the evaluation (this uses the Groq API):

```bash
python eval/score.py
```

The script builds the repositories, runs the agent on each one, and gives the precision and the recall.
The agent does not read these files. No finding is hardcoded in the agent.

## 9. Limits

- The agent supports public GitHub repositories only.
- The agent does not examine remote settings, for example branch protection.
- `run_tests` needs Docker and `--allow-exec`. Without it, the agent does not report failed tests.
- The quality of the findings depends on the LLM. The validation rejects unsupported findings, but it cannot prove that each finding is correct.
