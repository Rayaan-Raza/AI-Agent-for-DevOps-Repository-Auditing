import pytest

from auditor.redact import redact, redact_obj, register_secret


@pytest.mark.parametrize(
    "secret",
    [
        "AKIAABCDEFGHIJKLMNOP",
        "ghp_" + "a" * 36,
        "github_pat_" + "A1b2" * 10,
        "xoxb-1234567890-abcdefghij",
        "sk_live_" + "x9" * 12,
        "AIza" + "b" * 35,
        "gsk_" + "Q" * 40,
        "sk-proj-" + "z" * 30,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkw.abcdefghijklmnop",
    ],
)
def test_specific_patterns(secret):
    out = redact(f"value = {secret} end")
    assert secret not in out
    assert "<REDACTED:" in out


def test_private_key_block():
    text = "a\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----\nb"
    out = redact(text)
    assert "MIIEow" not in out
    assert out.startswith("a\n<REDACTED:private_key real-looking>")
    assert out.endswith("\nb")


def test_long_base64_line_redacted():
    body = "A" * 64
    assert body not in redact(f"{body}\n")


def test_quoted_assignment_in_code():
    out = redact('API_KEY = "abc123def456"', path="app/config.py")
    assert "abc123def456" not in out
    assert 'API_KEY = "<REDACTED:generic_secret len=12 real-looking>"' == out


def test_json_style_and_placeholder_hint():
    out = redact('{"password": "changeme123"}', path="x.json")
    assert "changeme123" not in out
    assert "placeholder-like" in out


def test_env_style_unquoted():
    out = redact("DB_PASSWORD=hunter2hunter2\nDEBUG=1\n", path=".env")
    assert "hunter2hunter2" not in out
    assert "DEBUG=1" in out


def test_unquoted_in_code_not_redacted():
    # A Python reference is not a secret.
    text = "token = get_token()\nSECRET_KEY = settings.SECRET_KEY\n"
    assert redact(text, path="a.py") == text


def test_references_not_redacted():
    text = 'password = os.environ["DB_PASSWORD"]\n  TOKEN: ${{ secrets.DEPLOY_TOKEN }}\napi_key: ${API_KEY}\n'
    assert redact(text, path="ci.yml") == text


def test_url_credentials():
    out = redact("postgres://admin:s3cretpw@db.local:5432/app")
    assert "s3cretpw" not in out
    assert "admin:<REDACTED:url_password real-looking>@db.local" in out


def test_runtime_secret_and_obj():
    register_secret("my-runtime-secret-value")
    out = redact_obj({"a": ["x my-runtime-secret-value y"], "b": 3})
    assert out == {"a": ["x <REDACTED:runtime_secret> y"], "b": 3}


def test_empty_and_plain_text_unchanged():
    assert redact("") == ""
    plain = "def add(a, b):\n    return a + b\n"
    assert redact(plain, path="m.py") == plain


def test_hints_on_every_redaction():
    assert redact('k = "AKIAQ3XK7PLM2RTV9WNB"', path="a.py") == 'k = "<REDACTED:aws_access_key real-looking>"'
    assert "placeholder-like" in redact('k = "AKIAIOSFODNN7EXAMPLE"', path="a.py")
    assert "url_password real-looking" in redact("postgres://u:Pr0dPassw0rd2024@h/db")
    assert "placeholder-like" in redact("SECRET_KEY=changeme", path=".env")
