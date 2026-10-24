"""CLI entry point.

    python agent.py --repo https://github.com/<owner>/<repo> --output report.json

Exit codes: 0 report written, 1 unexpected error, 2 invalid input, 3 clone failure,
4 missing or rejected API key, 5 LLM API failure, 6 report could not be written,
130 interrupted.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from auditor.redact import redact, register_secret


def log(msg: str) -> None:
    print(redact(msg), file=sys.stderr, flush=True)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="agent.py",
        description="Audit a public GitHub repository for DevOps best-practice violations.",
    )
    p.add_argument("--repo", required=True, help="https://github.com/<owner>/<repo>")
    p.add_argument("--output", required=True, help="path of the JSON report to write")
    p.add_argument("--trace", help="path of the JSONL tool-call trace (default: <output>.trace.jsonl)")
    p.add_argument("--model", help="Groq model ID (default: LLM_MODEL or openai/gpt-oss-120b)")
    p.add_argument("--max-steps", type=int, help="investigation step limit (default: MAX_STEPS or 40)")
    p.add_argument("--max-seconds", type=int, help="time limit in seconds (default: MAX_SECONDS or 1800)")
    p.add_argument(
        "--allow-exec",
        action="store_true",
        help="allow run_tests (pytest in a Docker container without network)",
    )
    p.add_argument(
        "--keep-low-confidence", action="store_true", help="keep low-confidence findings in the report"
    )
    p.add_argument("--allow-local", action="store_true", help=argparse.SUPPRESS)  # local test repos
    p.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    return p.parse_args(argv)


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env")


def main(argv: list[str] | None = None, llm_factory=None) -> int:
    args = parse_args(argv)
    load_env()
    say = (lambda m: None) if args.quiet else log

    from auditor.llm import DEFAULT_MODEL, GroqClient, LLMAuthError, LLMError
    from auditor.loop import AuditLoop, LoopConfig, Trace
    from auditor.repo import RepoError, clone, normalize_url
    from auditor.report import build_report, write_report
    from auditor.state import AuditState

    # 1. Validate the input before any slow work.
    try:
        if not (args.allow_local and os.path.isdir(args.repo)):
            normalize_url(args.repo)
    except RepoError as e:
        log(f"error: {e}")
        return e.exit_code
    out_dir = os.path.dirname(os.path.abspath(args.output))
    if os.path.isdir(args.output):
        log(f"error: --output {args.output!r} is a directory.")
        return 2

    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    register_secret(api_key)
    if llm_factory is None and not api_key:
        log("error: GROQ_API_KEY is not set. Copy .env.example to .env and set the key.")
        return 4

    model = args.model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    effort = os.environ.get("LLM_REASONING_EFFORT") or "medium"
    config = LoopConfig(
        max_steps=args.max_steps or env_int("MAX_STEPS", 40),
        max_seconds=args.max_seconds or env_int("MAX_SECONDS", 1800),
        allow_exec=args.allow_exec,
    )
    trace_path = args.trace or os.path.splitext(args.output)[0] + ".trace.jsonl"

    repo = None
    trace = None
    t0 = time.monotonic()
    try:
        llm = (
            llm_factory()
            if llm_factory
            else GroqClient(api_key=api_key, model=model, reasoning_effort=effort, log=say)
        )
        say(f"cloning {args.repo} ...")
        repo = clone(args.repo, allow_local=args.allow_local)
        say(f"cloned: {repo.profile['files']} files, {repo.profile['commits']} commits. model: {llm.model}")
        os.makedirs(out_dir, exist_ok=True)
        trace = Trace(trace_path)
        state = AuditState(repo=repo)
        result = AuditLoop(state, llm, config, trace, log=say).run()
        if result.stop_reason != "finished":
            log(
                f"warning: investigation stopped early ({result.stop_reason}); the report has the "
                "findings recorded so far."
            )
        report_url = args.repo if args.allow_local else normalize_url(args.repo)
        report = build_report(state, report_url, drop_low_confidence=not args.keep_low_confidence)
        try:
            write_report(report, args.output)
        except OSError as e:
            log(f"error: cannot write the report: {e}")
            return 6
        say(
            f"report written: {args.output} ({len(report['violations'])} violations, "
            f"{result.steps} steps, {time.monotonic() - t0:.0f} s). trace: {trace_path}"
        )
        return 0
    except RepoError as e:
        log(f"error: {e}")
        return e.exit_code
    except LLMAuthError as e:
        log(f"error: {e}")
        return 4
    except LLMError as e:
        log(f"error: LLM failure: {e}")
        return 5
    except KeyboardInterrupt:
        log("interrupted.")
        return 130
    except Exception as e:  # noqa: BLE001 - last line of defense: never print a traceback with secrets
        log(f"error: unexpected {type(e).__name__}: {str(e)[:300]}")
        return 1
    finally:
        if trace:
            trace.close()
        if repo:
            repo.cleanup()


if __name__ == "__main__":
    sys.exit(main())
