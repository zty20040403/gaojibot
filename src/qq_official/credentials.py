"""Protected credentials and narrowly scoped endpoint validation."""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

DEFAULT_API_BASE = "https://api.bot.qq.com"
# Explicit compatibility address; target-account sandbox behavior is unverified.
# Never infer sandbox from an authentication error or switch environments.
SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"
_API_HOSTS = frozenset({"api.bot.qq.com", "api.sgroup.qq.com", "sandbox.api.sgroup.qq.com"})
_LOOPBACK = frozenset({"127.0.0.1", "::1"})


class CredentialError(ValueError):
    """Safe diagnostic; intentionally excludes filesystem paths and values."""


def verify_private_file(fd: int, *, acl_validator=None) -> None:
    """Only require a regular file which is not world-readable."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise CredentialError("Credential storage must be a regular file")
    if os.name != 'nt' and info.st_mode & stat.S_IROTH:
        raise CredentialError("Credential file must not be world-readable")


def _read_secret(path: str | Path, acl_validator=None) -> str:
    try:
        with open(path, 'r', encoding='utf-8') as stream:
            verify_private_file(stream.fileno())
            return stream.read().strip()
    except (OSError, UnicodeError):
        raise CredentialError("Credential file could not be read") from None


@dataclass(frozen=True)
class Credentials:
    app_id: str = field(repr=False)
    app_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        for value in (self.app_id, self.app_secret):
            if not isinstance(value, str) or not value or len(value) > 16384 or re.search(r"\s", value):
                raise CredentialError("Credential value is missing or invalid")

    @classmethod
    def from_files(
        cls,
        app_id_file: str | Path,
        app_secret_file: str | Path,
        *,
        acl_validator: Callable[[int], bool] | None = None,
    ) -> Credentials:
        return cls(_read_secret(app_id_file, acl_validator), _read_secret(app_secret_file, acl_validator))

    @property
    def is_test(self) -> bool:
        return self.app_id.startswith("TEST_") and self.app_secret.startswith("TEST_")


def _validate_url(
    url: str, *, websocket: bool, allow_test_server: bool, credentials: Credentials | None,
) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise CredentialError("Invalid platform endpoint") from None
    if (
        not parsed.hostname or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or "\\" in url or re.search(r"\s", url)
    ):
        raise CredentialError("Invalid platform endpoint")
    if allow_test_server:
        if credentials is None or not credentials.is_test:
            raise CredentialError("Local test transport requires TEST_ credentials")
        if parsed.hostname not in _LOOPBACK or parsed.scheme != ("ws" if websocket else "http"):
            raise CredentialError("Test transport only permits literal loopback endpoints")
    elif (
        parsed.scheme != ("wss" if websocket else "https")
        or parsed.hostname not in _API_HOSTS or port not in {None, 443}
    ):
        raise CredentialError("Untrusted platform endpoint")
    if not websocket and parsed.path not in {"", "/"}:
        raise CredentialError("API base must not contain a path")
    return url.rstrip("/") if not websocket else url


def validate_api_base(
    url: str, *, allow_test_server: bool = False, credentials: Credentials | None = None,
) -> str:
    return _validate_url(url, websocket=False, allow_test_server=allow_test_server, credentials=credentials)


def validate_gateway_url(
    url: str, *, allow_test_server: bool = False, credentials: Credentials | None = None,
) -> str:
    return _validate_url(url, websocket=True, allow_test_server=allow_test_server, credentials=credentials)
