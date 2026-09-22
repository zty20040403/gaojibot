{config, lib, pkgs, ...}: let
  cfg = config.services.gaoji.napcat;
  name = "gaoji-napcat";
  fonts = pkgs.makeFontsConf {fontDirectories = [pkgs.source-han-sans];};
  inner = pkgs.writeShellScript "gaoji-napcat-inner" ''
    set -eu
    export HOME=/root XDG_CONFIG_HOME=/root/.config XDG_DATA_HOME=/root/.local/share
    export FONTCONFIG_FILE=${fonts}
    export SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
    export PATH=${lib.makeBinPath [pkgs.coreutils pkgs.bash pkgs.xorg-server pkgs.procps]}
    mkdir -p /usr/bin /bin /root/.local/share
    ln -s ${pkgs.coreutils}/bin/env /usr/bin/env
    ln -s ${pkgs.bash}/bin/sh /bin/sh
    cp -r --update=none ${cfg.nativePackage}/napcat/. /root/napcat/
    Xvfb -displayfd 3 -nolisten tcp 3>/tmp/display >/dev/null 2>&1 &
    display_pid=$!
    trap 'kill "$display_pid" 2>/dev/null || true' EXIT
    for _ in $(seq 1 100); do
      test -s /tmp/display && break
      kill -0 "$display_pid"
      sleep 0.1
    done
    test -s /tmp/display
    export DISPLAY=":$(cat /tmp/display)"
    ${cfg.nativePackage}/bin/qq --no-sandbox -q ${lib.escapeShellArg cfg.account}
  '';
  launcher = pkgs.writeShellScript "gaoji-napcat" ''
    exec ${pkgs.bubblewrap}/bin/bwrap \
      --unshare-all --share-net --as-pid-1 --uid 0 --gid 0 --clearenv \
      --ro-bind /nix/store /nix/store \
      --ro-bind /etc/resolv.conf /etc/resolv.conf \
      --ro-bind ${pkgs.tzdata}/share/zoneinfo/Asia/Shanghai /etc/localtime \
      --bind ${lib.escapeShellArg "${cfg.dataDirectory}/QQ"} /root/.config/QQ \
      --bind ${lib.escapeShellArg "${cfg.dataDirectory}/config"} /root/napcat/config \
      --ro-bind ${lib.escapeShellArg "${cfg.dataDirectory}/outbox"} /data/outbox \
      --proc /proc --dev /dev --tmpfs /tmp \
      ${inner}
  '';
in {
  config = lib.mkIf (config.services.gaoji.enable && cfg.enable && cfg.backend == "native") {
    users.groups.${name} = {};
    users.users.${name} = {isSystemUser = true; group = name;};
    systemd.services.${name} = {
      description = "Gaoji native isolated QQ client";
      wantedBy = ["multi-user.target"];
      wants = ["network-online.target"];
      conflicts = ["docker-${cfg.containerName}.service"];
      after = ["network-online.target" "docker-${cfg.containerName}.service"];
      preStart = ''
        ${pkgs.python3}/bin/python3 ${./napcat-native-config.py} \
          --directory ${lib.escapeShellArg "${cfg.dataDirectory}/config"} \
          --account ${lib.escapeShellArg cfg.account} \
          --url ${lib.escapeShellArg cfg.reverseWebsocketUrl} \
          --previous-url ${lib.escapeShellArg "ws://host.docker.internal:${toString config.services.gaoji.port}/onebot/v11/ws"} \
          --host ${lib.escapeShellArg cfg.webuiAddress} --port ${toString cfg.webuiPort} \
          ${lib.optionalString (cfg.reverseWebsocketTokenFile != null) ''--token-file "$CREDENTIALS_DIRECTORY/onebot-token"''}
      '';
      serviceConfig = {
        ExecStart = launcher;
        User = name;
        Group = name;
        WorkingDirectory = cfg.dataDirectory;
        ReadWritePaths = [cfg.dataDirectory];
        NoNewPrivileges = true;
        CapabilityBoundingSet = [""];
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        # A /proc/sys overmount prevents bubblewrap from mounting its private /proc.
        ProtectKernelTunables = false;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        UMask = "0077";
        MemoryMax = "4G";
        TasksMax = 512;
        KillMode = "mixed";
        TimeoutStopSec = 30;
        Restart = "on-failure";
        RestartSec = 10;
      };
    };
  };
}
