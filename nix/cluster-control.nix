{self}: {
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.gaoji-cluster-control;
  defaultPackage = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
  inventoryHostIds = map (host: host.host_id) cfg.inventory;
  diagnosticTargetIds = map (target: target.target_id) cfg.diagnostics.targets;
  workerIds = map (worker: worker.workerId) cfg.workers;
  deploymentRepositoryIds = map (repository: repository.repositoryId) cfg.deployments.repositories;
  deployerIds = map (deployer: deployer.deployerId) cfg.deployments.deployers;
  inventoryHostType = lib.types.submodule {
    options = {
      host_id = lib.mkOption {
        type = lib.types.str;
        description = "Stable host identifier shared with the Nix registry and Ops.";
      };
      label = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "Human-readable host label.";
      };
      architecture = lib.mkOption {
        type = lib.types.str;
        default = "unknown";
        description = "Declared system architecture; this is inventory, not a live observation.";
      };
      site = lib.mkOption {
        type = lib.types.str;
        default = "unknown";
        description = "Declared host site or failure domain.";
      };
      maintainer = lib.mkOption {
        type = lib.types.str;
        default = "unknown";
        description = "Maintainer responsible for approving access to this host.";
      };
      permission_source = lib.mkOption {
        type = lib.types.str;
        default = "unconfirmed";
        description = "Configuration or owner decision that granted this host scope.";
      };
      roles = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [];
        description = "Declared roles used for display and later scheduling decisions.";
      };
      observe = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Allow gaoji to request read-only observations for this host.";
      };
      operate = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Declare future host-operation eligibility; it grants no P1 write capability.";
      };
      compute = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Declare future isolated-worker eligibility; it grants no P1 execution capability.";
      };
      readable_units = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [];
        description = "Exact systemd services gaoji may query on this host.";
      };
      operable_units = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [];
        description = "Exact service allowlist for a separately approved P3 write backend.";
      };
    };
  };
  diagnosticTargetType = lib.types.submodule {
    options = {
      target_id = lib.mkOption {
        type = lib.types.str;
        description = "Stable identifier exposed to diagnostic templates.";
      };
      label = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "Human-readable target label.";
      };
      kind = lib.mkOption {
        type = lib.types.enum ["model" "admin" "service"];
        description = "Diagnostic target kind used by fixed templates.";
      };
      url = lib.mkOption {
        type = lib.types.str;
        description = "Exact approved HTTP(S) URL. Models and users cannot replace it.";
      };
      observer_host = lib.mkOption {
        type = lib.types.str;
        default = "h610";
        description = "Inventory host whose network path performs the probe.";
      };
      host_id = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "Inventory host that owns the guarded target; defaults to observer_host.";
      };
      service_ref = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "Optional registered systemd service represented by this target.";
      };
    };
  };
  workerIdentityType = lib.types.submodule {
    options = {
      workerId = lib.mkOption {type = lib.types.str;};
      hostId = lib.mkOption {type = lib.types.str;};
      tokenFile = lib.mkOption {type = lib.types.str;};
      ownerAliases = lib.mkOption {
        type = lib.types.listOf (lib.types.strMatching "qq:[0-9]+");
        default = [];
        description = "Verified QQ identities of the administrator who owns this worker. Other callers require borrow grants.";
      };
    };
  };
  deploymentTargetType = lib.types.submodule {
    options = {
      hostId = lib.mkOption {type = lib.types.str;};
      flakeHost = lib.mkOption {type = lib.types.str;};
      sshTarget = lib.mkOption {type = lib.types.str; default = "";};
      opsRepository = lib.mkOption {
        type = lib.types.str; default = "";
        description = "Exact repository identity in the upstream Ops catalog.";
      };
      opsProfile = lib.mkOption {
        type = lib.types.str; default = "";
        description = "Exact upstream deployment profile for this target.";
      };
      verificationUnits = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [];
      };
      useRemoteSudo = lib.mkOption {type = lib.types.bool; default = false;};
      resourceVersion = lib.mkOption {type = lib.types.ints.positive; default = 1;};
    };
  };
  deploymentRepositoryType = lib.types.submodule {
    options = {
      repositoryId = lib.mkOption {type = lib.types.str;};
      backend = lib.mkOption {type = lib.types.enum ["ssh" "ops"]; default = "ssh";};
      url = lib.mkOption {type = lib.types.str;};
      defaultBranch = lib.mkOption {type = lib.types.str; default = "main";};
      resourceVersion = lib.mkOption {type = lib.types.ints.positive; default = 1;};
      allowedChanges = lib.mkOption {type = lib.types.listOf lib.types.str;};
      targets = lib.mkOption {type = lib.types.listOf deploymentTargetType;};
    };
  };
  deployerIdentityType = lib.types.submodule {
    options = {
      deployerId = lib.mkOption {type = lib.types.str;};
      tokenFile = lib.mkOption {type = lib.types.str;};
      repositoryIds = lib.mkOption {type = lib.types.listOf lib.types.str;};
    };
  };
