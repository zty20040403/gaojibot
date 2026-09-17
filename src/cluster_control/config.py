from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .host_operations import parse_helpers


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "enabled"}


def _validate_url(value: str, name: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{name} must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{name} must not contain credentials, query, or fragment")
    return normalized


def _inventory(raw: str) -> tuple[dict[str, object], ...]:
    if not raw.strip():
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("KC_INVENTORY_JSON must be valid JSON") from exc
    if not isinstance(value, list):
        raise ValueError("KC_INVENTORY_JSON must be a JSON array")
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Every inventory item must be an object")
        host_id = str(item.get("host_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", host_id):
            raise ValueError(f"Invalid inventory host_id: {host_id!r}")
        if host_id in seen:
            raise ValueError(f"Duplicate inventory host_id: {host_id}")
        roles = item.get("roles", [])
        readable_units = item.get("readable_units", [])
        operable_units = item.get("operable_units", [])
        for flag in ("observe", "operate", "compute", "gpu_compute"):
            if flag in item and not isinstance(item[flag], bool):
                raise ValueError(f"Invalid {flag} flag for inventory host {host_id}")
        if not isinstance(roles, list) or not all(
            isinstance(role, str) and role.strip() for role in roles
        ):
            raise ValueError(f"Invalid roles for inventory host {host_id}")
        if not isinstance(readable_units, list) or not all(
            isinstance(unit, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,119}\.service", unit)
            for unit in readable_units
        ):
            raise ValueError(f"Invalid readable_units for inventory host {host_id}")
        if not isinstance(operable_units, list) or not all(
            isinstance(unit, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,119}\.service", unit)
            for unit in operable_units
        ):
            raise ValueError(f"Invalid operable_units for inventory host {host_id}")
        if any(unit not in readable_units for unit in operable_units):
            raise ValueError(
                f"operable_units must be a subset of readable_units for {host_id}"
            )
        seen.add(host_id)
        result.append(
            {
                "host_id": host_id,
                "label": str(item.get("label") or host_id).strip()[:80],
                "architecture": str(item.get("architecture") or "unknown").strip()[:40],
                "site": str(item.get("site") or "unknown").strip()[:80],
                "maintainer": str(item.get("maintainer") or "unknown").strip()[:80],
                "permission_source": str(
                    item.get("permission_source") or "unconfirmed"
                ).strip()[:120],
                "roles": [str(role).strip()[:80] for role in roles],
                "observe": bool(item.get("observe", False)),
                "operate": bool(item.get("operate", False)),
                "compute": bool(item.get("compute", False)),
                "gpu_compute": bool(item.get("gpu_compute", False)),
                "readable_units": list(dict.fromkeys(readable_units)),
                "operable_units": list(dict.fromkeys(operable_units)),
            }
        )
    return tuple(result)


def _worker_identities(raw: str) -> tuple[dict[str, object], ...]:
    if not raw.strip():
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("KC_WORKER_IDENTITIES_JSON must be valid JSON") from exc
    if not isinstance(value, list):
        raise ValueError("KC_WORKER_IDENTITIES_JSON must be a JSON array")
    result: list[dict[str, object]] = []
    worker_ids: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Every worker identity must be an object")
        worker_id = str(item.get("worker_id") or "").strip()
        host_id = str(item.get("host_id") or "").strip()
        token_file = str(item.get("token_file") or "").strip()
        owner_actor_id = str(item.get("owner_actor_id") or "admin:kenneth").strip()
        owner_aliases = item.get("owner_aliases", [])
        if not isinstance(owner_aliases, list) or any(
            not isinstance(actor, str)
            or not re.fullmatch(r"qq:[0-9]+", actor)
            for actor in owner_aliases
        ):
            raise ValueError("Worker owner aliases must be exact QQ identities")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", worker_id):
            raise ValueError("Invalid worker_id")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", host_id):
            raise ValueError("Invalid worker host_id")
        if (
            worker_id in worker_ids
            or not token_file
            or not owner_actor_id.startswith("admin:")
            or len(owner_actor_id) > 200
        ):
            raise ValueError("Duplicate worker_id or missing worker token file")
        worker_ids.add(worker_id)
        result.append(
            {
                "worker_id": worker_id,
                "host_id": host_id,
                "token_file": token_file,
                "owner_actor_id": owner_actor_id,
                "owner_aliases": list(dict.fromkeys(owner_aliases)),
            }
        )
    return tuple(result)


def _diagnostic_targets(raw: str) -> tuple[dict[str, str], ...]:
    if not raw.strip():
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("KC_DIAGNOSTIC_TARGETS_JSON must be valid JSON") from exc
    if not isinstance(value, list):
        raise ValueError("KC_DIAGNOSTIC_TARGETS_JSON must be a JSON array")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Every diagnostic target must be an object")
        target_id = str(item.get("target_id") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", target_id):
            raise ValueError(f"Invalid diagnostic target_id: {target_id!r}")
        if target_id in seen:
            raise ValueError(f"Duplicate diagnostic target_id: {target_id}")
        kind = str(item.get("kind") or "http").strip().lower()
        if kind not in {"model", "admin", "service"}:
            raise ValueError(f"Invalid diagnostic target kind: {kind!r}")
        url = _validate_url(str(item.get("url") or ""), "diagnostic target URL")
        if not url:
            raise ValueError(f"Diagnostic target {target_id!r} requires a URL")
        observer_host = str(item.get("observer_host") or "h610").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", observer_host):
            raise ValueError(
                f"Invalid diagnostic observer_host: {observer_host!r}"
            )
        host_id = str(item.get("host_id") or observer_host).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", host_id):
            raise ValueError(f"Invalid diagnostic host_id: {host_id!r}")
        service_ref = str(item.get("service_ref") or "").strip()
        if service_ref and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,119}\.service",
            service_ref,
        ):
            raise ValueError(
                f"Invalid diagnostic service_ref: {service_ref!r}"
            )
        seen.add(target_id)
        result.append(
            {
                "target_id": target_id,
                "label": str(item.get("label") or target_id).strip()[:80],
                "kind": kind,
                "url": url,
                "observer_host": observer_host,
                "host_id": host_id,
                "service_ref": service_ref,
            }
        )
    return tuple(result)


def _deployment_repositories(raw: str) -> tuple[dict[str, object], ...]:
    if not raw.strip():
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("KC_DEPLOYMENT_REPOSITORIES_JSON must be valid JSON") from exc
    if not isinstance(value, list):
        raise ValueError("KC_DEPLOYMENT_REPOSITORIES_JSON must be a JSON array")
    repositories: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Every deployment repository must be an object")
        repository_id = str(item.get("repository_id") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", repository_id):
            raise ValueError("Invalid deployment repository_id")
        if repository_id in seen:
            raise ValueError("Duplicate deployment repository_id")
        url = str(item.get("url") or "").strip()
        default_branch = str(item.get("default_branch") or "main").strip()
        repository_version = int(item.get("resource_version") or 1)
        backend = str(item.get("backend") or "ssh")
        if backend not in {"ssh", "ops"}:
            raise ValueError("Invalid deployment backend")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "ssh"}
            and not url.startswith("git@")
        ):
            raise ValueError("Deployment repository URL must use HTTPS or SSH")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Deployment repository URL must not contain credentials")
        if any(value in url for value in ("\n", "\r", "\x00")):
            raise ValueError("Invalid deployment repository URL")
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/-]{0,119}", default_branch
        ) or repository_version < 1:
            raise ValueError("Invalid deployment repository version or branch")
        allowed_changes = item.get("allowed_changes", [])
        targets = item.get("targets", [])
        if not isinstance(allowed_changes, list) or not allowed_changes or not all(
            isinstance(change, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,159}", change)
            for change in allowed_changes
        ):
            raise ValueError("Deployment repository needs valid allowed_changes")
        if not isinstance(targets, list) or not targets:
            raise ValueError("Deployment repository needs at least one target")
        normalized_targets: list[dict[str, object]] = []
        target_ids: set[str] = set()
        for target in targets:
            if not isinstance(target, dict):
                raise ValueError("Deployment target must be an object")
            host_id = str(target.get("host_id") or "").strip()
            flake_host = str(target.get("flake_host") or host_id).strip()
            ssh_target = str(target.get("ssh_target") or "").strip()
            verification_units = target.get("verification_units", [])
            target_version = int(target.get("resource_version") or 1)
            if (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", host_id)
                or host_id in target_ids
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", flake_host)
                or (backend == "ssh" and not re.fullmatch(
                    r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9.-]{0,199}",
                    ssh_target,
                ))
                or not isinstance(verification_units, list)
                or target_version < 1
                or not all(
                    isinstance(unit, str)
                    and re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,119}\.service",
                        unit,
                    )
                    for unit in verification_units
                )
            ):
                raise ValueError("Invalid deployment target")
            ops_target = {key: str(target.get(key) or "") for key in ("ops_repository", "ops_profile")}
            if backend == "ops" and any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value)
                                         for value in ops_target.values()):
                raise ValueError("Ops deployment targets require an exact repository and profile")
            target_ids.add(host_id)
            normalized_targets.append(
                {
                    "host_id": host_id,
                    "flake_host": flake_host,
                    "ssh_target": ssh_target,
                    "verification_units": list(dict.fromkeys(verification_units)),
                    "use_remote_sudo": bool(target.get("use_remote_sudo", False)),
                    "resource_version": target_version,
                    **(ops_target if backend == "ops" else {}),
                }
            )
        seen.add(repository_id)
        repositories.append(
            {
                "repository_id": repository_id,
                "url": url,
                "default_branch": default_branch,
                "resource_version": repository_version,
                "backend": backend,
                "allowed_changes": list(dict.fromkeys(allowed_changes)),
                "targets": normalized_targets,
            }
        )
    return tuple(repositories)


