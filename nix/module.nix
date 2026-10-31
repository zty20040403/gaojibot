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
  defaultPackage = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
  defaultSandboxImage = self.packages.${pkgs.stdenv.hostPlatform.system}.sandbox-image;
  sandboxUsesDocker = cfg.sandbox.backend == "docker";
  sandboxUsesPodman = cfg.sandbox.backend == "podman";
  sandboxUsesVm = cfg.sandbox.backend == "vm";
  sandboxIsolated = cfg.sandbox.enable && sandboxUsesDocker && cfg.sandbox.network == "isolated";
  sandboxVm = cfg.runBot && cfg.sandbox.enable && sandboxUsesVm;
  vmServiceName = "${serviceName}-vm";
  vmNetworkSetup = pkgs.writeShellScript "gaoji-vm-network" ''
    exec ${pkgs.python3}/bin/python ${../src/sandbox_runtime/vm_network.py} \
      --nft ${pkgs.nftables}/bin/nft \
      --systemctl ${config.systemd.package}/bin/systemctl \
      --unit ${vmServiceName}.service \
      ${lib.concatMapStringsSep " " (port: "--host-port ${toString port}") cfg.sandbox.vmHostPorts} "$@"
  '';
  sandboxNetworkSetup = pkgs.writeShellScript "gaoji-sandbox-network" ''
    exec ${pkgs.python3}/bin/python ${../src/sandbox_runtime/network.py} \
      --docker ${pkgs.docker}/bin/docker \
      --iptables ${pkgs.iptables}/bin/iptables \
      --ip6tables ${pkgs.iptables}/bin/ip6tables
  '';
  sandboxCli =
    if sandboxUsesDocker
    then pkgs.docker
    else pkgs.writeShellScriptBin "docker" ''
      exec ${pkgs.podman}/bin/podman "$@"
    '';
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
      botId = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "QQ bot account receiving private six-digit confirmations; required if multiple bots connect.";
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

      maxTextChars = lib.mkOption {
        type = lib.types.ints.between 1 20000;
        default = 2000;
        description = "Maximum Unicode characters per rendered part; UTF-8 bytes are checked independently.";
      };

      maxTextParts = lib.mkOption {
        type = lib.types.ints.between 1 16;
        default = 4;
        description = "Maximum rendered message parts; each part uses its own authorized window charge.";
      };

      maxTextBytes = lib.mkOption {
        type = lib.types.nullOr (lib.types.ints.between 1 1048576);
        default = null;
        description = "Optional UTF-8 byte cap per rendered part.";
      };

      deferMaxHours = lib.mkOption {
        type = lib.types.ints.between 1 24;
        default = 24;
        description = "Maximum original result waiting deadline in hours; never refresh on reconnect or apply to expired proactive speech/OTP.";
      };

      fileLinkTtlSeconds = lib.mkOption {
        type = lib.types.ints.between 60 3600;
        default = 3600;
        description = "Maximum new download publication lifetime in seconds, also bounded by the original artifact retention and result deadline. Existing publications are never renewed.";
      };

      allowNewerWindows = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "When proactive sending is unavailable, let a pending result use a newer inbound reply window in the same conversation.";
      };

      certification = lib.mkOption {
        type = lib.types.enum ["unknown" "unverified" "certified"];
        default = "unknown";
        description = "Trusted operator observation of Tencent account certification; unknown uses unverified limits and grants no target API permissions.";
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

    sandbox.backend = lib.mkOption {
      type = lib.types.enum ["docker" "podman" "vm"];
      default = "docker";
      description = "Sandbox runtime: Docker, rootless Podman, or KVM virtual machines.";
    };

    sandbox.allowedUsers = lib.mkOption {
      type = lib.types.listOf lib.types.ints.unsigned;
      default = [];
      description = "QQ users allowed to use sandboxes. An empty list denies access.";
    };

    sandbox.network = lib.mkOption {
      type = lib.types.enum ["isolated" "none" "bridge"];
      default = "isolated";
      description = "OCI networking: isolated public egress, none, or explicitly unrestricted bridge.";
    };

    sandbox.allowAll = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Explicitly allow every user to run sandbox commands.";
    };

    sandbox.cpus = lib.mkOption {
      type = lib.types.ints.between 1 16;
      default = 2;
      description = "CPU limit for each sandbox and package helper (VMs use at most 4 vCPUs).";
    };

    sandbox.pidsLimit = lib.mkOption {
      type = lib.types.ints.between 32 4096;
      default = 256;
      description = "Process limit for each OCI sandbox or VM sandbox user.";
    };

    sandbox.memoryMiB = lib.mkOption {
      type = lib.types.ints.between 256 8192;
      default = 8192;
      description = "Memory cap in MiB per sandbox; VMs require at least 512 MiB.";
    };

    sandbox.vmImage = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      description = "Pinned Debian genericcloud QCOW2 image used as the KVM sandbox backing disk.";
    };

    sandbox.vmRoot = lib.mkOption {
      type = lib.types.str;
      default = "${statePath}/vms";
      description = "Persistent root for per-task VM disks and metadata.";
    };

    sandbox.vmHostPorts = lib.mkOption {
      type = lib.types.nonEmptyListOf lib.types.port;
      default = [18640 18641];
      description = "Host loopback TCP ports a VM may reach at 10.0.2.2: the background-agent model relay and app-server tunnel. Every other host, LAN, tailnet and link-local address is rejected for VM traffic.";
    };

    sandbox.imageArchive = lib.mkOption {
      type = lib.types.package;
      default = defaultSandboxImage;
      defaultText = lib.literalExpression "inputs.qq-bot.packages.${pkgs.stdenv.hostPlatform.system}.sandbox-image";
      description = "Reproducible OCI archive loaded for advanced bot sandboxes.";
    };

    sandbox.imageName = lib.mkOption {
      type = lib.types.str;
      default = "gaoji-sandbox:latest";
      description = "Docker image name used when creating advanced sandboxes.";
    };

    sandbox.nixCacheVolume = lib.mkOption {
      type = lib.types.str;
      default = "gaoji-nix-v2";
      description = "Docker volume shared by trusted Nix package helpers and mounted read-only in task sandboxes.";
    };

    sandbox.nixCacheRetentionDays = lib.mkOption {
      type = lib.types.ints.between 1 90;
      default = 14;
      description = "Days to retain unused on-demand package roots before the dedicated sandbox cache GC removes them.";
    };

    opsRead = {
      baseUrl = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "Cluster-control URL for owner-private background read tools.";
      };
      tokenFile = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "Host-only cluster-control API token file. Never passed to a VM.";
      };
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

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = !cfg.runBot || cfg.qqOfficial.enable;
        message = "A running Gaoji application requires QQ official to be enabled";
      }
      {
        assertion = cfg.opsRead.baseUrl == "" || officialCredentialPath cfg.opsRead.tokenFile;
        message = "opsRead requires a private absolute tokenFile outside /nix/store";
      }
      {
        assertion = !(cfg.sandbox.enable && sandboxUsesPodman && cfg.sandbox.network == "isolated");
        message = "Isolated egress requires rootful Docker; select none for rootless Podman or explicitly opt into bridge";
      }
      {
        assertion = !(cfg.sandbox.enable && sandboxUsesVm) || cfg.sandbox.memoryMiB >= 512;
        message = "KVM sandboxes require at least 512 MiB";
      }
      {
        assertion = !(cfg.sandbox.enable && sandboxUsesVm) || (
          cfg.sandbox.vmImage != null
          && lib.hasPrefix "/" cfg.sandbox.vmRoot
          && !(lib.hasPrefix "/nix/store/" cfg.sandbox.vmRoot)
        );
        message = "KVM sandboxes require a pinned vmImage and a persistent absolute vmRoot outside the Nix store";
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
    users.users.${serviceName} = lib.mkIf (cfg.user == serviceName) {
      isSystemUser = true;
      group = cfg.group;
      home = statePath;
    };

    networking.firewall.allowedTCPPorts = lib.optionals cfg.openFirewall [cfg.port];

    virtualisation.docker.enable = lib.mkDefault (cfg.sandbox.enable && sandboxUsesDocker);
    virtualisation.podman.enable = lib.mkDefault (cfg.sandbox.enable && sandboxUsesPodman);
    virtualisation.libvirtd.enable = lib.mkDefault (cfg.sandbox.enable && sandboxUsesVm);

    systemd.services.${serviceName} = lib.mkIf cfg.runBot {
      description = "gaoji multi-model bot";
      wantedBy = ["multi-user.target"];
      unitConfig.RequiresMountsFor = lib.optionals (cfg.sandbox.enable && sandboxUsesVm) [cfg.sandbox.vmRoot];
      wants =
        ["network-online.target"];
      # Without the filtered daemon the bot must not run: a stray session
      # daemon would otherwise serve virsh with unfiltered qemu.
      requires = lib.optionals sandboxVm ["${vmServiceName}.service"]
        ++ lib.optionals (cfg.sandbox.enable && !sandboxUsesVm) [
        "${serviceName}-sandbox-image.service"
      ] ++ lib.optionals sandboxIsolated ["gaoji-sandbox-network.service"];
      after =
        ["network-online.target"]
        ++ lib.optionals sandboxVm ["${vmServiceName}.service"]
        ++ lib.optionals (cfg.sandbox.enable && !sandboxUsesVm) [
          "${serviceName}-sandbox-image.service"
        ] ++ lib.optionals sandboxIsolated ["gaoji-sandbox-network.service"];
      path =
        cfg.runtimePackages
        ++ lib.optionals (cfg.sandbox.enable && !sandboxUsesVm) [sandboxCli]
        ++ lib.optionals (cfg.sandbox.enable && sandboxUsesVm) [pkgs.libvirt pkgs.qemu pkgs.cloud-utils]
        ++ lib.optionals cfg.voiceAsr.enable [pkgs.ffmpeg-headless cfg.voiceAsr.package];
      environment =
        {
          HOME = statePath;
          AI_STATE_DIR = "${statePath}/state";
          AI_OPS_READ_BASE_URL = cfg.opsRead.baseUrl;
          AI_OPS_READ_TOKEN_FILE = if cfg.opsRead.baseUrl == "" then "" else "/run/credentials/${serviceName}.service/ops-read-token";
          AI_CACHE_DIR = cachePath;
          AI_SANDBOX_ENABLED = boolString cfg.sandbox.enable;
          AI_SANDBOX_ALLOWED_USERS = lib.concatMapStringsSep "," toString cfg.sandbox.allowedUsers;
          AI_SANDBOX_ALLOW_ALL = boolString cfg.sandbox.allowAll;
          AI_SANDBOX_CPUS = toString cfg.sandbox.cpus;
          AI_SANDBOX_PIDS_LIMIT = toString cfg.sandbox.pidsLimit;
          AI_SANDBOX_MEMORY_MIB = toString cfg.sandbox.memoryMiB;
          AI_SANDBOX_NETWORK = cfg.sandbox.network;
          AI_SANDBOX_BACKEND = if sandboxUsesVm then "vm" else "oci";
          AI_SANDBOX_IMAGE = cfg.sandbox.imageName;
          AI_SANDBOX_NIX_CACHE_VOLUME = cfg.sandbox.nixCacheVolume;
          HOST = cfg.host;
          PORT = toString cfg.port;
          PYTHONUNBUFFERED = "1";
        }
        // lib.optionalAttrs (cfg.sandbox.enable && sandboxUsesPodman) {
          XDG_RUNTIME_DIR = "${statePath}/podman-runtime";
        }
        // lib.optionalAttrs (cfg.sandbox.enable && sandboxUsesVm) {
          XDG_RUNTIME_DIR = "${statePath}/vm-runtime";
          AI_SANDBOX_VM_IMAGE = toString cfg.sandbox.vmImage;
          AI_SANDBOX_VM_ROOT = cfg.sandbox.vmRoot;
        }
        // lib.optionalAttrs (cfg.admin.secretFile != null) {
          AI_ADMIN_SECRET_FILE = "/run/credentials/${serviceName}.service/admin-authorization-key";
          AI_ADMIN_ORIGIN = cfg.admin.origin;
          AI_ADMIN_BOT_ID = cfg.admin.botId;
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
          AI_QQ_OFFICIAL_MAX_TEXT_CHARS = toString cfg.qqOfficial.maxTextChars;
          AI_QQ_OFFICIAL_MAX_TEXT_PARTS = toString cfg.qqOfficial.maxTextParts;
          AI_QQ_OFFICIAL_MAX_TEXT_BYTES = (if cfg.qqOfficial.maxTextBytes == null then "" else toString cfg.qqOfficial.maxTextBytes);
          AI_QQ_OFFICIAL_DEFER_MAX_HOURS = toString cfg.qqOfficial.deferMaxHours;
          AI_QQ_OFFICIAL_FILE_LINK_TTL_SECONDS = toString cfg.qqOfficial.fileLinkTtlSeconds;
          AI_QQ_OFFICIAL_ALLOW_NEWER_WINDOWS = boolString cfg.qqOfficial.allowNewerWindows;
          AI_QQ_OFFICIAL_CERTIFICATION = cfg.qqOfficial.certification;
        }
        // lib.optionalAttrs cfg.voiceAsr.enable {
          AI_VIDEO_WHISPER_MODEL_PATH = toString cfg.voiceAsr.model;
        }
        // cfg.environment;

      serviceConfig =
        {
          Type = "simple";
          User = cfg.user;
          Group = cfg.group;
          SupplementaryGroups = lib.optional (cfg.sandbox.enable && sandboxUsesDocker) "docker"
            ++ lib.optional (cfg.sandbox.enable && sandboxUsesVm) "kvm";
          StateDirectory = cfg.stateDirectory;
          CacheDirectory = cfg.cacheDirectory;
          WorkingDirectory = "${cfg.package}/share/gaoji";
          ExecStartPre = "${cfg.package}/bin/gaoji-db ${
            if cfg.database.migrateOnStart
            then "upgrade"
            else "check"
          }";
          ExecStart = lib.getExe cfg.package;
          Restart = "on-failure";
          RestartSec = 5;
          UMask = "0077";
          LoadCredential =
            lib.optional (cfg.opsRead.baseUrl != "") "ops-read-token:${cfg.opsRead.tokenFile}"
            ++ lib.optional (cfg.admin.secretFile != null) "admin-authorization-key:${cfg.admin.secretFile}"
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
          ReadWritePaths = lib.optionals (cfg.sandbox.enable && sandboxUsesVm) [cfg.sandbox.vmRoot];
          RestrictSUIDSGID = true;
        }
        // lib.optionalAttrs (cfg.environmentFile != null) {
          EnvironmentFile = cfg.environmentFile;
        };
    };

    # libvirt session daemon for KVM workspaces. Its qemu children inherit this
    # unit's cgroup, which the gaoji_vm nftables table filters; the bot never
    # autospawns its own daemon (LIBVIRT_AUTOSTART=0 in vm_sandbox.py).
    systemd.services.${vmServiceName} = lib.mkIf sandboxVm {
      description = "gaoji KVM workspaces with filtered egress";
      wantedBy = ["multi-user.target"];
      unitConfig.RequiresMountsFor = [cfg.sandbox.vmRoot];
      path = [pkgs.libvirt pkgs.qemu];
      environment = {
        HOME = statePath;
        XDG_RUNTIME_DIR = "${statePath}/vm-runtime";
      };
      serviceConfig = {
        Type = "notify";
        User = cfg.user;
        Group = cfg.group;
        SupplementaryGroups = ["kvm"];
        StateDirectory = cfg.stateDirectory;
        # Runs inside this unit's fresh cgroup, before any qemu; failure blocks the start.
        ExecStartPre = "+${vmNetworkSetup}";
        ExecStart = "${pkgs.libvirt}/sbin/virtqemud";
        Restart = "on-failure";
        RestartSec = 5;
        UMask = "0077";
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectControlGroups = true;
        ProtectHome = "read-only";
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        ProtectSystem = "strict";
        ReadWritePaths = [cfg.sandbox.vmRoot];
        RestrictSUIDSGID = true;
      };
    };

    # nftables.service flushes the whole ruleset on start and reload; put the
    # VM table back in the same job. On failure the hook stops the VM unit.
    systemd.services.nftables.serviceConfig = lib.mkIf (sandboxVm && config.networking.nftables.enable) {
      ExecStartPost = lib.mkAfter ["${vmNetworkSetup} --if-running"];
      ExecReload = lib.mkAfter ["${vmNetworkSetup} --if-running"];
    };

    systemd.services.gaoji-sandbox-network = lib.mkIf sandboxIsolated {
      description = "Install fail-closed sandbox egress rules";
      after = ["docker.service"];
      requires = ["docker.service"];
      partOf = ["docker.service"];
      wantedBy = ["docker.service"];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = sandboxNetworkSetup;
      };
    };

    networking.firewall.extraCommands = lib.mkIf sandboxIsolated ''
      if ${pkgs.systemd}/bin/systemctl is-active --quiet docker.service; then
        ${sandboxNetworkSetup}
      fi
    '';

    systemd.services."${serviceName}-sandbox-image" = lib.mkIf (cfg.runBot && cfg.sandbox.enable && !sandboxUsesVm) {
      description = "Load the gaoji advanced sandbox image";
      wantedBy = ["multi-user.target"];
      requires = lib.optional sandboxUsesDocker "docker.service";
      after = lib.optional sandboxUsesDocker "docker.service";
      path = [sandboxCli];
      environment = lib.optionalAttrs (!sandboxUsesDocker) {
        HOME = statePath;
        XDG_RUNTIME_DIR = "${statePath}/podman-runtime";
      };
      script = ''
        set -euo pipefail
        ${sandboxCli}/bin/docker load --input ${cfg.sandbox.imageArchive}
        image=${lib.escapeShellArg cfg.sandbox.imageName}
        image_id=$(${sandboxCli}/bin/docker image inspect --format '{{.Id}}' "$image")

        volume=${lib.escapeShellArg cfg.sandbox.nixCacheVolume}
        ${sandboxCli}/bin/docker volume create \
          --label io.gaoji.nix-cache=true "$volume" >/dev/null
        ${sandboxCli}/bin/docker run --rm \
          --pull=never \
          --network none \
          --user 0:0 \
          --label io.gaoji.cache-initializer=true \
          --cap-drop ALL \
          --security-opt no-new-privileges \
          --memory 2g \
          --memory-swap 2g \
          --read-only \
          --tmpfs /tmp:rw,nosuid,nodev,size=256m,mode=1777 \
          --tmpfs /root:rw,nosuid,nodev,size=64m,mode=700 \
          --mount type=volume,source="$volume",target=/cache/nix \
          "$image_id" \
          gaoji-cache-seed

        # Loading an image must never collect images; verify the task path last.
        ${sandboxCli}/bin/docker run --rm -i \
          --pull=never \
          --network none \
          --user 1000:1000 \
          --cap-drop ALL \
          --security-opt no-new-privileges \
          --memory 512m \
          --memory-swap 512m \
          --read-only \
          --env HOME=/home/sandbox \
          --workdir /workspace \
          --tmpfs /tmp:rw,nosuid,nodev,size=128m,mode=1777 \
          --tmpfs /home/sandbox:rw,nosuid,nodev,size=64m,uid=1000,gid=1000,mode=700 \
          --tmpfs /workspace:rw,nosuid,nodev,size=64m,uid=1000,gid=1000,mode=700 \
          --mount type=volume,source="$volume",target=/nix,readonly \
          "$image" python - < ${./sandbox-smoke.py}
        test "$(${sandboxCli}/bin/docker image inspect --format '{{.Id}}' "$image")" = "$image_id"
      '';
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        TimeoutStartSec = "15min";
      } // lib.optionalAttrs (!sandboxUsesDocker) {
        User = cfg.user;
        Group = cfg.group;
        StateDirectory = cfg.stateDirectory;
      };
    };

    systemd.services."${serviceName}-sandbox-nix-gc" = lib.mkIf (cfg.runBot && cfg.sandbox.enable && !sandboxUsesVm) {
      description = "Collect expired gaoji on-demand Nix packages";
      requires = lib.optional sandboxUsesDocker "docker.service" ++ ["${serviceName}-sandbox-image.service"];
      after = lib.optional sandboxUsesDocker "docker.service" ++ ["${serviceName}-sandbox-image.service"];
      path = [sandboxCli];
      environment = lib.optionalAttrs (!sandboxUsesDocker) {
        HOME = statePath;
        XDG_RUNTIME_DIR = "${statePath}/podman-runtime";
      };
      script = ''
        ${sandboxCli}/bin/docker run --rm \
          --pull=never \
          --network none \
          --user 0:0 \
          --cap-drop ALL \
          --security-opt no-new-privileges \
          --memory 2g \
          --memory-swap 2g \
          --read-only \
          --tmpfs /tmp:rw,nosuid,nodev,size=256m,mode=1777 \
          --tmpfs /root:rw,nosuid,nodev,size=64m,mode=700 \
          --mount type=volume,source=${lib.escapeShellArg cfg.sandbox.nixCacheVolume},target=/nix \
          ${lib.escapeShellArg cfg.sandbox.imageName} \
          sh -lc ${lib.escapeShellArg ''
            set -eu
            for roots in /nix/var/nix/gcroots/gaoji-packages /nix/var/nix/gcroots/kennethbot-packages; do
              if [ -d "$roots" ]; then
                find "$roots" -mindepth 1 -maxdepth 1 -type d \
                  -mtime +${toString cfg.sandbox.nixCacheRetentionDays} -exec rm -rf -- {} +
              fi
            done
            nix-store --gc
          ''}
      '';
      serviceConfig = {
        Type = "oneshot";
      } // lib.optionalAttrs (!sandboxUsesDocker) {
        User = cfg.user;
        Group = cfg.group;
        StateDirectory = cfg.stateDirectory;
      };
    };

    systemd.timers."${serviceName}-sandbox-nix-gc" = lib.mkIf (cfg.runBot && cfg.sandbox.enable && !sandboxUsesVm) {
      description = "Schedule gaoji sandbox Nix cache collection";
      wantedBy = ["timers.target"];
      timerConfig = {
        OnCalendar = "weekly";
        Persistent = true;
        RandomizedDelaySec = "2h";
      };
    };

    systemd.tmpfiles.rules =
      lib.optionals (cfg.sandbox.enable && sandboxUsesPodman) [
        "d ${statePath} 0700 ${cfg.user} ${cfg.group} -"
        "d ${statePath}/podman-runtime 0700 ${cfg.user} ${cfg.group} -"
      ]
      ++ lib.optionals (cfg.sandbox.enable && sandboxUsesVm) [
        "d ${statePath} 0700 ${cfg.user} ${cfg.group} -"
        "d ${statePath}/vm-runtime 0700 ${cfg.user} ${cfg.group} -"
        "d ${cfg.sandbox.vmRoot} 0700 ${cfg.user} ${cfg.group} -"
      ];
  };
}
