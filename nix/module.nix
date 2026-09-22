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
  codesnapFonts = pkgs.runCommand "qq-bot-codesnap-fonts" {} ''
    mkdir -p "$out/share/fonts"
    cp ${pkgs.sarasa-gothic}/share/fonts/truetype/Sarasa-Regular.ttc \
      "$out/share/fonts/Sarasa-Regular.ttc"
  '';
  richFontConfig = pkgs.makeFontsConf {
    fontDirectories = [
      pkgs.sarasa-gothic
      pkgs.noto-fonts-cjk-sans
    ];
  };
  defaultWhisperModel = pkgs.fetchurl {
    url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin";
    hash = "sha256-YO1bw90U7qhWST0zQ0m0BXgt3K8AKNS130CINF+6Lv4=";
  };
  codesnapConfig = pkgs.writeText "qq-bot-codesnap.json" (builtins.toJSON {
    print_eggs = false;
    snapshot_config = {
      theme = cfg.codesnap.theme;
      fonts_folders = ["${codesnapFonts}/share/fonts"];
      window = {
        mac_window_bar = true;
        shadow = {
          radius = 16;
          color = "#00000040";
        };
        margin = {
          x = 42;
          y = 42;
        };
        border = {
          width = 1;
          color = "#ffffff24";
        };
        title_config = {
          color = "#d8dee9";
          font_family = cfg.codesnap.fontFamily;
        };
        radius = 8;
      };
      code_config = {
        font_family = cfg.codesnap.fontFamily;
        breadcrumbs = {
          enable = false;
          separator = "/";
          color = "#80848b";
          font_family = cfg.codesnap.fontFamily;
        };
      };
      watermark = {
        content = "";
        font_family = cfg.codesnap.fontFamily;
        color = "#ffffff";
      };
      background = "#15171c";
    };
  });
  boolString = value:
    if value
    then "true"
    else "false";
  napcatServiceName = "docker-${cfg.napcat.containerName}";