in {
  options.services.gaoji-cluster-control = {
    enable = lib.mkEnableOption "gaoji's authenticated cluster control service";
    stateDirectory = lib.mkOption {
      type = lib.types.strMatching "[A-Za-z0-9][A-Za-z0-9_-]*";
      default = "gaoji-cluster-control";
      description = "Persistent directory under /var/lib; preserve it when renaming an existing service.";
    };

    package = lib.mkOption {
      type = lib.types.package;
      default = defaultPackage;
      defaultText = lib.literalExpression "inputs.qq-bot.packages.${pkgs.stdenv.hostPlatform.system}.default";
      description = "gaoji package containing the cluster-control executable.";
    };

    listenAddress = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Address for the internal control API; keep loopback unless a firewall and caller identity are configured.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8091;
      description = "Port for the internal control API.";
    };

    localHostId = lib.mkOption {
      type = lib.types.str;
      default = config.networking.hostName;
      defaultText = lib.literalExpression "config.networking.hostName";
      description = "Inventory identity of the machine that actually executes local diagnostic probes.";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open the control API port. This should remain false for a colocated bot.";
    };

    firewallInterfaces = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      description = "Limit an explicitly opened API port to these interfaces; empty means all interfaces.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Environment file containing the PostgreSQL DSN and pool settings.";
    };

    apiTokenFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Credential shared only with the bot-side internal API client.";
    };

    ops = {
      enable = lib.mkEnableOption "the Ops read-only adapter";
      management = {
        enable = lib.mkEnableOption "catalog-backed Ops management with mandatory approval";
        tokenFile = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          description = "Separate management credential restricted upstream to the granted hosts and repositories.";
        };
        hosts = lib.mkOption {type = lib.types.listOf lib.types.str; default = [];};
        actors = lib.mkOption {
          type = lib.types.listOf lib.types.str;
          default = [];
          description = "Exact authenticated actor identities, e.g. qq:123 and admin:kenneth.";
        };
      };

      baseUrl = lib.mkOption {
        type = lib.types.str;
        default = "";
        example = "http://100.64.0.3:9721";
        description = "Fixed Ops hub URL; it cannot be supplied by a model tool call.";
      };

      tokenFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Dedicated Ops credential for gaoji.";
      };

      timeoutSeconds = lib.mkOption {
        type = lib.types.ints.between 1 30;
        default = 15;
        description = "Total budget for catalog validation and one Ops query.";
      };
    };

    ssh = {
      enable = lib.mkEnableOption "native SSH operations instead of retired MaxOps";
      targets = lib.mkOption {
        type = lib.types.attrsOf (lib.types.submodule {
          options = {
            destination = lib.mkOption {type = lib.types.str;};
            port = lib.mkOption {type = lib.types.port; default = 22;};
            helper = lib.mkOption {type = lib.types.str; default = "/run/current-system/sw/bin/gaoji-ssh-operations";};
          };
        });
        default = {};
        description = "Fixed SSH destinations from the host registry, never model-supplied.";
      };
      knownHostsFile = lib.mkOption {type = lib.types.nullOr lib.types.path; default = null;};
      identityFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str; default = null;
        description = "Dedicated private key injected as a service credential; never a bot sandbox mount.";
      };
      managementHosts = lib.mkOption {type = lib.types.listOf lib.types.str; default = [];};
      administrators = lib.mkOption {type = lib.types.listOf lib.types.str; default = [];};
    };

    cacheSeconds = lib.mkOption {
      type = lib.types.ints.between 1 300;
      default = 20;
      description = "Maximum age of a non-sensitive read-only query projection.";
    };

    hostControlHelpers = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = {};
      description = "Target-installed gaoji-host-control paths, keyed by authorized host. Unconfigured hosts cannot run free commands or reboot.";
    };

    diagnostics.targets = lib.mkOption {
      type = lib.types.listOf diagnosticTargetType;
      default = [];
      example = [{
        target_id = "qwen-local";
        label = "Qwen local API";
        kind = "model";
        url = "http://b650.inner.example:8000/v1/models";
        observer_host = "h610";
      }];
      description = "Fixed, trusted diagnostic endpoints. Arbitrary tool-call URLs are rejected.";
    };

    workers = lib.mkOption {
      type = lib.types.listOf workerIdentityType;
      default = [];
      description = "Worker identities bound to credentials and compute-enabled inventory hosts.";
    };

    deployments = {
      repositories = lib.mkOption {
        type = lib.types.listOf deploymentRepositoryType;
        default = [];
        description = "Server-owned repositories, change labels and deployment target allowlists.";
      };
      deployers = lib.mkOption {
        type = lib.types.listOf deployerIdentityType;
        default = [];
        description = "Independent deployer identities and the repositories each may claim.";
      };
    };

    inventory = lib.mkOption {
      type = lib.types.listOf inventoryHostType;
      default = [];
      example = [{host_id = "h610"; roles = ["control"]; observe = true;}];
      description = "Explicit host scope. Presence alone does not grant observation or operation.";
    };

    environment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = {};
      description = "Additional non-secret environment variables.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = !cfg.ssh.enable || (!cfg.ops.enable && !cfg.ops.management.enable
          && cfg.ssh.targets != {} && cfg.ssh.knownHostsFile != null
          && lib.all (name: builtins.hasAttr name cfg.ssh.targets) cfg.ssh.managementHosts
          && ((cfg.ssh.managementHosts == []) == (cfg.ssh.administrators == [])));
        message = "Gaoji SSH requires pinned host keys, fixed targets and explicit administrators; legacy Ops must be disabled.";
      }
      {
        assertion = cfg.apiTokenFile != null;
        message = "services.gaoji-cluster-control.apiTokenFile is required";
      }
      {
        assertion = cfg.environmentFile != null;
        message = "services.gaoji-cluster-control.environmentFile is required for PostgreSQL";
      }
      {
        assertion = !cfg.ops.enable || (cfg.ops.baseUrl != "" && cfg.ops.tokenFile != null);
        message = "Ops baseUrl and tokenFile are required when the adapter is enabled";
      }
      {
        assertion = !cfg.ops.management.enable || (
          cfg.ops.enable && cfg.ops.management.tokenFile != null
          && cfg.ops.management.tokenFile != cfg.ops.tokenFile
          && cfg.ops.management.hosts != [] && cfg.ops.management.actors != []
          && lib.all (name: builtins.elem name inventoryHostIds) cfg.ops.management.hosts
        );
        message = "Ops management requires separate credentials and explicit host/actor grants";
      }
      {
        assertion = cfg.listenAddress == "127.0.0.1" || cfg.listenAddress == "::1" || cfg.openFirewall;
        message = "A non-loopback cluster-control listener requires an explicit firewall decision";
      }
      {
        assertion = builtins.length inventoryHostIds == builtins.length (lib.unique inventoryHostIds);
        message = "gaoji cluster inventory contains duplicate host_id values";
      }
      {
        assertion = lib.all (host: builtins.match "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$" host.host_id != null) cfg.inventory;
        message = "gaoji cluster inventory contains an invalid host_id";
      }
      {
        assertion = lib.all (host: lib.all (unit: builtins.match "^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,119}\\.service$" unit != null) host.readable_units) cfg.inventory;
        message = "gaoji cluster inventory contains an invalid readable systemd unit";
      }
      {
        assertion = lib.all (host: lib.all (unit: builtins.elem unit host.readable_units) host.operable_units) cfg.inventory;
        message = "gaoji operable units must also be readable units";
      }
      {
        assertion = lib.all (worker: builtins.elem worker.hostId inventoryHostIds) cfg.workers;
        message = "Every gaoji worker host must exist in inventory";
      }
      {
        assertion = builtins.length workerIds == builtins.length (lib.unique workerIds);
        message = "gaoji worker identities must be unique";
      }
      {
        assertion = lib.all (worker: builtins.match "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$" worker.workerId != null) cfg.workers;
        message = "gaoji worker identity is invalid";
      }
      {
        assertion = lib.all (worker:
          lib.any (host: host.host_id == worker.hostId && host.compute) cfg.inventory
        ) cfg.workers;
        message = "Every gaoji worker host must be compute-enabled in inventory";
      }
      {
        assertion = builtins.length diagnosticTargetIds == builtins.length (lib.unique diagnosticTargetIds);
        message = "gaoji diagnostics contains duplicate target_id values";
      }
      {
        assertion = lib.all (target:
          builtins.elem target.observer_host inventoryHostIds
          && builtins.elem (if target.host_id == "" then target.observer_host else target.host_id) inventoryHostIds
        ) cfg.diagnostics.targets;
        message = "Every diagnostic observer_host and target host_id must exist in the cluster inventory";
      }
      {
        assertion = lib.all (target:
          target.service_ref == ""
          || lib.any (host:
            host.host_id == (if target.host_id == "" then target.observer_host else target.host_id)
            && builtins.elem target.service_ref host.readable_units
          ) cfg.inventory
        ) cfg.diagnostics.targets;
        message = "Every diagnostic service_ref must be readable on its registered target host";
      }
      {
        assertion = builtins.elem cfg.localHostId inventoryHostIds;
        message = "The cluster-control localHostId must exist in the cluster inventory";
      }
      {
        assertion = builtins.length deploymentRepositoryIds == builtins.length (lib.unique deploymentRepositoryIds);
        message = "gaoji deployment repository identities must be unique";
      }
      {
        assertion = builtins.length deployerIds == builtins.length (lib.unique deployerIds);
        message = "gaoji deployer identities must be unique";
      }
      {
        assertion = lib.all (repository:
          builtins.match "^[a-z][a-z0-9_-]{0,63}$" repository.repositoryId != null
          && repository.allowedChanges != []
          && repository.targets != []
          && lib.all (target: builtins.elem target.hostId inventoryHostIds) repository.targets
          && (if repository.backend == "ops" then
            cfg.ops.management.enable && lib.all (target:
              builtins.elem target.hostId cfg.ops.management.hosts
              && builtins.match "^[a-z][a-z0-9_-]{0,63}$" target.opsRepository != null
              && builtins.match "^[a-z][a-z0-9_-]{0,63}$" target.opsProfile != null
            ) repository.targets
          else lib.all (target: target.sshTarget != "") repository.targets)
        ) cfg.deployments.repositories;
        message = "gaoji deployment repositories require valid IDs, changes and inventory targets";
      }
      {
        assertion = lib.all (deployer:
          builtins.match "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$" deployer.deployerId != null
          && deployer.repositoryIds != []
          && lib.all (repositoryId: builtins.elem repositoryId deploymentRepositoryIds) deployer.repositoryIds
          && lib.all (repositoryId: lib.any (repository:
            repository.repositoryId == repositoryId && repository.backend == "ssh"
          ) cfg.deployments.repositories) deployer.repositoryIds
        ) cfg.deployments.deployers;
        message = "gaoji deployers may only reference configured repositories";
      }
    ];

    networking.firewall.allowedTCPPorts = lib.optionals (cfg.openFirewall && cfg.firewallInterfaces == []) [cfg.port];
    networking.firewall.interfaces = lib.genAttrs (lib.optionals cfg.openFirewall cfg.firewallInterfaces) (_: {
      allowedTCPPorts = [cfg.port];
    });

    systemd.services.gaoji-cluster-control = {
      description = "gaoji authenticated cluster control service";
      wantedBy = ["multi-user.target"];
      wants = ["network-online.target"];
      after = ["network-online.target"];
      environment = {
        KC_HOST = cfg.listenAddress;
        KC_PORT = toString cfg.port;
        KC_LOCAL_HOST_ID = cfg.localHostId;
        KC_API_TOKEN_FILE = "%d/api-token";
        KC_OPS_ENABLED = if cfg.ops.enable then "true" else "false";
        KC_OPS_BASE_URL = cfg.ops.baseUrl;
        KC_OPS_TOKEN_FILE = if cfg.ops.enable then "%d/ops-token" else "";
        KC_OPS_MANAGEMENT_TOKEN_FILE = if cfg.ops.management.enable then "%d/ops-management-token" else "";
        KC_OPS_MANAGEMENT_HOSTS = builtins.toJSON cfg.ops.management.hosts;
        KC_OPS_MANAGEMENT_ACTORS = builtins.toJSON cfg.ops.management.actors;
        KC_HOST_CONTROL_HELPERS_JSON = builtins.toJSON cfg.hostControlHelpers;
        KC_SSH_TARGETS_JSON = builtins.toJSON (if cfg.ssh.enable then cfg.ssh.targets else {});
        KC_SSH_KNOWN_HOSTS_FILE = if cfg.ssh.enable then "%d/ssh-known-hosts" else "";
        KC_SSH_IDENTITY_FILE = if cfg.ssh.enable && cfg.ssh.identityFile != null then "%d/ssh-identity" else "";
        KC_SSH_BINARY = "${pkgs.openssh}/bin/ssh";
        KC_SSH_MANAGEMENT_HOSTS = builtins.toJSON (if cfg.ssh.enable then cfg.ssh.managementHosts else []);
        KC_SSH_MANAGEMENT_ACTORS = builtins.toJSON (if cfg.ssh.enable then cfg.ssh.administrators else []);
        KC_OPS_TIMEOUT_SECONDS = toString cfg.ops.timeoutSeconds;
        KC_CACHE_SECONDS = toString cfg.cacheSeconds;
        KC_INVENTORY_JSON = builtins.toJSON cfg.inventory;
        KC_DIAGNOSTIC_TARGETS_JSON = builtins.toJSON cfg.diagnostics.targets;
        KC_WORKER_IDENTITIES_JSON = builtins.toJSON (map (worker: {
          worker_id = worker.workerId;
          host_id = worker.hostId;
          owner_aliases = worker.ownerAliases;
          token_file = "%d/worker-${worker.workerId}";
        }) cfg.workers);
        KC_DEPLOYMENT_REPOSITORIES_JSON = builtins.toJSON (map (repository: {
          repository_id = repository.repositoryId;
          backend = repository.backend;
          url = repository.url;
          default_branch = repository.defaultBranch;
          resource_version = repository.resourceVersion;
          allowed_changes = repository.allowedChanges;
          targets = map (target: {
            host_id = target.hostId;
            flake_host = target.flakeHost;
            ssh_target = target.sshTarget;
            ops_repository = target.opsRepository;
            ops_profile = target.opsProfile;
            verification_units = target.verificationUnits;
            use_remote_sudo = target.useRemoteSudo;
            resource_version = target.resourceVersion;
          }) repository.targets;
        }) cfg.deployments.repositories);
        KC_DEPLOYER_IDENTITIES_JSON = builtins.toJSON (map (deployer: {
          deployer_id = deployer.deployerId;
          token_file = "%d/deployer-${deployer.deployerId}";
          repository_ids = deployer.repositoryIds;
        }) cfg.deployments.deployers);
        KC_ARTIFACT_DIR = "/var/lib/${cfg.stateDirectory}/artifacts";
        PYTHONUNBUFFERED = "1";
      } // cfg.environment;
      serviceConfig =
        {
          Type = "simple";
          DynamicUser = true;
          StateDirectory = cfg.stateDirectory;
          WorkingDirectory = "${cfg.package}/share/gaoji";
          LoadCredential =
            lib.optional (cfg.apiTokenFile != null) "api-token:${cfg.apiTokenFile}"
            ++ lib.optional (cfg.ssh.enable && cfg.ssh.knownHostsFile != null) "ssh-known-hosts:${toString cfg.ssh.knownHostsFile}"
            ++ lib.optional (cfg.ssh.enable && cfg.ssh.identityFile != null) "ssh-identity:${cfg.ssh.identityFile}"
            ++ lib.optional (cfg.ops.enable && cfg.ops.tokenFile != null) "ops-token:${cfg.ops.tokenFile}"
            ++ lib.optional (cfg.ops.management.enable && cfg.ops.management.tokenFile != null) "ops-management-token:${cfg.ops.management.tokenFile}"
            ++ map (worker: "worker-${worker.workerId}:${worker.tokenFile}") cfg.workers
            ++ map (deployer: "deployer-${deployer.deployerId}:${deployer.tokenFile}") cfg.deployments.deployers;
          # The bot starts after this service, so the control plane owns the
          # idempotent schema upgrade and avoids a startup dependency cycle.
          ExecStartPre = "${cfg.package}/bin/gaoji-db upgrade";
          ExecStart = "${cfg.package}/bin/gaoji-cluster-control";
          Restart = "on-failure";
          RestartSec = 5;
          UMask = "0077";

          LockPersonality = true;
          NoNewPrivileges = true;
          PrivateDevices = true;
          PrivateTmp = true;
          ProtectClock = true;
          ProtectControlGroups = true;
          ProtectHome = true;
          ProtectKernelLogs = true;
          ProtectKernelModules = true;
          ProtectKernelTunables = true;
          ProtectSystem = "strict";
          RestrictAddressFamilies = ["AF_INET" "AF_INET6" "AF_UNIX"];
          RestrictSUIDSGID = true;
        }
        // lib.optionalAttrs (cfg.environmentFile != null) {
          EnvironmentFile = cfg.environmentFile;
        };
    };
  };
}
