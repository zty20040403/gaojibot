{self}: {
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.gaoji;
  serviceName = "gaoji";
  statePath = "/var/lib/${cfg.stateDirectory}";
  cachePath = "/var/cache/${cfg.cacheDirectory}";
  interestName = "gaoji-interest";
  interestStateDirectory = "${cfg.stateDirectory}-interest";
  interestStatePath = "/var/lib/${interestStateDirectory}";
  interestResolver = pkgs.writeText "gaoji-interest-resolv.conf" "nameserver 1.1.1.1\n";
  interestNss = pkgs.writeText "gaoji-interest-nsswitch.conf" "hosts: files dns\n";
  defaultPackage = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
  sandboxCt = cfg.runBot && cfg.sandbox.enable && cfg.sandbox.backend == "container";
  ctBroker = "/run/gaoji-ct/broker.sock";
  snapshotDir = "/data/services/gaoji/backups/work";
  ctBridge = "vz-gaoji";
  ctResolver = pkgs.writeText "gaoji-ct-resolv.conf" (lib.concatMapStrings (server: "nameserver ${server}\n") cfg.sandbox.containerDns);
  # The operations network: some conversations' containers join a network
  # namespace with a Tailscale node of its own, so `ssh <host>` in them reaches
  # the fleet as gaoji (Tailscale SSH, no key in the container). Other
  # containers stay on the filtered public-only bridge.
  ops = cfg.sandbox.operations;
  sandboxOps = sandboxCt && ops.enable;
  opsName = "${serviceName}-ops";
  opsNetns = opsName;
  opsUplink = "${serviceName}-uplink";
  opsHost = "${ops.uplink}.1";
  opsPeer = "${ops.uplink}.2";
  opsSocket = "/run/${opsName}-tailscale/tailscaled.sock";
  opsTailscale = "${pkgs.tailscale}/bin/tailscale --socket=${opsSocket}";
  opsResolver = pkgs.writeText "${opsName}-resolv.conf" (lib.concatMapStrings (server: "nameserver ${server}\n") ops.dns);
  # Containers on the operations network resolve fleet names via MagicDNS first, falling back to public DNS.
  opsGuestResolver = pkgs.writeText "${opsName}-guest-resolv.conf" (
    "nameserver 100.100.100.100\n" + lib.concatMapStrings (server: "nameserver ${server}\n") ops.dns
  );
  defaultWhisperModel = pkgs.fetchurl {
    url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin";
    hash = "sha256-YO1bw90U7qhWST0zQ0m0BXgt3K8AKNS130CINF+6Lv4=";
  };
  boolString = value:
    if value
    then "true"
    else "false";
  officialCredentialPath = value:
    value
    != null
    && !builtins.hasContext value
    && lib.hasPrefix "/" value
    && value != "/"
    && value != "/nix/store"
    && !(lib.hasPrefix "/nix/store/" value)
    && !(lib.hasInfix "//" value)
    && !(lib.hasInfix "\n" value)
    && !(lib.hasInfix "\r" value)
    && lib.all (part: part != "." && part != "..") (lib.splitString "/" value);
in {
  options.services.gaoji = {
    enable = lib.mkEnableOption "the gaoji multi-model bot";

    runBot = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Run the bot process on this host; disable to keep only a local QQ transport during a single-active migration.";
    };

    package = lib.mkOption {
      type = lib.types.package;
      default = defaultPackage;
      defaultText = lib.literalExpression "inputs.qq-bot.packages.${pkgs.stdenv.hostPlatform.system}.default";
      description = "Packaged bot application to run.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/run/secrets/gaoji.env";
      description = ''
        Runtime environment file containing API keys and bot settings. Keep this
        file outside the Nix store; do not pass a Nix path containing secrets.
      '';
    };

    admin = {
      secretFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Private persistent mobile-authorization key file; kept outside the Nix store and loaded as a systemd credential.";
      };
      origin = lib.mkOption {
        type = lib.types.str;
        default = "";
        example = "https://bot.example.com";
        description = "Public HTTPS origin of the account console, without a path.";
      };
    };

    environment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = {};
      example = {AI_ENABLED_GROUPS = "123456789";};
      description = "Non-secret environment variables for the bot service.";
    };

    qqOfficial = {
      enable = lib.mkEnableOption "QQ official durable ingress, shared turns and delivery";

      hostConfigFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "/run/secrets/gaoji-qq-official-host.json";
        description = "Protected host configuration for native services, verified native/download file routes and reviewed migration sources; loaded with systemd credentials. Absent file routes stay disabled. Loading this file never starts a migration or configures a public proxy.";
      };

      appIdFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "/run/secrets/gaoji-qq-app-id";
        description = "Protected AppID file, supplied as an absolute string outside the Nix store; loaded with systemd credentials.";
      };

      appSecretFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "/run/secrets/gaoji-qq-app-secret";
        description = "Protected AppSecret file, supplied as an absolute string outside the Nix store. Never put its contents in Nix configuration.";
      };

      bindPepperFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "/run/secrets/gaoji-qq-bind-pepper";
        description = "Protected binding HMAC pepper file containing 32 independently random bytes as 64 hexadecimal characters; loaded with systemd credentials.";
      };

      apiBase = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "Empty selects the protocol default for the chosen environment. Explicit addresses must pass the application's trusted-host and environment checks.";
      };

      sandbox = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Use the separate sandbox account namespace and API default; actual sandbox availability still requires verification.";
      };

      accountKey = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "Optional exact canonical AccountKey guard; when set, it must match the protected AppID and selected environment.";
      };

      ownerOpenids = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [];
        description = "Explicit C2C owner openids for this account, at most 32. First contact, group roles and matching nicknames never grant ownership.";
      };

      privatePolicy = lib.mkOption {
        type = lib.types.enum ["owner_only" "allowlist" "public"];
        default = "owner_only";
        description = "Local private-message admission, independent of Tencent's service scope. Public admission grants no owner privilege.";
      };

      privateAllowedOpenids = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [];
        description = "C2C identities admitted by allowlist policy for this account, at most 256; these entries do not grant management authority.";
      };

      groupAdmission = lib.mkOption {
        type = lib.types.enum ["manual_bind"];
        default = "manual_bind";
        description = "Groups require the two-sided binding flow. Receiving all group messages never enables an unbound group.";
      };

      markdown = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Enable Markdown rendering when the endpoint contract supports it; explicit unaccepted format rejection may downgrade.";
      };

      inboxPayloadTtlHours = lib.mkOption {
        type = lib.types.ints.between 1 168;
        default = 24;
        description = "Bounded inbox payload retention. Expire pending work before purging; retain deduplication tombstones, indexes and dispatch uniqueness.";
      };
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = serviceName;
      description = "User account that runs the bot.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = serviceName;
      description = "Primary group of the bot process.";
    };

    stateDirectory = lib.mkOption {
      type = lib.types.str;
      default = serviceName;
      description = "Name of the systemd-managed directory under /var/lib.";
    };

    cacheDirectory = lib.mkOption {
      type = lib.types.str;
      default = serviceName;
      description = "Name of the systemd-managed directory under /var/cache.";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Listen address for the management HTTP server.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8080;
      description = "Listen port for the management HTTP server.";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open the configured TCP port in the NixOS firewall.";
    };

    runtimePackages = lib.mkOption {
      type = lib.types.listOf lib.types.package;
      default = [];
      description = "Additional executables exposed to bot tools through PATH.";
    };

    database.migrateOnStart = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Run Alembic before starting the bot. When disabled, startup only checks
        that PostgreSQL is already at the revision required by this package.
      '';
    };

    sandbox.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Allow persistent conversation execution workspaces.";
    };

    sandbox.workSnapshots.enable = lib.mkEnableOption "daily host-only archives of the container work directories";

    sandbox.allowedUsers = lib.mkOption {
      type = lib.types.listOf lib.types.ints.unsigned;
      default = [];
      description = "QQ users allowed to use sandboxes. An empty list denies access.";
    };

    sandbox.allowAll = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Explicitly allow every user to run sandbox commands.";
    };

    sandbox.cpus = lib.mkOption {
      type = lib.types.ints.between 1 16;
      default = 2;
      description = "CPU limit for each conversation container.";
    };

    sandbox.pidsLimit = lib.mkOption {
      type = lib.types.ints.between 32 4096;
      default = 256;
      description = "Process limit for each sandbox user.";
    };

    sandbox.memoryMiB = lib.mkOption {
      type = lib.types.ints.between 256 8192;
      default = 8192;
      description = "Memory cap in MiB per conversation container.";
    };

    sandbox.backend = lib.mkOption {
      type = lib.types.enum ["container"];
      default = "container";
      description = "Conversation workspaces run as NixOS containers (systemd-nspawn).";
    };

    sandbox.containerSystem = lib.mkOption {
      type = lib.types.package;
      default = self.nixosConfigurations.gaoji-container.config.system.build.toplevel;
      defaultText = lib.literalExpression "inputs.qq-bot.nixosConfigurations.gaoji-container.config.system.build.toplevel";
      description = "The NixOS system every conversation container boots.";
    };

    sandbox.containerDns = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = ["1.1.1.1" "223.5.5.5"];
      description = "Public resolvers for ordinary conversation containers.";
    };

    sandbox.vmRoot = lib.mkOption {
      type = lib.types.str;
      default = "${statePath}/vms";
      description = "Persistent root for per-conversation sandbox metadata and host locks.";
    };

    sandbox.operations = {
      enable = lib.mkEnableOption "an operations network for some conversations' VMs";
      owners = lib.mkOption {
        type = lib.types.listOf (lib.types.strMatching "qqo:conversation:[0-9a-f]{64}");
        default = [];
        description = "VM owners (qqo:conversation:<sha256 of the endpoint key>) whose VMs run on the operations network.";
      };
      loginServer = lib.mkOption {
        type = lib.types.str;
        description = "The tailnet's control server.";
      };
      domain = lib.mkOption {
        type = lib.types.str;
        description = "The tailnet's DNS domain; `ssh h610` means h610.<domain>.";
      };
      hostname = lib.mkOption {
        type = lib.types.str;
        default = "gaojiops";
        description = "The operations node's name in the tailnet.";
      };
      authKeyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Pre-auth key for the node's first login, outside the Nix store; later starts reuse its state.";
      };
      uplink = lib.mkOption {
        type = lib.types.strMatching "[0-9]+\\.[0-9]+\\.[0-9]+";
        default = "10.237.0";
        description = "The /30 between the host (.1) and the namespace (.2), without its last octet.";
      };
      dns = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = ["1.1.1.1" "223.5.5.5"];
        description = "Public resolvers the node uses for names outside the tailnet.";
      };
    };

    sandbox.vmHostPorts = lib.mkOption {
      type = lib.types.nonEmptyListOf lib.types.port;
      default = [18640 18641];
      description = "Host loopback TCP ports a VM may reach at 10.0.2.2: the background-agent model relay and app-server tunnel. Every other host, LAN, tailnet and link-local address is rejected for VM traffic.";
    };

    voiceAsr = {
      enable = lib.mkEnableOption "host-local voice transcription when Tencent provides no text";
      package = lib.mkOption {
        type = lib.types.package;
        default = pkgs.whisper-cpp;
        description = "whisper.cpp package for short QQ voice messages.";
      };
      model = lib.mkOption {
        type = lib.types.package;
        default = defaultWhisperModel;
        description = "Multilingual Whisper model for short QQ voice messages.";
      };
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [{
    assertions = [
      {
        assertion = !cfg.runBot || cfg.qqOfficial.enable;
        message = "A running Gaoji application requires QQ official to be enabled";
      }
      {
        assertion = !cfg.sandbox.enable || (
          lib.hasPrefix "/" cfg.sandbox.vmRoot
          && !(lib.hasPrefix "/nix/store/" cfg.sandbox.vmRoot)
        );
        message = "Sandboxes require a persistent absolute vmRoot outside the Nix store";
      }
      {
        assertion = builtins.match "^[A-Za-z0-9_.-]+$" cfg.stateDirectory != null;
        message = "services.gaoji.stateDirectory must be a directory name, not a path";
      }
      {
        assertion = builtins.match "^[A-Za-z0-9_.-]+$" cfg.cacheDirectory != null;
        message = "services.gaoji.cacheDirectory must be a directory name, not a path";
      }
      {
        assertion = !cfg.runBot || (cfg.user != interestName && cfg.group != interestName);
        message = "The bot and its primary group must differ from the dedicated interest account";
      }
      {
        assertion = !cfg.qqOfficial.enable || cfg.qqOfficial.hostConfigFile == null
          || officialCredentialPath cfg.qqOfficial.hostConfigFile;
        message = "QQ official hostConfigFile must be a protected absolute string path outside /nix/store";
      }
      {
        assertion =
          !cfg.qqOfficial.enable
          || lib.all officialCredentialPath [
            cfg.qqOfficial.appIdFile
            cfg.qqOfficial.appSecretFile
            cfg.qqOfficial.bindPepperFile
          ];
        message = "QQ official ingress requires protected appIdFile, appSecretFile and bindPepperFile absolute string paths outside /nix/store, without Nix string context or dot segments";
      }
      {
        assertion =
          !cfg.qqOfficial.enable
          || (
            builtins.length cfg.qqOfficial.ownerOpenids
            <= 32
            && builtins.length cfg.qqOfficial.privateAllowedOpenids <= 256
          );
        message = "QQ official configuration permits at most 32 owner openids and 256 private allowlist entries";
      }
    ];

    users.groups.${serviceName} = lib.mkIf (cfg.group == serviceName) {};
    users.groups.${interestName} = lib.mkIf cfg.runBot {};
    users.users.${interestName} = lib.mkIf cfg.runBot {
      isSystemUser = true;
      group = interestName;
    };
    users.users.${serviceName} = lib.mkIf (cfg.user == serviceName) {
      isSystemUser = true;
      group = cfg.group;
      home = statePath;
    };

    networking.firewall.allowedTCPPorts = lib.optionals cfg.openFirewall [cfg.port];

    systemd.services.${serviceName} = lib.mkIf cfg.runBot {
      description = "gaoji multi-model bot";
      wantedBy = ["multi-user.target"];
      unitConfig.RequiresMountsFor = lib.optionals cfg.sandbox.enable [cfg.sandbox.vmRoot];
      wants = ["network-online.target"];
      requires = lib.optionals sandboxCt ["gaoji-ct-broker.socket"];
      after =
        ["network-online.target"]
        ++ lib.optionals sandboxCt ["gaoji-ct-broker.socket"];
      path =
        (cfg.runtimePackages ++ [pkgs.ffmpeg-headless])
        ++ lib.optionals cfg.voiceAsr.enable [cfg.voiceAsr.package];
      environment =
        {
          HOME = statePath;
          AI_STATE_DIR = "${statePath}/state";
          AI_CACHE_DIR = cachePath;
          AI_SANDBOX_ENABLED = boolString cfg.sandbox.enable;
          AI_SANDBOX_ALLOWED_USERS = lib.concatMapStringsSep "," toString cfg.sandbox.allowedUsers;
          AI_SANDBOX_ALLOW_ALL = boolString cfg.sandbox.allowAll;
          AI_SANDBOX_CPUS = toString cfg.sandbox.cpus;
          AI_SANDBOX_PIDS_LIMIT = toString cfg.sandbox.pidsLimit;
          AI_SANDBOX_MEMORY_MIB = toString cfg.sandbox.memoryMiB;
          HOST = cfg.host;
          PORT = toString cfg.port;
          PYTHONUNBUFFERED = "1";
        }
        // lib.optionalAttrs cfg.sandbox.enable {
          AI_SANDBOX_BACKEND = cfg.sandbox.backend;
          AI_SANDBOX_VM_ROOT = cfg.sandbox.vmRoot;
        }
        // lib.optionalAttrs sandboxCt {
          AI_SANDBOX_CONTAINER_BROKER = ctBroker;
        }
        // lib.optionalAttrs sandboxOps {
          AI_SANDBOX_OPERATIONS_OWNERS = lib.concatStringsSep "," ops.owners;
          AI_SANDBOX_OPERATIONS_DOMAIN = ops.domain;
        }
        // lib.optionalAttrs (cfg.admin.secretFile != null) {
          AI_ADMIN_SECRET_FILE = "/run/credentials/${serviceName}.service/admin-authorization-key";
          AI_ADMIN_ORIGIN = cfg.admin.origin;
        }
        // lib.optionalAttrs (cfg.qqOfficial.enable && cfg.qqOfficial.hostConfigFile != null) {
          AI_QQ_OFFICIAL_HOST_CONFIG_FILE = "/run/credentials/${serviceName}.service/qq-official-host-config";
        }
        // lib.optionalAttrs cfg.qqOfficial.enable {
          AI_QQ_OFFICIAL_ENABLED = "true";
          AI_QQ_OFFICIAL_APP_ID_FILE = "/run/credentials/${serviceName}.service/qq-official-app-id";
          AI_QQ_OFFICIAL_APP_SECRET_FILE = "/run/credentials/${serviceName}.service/qq-official-app-secret";
          AI_QQ_OFFICIAL_BIND_PEPPER_FILE = "/run/credentials/${serviceName}.service/qq-official-bind-pepper";
          AI_QQ_OFFICIAL_API_BASE = cfg.qqOfficial.apiBase;
          AI_QQ_OFFICIAL_SANDBOX = boolString cfg.qqOfficial.sandbox;
          AI_QQ_OFFICIAL_ACCOUNT_KEY = cfg.qqOfficial.accountKey;
          AI_QQ_OFFICIAL_OWNER_OPENIDS = builtins.toJSON cfg.qqOfficial.ownerOpenids;
          AI_QQ_OFFICIAL_PRIVATE_POLICY = cfg.qqOfficial.privatePolicy;
          AI_QQ_OFFICIAL_PRIVATE_ALLOWED_OPENIDS = builtins.toJSON cfg.qqOfficial.privateAllowedOpenids;
          AI_QQ_OFFICIAL_GROUP_ADMISSION = cfg.qqOfficial.groupAdmission;
          AI_QQ_OFFICIAL_INBOX_PAYLOAD_TTL_HOURS = toString cfg.qqOfficial.inboxPayloadTtlHours;
          AI_QQ_OFFICIAL_MARKDOWN = boolString cfg.qqOfficial.markdown;
        }
        // lib.optionalAttrs cfg.voiceAsr.enable {
          AI_VIDEO_WHISPER_MODEL_PATH = toString cfg.voiceAsr.model;
        }
        // cfg.environment
        // {AI_GLOBAL_INTEREST_STATE_DIR = interestStatePath;};

      serviceConfig =
        {
          Type = "simple";
          User = cfg.user;
          Group = cfg.group;
          SupplementaryGroups = [interestName];
          StateDirectory = cfg.stateDirectory;
          CacheDirectory = cfg.cacheDirectory;
          WorkingDirectory = "${cfg.package}/share/gaoji";
          ExecStartPre = "${cfg.package}/bin/gaoji-db ${
            if cfg.database.migrateOnStart
            then "upgrade"
            else "check"
          }";
          ExecStart = "${pkgs.coreutils}/bin/env AI_GLOBAL_INTEREST_STATE_DIR=${interestStatePath} ${lib.getExe cfg.package}";
          Restart = "on-failure";
          RestartSec = 5;
          UMask = "0077";
          LoadCredential =
            lib.optional (cfg.admin.secretFile != null) "admin-authorization-key:${cfg.admin.secretFile}"
            ++ lib.optional (cfg.qqOfficial.enable && cfg.qqOfficial.appIdFile != null) "qq-official-app-id:${cfg.qqOfficial.appIdFile}"
            ++ lib.optional (cfg.qqOfficial.enable && cfg.qqOfficial.appSecretFile != null) "qq-official-app-secret:${cfg.qqOfficial.appSecretFile}"
            ++ lib.optional (cfg.qqOfficial.enable && cfg.qqOfficial.bindPepperFile != null) "qq-official-bind-pepper:${cfg.qqOfficial.bindPepperFile}"
            ++ lib.optional (cfg.qqOfficial.enable && cfg.qqOfficial.hostConfigFile != null) "qq-official-host-config:${cfg.qqOfficial.hostConfigFile}";

          NoNewPrivileges = true;
          PrivateTmp = true;
          ProtectControlGroups = true;
          ProtectHome = "read-only";
          ProtectKernelModules = true;
          ProtectKernelTunables = true;
          ProtectSystem = "strict";
          ReadWritePaths = lib.optionals cfg.sandbox.enable [cfg.sandbox.vmRoot];
          RestrictSUIDSGID = true;
        }
        // lib.optionalAttrs (cfg.environmentFile != null) {
          EnvironmentFile = cfg.environmentFile;
        };
    };

    systemd.services.${interestName} = lib.mkIf cfg.runBot {
      description = "Collect public global interest candidates";
      wants = ["network-online.target"];
      after = ["network-online.target"];
      serviceConfig = {
        Type = "oneshot";
        User = interestName;
        Group = interestName;
        StateDirectory = interestStateDirectory;
        StateDirectoryMode = "0750";
        ExecStart = "${cfg.package}/bin/gaoji-interest ${interestStatePath}";
        TimeoutStartSec = 30;
        UMask = "0027";
        NoNewPrivileges = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        ProtectProc = "invisible";
        RestrictSUIDSGID = true;
        LockPersonality = true;
        CapabilityBoundingSet = "";
        RestrictAddressFamilies = ["AF_UNIX" "AF_INET" "AF_INET6"];
        InaccessiblePaths = ["-${statePath}" "-${cachePath}" "-/run/postgresql" "-/run/credentials"];
        BindReadOnlyPaths = ["${interestResolver}:/etc/resolv.conf" "${interestNss}:/etc/nsswitch.conf"];
        IPAddressDeny = ["localhost" "link-local" "multicast" "0.0.0.0/8" "10.0.0.0/8"
          "100.64.0.0/10" "172.16.0.0/12" "192.168.0.0/16" "198.18.0.0/15"
          "fc00::/7" "::/128" "::ffff:0:0/96"];
      };
    };

    systemd.timers.${interestName} = lib.mkIf cfg.runBot {
      description = "Daily public global interest collection";
      wantedBy = ["timers.target"];
      timerConfig = {
        OnCalendar = "*-*-* 05:00:00 Asia/Shanghai";
        Persistent = true;
        Unit = "${interestName}.service";
      };
    };

    # The operations namespace, its uplink, and what it may reach: the
    # internet as itself (NAT on the host), the fleet by its own tailnet node.
    systemd.services."${opsName}-network" = lib.mkIf sandboxOps {
      description = "gaoji operations network namespace";
      wantedBy = ["multi-user.target"];
      # Relay sockets in sockets.target (before basic.target) depend on this
      # namespace; omit implicit After=basic.target to avoid an ordering cycle.
      unitConfig.DefaultDependencies = false;
      conflicts = ["shutdown.target"];
      after = ["sysinit.target" "nftables.service"];
      before = ["shutdown.target" "${opsName}-tailscaled.service"];
      path = [pkgs.iproute2 pkgs.nftables pkgs.procps];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      script = ''
        if [ ! -e /run/netns/${opsNetns} ]; then
          ip netns add ${opsNetns}
        fi
        if ! ip link show dev ${opsUplink} >/dev/null 2>&1; then
          ip link add ${opsUplink} type veth peer name uplink netns ${opsNetns}
        fi
        ip address replace ${opsHost}/30 dev ${opsUplink}
        ip link set dev ${opsUplink} up
        ip -n ${opsNetns} link set lo up
        ip -n ${opsNetns} address replace ${opsPeer}/30 dev uplink
        ip -n ${opsNetns} link set uplink up
        ip -n ${opsNetns} route replace default via ${opsHost}
        # Containers on this network share the namespace and reach the
        # host's relay at 10.0.2.2, as every guest does.
        ip -n ${opsNetns} address replace 10.0.2.2/32 dev lo
        # Nothing in the namespace takes a connection from the uplink.
        ip netns exec ${opsNetns} nft -f - <<EOF
        table inet ${opsNetns}
        delete table inet ${opsNetns}
        table inet ${opsNetns} {
          chain input {
            type filter hook input priority filter; policy accept;
            iifname "uplink" ct state { established, related } accept
            iifname "uplink" drop
          }
          chain output {
            type filter hook output priority mangle; policy accept;
            oifname "tailscale0" tcp flags syn tcp option maxseg size set rt mtu
          }
        }
        EOF
      '';
      preStop = ''
        ip link del ${opsUplink} 2>/dev/null || true
        ip netns del ${opsNetns} 2>/dev/null || true
      '';
    };

    # The namespace reaches the internet and nothing private on the host's side.
    networking.firewall.extraForwardRules = lib.mkIf sandboxOps ''
      iifname "${opsUplink}" accept
    '';
    networking.nftables.tables.${opsName} = lib.mkIf sandboxOps {
      family = "inet";
      content = ''
        set non_public_v4 {
          type ipv4_addr
          flags interval
          elements = {
            0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8,
            169.254.0.0/16, 172.16.0.0/12, 192.0.0.0/24,
            192.0.2.0/24, 192.168.0.0/16, 198.18.0.0/15,
            198.51.100.0/24, 203.0.113.0/24, 224.0.0.0/4, 240.0.0.0/4
          }
        }
        chain input {
          type filter hook input priority -10; policy accept;
          iifname "${opsUplink}" ct state { established, related } accept
          iifname "${opsUplink}" counter reject with icmpx type admin-prohibited
        }
        chain forward {
          type filter hook forward priority -10; policy accept;
          iifname "${opsUplink}" ct state { established, related } accept
          iifname "${opsUplink}" meta nfproto ipv6 counter drop
          iifname "${opsUplink}" ip saddr != ${opsPeer} counter drop
          iifname "${opsUplink}" ip daddr @non_public_v4 counter reject with icmpx type admin-prohibited
          oifname "${opsUplink}" ct state != { established, related } counter drop
        }
        chain postrouting {
          type nat hook postrouting priority srcnat; policy accept;
          ip saddr ${opsPeer} oifname != "${opsUplink}" masquerade
        }
      '';
    };

    # The fleet's side: a tailnet node of its own in the namespace, no SSH
    # server, the tailnet's DNS answered at 100.100.100.100.
    systemd.services."${opsName}-tailscaled" = lib.mkIf sandboxOps {
      description = "gaoji operations network Tailscale client";
      wantedBy = ["multi-user.target"];
      requires = ["${opsName}-network.service"];
      after = ["${opsName}-network.service" "network-online.target"];
      wants = ["network-online.target"];
      path = [pkgs.procps pkgs.getent pkgs.kmod];
      serviceConfig =
        {
          Type = "notify";
          ExecStart = "${pkgs.tailscale}/bin/tailscaled --state=/var/lib/${opsName}-tailscale/tailscaled.state --socket=${opsSocket} --tun=tailscale0 --port=41642";
          NetworkNamespacePath = "/run/netns/${opsNetns}";
          StateDirectory = "${opsName}-tailscale";
          StateDirectoryMode = "0700";
          RuntimeDirectory = "${opsName}-tailscale";
          RuntimeDirectoryMode = "0700";
          BindReadOnlyPaths = ["${opsResolver}:/etc/resolv.conf"];
          Restart = "on-failure";
          RestartSec = 5;
        }
        // lib.optionalAttrs (ops.authKeyFile != null) {
          LoadCredential = ["auth-key:${ops.authKeyFile}"];
        };
      postStart = ''
        ${opsTailscale} up --login-server=${lib.escapeShellArg ops.loginServer} \
          ${lib.optionalString (ops.authKeyFile != null) ''--auth-key="file:$CREDENTIALS_DIRECTORY/auth-key"''} \
          --hostname=${lib.escapeShellArg ops.hostname} --accept-routes=true --accept-dns=false --ssh=false --timeout=30s
      '';
    };

    systemd.tmpfiles.rules =
      lib.optionals cfg.sandbox.enable [
        "d ${statePath} 0700 ${cfg.user} ${cfg.group} -"
        "d ${cfg.sandbox.vmRoot} 0700 ${cfg.user} ${cfg.group} -"
      ];
  }
  (lib.mkIf sandboxOps {
      # The VMs' relay ports (10.0.2.2 → 127.0.0.1) are the host's: listen on
      # the namespace's loopback and forward to the host's.
      systemd.sockets = (lib.listToAttrs (map (port:
        lib.nameValuePair "${opsName}-relay-${toString port}" {
          description = "gaoji operations network relay port ${toString port}";
          wantedBy = ["sockets.target"];
          requires = ["${opsName}-network.service"];
          after = ["${opsName}-network.service"];
          socketConfig = {
            ListenStream = ["127.0.0.1:${toString port}" "10.0.2.2:${toString port}"];
            FreeBind = true;
            NetworkNamespacePath = "/run/netns/${opsNetns}";
          };
        })
      cfg.sandbox.vmHostPorts));
      systemd.services = (lib.listToAttrs (map (port:
        lib.nameValuePair "${opsName}-relay-${toString port}" {
          description = "gaoji operations network relay port ${toString port}";
          requires = ["${opsName}-relay-${toString port}.socket"];
          serviceConfig = {
            ExecStart = "${config.systemd.package}/lib/systemd/systemd-socket-proxyd 127.0.0.1:${toString port}";
            DynamicUser = true;
            PrivateTmp = true;
          };
        })
      cfg.sandbox.vmHostPorts));
  })
  (lib.mkIf sandboxCt (let
    # One root script starts every conversation container. The root is
    # recreated each start (stateless system); /workspace persists and is
    # idmapped so the container's sandbox user (1000) owns it.
    start = pkgs.writeShellScript "gaoji-ct-start" ''
      set -euo pipefail
      id=$1 mode=$2
      [[ $id =~ ^s[0-9a-f]{6}$ ]] || { echo "invalid sandbox id" >&2; exit 2; }
      base=/var/lib/gaoji-ct/$id
      install -d -m 0700 /var/lib/gaoji-ct "$base"
      [ -d "$base/work" ] || install -d -m 0700 -o 1000 -g 1000 "$base/work"
      rm -rf "$base/root"
      install -d -m 0755 "$base/root" "$base/root/etc" "$base/root/usr" "$base/root/var/lib"
      touch "$base/root/etc/os-release"
      if [ "$mode" = ops ]; then
        net=(); resolv=${opsGuestResolver}
      else
        net=(--network-bridge=${ctBridge}); resolv=${ctResolver}
      fi
      exec ${config.systemd.package}/bin/systemd-nspawn --quiet --keep-unit --register=yes \
        --machine="gct-$id" --notify-ready=yes --kill-signal=SIGRTMIN+3 \
        --directory="$base/root" --private-users=pick --private-users-ownership=auto \
        --link-journal=no --resolv-conf=off --timezone=off --bind-ro=/nix/store \
        --bind="$base/work:/workspace:idmap" --bind="$base/work:/work:idmap" \
        --bind-ro="$resolv:/etc/resolv.conf" "''${net[@]}" \
        ${cfg.sandbox.containerSystem}/init
    '';
    unit = mode: {
      description = "gaoji conversation container %i (${mode})";
      wants = lib.optional (mode == "plain") "gaoji-ct-cdp@%i.service";
      restartIfChanged = false;
      requires = lib.optionals (mode == "ops") ["${opsName}-network.service" "${opsName}-tailscaled.service"];
      after = ["network.target"] ++ lib.optionals (mode == "ops") ["${opsName}-network.service" "${opsName}-tailscaled.service"];
      serviceConfig =
        {
          Type = "notify";
          NotifyAccess = "all";
          ExecStart = "${start} %i ${mode}";
          KillMode = "mixed";
          Delegate = true;
          Slice = "machine.slice";
          DevicePolicy = "closed";
          DeviceAllow = ["char-pts rwm" "/dev/net/tun rwm"];
          TimeoutStartSec = 180;
          TimeoutStopSec = 60;
          SuccessExitStatus = 133;
          MemoryMax = "${toString cfg.sandbox.memoryMiB}M";
          CPUQuota = "${toString (cfg.sandbox.cpus * 100)}%";
          TasksMax = cfg.sandbox.pidsLimit * 8;
        }
        // lib.optionalAttrs (mode == "ops") {
          NetworkNamespacePath = "/run/netns/${opsNetns}";
        };
    };
  in lib.mkMerge [{
    assertions = [
      {
        assertion = config.systemd.network.enable;
        message = "gaoji's container sandbox uses systemd-networkd for its bridge and DHCP";
      }
      {
        assertion = config.networking.nftables.enable;
        message = "gaoji's container sandbox needs nftables";
      }
    ];

    # The only privileged path for the unprivileged bot: start, stop, status,
    # exec and remove, validated per request (src/sandbox_runtime/container_broker.py).
    systemd.sockets.gaoji-ct-broker = {
      description = "gaoji container broker socket";
      wantedBy = ["sockets.target"];
      socketConfig = {
        ListenStream = ctBroker;
        SocketUser = cfg.user;
        SocketGroup = cfg.group;
        SocketMode = "0600";
      };
    };
    systemd.services.gaoji-ct-broker = {
      description = "gaoji container broker";
      requires = ["gaoji-ct-broker.socket"];
      path = [config.systemd.package];
      serviceConfig.ExecStart = "${pkgs.python3}/bin/python3 ${../src/sandbox_runtime/container_broker.py}";
    };
    systemd.services.gaoji-work-snapshot = lib.mkIf cfg.sandbox.workSnapshots.enable {
      description = "Archive local conversation workspaces";
      unitConfig.RequiresMountsFor = [snapshotDir "/var/lib/gaoji-ct"];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${pkgs.python3}/bin/python3 -I ${./work_snapshot.py} /var/lib/gaoji-ct ${snapshotDir}";
        UMask = "0077";
        ProtectSystem = "strict";
        ReadWritePaths = [snapshotDir];
        ProtectHome = true;
        PrivateNetwork = true;
        PrivateDevices = true;
        NoNewPrivileges = true;
        CapabilityBoundingSet = ["CAP_DAC_READ_SEARCH"];
      };
    };
    systemd.timers.gaoji-work-snapshot = lib.mkIf cfg.sandbox.workSnapshots.enable {
      wantedBy = ["timers.target"];
      timerConfig = {
        OnCalendar = "*-*-* 03:00:00 UTC";
        Persistent = true;
      };
    };
    # Ops containers share loopback. Only plain containers get a per-machine CDP socket.
    systemd.services."gaoji-ct-cdp@" = let
      pipe = pkgs.writeShellScript "gaoji-cdp-pipe" ''
        exec ${config.systemd.package}/bin/systemd-run --machine="gct-$1" --pipe --quiet --wait --collect \
          --property=RuntimeMaxSec=610 -- /bin/sh -c \
          '${pkgs.coreutils}/bin/touch /run/gaoji-browser.used && exec ${pkgs.socat}/bin/socat STDIO TCP4:127.0.0.1:9222'
      '';
    in {
      bindsTo = ["gaoji-ct@%i.service"];
      after = ["gaoji-ct@%i.service"];
      serviceConfig.ExecStart = lib.escapeShellArgs [
        "${pkgs.socat}/bin/socat"
        "UNIX-LISTEN:/run/gaoji-ct/cdp-%i.sock,unlink-early,mode=0600,user=${cfg.user},fork"
        "EXEC:${pipe} %i"
      ];
    };
    systemd.services."gaoji-ct@" = unit "plain";
    systemd.services."gaoji-ct-ops@" = unit "ops";
    systemd.tmpfiles.rules = ["d /var/lib/gaoji-ct 0700 root root -"];

    # An isolated bridge: the host is 10.0.2.2, as for QEMU guests, so the
    # relay address and every guest tool stay the same. Ports cannot reach
    # each other; the host lets a container reach the internet, DHCP and the
    # relay ports, and nothing private.
    systemd.network.netdevs."60-${ctBridge}".netdevConfig = {
      Kind = "bridge";
      Name = ctBridge;
    };
    systemd.network.networks."60-${ctBridge}" = {
      matchConfig.Name = ctBridge;
      address = ["10.0.2.2/24"];
      networkConfig = {
        DHCPServer = true;
        ConfigureWithoutCarrier = true;
      };
      dhcpServerConfig = {
        PoolOffset = 16;
        PoolSize = 200;
        EmitDNS = false;
      };
      linkConfig.RequiredForOnline = "no";
    };
    systemd.network.networks."61-gaoji-ct-ports" = {
      matchConfig.Name = "vb-gct-*";
      networkConfig.Bridge = ctBridge;
      bridgeConfig.Isolated = true;
      linkConfig.RequiredForOnline = "no";
    };
    networking.firewall.interfaces.${ctBridge} = {
      allowedTCPPorts = cfg.sandbox.vmHostPorts;
      allowedUDPPorts = [67];
    };
    networking.firewall.extraForwardRules = ''
      iifname "${ctBridge}" accept
      oifname "${ctBridge}" ct state { established, related } accept
    '';
    networking.nftables.tables.gaoji-ct = {
      family = "inet";
      content = ''
        set non_public_v4 {
          type ipv4_addr
          flags interval
          elements = {
            0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8,
            169.254.0.0/16, 172.16.0.0/12, 192.0.0.0/24,
            192.0.2.0/24, 192.168.0.0/16, 198.18.0.0/15,
            198.51.100.0/24, 203.0.113.0/24, 224.0.0.0/4, 240.0.0.0/4
          }
        }
        chain input {
          type filter hook input priority -10; policy accept;
          iifname "${ctBridge}" ct state { established, related } accept
          iifname "${ctBridge}" udp dport 67 accept
          iifname "${ctBridge}" ip daddr 10.0.2.2 tcp dport { ${lib.concatMapStringsSep ", " toString cfg.sandbox.vmHostPorts} } accept
          iifname "${ctBridge}" counter reject with icmpx type admin-prohibited
        }
        chain forward {
          type filter hook forward priority -10; policy accept;
          iifname "${ctBridge}" meta nfproto ipv6 counter drop
          iifname "${ctBridge}" ip daddr @non_public_v4 counter reject with icmpx type admin-prohibited
          oifname "${ctBridge}" ct state != { established, related } counter drop
        }
        chain postrouting {
          type nat hook postrouting priority srcnat; policy accept;
          ip saddr 10.0.2.0/24 oifname != "${ctBridge}" masquerade
        }
      '';
    };

  }
  {
    # The relay ports are the host's loopback; forward them from 10.0.2.2.
    systemd.sockets = lib.listToAttrs (map (port:
      lib.nameValuePair "gaoji-ct-relay-${toString port}" {
        description = "gaoji container relay port ${toString port}";
        wantedBy = ["sockets.target"];
        socketConfig = {
          ListenStream = "10.0.2.2:${toString port}";
          FreeBind = true;
        };
      })
    cfg.sandbox.vmHostPorts);
    systemd.services = lib.listToAttrs (map (port:
      lib.nameValuePair "gaoji-ct-relay-${toString port}" {
        description = "gaoji container relay port ${toString port}";
        requires = ["gaoji-ct-relay-${toString port}.socket"];
        serviceConfig = {
          ExecStart = "${config.systemd.package}/lib/systemd/systemd-socket-proxyd 127.0.0.1:${toString port}";
          DynamicUser = true;
          PrivateTmp = true;
        };
      })
    cfg.sandbox.vmHostPorts);
  }]))
]);
}