def _deployer_identities(raw: str) -> tuple[dict[str, object], ...]:
    if not raw.strip():
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("KC_DEPLOYER_IDENTITIES_JSON must be valid JSON") from exc
    if not isinstance(value, list):
        raise ValueError("KC_DEPLOYER_IDENTITIES_JSON must be a JSON array")
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Every deployer identity must be an object")
        deployer_id = str(item.get("deployer_id") or "").strip()
        token_file = str(item.get("token_file") or "").strip()
        repositories = item.get("repository_ids", [])
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", deployer_id)
            or deployer_id in seen
            or not token_file
            or not isinstance(repositories, list)
            or not repositories
            or not all(
                isinstance(repo, str)
                and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", repo)
                for repo in repositories
            )
        ):
            raise ValueError("Invalid deployer identity")
        seen.add(deployer_id)
        result.append(
            {
                "deployer_id": deployer_id,
                "token_file": token_file,
                "repository_ids": list(dict.fromkeys(repositories)),
            }
        )
    return tuple(result)


def _identity_list(name: str) -> tuple[str, ...]:
    values = json.loads(os.getenv(name, "[]"))
    if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f"{name} must be a JSON array of nonempty identities")
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True)
class ClusterControlSettings:
    host: str
    port: int
    local_host_id: str
    api_token_file: str
    ops_enabled: bool
    ops_base_url: str
    ops_token_file: str
    ops_timeout_seconds: int
    cache_seconds: int
    inventory: tuple[dict[str, object], ...]
    diagnostic_targets: tuple[dict[str, str], ...]
    worker_identities: tuple[dict[str, object], ...]
    deployment_repositories: tuple[dict[str, object], ...]
    deployer_identities: tuple[dict[str, object], ...]
    artifact_dir: str
    postgres_dsn: str
    postgres_schema: str
    postgres_pool_min_size: int
    postgres_pool_max_size: int
    postgres_pool_timeout_seconds: int
    ops_management_token_file: str = ""
    ops_management_hosts: tuple[str, ...] = ()
    ops_management_actors: tuple[str, ...] = ()
    host_control_helpers: dict[str, str] = field(default_factory=dict)
    ssh_targets: dict[str, dict[str, str]] = field(default_factory=dict)
    ssh_known_hosts_file: str = ""
    ssh_identity_file: str = ""
    ssh_binary: str = "ssh"
    ssh_management_hosts: tuple[str, ...] = ()
    ssh_management_actors: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "ClusterControlSettings":
        from .adapters.ssh import parse_targets
        schema = os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError("AI_POSTGRES_SCHEMA is not a valid identifier")
        min_size = _int("AI_POSTGRES_POOL_MIN_SIZE", 1, 1, 20)
        max_size = _int("AI_POSTGRES_POOL_MAX_SIZE", 5, min_size, 100)
        local_host_id = os.getenv("KC_LOCAL_HOST_ID", "h610").strip() or "h610"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", local_host_id):
            raise ValueError("KC_LOCAL_HOST_ID is not a valid host identifier")
        return cls(
            host=os.getenv("KC_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=_int("KC_PORT", 8091, 1, 65535),
            local_host_id=local_host_id,
            api_token_file=os.getenv("KC_API_TOKEN_FILE", "").strip(),
            ops_enabled=_bool("KC_OPS_ENABLED", False),
            ops_base_url=_validate_url(
                os.getenv("KC_OPS_BASE_URL", ""), "KC_OPS_BASE_URL"
            ),
            ops_token_file=os.getenv("KC_OPS_TOKEN_FILE", "").strip(),
            ops_timeout_seconds=_int("KC_OPS_TIMEOUT_SECONDS", 15, 1, 30),
            ops_management_token_file=os.getenv("KC_OPS_MANAGEMENT_TOKEN_FILE", "").strip(),
            ops_management_hosts=_identity_list("KC_OPS_MANAGEMENT_HOSTS"),
            ops_management_actors=_identity_list("KC_OPS_MANAGEMENT_ACTORS"),
            host_control_helpers=parse_helpers(os.getenv("KC_HOST_CONTROL_HELPERS_JSON", "")),
            ssh_targets=parse_targets(os.getenv("KC_SSH_TARGETS_JSON", "")),
            ssh_known_hosts_file=os.getenv("KC_SSH_KNOWN_HOSTS_FILE", ""),
            ssh_identity_file=os.getenv("KC_SSH_IDENTITY_FILE", ""),
            ssh_binary=os.getenv("KC_SSH_BINARY", "ssh"),
            ssh_management_hosts=_identity_list("KC_SSH_MANAGEMENT_HOSTS"),
            ssh_management_actors=_identity_list("KC_SSH_MANAGEMENT_ACTORS"),
            cache_seconds=_int("KC_CACHE_SECONDS", 20, 1, 300),
            inventory=_inventory(os.getenv("KC_INVENTORY_JSON", "")),
            diagnostic_targets=_diagnostic_targets(
                os.getenv("KC_DIAGNOSTIC_TARGETS_JSON", "")
            ),
            worker_identities=_worker_identities(
                os.getenv("KC_WORKER_IDENTITIES_JSON", "")
            ),
            deployment_repositories=_deployment_repositories(
                os.getenv("KC_DEPLOYMENT_REPOSITORIES_JSON", "")
            ),
            deployer_identities=_deployer_identities(
                os.getenv("KC_DEPLOYER_IDENTITIES_JSON", "")
            ),
            artifact_dir=os.getenv(
                "KC_ARTIFACT_DIR", "/var/lib/gaoji-cluster-control/artifacts"
            ).strip(),
            postgres_dsn=os.getenv("AI_POSTGRES_DSN", "").strip(),
            postgres_schema=schema,
            postgres_pool_min_size=min_size,
            postgres_pool_max_size=max_size,
            postgres_pool_timeout_seconds=_int(
                "AI_POSTGRES_POOL_TIMEOUT_SECONDS", 10, 1, 60
            ),
        )

    def validate(self) -> None:
        if not self.api_token_file:
            raise ValueError("KC_API_TOKEN_FILE is required")
        if not self.postgres_dsn:
            raise ValueError("AI_POSTGRES_DSN is required")
        if self.ops_enabled and (
            not self.ops_base_url or not self.ops_token_file
        ):
            raise ValueError(
                "KC_OPS_BASE_URL and KC_OPS_TOKEN_FILE are required "
                "when Ops is enabled"
            )
        if self.inventory and self.local_host_id not in {
            str(item.get("host_id") or "") for item in self.inventory
        }:
            raise ValueError("KC_LOCAL_HOST_ID must exist in KC_INVENTORY_JSON")
        inventory = {str(item.get("host_id") or ""): item for item in self.inventory}
        if self.ssh_targets:
            if self.ops_enabled or self.ops_management_token_file:
                raise ValueError("Choose SSH or legacy Ops; never route the same write to both")
            if not self.ssh_known_hosts_file or not self.ssh_known_hosts_file.startswith("/"):
                raise ValueError("SSH requires administrator-pinned host keys")
            if any(host not in inventory or inventory[host].get("observe") is not True for host in self.ssh_targets):
                raise ValueError("SSH targets must be observable inventory hosts")
            if bool(self.ssh_management_hosts) != bool(self.ssh_management_actors):
                raise ValueError("SSH writes require explicit hosts and administrators")
            if any(host not in self.ssh_targets or inventory[host].get("operate") is not True for host in self.ssh_management_hosts):
                raise ValueError("SSH management host is outside the operation grant")
            if any(not re.fullmatch(r"(?:qq:[0-9]+|admin:[A-Za-z0-9_-]+)", actor) for actor in self.ssh_management_actors):
                raise ValueError("Invalid SSH management administrator")
        elif self.ssh_management_hosts or self.ssh_management_actors:
            raise ValueError("SSH management requires configured targets")
        if self.ops_management_token_file:
            if not self.ops_enabled or not self.ops_management_hosts or not self.ops_management_actors:
                raise ValueError("Ops management requires a dedicated identity, hosts and actors")
            if any(host not in inventory for host in self.ops_management_hosts):
                raise ValueError("Unknown Ops management host")
            if any(not re.fullmatch(r"(?:qq:[0-9]+|admin:[A-Za-z0-9_-]+)", actor)
                   for actor in self.ops_management_actors):
                raise ValueError("Invalid Ops management administrator")
        for target in self.diagnostic_targets:
            observer_host = str(target["observer_host"])
            target_host_id = str(target["host_id"])
            if observer_host not in inventory or target_host_id not in inventory:
                raise ValueError(
                    f"Diagnostic target {target['target_id']} references an unknown host"
                )
            service_ref = str(target.get("service_ref") or "")
            if service_ref and service_ref not in set(
                inventory[target_host_id].get("readable_units", [])
            ):
                raise ValueError(
                    f"Diagnostic target {target['target_id']} references an unreadable service"
                )
        for worker in self.worker_identities:
            host = inventory.get(worker["host_id"])
            if host is None or not host.get("compute"):
                raise ValueError(
                    f"Worker {worker['worker_id']} requires a compute-enabled inventory host"
                )
        repositories = {
            str(item["repository_id"]): item
            for item in self.deployment_repositories
        }
        for repository in repositories.values():
            if repository.get("backend") == "ops" and not self.ops_management_token_file:
                raise ValueError("Ops deployments require the management backend")
            for target in repository["targets"]:
                if str(target["host_id"]) not in inventory:
                    raise ValueError(
                        f"Deployment target {target['host_id']} is not in inventory"
                    )
                if repository.get("backend") == "ops" and target["host_id"] not in self.ops_management_hosts:
                    raise ValueError("Ops deployment target is outside the management grant")
        for deployer in self.deployer_identities:
            unknown = set(deployer["repository_ids"]) - set(repositories)
            if unknown:
                raise ValueError(
                    f"Deployer {deployer['deployer_id']} references unknown repositories"
                )
            if any(repositories[key].get("backend") == "ops" for key in deployer["repository_ids"]):
                raise ValueError("Ops deployments cannot also be assigned to an SSH deployer")
        if not self.artifact_dir:
            raise ValueError("KC_ARTIFACT_DIR is required")
