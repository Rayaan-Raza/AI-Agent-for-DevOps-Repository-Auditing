# Contributing

This document is written in ASD-STE100 Simplified Technical English (STE).

## 1. Branches

- Do not push directly to `main`.
- Make a branch from `main` for each change.
- Use a prefix that tells the type of change:

| Prefix | Use |
|---|---|
| `feat/` | New behavior |
| `fix/` | Error correction |
| `docs/` | Documentation only |
| `ci/` | CI or tooling |
| `test/` | Tests only |

Example: `feat/openai-compatible-provider`.

## 2. Local checks

Do these steps before you push:

1. Install the dependencies.

   ```bash
   pip install -r requirements.txt
   ```

2. Run the linter and the formatter.

   ```bash
   ruff check .
   ruff format --check .
   ```

3. Run the tests. The tests do not need an API key.

   ```bash
   pytest
   ```

## 3. Pull requests

1. Push your branch.
2. Open a pull request to `main`.
3. Complete the checklist in the pull request template.
4. Make sure that all CI checks pass.
5. Get one review. Then merge.

## 4. Commits

- Write one commit for each logical change.
- Start the message with a type: `feat:`, `fix:`, `docs:`, `test:`, `ci:`, `perf:`, or `chore:`.
- Write the message in the imperative mood. Example: `fix: reject paths outside the clone`.

## 5. Secrets

- Never commit `.env` or an API key.
- Put new configuration names in `.env.example` with no value.
