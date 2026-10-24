"""Rule ID catalog. Names only: the catalog does not run checks.

The LLM picks an ID from here, or makes a new one with the correct prefix.
"""

CATEGORIES = {
    "repository_hygiene": "HYG",
    "dependency_management": "DEP",
    "code_quality": "QUA",
    "testing": "TST",
    "ci": "CI",
    "security": "SEC",
    "configuration": "CFG",
    "collaboration": "COL",
    "build_release": "BLD",
}

PREFIX_TO_CATEGORY = {v: k for k, v in CATEGORIES.items()}

RULES = {
    "HYG-001": "Missing README",
    "HYG-002": "Missing or unsuitable .gitignore",
    "HYG-003": "Tracked temporary, cache or generated files",
    "HYG-004": "Tracked vendored dependencies or virtual environment",
    "HYG-005": "Large binary or data files committed without need",
    "DEP-001": "Dependencies without version constraints in an application",
    "DEP-002": "Missing lockfile where the tool chain needs one",
    "DEP-003": "Inconsistent dependency specifications between files",
    "DEP-004": "Dependency from an unpinned URL or branch",
    "QUA-001": "No linting or formatting configuration or enforcement",
    "QUA-002": "Obvious maintainability problem (dead code, wildcard imports, bare except)",
    "TST-001": "No automated tests",
    "TST-002": "Tests are not run in CI",
    "TST-003": "Tests do not run on pull requests",
    "TST-004": "Failing tests",
    "CI-001": "Incorrect or missing workflow triggers",
    "CI-002": "Test or check failures ignored (|| true, continue-on-error)",
    "CI-003": "Validation step skipped or disabled",
    "CI-004": "Third-party action pinned to a mutable branch",
    "CI-005": "Invalid workflow file",
    "SEC-001": "Hardcoded credential in source code",
    "SEC-002": "Committed secret file (.env, key, certificate)",
    "SEC-003": "Secret in git history",
    "SEC-004": "Excessive workflow token permissions",
    "SEC-005": "pull_request_target or workflow_run runs untrusted PR code",
    "SEC-006": "Script injection from untrusted event data in a run step",
    "SEC-007": "Secret printed or exposed in workflow logs",
    "SEC-008": "Insecure default (debug on, TLS verification off)",
    "CFG-001": "Machine-specific absolute path",
    "CFG-002": "Committed local IDE or tool settings",
    "CFG-003": "No separation of environment configuration",
    "COL-001": "Unsafe pull request automation (auto-merge without checks)",
    "COL-002": "No contribution or review guidance where relevant",
    "BLD-001": "No build validation",
    "BLD-002": "Risky release or deploy automation (no gate, deploy on every push)",
    "BLD-003": "Docker image uses a mutable or latest tag, or runs as root",
}


def catalog_text() -> str:
    lines = []
    for cat, prefix in CATEGORIES.items():
        ids = ", ".join(f"{k} {v}" for k, v in RULES.items() if k.startswith(prefix + "-"))
        lines.append(f"{prefix} ({cat}): {ids}")
    return "\n".join(lines)