in {
  options.services.gaoji = {
    enable = lib.mkEnableOption "the gaoji multi-model bot";

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
      description = "Address used by the OneBot reverse WebSocket server.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8080;
      description = "Port used by the OneBot reverse WebSocket server.";
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

    cluster = {
      enable = lib.mkEnableOption "read-only gaoji fleet tools";

      controlUrl = lib.mkOption {
        type = lib.types.str;
        default = "http://127.0.0.1:8091";
        description = "Fixed gaoji cluster-control API URL.";
      };

      tokenFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Credential file for the internal cluster-control API.";
      };

      allowedGroups = lib.mkOption {
        type = lib.types.listOf lib.types.ints.unsigned;
        default = [];
        description = "QQ groups allowed to use non-sensitive fleet queries.";
      };

      logAllowedGroups = lib.mkOption {
        type = lib.types.listOf lib.types.ints.unsigned;
        default = [];
        description = "QQ groups allowed to request allowlisted service logs.";
      };

      timeoutSeconds = lib.mkOption {
        type = lib.types.ints.between 1 30;
        default = 12;
        description = "Whole-request timeout used by the bot-side control client.";
      };
    };

    sandbox.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Allow the bot to create Docker-backed execution sandboxes.";
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

    browser = {
      enable = lib.mkEnableOption "the bot's persistent Playwright browser and rich rendering";

      package = lib.mkOption {
        type = lib.types.package;
        default = pkgs.chromium;
        defaultText = lib.literalExpression "pkgs.chromium";
        description = "Chromium-compatible browser executable used by Playwright.";
      };
    };

    codesnap = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Render fenced code blocks with the CodeSnap CLI.";
      };

      package = lib.mkOption {
        type = lib.types.package;
        default = pkgs.codesnap;
        defaultText = lib.literalExpression "pkgs.codesnap";
        description = "CodeSnap CLI package used for code block images.";
      };

      fontFamily = lib.mkOption {
        type = lib.types.str;
        default = "Sarasa Mono SC";
        description = "Chinese-capable monospace family used for code snapshots.";
      };

      theme = lib.mkOption {
        type = lib.types.str;
        default = "candy";
        description = "Built-in CodeSnap syntax theme.";
      };
    };

    videoDeep = {
      enable = lib.mkEnableOption "temporary deep analysis of shared and QQ-native videos";

      whisperPackage = lib.mkOption {
        type = lib.types.package;
        default = pkgs.whisper-cpp;
        defaultText = lib.literalExpression "pkgs.whisper-cpp";
        description = "whisper.cpp package used for local audio transcription.";
      };

      whisperModel = lib.mkOption {
        type = lib.types.package;
        default = defaultWhisperModel;
        description = "Fixed-output multilingual Whisper model file.";
      };

      frameCount = lib.mkOption {
        type = lib.types.ints.between 4 12;
        default = 12;
        description = "Number of evenly sampled video frames sent to the vision model.";
      };

      maxDownloadMB = lib.mkOption {
        type = lib.types.ints.positive;
        default = 1024;
        description = "Maximum combined temporary video and audio download size.";
      };

      maxDurationMinutes = lib.mkOption {
        type = lib.types.ints.positive;
        default = 60;
        description = "Maximum video duration accepted by deep analysis.";
      };

      timeoutSeconds = lib.mkOption {
        type = lib.types.ints.positive;
        default = 1800;
        description = "Timeout for media preparation and local transcription.";
      };

      recentSeconds = lib.mkOption {
        type = lib.types.ints.positive;
        default = 300;
        description = "How long a sender's latest QQ video remains available for a follow-up request.";
      };
    };

    napcat = {
      enable = lib.mkEnableOption "a dedicated NapCat container for this bot";

      account = lib.mkOption {
        type = lib.types.str;
        example = "123456789";
        description = "QQ account logged in by the dedicated NapCat container.";
      };

      image = lib.mkOption {
        type = lib.types.str;
        default = "mlikiowa/napcat-docker:latest";
        description = "NapCat container image; pin a version or digest in production.";
      };

      containerName = lib.mkOption {
        type = lib.types.str;
        default = "napcat-gaoji";
        description = "OCI container name for the dedicated NapCat instance.";
      };

      dataDirectory = lib.mkOption {
        type = lib.types.str;
        default = "/var/lib/napcat-gaoji";
        description = "Persistent NapCat data directory on the host.";
      };

      environmentFiles = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [];
        description = "Runtime environment files passed to the NapCat container.";
      };

      environment = lib.mkOption {
        type = lib.types.attrsOf lib.types.str;
        default = {};
        description = "Additional non-secret environment variables for NapCat.";
      };

      reverseWebsocketUrl = lib.mkOption {
        type = lib.types.str;
        default = "ws://host.docker.internal:${toString cfg.port}/onebot/v11/ws";
        description = "OneBot reverse WebSocket URL used by NapCat.";
      };

      reverseWebsocketTokenFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Private OneBot token file; updates only the matching configured reverse-WebSocket client before NapCat starts.";
      };

      webuiAddress = lib.mkOption {
        type = lib.types.str;
        default = "127.0.0.1";
        description = "Host address used for the NapCat WebUI port mapping.";
      };

      webuiPort = lib.mkOption {
        type = lib.types.port;
        default = 6100;
        description = "Host port used for the dedicated NapCat WebUI.";
      };

      healthCheck = {
        enable = lib.mkEnableOption "QQ account probes and bounded transport recovery";
        metricsFile = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          description = "Optional node-exporter textfile path for actual QQ account status.";
        };
      };

      passwordLogin = {
        enable = lib.mkEnableOption "bounded password login when QQ is logged out";
        passwordFile = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          description = "Private runtime QQ password file, outside the Nix store; loaded with systemd credentials. Never put the password in Nix configuration.";
        };
        notification = {
          enable = lib.mkEnableOption "one targeted login-verification reminder through another QQ instance";
          webuiConfigFile = lib.mkOption {
            type = lib.types.str;
            default = "";
            description = "Private runtime WebUI config of the independent notification sender.";
          };
          webuiPort = lib.mkOption { type = lib.types.port; default = 6099; };
          account = lib.mkOption { type = lib.types.strMatching "[1-9][0-9]{4,19}"; };
          groupId = lib.mkOption { type = lib.types.strMatching "[1-9][0-9]{4,19}"; };
          userId = lib.mkOption { type = lib.types.strMatching "[1-9][0-9]{4,19}"; };
        };
      };
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = builtins.match "^[A-Za-z0-9_.-]+$" cfg.stateDirectory != null;
        message = "services.gaoji.stateDirectory must be a directory name, not a path";
      }
      {
        assertion = builtins.match "^[A-Za-z0-9_.-]+$" cfg.cacheDirectory != null;
        message = "services.gaoji.cacheDirectory must be a directory name, not a path";
      }
      {
        assertion = !cfg.cluster.enable || cfg.cluster.tokenFile != null;
        message = "services.gaoji.cluster.tokenFile is required when cluster tools are enabled";
      }
      {
        assertion = !cfg.napcat.passwordLogin.enable || (
          cfg.napcat.enable
          && cfg.napcat.passwordLogin.passwordFile != null
          && lib.hasPrefix "/" cfg.napcat.passwordLogin.passwordFile
          && !(lib.hasPrefix "/nix/store/" cfg.napcat.passwordLogin.passwordFile)
        );
        message = "NapCat password login requires NapCat and a private absolute passwordFile outside /nix/store";
      }
      {
        assertion = !cfg.napcat.passwordLogin.notification.enable || (
          cfg.napcat.passwordLogin.enable
          && lib.hasPrefix "/" cfg.napcat.passwordLogin.notification.webuiConfigFile
          && !(lib.hasPrefix "/nix/store/" cfg.napcat.passwordLogin.notification.webuiConfigFile)
          && cfg.napcat.passwordLogin.notification.account != cfg.napcat.account
          && cfg.napcat.passwordLogin.notification.webuiPort != cfg.napcat.webuiPort
        );
        message = "Login notifications require a separate QQ instance and its private runtime WebUI config";
      }
    ];

    users.groups.${serviceName} = lib.mkIf (cfg.group == serviceName) {};
    users.users.${serviceName} = lib.mkIf (cfg.user == serviceName) {
      isSystemUser = true;
      group = cfg.group;
      home = statePath;
    };

    networking.firewall.allowedTCPPorts = lib.optionals cfg.openFirewall [cfg.port];

    virtualisation.docker.enable = lib.mkDefault (cfg.sandbox.enable || cfg.napcat.enable);

    systemd.services.${serviceName} = {
      description = "gaoji multi-model bot";
      wantedBy = ["multi-user.target"];
      wants =
        ["network-online.target"]
        ++ lib.optional cfg.cluster.enable "gaoji-cluster-control.service";
      requires = lib.optionals cfg.sandbox.enable [
        "${serviceName}-sandbox-image.service"
      ];
      after =
        ["network-online.target"]
        ++ lib.optional cfg.cluster.enable "gaoji-cluster-control.service"
        ++ lib.optionals cfg.sandbox.enable [
          "${serviceName}-sandbox-image.service"
        ];
      path =
        cfg.runtimePackages
        ++ lib.optionals cfg.sandbox.enable [pkgs.docker]
        ++ lib.optionals cfg.browser.enable [cfg.browser.package]
        ++ lib.optionals cfg.codesnap.enable [cfg.codesnap.package]
        ++ lib.optionals cfg.videoDeep.enable [
          pkgs.ffmpeg-headless
          cfg.videoDeep.whisperPackage
        ];
      environment =
        {
          HOME = statePath;
          AI_STATE_DIR = "${statePath}/state";
          AI_CACHE_DIR = cachePath;
          AI_SANDBOX_ENABLED = boolString cfg.sandbox.enable;
          AI_SANDBOX_IMAGE = cfg.sandbox.imageName;
          AI_SANDBOX_NIX_CACHE_VOLUME = cfg.sandbox.nixCacheVolume;
          HOST = cfg.host;
          PORT = toString cfg.port;
          PYTHONUNBUFFERED = "1";
        }
        // lib.optionalAttrs (cfg.admin.secretFile != null) {
          AI_ADMIN_SECRET_FILE = "/run/credentials/${serviceName}.service/admin-authorization-key";
          AI_ADMIN_ORIGIN = cfg.admin.origin;
          AI_ADMIN_BOT_ID = cfg.admin.botId;
        }
        // lib.optionalAttrs cfg.browser.enable {
          AI_BROWSER_ENABLED = "true";
          AI_BROWSER_EXECUTABLE_PATH = lib.getExe cfg.browser.package;
          FONTCONFIG_FILE = "${richFontConfig}";
        }
        // lib.optionalAttrs cfg.codesnap.enable {
          AI_CODESNAP_ENABLED = "true";
          AI_CODESNAP_EXECUTABLE_PATH = lib.getExe cfg.codesnap.package;
          AI_CODESNAP_CONFIG_PATH = codesnapConfig;
          AI_CODESNAP_FONT_FAMILY = cfg.codesnap.fontFamily;
          AI_CODESNAP_THEME = cfg.codesnap.theme;
        }
        // lib.optionalAttrs (!cfg.codesnap.enable) {
          AI_CODESNAP_ENABLED = "false";
        }
        // lib.optionalAttrs cfg.videoDeep.enable {
          AI_VIDEO_DEEP_ENABLED = "true";
          AI_VIDEO_WHISPER_MODEL_PATH = toString cfg.videoDeep.whisperModel;
          AI_VIDEO_FRAME_COUNT = toString cfg.videoDeep.frameCount;
          AI_VIDEO_MAX_DOWNLOAD_MB = toString cfg.videoDeep.maxDownloadMB;
          AI_VIDEO_MAX_DURATION_MINUTES = toString cfg.videoDeep.maxDurationMinutes;
          AI_VIDEO_TIMEOUT_SECONDS = toString cfg.videoDeep.timeoutSeconds;
          AI_VIDEO_RECENT_SECONDS = toString cfg.videoDeep.recentSeconds;
        }
        // lib.optionalAttrs (!cfg.videoDeep.enable) {
          AI_VIDEO_DEEP_ENABLED = "false";
        }
        // lib.optionalAttrs cfg.cluster.enable {
          AI_CLUSTER_ENABLED = "true";
          AI_CLUSTER_CONTROL_URL = cfg.cluster.controlUrl;
          AI_CLUSTER_CONTROL_TOKEN_FILE = "%d/fleet-control-token";
          AI_CLUSTER_CONTROL_TIMEOUT_SECONDS = toString cfg.cluster.timeoutSeconds;
          AI_FLEET_ALLOWED_GROUPS = lib.concatMapStringsSep "," toString cfg.cluster.allowedGroups;
          AI_FLEET_LOG_ALLOWED_GROUPS = lib.concatMapStringsSep "," toString cfg.cluster.logAllowedGroups;
        }
        // lib.optionalAttrs (!cfg.cluster.enable) {
          AI_CLUSTER_ENABLED = "false";
        }
        // cfg.environment;

      serviceConfig =
        {
          Type = "simple";
          User = cfg.user;
          Group = cfg.group;
          SupplementaryGroups = lib.optional cfg.sandbox.enable "docker";
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
          LoadCredential = lib.optional (cfg.cluster.enable && cfg.cluster.tokenFile != null) "fleet-control-token:${cfg.cluster.tokenFile}"
            ++ lib.optional (cfg.admin.secretFile != null) "admin-authorization-key:${cfg.admin.secretFile}";

          NoNewPrivileges = true;
          PrivateTmp = true;
          ProtectControlGroups = true;
          ProtectHome = "read-only";
          ProtectKernelModules = true;
          ProtectKernelTunables = true;
          ProtectSystem = "strict";
          RestrictSUIDSGID = true;
        }
        // lib.optionalAttrs (cfg.environmentFile != null) {
          EnvironmentFile = cfg.environmentFile;
        };
    };

    systemd.services."${serviceName}-sandbox-image" = lib.mkIf cfg.sandbox.enable {
      description = "Load the gaoji advanced sandbox image";
      wantedBy = ["multi-user.target"];
      requires = ["docker.service"];
      after = ["docker.service"];
      script = ''
        set -euo pipefail
        ${pkgs.docker}/bin/docker load --input ${cfg.sandbox.imageArchive}
        image=${lib.escapeShellArg cfg.sandbox.imageName}
        image_id=$(${pkgs.docker}/bin/docker image inspect --format '{{.Id}}' "$image")

        volume=${lib.escapeShellArg cfg.sandbox.nixCacheVolume}
        ${pkgs.docker}/bin/docker volume create \
          --label io.gaoji.nix-cache=true "$volume" >/dev/null
        ${pkgs.docker}/bin/docker run --rm \
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
        ${pkgs.docker}/bin/docker run --rm -i \
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
        test "$(${pkgs.docker}/bin/docker image inspect --format '{{.Id}}' "$image")" = "$image_id"
      '';
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        TimeoutStartSec = "15min";
      };
    };

    systemd.services."${serviceName}-sandbox-nix-gc" = lib.mkIf cfg.sandbox.enable {
      description = "Collect expired gaoji on-demand Nix packages";
      requires = ["docker.service" "${serviceName}-sandbox-image.service"];
      after = ["docker.service" "${serviceName}-sandbox-image.service"];
      script = ''
        ${pkgs.docker}/bin/docker run --rm \
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
      serviceConfig.Type = "oneshot";
    };

    systemd.timers."${serviceName}-sandbox-nix-gc" = lib.mkIf cfg.sandbox.enable {
      description = "Schedule gaoji sandbox Nix cache collection";
      wantedBy = ["timers.target"];
      timerConfig = {
        OnCalendar = "weekly";
        Persistent = true;
        RandomizedDelaySec = "2h";
      };
    };

    virtualisation.oci-containers = lib.mkIf cfg.napcat.enable {
      backend = "docker";
      containers.${cfg.napcat.containerName} = {
        image = cfg.napcat.image;
        autoStart = true;
        environment =
          {
            ACCOUNT = cfg.napcat.account;
            WSR_ENABLE = "true";
            WS_URLS = builtins.toJSON [cfg.napcat.reverseWebsocketUrl];
          }
          // cfg.napcat.environment;
        environmentFiles = cfg.napcat.environmentFiles;
        ports = [
          "${cfg.napcat.webuiAddress}:${toString cfg.napcat.webuiPort}:6099"
        ];
        volumes = [
          "${cfg.napcat.dataDirectory}/QQ:/app/.config/QQ"
          "${cfg.napcat.dataDirectory}/config:/app/napcat/config"
          "${cfg.napcat.dataDirectory}/outbox:/data/outbox"
        ];
        extraOptions = ["--add-host=host.docker.internal:host-gateway"];
      };
    };

    systemd.services.${napcatServiceName} = lib.mkIf cfg.napcat.enable {
      wants = ["${serviceName}.service"];
      after = ["${serviceName}.service"];
      serviceConfig.LoadCredential = lib.optional (cfg.napcat.reverseWebsocketTokenFile != null)
        "onebot-token:${cfg.napcat.reverseWebsocketTokenFile}";
      preStart = lib.mkIf (cfg.napcat.reverseWebsocketTokenFile != null) (lib.mkBefore ''
        ${pkgs.python3}/bin/python ${./napcat-auth.py} \
          ${lib.escapeShellArg "${cfg.napcat.dataDirectory}/config/onebot11_${cfg.napcat.account}.json"} \
          "$CREDENTIALS_DIRECTORY/onebot-token" \
          ${lib.escapeShellArg cfg.napcat.reverseWebsocketUrl}
      '');
    };

    systemd.services."${serviceName}-qq-health" = lib.mkIf (cfg.napcat.enable && cfg.napcat.healthCheck.enable) {
      description = "Probe actual QQ login and recover stalled transport without restart loops";
      after = ["${napcatServiceName}.service"];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = lib.concatStringsSep " " ([
          "${pkgs.python3}/bin/python3" "${./napcat-health.py}"
          "--config" (lib.escapeShellArg "${cfg.napcat.dataDirectory}/config/webui.json")
          "--port" (toString cfg.napcat.webuiPort)
          "--unit" "${napcatServiceName}.service"
          "--systemctl" "${pkgs.systemd}/bin/systemctl"
          "--state" "/var/lib/${serviceName}-qq-health/state.json"
        ] ++ lib.optionals (cfg.napcat.healthCheck.metricsFile != null) [
          "--metrics" (lib.escapeShellArg cfg.napcat.healthCheck.metricsFile)
        ]);
        StateDirectory = "${serviceName}-qq-health";
        StateDirectoryMode = "0700";
        TimeoutStartSec = "25s";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        ReadWritePaths = lib.optional (cfg.napcat.healthCheck.metricsFile != null)
          (builtins.dirOf cfg.napcat.healthCheck.metricsFile);
      };
    };

    systemd.timers."${serviceName}-qq-health" = lib.mkIf (cfg.napcat.enable && cfg.napcat.healthCheck.enable) {
      wantedBy = ["timers.target"];
      timerConfig = { OnBootSec = "90s"; OnUnitInactiveSec = "30s"; };
    };

    environment.systemPackages = lib.optionals cfg.napcat.passwordLogin.enable [
      (pkgs.writeShellScriptBin "${serviceName}-qq-password-login" ''
        exec ${pkgs.python3}/bin/python3 ${./napcat-password-login.py} "$@"
      '')
    ];

    systemd.services."${serviceName}-qq-password-login" = lib.mkIf cfg.napcat.passwordLogin.enable {
      description = "Attempt QQ password login once; stop for manual verification on failure";
      after = ["${napcatServiceName}.service"];
      serviceConfig = {
        Type = "oneshot";
        ExecCondition = "${pkgs.systemd}/bin/systemctl is-active --quiet ${napcatServiceName}.service";
        ExecStart = lib.concatStringsSep " " ([
          "${pkgs.python3}/bin/python3" "${./napcat-password-login.py}"
          "--config" (lib.escapeShellArg "${cfg.napcat.dataDirectory}/config/webui.json")
          "--port" (toString cfg.napcat.webuiPort)
          "--uin" (lib.escapeShellArg cfg.napcat.account)
          "--password-file" "%d/qq-password"
          "--state" "/var/lib/${serviceName}-qq-password-login/state.json"
        ] ++ lib.optionals cfg.napcat.passwordLogin.notification.enable [
          "--notify-config" (lib.escapeShellArg cfg.napcat.passwordLogin.notification.webuiConfigFile)
          "--notify-port" (toString cfg.napcat.passwordLogin.notification.webuiPort)
          "--notify-uin" cfg.napcat.passwordLogin.notification.account
          "--notify-group" cfg.napcat.passwordLogin.notification.groupId
          "--notify-user" cfg.napcat.passwordLogin.notification.userId
        ]);
        LoadCredential = ["qq-password:${cfg.napcat.passwordLogin.passwordFile}"];
        StateDirectory = "${serviceName}-qq-password-login";
        StateDirectoryMode = "0700";
        TimeoutStartSec = "120s";
        UMask = "0077";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
      };
    };

    systemd.timers."${serviceName}-qq-password-login" = lib.mkIf cfg.napcat.passwordLogin.enable {
      wantedBy = ["timers.target"];
      timerConfig = { OnBootSec = "120s"; OnUnitInactiveSec = "60s"; };
    };

    systemd.tmpfiles.rules = lib.optionals cfg.napcat.enable [
      "d ${cfg.napcat.dataDirectory} 0700 root root -"
      "d ${cfg.napcat.dataDirectory}/QQ 0700 root root -"
      "d ${cfg.napcat.dataDirectory}/config 0700 root root -"
      "d ${cfg.napcat.dataDirectory}/outbox 0700 root root -"
    ];
  };
}
