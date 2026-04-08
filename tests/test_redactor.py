"""Tests unitarios del redactor de secretos."""

from sourcecode.redactor import SecretRedactor, redact_dict, redact_value

# Tokens validos que DEBEN ser redactados
VALID_GHP = "ghp_" + "A" * 36          # GitHub PAT: ghp_ + 36 alfanumericos
VALID_SK = "sk-" + "A" * 48            # OpenAI key: sk- + 48 alfanumericos
VALID_SK_PROJ = "sk-proj-" + "A" * 50  # OpenAI project key
VALID_AKIA = "AKIA" + "A" * 16         # AWS Access Key ID: AKIA + 16 mayusculas/digits
VALID_BEARER = "Bearer abc123token_XYZ"


def test_secret_patterns():
    result = redact_value(f"token={VALID_GHP}")
    assert "[REDACTED]" in result
    assert VALID_GHP not in result


def test_sk_openai_pattern():
    result = redact_value(f"key={VALID_SK}")
    assert "[REDACTED]" in result
    assert VALID_SK not in result


def test_sk_proj_pattern():
    result = redact_value(f"key={VALID_SK_PROJ}")
    assert "[REDACTED]" in result


def test_akia_pattern():
    result = redact_value(f"aws_key={VALID_AKIA}")
    assert "[REDACTED]" in result
    assert VALID_AKIA not in result


def test_bearer_pattern():
    result = redact_value(f"Authorization: {VALID_BEARER}")
    assert "[REDACTED]" in result


def test_no_false_positive_normal_text():
    normal = "src/main.py"
    assert redact_value(normal) == normal


def test_no_false_positive_short_sk():
    # "sk-" con menos de 48 chars no debe redactarse
    short = "sk-abc"
    result = redact_value(short)
    assert result == short


def test_redact_dict_recursive():
    data = {
        "token": VALID_GHP,
        "nested": {"api_key": VALID_SK},
        "list_field": [f"Bearer {VALID_BEARER}"],
    }
    result = redact_dict(data)
    assert "[REDACTED]" in result["token"]
    assert "[REDACTED]" in result["nested"]["api_key"]
    assert "[REDACTED]" in result["list_field"][0]


def test_redact_dict_preserves_none():
    data = {"file.py": None, "dir": {"nested.py": None}}
    result = redact_dict(data)
    assert result["file.py"] is None
    assert result["dir"]["nested.py"] is None


def test_redact_dict_preserves_numbers():
    data = {"count": 42, "ratio": 3.14}
    result = redact_dict(data)
    assert result["count"] == 42
    assert result["ratio"] == 3.14


def test_env_file_excluded():
    redactor = SecretRedactor()
    assert redactor.should_exclude_file(".env") is True
    assert redactor.should_exclude_file(".env.local") is True
    assert redactor.should_exclude_file(".env.production") is True


def test_secret_file_excluded():
    redactor = SecretRedactor()
    assert redactor.should_exclude_file("database.secret") is True
    assert redactor.should_exclude_file("credentials.secret") is True


def test_normal_file_not_excluded():
    redactor = SecretRedactor()
    assert redactor.should_exclude_file("main.py") is False
    assert redactor.should_exclude_file("config.json") is False
    assert redactor.should_exclude_file(".gitignore") is False


def test_no_redact_disabled():
    redactor = SecretRedactor(enabled=False)
    original = {"token": VALID_GHP}
    result = redactor.redact(original)
    assert result["token"] == VALID_GHP  # sin redactar
