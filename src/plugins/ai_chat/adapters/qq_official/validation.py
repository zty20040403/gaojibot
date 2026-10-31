"""Small shared validators and fail-closed errors for official authority stores."""
from __future__ import annotations

import unicodedata
from functools import wraps

from psycopg import Error as DatabaseError
from src.bot_storage.database import DatabaseError as StoreDatabaseError


class Denied(PermissionError):
    def __init__(self):
        super().__init__('authority_denied')


class Unavailable(RuntimeError):
    """The store could not answer; never treated as an allow."""

    def __init__(self):
        super().__init__('authority_unavailable')


def _deny(*args, **kwargs):
    return False


def _positive(value: object) -> bool:
    return type(value) is int and 1 <= value <= 2**63-1


def _text(value: object, limit: int) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return (len(value.encode('utf-8')) <= limit and not any(
            unicodedata.category(char) in {'Cc', 'Cf', 'Cs'} for char in value))
    except UnicodeEncodeError:
        return False


def _safe_database_errors(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (DatabaseError, StoreDatabaseError):
            # SQL details can contain full actor keys; callers may retry but never allow.
            raise Unavailable() from None
    return call
