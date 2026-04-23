from __future__ import annotations

"""Redactor de secretos para sourcecode.

Aplica patrones regex sobre los valores de texto del output JSON/YAML.
Activo por defecto; desactivable con --no-redact.

NOTA (Fase 1): La redaccion actua sobre el OUTPUT (nombres de ficheros, paths,
metadatos) — no sobre el contenido de ficheros. En Fase 1 el output no incluye
contenido de ficheros, por lo que la redaccion es principalmente una red de
seguridad. En Fases 2+ cuando se lean manifiestos, la redaccion sera mas critica.
"""

import re
from typing import Any

REDACTED = "[REDACTED]"

# Patrones de secretos conocidos (compilados en modulo-load para rendimiento)
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                      # GitHub PAT
    re.compile(r"sk-proj-[A-Za-z0-9\-_]{50,}"),              # OpenAI project key (mas especifico primero)
    re.compile(r"sk-[A-Za-z0-9]{48}"),                        # OpenAI legacy key
    re.compile(r"AKIA[0-9A-Z]{16}"),                          # AWS Access Key ID
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*"),           # Bearer tokens
]

# Patrones de nombres de fichero que deben excluirse (SEC-02)
_EXCLUDE_FILENAME_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\.env(\..+)?$"),   # .env, .env.local, .env.production, etc.
    re.compile(r"^.+\.secret$"),     # cualquier fichero *.secret
]


def redact_value(value: str) -> str:
    """Aplica todos los patrones de secreto sobre un string."""
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def redact_dict(data: Any) -> Any:
    """Redaccion recursiva sobre el dict/list del output.

    - str: aplica patrones
    - dict: redacta cada valor
    - list: redacta cada elemento
    - None, int, float, bool: retorna sin modificar
    """
    if isinstance(data, str):
        return redact_value(data)
    elif isinstance(data, dict):
        return {k: redact_dict(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [redact_dict(item) for item in data]
    # None, int, float, bool — sin modificar
    return data


class SecretRedactor:
    """Redactor configurable de secretos.

    Args:
        enabled: Si False, redact() retorna los datos sin modificar (--no-redact).
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def redact(self, data: Any) -> Any:
        """Redacta secretos del dict si esta habilitado."""
        if not self.enabled:
            return data
        return redact_dict(data)

    @staticmethod
    def should_exclude_file(filename: str) -> bool:
        """Determina si un fichero debe excluirse del analisis de contenido (SEC-02).

        Excluye: .env, .env.*, *.secret
        """
        return any(pattern.match(filename) for pattern in _EXCLUDE_FILENAME_PATTERNS)
