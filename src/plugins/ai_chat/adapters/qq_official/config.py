"""Explicit official account configuration; disabled means no credential I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Mapping
import unicodedata
from urllib.parse import urlsplit

from src.plugins.ai_chat.platform_types import AccountKey, ActorKey
from src.qq_official.credentials import (
    Credentials, CredentialError, DEFAULT_API_BASE, SANDBOX_API_BASE,
    _read_secret, validate_api_base,
)
from .delivery_configuration import DeliveryConfiguration


class OfficialConfigurationError(ValueError):
    """Fixed diagnostics without configuration values or credential paths."""


def _boolean(value: str, name: str) -> bool:
    if value.strip().lower() in {"true", "1", "yes", "on"}:
        return True
    if value.strip().lower() in {"", "false", "0", "no", "off"}:
        return False
    raise OfficialConfigurationError(f"Invalid official {name} switch")


def _identifiers(value: str) -> tuple[str, ...]:
    if not value.strip():
        return ()
    try:
        # JSON preserves exact opaque identifiers. Comma-separated configuration
        # retains the established environment style; delimiter whitespace is not ID.
        raw = json.loads(value) if value.lstrip().startswith("[") else [part.strip() for part in value.split(",")]
        if not isinstance(raw, list) or len(raw) > 256:
            raise ValueError
        for item in raw:
            if (not isinstance(item, str) or not item.strip()
                    or len(item.encode("utf-8")) > 256
                    or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in item)):
                raise ValueError
        return tuple(dict.fromkeys(raw))
    except (ValueError, TypeError, UnicodeError):
        raise OfficialConfigurationError("Invalid official identity allowlist") from None


@dataclass(frozen=True)
class LoadedOfficialConfig:
    account: AccountKey = field(repr=False)
    credentials: Credentials = field(repr=False)
    api_base: str = field(repr=False)
    owner_openids: tuple[str, ...] = field(repr=False)
    private_policy: str
    private_allowed_openids: tuple[str, ...] = field(repr=False)
    binding_pepper: bytes = field(repr=False)
    payload_retention_hours: int = 24
    allow_test_server: bool = False
    delivery: DeliveryConfiguration = DeliveryConfiguration()

    @property
    def owner_actors(self) -> tuple[ActorKey, ...]:
        return tuple(ActorKey(self.account, "c2c_user", "", native_id) for native_id in self.owner_openids)


@dataclass(frozen=True)
class OfficialConfig:
    enabled: bool = False
    app_id_file: str = field(default="", repr=False)
    app_secret_file: str = field(default="", repr=False)
    api_base: str = field(default="", repr=False)
    sandbox: bool = False
    expected_account_key: str = field(default="", repr=False)
    owner_openids: tuple[str, ...] = field(default=(), repr=False)
    private_policy: str = "owner_only"
    private_allowed_openids: tuple[str, ...] = field(default=(), repr=False)
    binding_pepper_file: str = field(default="", repr=False)
    payload_retention_hours: int = 24
    delivery: DeliveryConfiguration = DeliveryConfiguration()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> OfficialConfig:
        if not _boolean(env.get("AI_QQ_OFFICIAL_ENABLED", "false"), "enabled"):
            # Dormant options cannot affect an existing OneBot deployment.
            return cls()
        sandbox = _boolean(env.get("AI_QQ_OFFICIAL_SANDBOX", "false"), "sandbox")
        policy = env.get("AI_QQ_OFFICIAL_PRIVATE_POLICY", "owner_only").strip()
        if policy not in {"owner_only", "allowlist", "public"}:
            raise OfficialConfigurationError("Invalid official private admission policy")
        if env.get("AI_QQ_OFFICIAL_GROUP_ADMISSION", "manual_bind").strip() != "manual_bind":
            raise OfficialConfigurationError("Official groups require manual binding")
        try:
            retention = int(env.get("AI_QQ_OFFICIAL_INBOX_PAYLOAD_TTL_HOURS", "24"))
        except ValueError:
            raise OfficialConfigurationError("Invalid official inbox retention") from None
        if not 1 <= retention <= 168:
            raise OfficialConfigurationError("Invalid official inbox retention")
        owners = _identifiers(env.get("AI_QQ_OFFICIAL_OWNER_OPENIDS", ""))
        if len(owners) > 32:
            raise OfficialConfigurationError("Official owner configuration exceeds account limit")
        try:
            delivery = DeliveryConfiguration.from_env(env)
        except ValueError:
            raise OfficialConfigurationError('Invalid official delivery configuration') from None
        return cls(
            enabled=True,
            app_id_file=env.get("AI_QQ_OFFICIAL_APP_ID_FILE", "").strip(),
            app_secret_file=env.get("AI_QQ_OFFICIAL_APP_SECRET_FILE", "").strip(),
            api_base=env.get("AI_QQ_OFFICIAL_API_BASE", "").strip(),
            sandbox=sandbox,
            expected_account_key=env.get("AI_QQ_OFFICIAL_ACCOUNT_KEY", "").strip(),
            owner_openids=owners,
            private_policy=policy,
            private_allowed_openids=_identifiers(env.get("AI_QQ_OFFICIAL_PRIVATE_ALLOWED_OPENIDS", "")),
            binding_pepper_file=env.get("AI_QQ_OFFICIAL_BIND_PEPPER_FILE", "").strip(),
            payload_retention_hours=retention,
            delivery=delivery,
        )

    def load(self, *, acl_validator=None, allow_test_server: bool = False) -> LoadedOfficialConfig | None:
        if not self.enabled:
            return None
        if not all((self.app_id_file, self.app_secret_file, self.binding_pepper_file)):
            raise OfficialConfigurationError("Official protected credential and binding files are required")
        try:
            credentials = Credentials.from_files(
                self.app_id_file, self.app_secret_file, acl_validator=acl_validator,
            )
            pepper_text = _read_secret(self.binding_pepper_file, acl_validator)
            if not re.fullmatch(r"[0-9a-fA-F]{64}", pepper_text):
                raise OfficialConfigurationError("Official binding pepper must contain 32 random bytes in hex")
            if allow_test_server and not credentials.is_test:
                raise OfficialConfigurationError("Official loopback mode requires synthetic test credentials")
            environment = "test" if allow_test_server else "sandbox" if self.sandbox else "production"
            account = AccountKey("qq-official", credentials.app_id, environment)
            if len(account.key.encode("utf-8")) > 512:
                raise OfficialConfigurationError("Official account identifier exceeds storage limit")
            if any(len(ActorKey(account, "c2c_user", "", value).key.encode("utf-8")) > 1280
                   for value in (*self.owner_openids, *self.private_allowed_openids)):
                raise OfficialConfigurationError("Official configured identity exceeds storage limit")
            if self.expected_account_key and AccountKey.parse(self.expected_account_key) != account:
                raise OfficialConfigurationError("Official configured account does not match credentials")
            base = validate_api_base(
                self.api_base or (SANDBOX_API_BASE if self.sandbox else DEFAULT_API_BASE),
                allow_test_server=allow_test_server, credentials=credentials,
            )
            host = urlsplit(base).hostname
            if not allow_test_server and (host == "sandbox.api.sgroup.qq.com") != self.sandbox:
                raise OfficialConfigurationError("Official API address does not match selected environment")
            if allow_test_server and host not in {"127.0.0.1", "::1"}:
                raise OfficialConfigurationError("Official test mode requires a loopback endpoint")
        except OfficialConfigurationError:
            raise
        except (CredentialError, ValueError, UnicodeError):
            raise OfficialConfigurationError("Official protected account configuration is invalid") from None
        return LoadedOfficialConfig(
            account, credentials, base, self.owner_openids, self.private_policy,
            self.private_allowed_openids, bytes.fromhex(pepper_text),
            self.payload_retention_hours, allow_test_server, self.delivery,
        )
