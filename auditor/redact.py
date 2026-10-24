"""Secret redaction.

Every string that leaves a tool, goes to the trace, goes to stderr or goes into
the report passes through ``redact``. The LLM therefore never sees a secret
value and cannot copy one into the report.
"""

from __future__ import annotations

import re

PLACEHOLDER_WORDS = (
    "changeme",
    "change_me",
    "example",
    "your_",
    "your-",
    "yourkey",
    "xxx",
    "dummy",
    "placeholder",
    "replace",
    "todo",
    "secret_here",
    "insert",
    "sample",
    "fake",
    "test",
    "redacted",
    "none",
    "null",
    "<",
    "***",
    "...",
)

# Files where an unquoted "NAME=value" or "name: value" is a literal value.
_CONFIG_SUFFIXES = (
    ".env",
    ".ini",
    ".cfg",
    ".conf",
    ".properties",
    ".yml",
    ".yaml",
    ".toml",
    ".json",
    ".txt",
    ".sh",
    ".bash",
    ".ps1",
    ".tf",
    ".tfvars",
    ".dockerfile",
)
_CONFIG_NAMES = ("dockerfile", "makefile", "procfile")

_SPECIFIC = [
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("stripe_key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("groq_api_key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}")),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_api_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
]

_URL_CREDS = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@'\"]+):([^/\s@'\"]{3,})@")

_NAME = (
    r"[A-Za-z0-9_.-]*(?:secret|token|passw(?:or)?d|pwd|api[_-]?key|access[_-]?key"
    r"|private[_-]?key|credential|auth[_-]?key|client[_-]?secret)[A-Za-z0-9_.-]*"
)

# name = "value"  /  "name": "value"  /  name: 'value'
_QUOTED = re.compile(
    r"(?i)(?P<pre>\b" + _NAME + r"[\"']?\s*(?::=|=>|[:=])\s*)(?P<q>[\"'])(?P<val>[^\"'\n]{6,})(?P=q)"
)
# NAME=value at the start of a line (env files, shell, YAML). Only used for config-like files.
_UNQUOTED = re.compile(
    r"(?im)^(?P<pre>[ \t]*(?:export[ \t]+)?-?[ \t]*" + _NAME + r"[ \t]*[:=][ \t]*)"
    r"(?P<val>[^\s\"'#$<{(\[][^\s#\"'(\[]{5,})(?=[ \t]*(?:#|$))"
)
# Long base64 lines, for example the body of a private key read in parts.
_B64_LINE = re.compile(r"(?m)^(?P<pre>\s*\d*\s*)(?P<val>[A-Za-z0-9+/]{60,}={0,2})\s*$")

_known: set[str] = set()


def register_secret(value: str | None) -> None:
    """Always redact this exact value (for example the GROQ_API_KEY of this run)."""
    if value and len(value) >= 6:
        _known.add(value)


def _placeholder_hint(value: str) -> str:
    low = value.lower()
    if any(w in low for w in PLACEHOLDER_WORDS) or len(set(low)) <= 2:
        return " placeholder-like"
    return ""


def _is_config(path: str | None) -> bool:
    if path is None:
        return True  # Unknown origin (diffs, mixed text): be strict.
    low = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return (
        low.startswith(".env")
        or low.endswith(_CONFIG_SUFFIXES)
        or low in _CONFIG_NAMES
        or low.startswith("dockerfile")
    )


def _looks_like_reference(value: str) -> bool:
    """True for values that are code references, not literal secrets."""
    if value.lower() in ("true", "false", "null", "none", "required", "optional"):
        return True
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", value))


def redact(text: str, path: str | None = None) -> str:
    """Return ``text`` with secret values replaced by ``<REDACTED:type ...>``."""
    if not text:
        return text
    for secret in _known:
        text = text.replace(secret, "<REDACTED:runtime_secret>")
    for kind, pattern in _SPECIFIC:
        text = pattern.sub(f"<REDACTED:{kind}>", text)
    text = _URL_CREDS.sub(r"\1:<REDACTED:url_password>@", text)

    def quoted(m: re.Match) -> str:
        val = m.group("val")
        if val.startswith(("${", "{{", "<REDACTED")) or _looks_like_reference(val):
            return m.group(0)
        q = m.group("q")
        tag = f"<REDACTED:generic_secret len={len(val)}{_placeholder_hint(val)}>"
        return f"{m.group('pre')}{q}{tag}{q}"

    text = _QUOTED.sub(quoted, text)

    if _is_config(path):

        def unquoted(m: re.Match) -> str:
            val = m.group("val")
            if val.startswith("<REDACTED") or _looks_like_reference(val):
                return m.group(0)
            return f"{m.group('pre')}<REDACTED:generic_secret len={len(val)}{_placeholder_hint(val)}>"

        text = _UNQUOTED.sub(unquoted, text)

    text = _B64_LINE.sub(lambda m: f"{m.group('pre')}<REDACTED:base64_blob>", text)
    return text


def redact_obj(obj):
    """Redact every string inside a JSON-like object."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    return obj
