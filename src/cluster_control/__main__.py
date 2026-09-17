from __future__ import annotations

import uvicorn
from pathlib import Path

from src.bot_storage import PostgresDatabase
from src.bot_storage.schema import HEAD_REVISION

from .api import create_app
from .auth import CredentialFileAuthenticator
from .config import ClusterControlSettings
from .deployment_service import DeploymentService
from .deployment_storage import DeploymentStore
from .diagnostics import DiagnosticStore, IncidentDiagnosticService
from .adapters.ops import OpsClient
from .adapters.ssh import SSHOperationsClient
from .service import FleetControlService
from .storage import FleetProjectionStore
from .execution_service import ClusterExecutionService, WorkerAuthenticator
from .execution_storage import ClusterExecutionStore
from .guardian import GuardianService
from .guardian_ops import GuardianOpsBridge
from .reliability import ReliabilityStore
from .resource_policy import ResourcePolicyStore
from .ops_management import OpsManagementService
from .ops_deployment_runner import OpsDeploymentRunner


def main() -> None:
    settings = ClusterControlSettings.from_env()
    settings.validate()
    database = PostgresDatabase(
        settings.postgres_dsn,
        schema=settings.postgres_schema,
        min_size=settings.postgres_pool_min_size,
        max_size=settings.postgres_pool_max_size,
        timeout_seconds=settings.postgres_pool_timeout_seconds,
        application_name="gaoji-cluster-control",
    )
    database.require_revision(HEAD_REVISION)
    ops = (SSHOperationsClient(settings.ssh_targets,
        known_hosts_file=settings.ssh_known_hosts_file, identity_file=settings.ssh_identity_file,
        ssh_binary=settings.ssh_binary) if settings.ssh_targets else (
        OpsClient(
            settings.ops_base_url,
            settings.ops_token_file,
            timeout_seconds=settings.ops_timeout_seconds,
        )
        if settings.ops_enabled
        else None
    ))
    service = FleetControlService(
        ops,
        store=FleetProjectionStore(database, backend_name=ops.backend_name if ops else "ops"),
        inventory=settings.inventory,
        cache_seconds=settings.cache_seconds,
    )
    diagnostics = IncidentDiagnosticService(
        service,
        DiagnosticStore(database),
        settings.diagnostic_targets,
        local_host_id=settings.local_host_id,
    )
    execution_store = ClusterExecutionStore(database, Path(settings.artifact_dir))
    management = (OpsManagementService(
        OpsClient(settings.ops_base_url, settings.ops_management_token_file, timeout_seconds=25),
        execution_store, hosts=settings.ops_management_hosts, actors=settings.ops_management_actors,
        host_helpers=settings.host_control_helpers,
    ) if settings.ops_management_token_file else None)
    if settings.ssh_management_hosts:
        management = OpsManagementService(SSHOperationsClient(
            {host: settings.ssh_targets[host] for host in settings.ssh_management_hosts},
            known_hosts_file=settings.ssh_known_hosts_file, identity_file=settings.ssh_identity_file,
            ssh_binary=settings.ssh_binary, writable=True), execution_store,
            hosts=settings.ssh_management_hosts, actors=settings.ssh_management_actors,
            host_helpers={host: path for host, path in settings.host_control_helpers.items()
                          if host in settings.ssh_management_hosts})
    resource_policies = ResourcePolicyStore(database)
    reliability = ReliabilityStore(database)
    worker_hosts = {
        item["worker_id"]: item["host_id"] for item in settings.worker_identities
    }
    worker_owners = {
        item["worker_id"]: item["owner_actor_id"] for item in settings.worker_identities
    }
    execution = ClusterExecutionService(
        execution_store,
        inventory=settings.inventory,
        diagnostic_targets=settings.diagnostic_targets,
        worker_hosts=worker_hosts,
        worker_owners=worker_owners,
        worker_owner_aliases={
            str(item["worker_id"]): tuple(item["owner_aliases"])
            for item in settings.worker_identities
        },
        resource_policies=resource_policies,
    )
    guardian_ops = (GuardianOpsBridge(management, reliability, execution.diagnostic_targets,
        settings.inventory) if management is not None else None)
    guardian = GuardianService(
        reliability,
        settings.diagnostic_targets,
        operation_factory=guardian_ops.submit if guardian_ops is not None else None,
    )
    worker_authenticator = WorkerAuthenticator(
        {item["worker_id"]: item["token_file"] for item in settings.worker_identities}
    )
    deployments = DeploymentService(
        DeploymentStore(database),
        repositories=settings.deployment_repositories,
        deployer_repositories={
            str(item["deployer_id"]): tuple(item["repository_ids"])
            for item in settings.deployer_identities
        },
    )
    ops_deployer = OpsDeploymentRunner(deployments, management) if management is not None else None
    deployer_authenticator = CredentialFileAuthenticator(
        {
            str(item["deployer_id"]): str(item["token_file"])
            for item in settings.deployer_identities
        }
    )
    app = create_app(
        service,
        api_token_file=settings.api_token_file,
        diagnostics=diagnostics,
        execution=execution,
        worker_authenticator=worker_authenticator,
        resource_policies=resource_policies,
        reliability=reliability,
        guardian=guardian,
        deployments=deployments,
        deployer_authenticator=deployer_authenticator,
        management=management,
        guardian_ops=guardian_ops,
        ops_deployer=ops_deployer,
    )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
