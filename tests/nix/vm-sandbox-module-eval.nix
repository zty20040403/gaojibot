{self}: let
  lib = self.inputs.nixpkgs.lib;
  evaluate = module: (import (self.inputs.nixpkgs + "/nixos/lib/eval-config.nix") {
    system = "x86_64-linux";
    modules = [self.nixosModules.default ({pkgs, ...}: {
      system.stateVersion = "26.05";
      boot.isContainer = true;
      fileSystems."/" = {device = "none"; fsType = "tmpfs";};
      services.gaoji = {
        enable = true;
        package = pkgs.hello;
        qqOfficial = {
          enable = true;
          appIdFile = "/run/secrets/TEST-app-id";
          appSecretFile = "/run/secrets/TEST-app-secret";
          bindPepperFile = "/run/secrets/TEST-bind-pepper";
        };
      };
    }) module];
  }).config;
  noSandbox = evaluate {};
  owner = "qqo:conversation:" + lib.fixedWidthString 64 "0" "1";
  container = extra: evaluate {
    imports = [extra];
    networking.nftables.enable = true;
    systemd.network.enable = true;
    services.gaoji.sandbox = {
      enable = true;
      vmRoot = "/var/lib/TEST/vms";
    };
  };
  ct = container {};
  snaps = (container {services.gaoji.sandbox.workSnapshots.enable = true;}).systemd;
  operations = {
    services.gaoji.sandbox.operations = {
      enable = true;
      owners = [owner];
      loginServer = "https://TEST.invalid";
      domain = "TEST.invalid";
      authKeyFile = "/run/secrets/TEST-authkey";
    };
  };
  ctOps = container operations;
  ctHost = container {
    imports = [operations];
    services.gaoji.sandbox.operations.hostAddress = "192.0.2.123"; # TEST-NET-1
    services.gaoji.sandbox.vmHostPorts = [18001 18003];
  };
  ctBot = ct.systemd.services.gaoji;
  opsBridge = ctOps.systemd.network.networks."60-vz-gaoji-ops";
  opsUplink = ctOps.systemd.network.networks."60-gaoji-uplink";
  opsUnit = ctOps.systemd.services."gaoji-ct-ops@";
  tests = {
    defaultBackendIsContainer = ct.services.gaoji.sandbox.backend == "container";
    workSnapshotsOptIn = !(ct.systemd.timers ? gaoji-work-snapshot)
      && !(ct.systemd.services ? gaoji-work-snapshot)
      && snaps.timers.gaoji-work-snapshot.timerConfig.Persistent
      && snaps.timers.gaoji-work-snapshot.wantedBy == ["timers.target"];
    workSnapshotsHostOnly = let
      unit = snaps.services.gaoji-work-snapshot;
    in unit.serviceConfig.Type == "oneshot"
      && unit.serviceConfig.ReadWritePaths == ["/data/services/gaoji/backups/work"]
      && unit.serviceConfig.ProtectSystem == "strict"
      && !(unit.serviceConfig ? EnvironmentFile)
      && lib.hasSuffix " /var/lib/gaoji-ct /data/services/gaoji/backups/work" unit.serviceConfig.ExecStart;
    relayPortsDefault = ct.services.gaoji.sandbox.vmHostPorts == [18640 18641];
    opsOffByDefault = !(ct.systemd.services ? gaoji-ops-tailscaled)
      && !(ct.systemd.network.netdevs ? "60-vz-gaoji-ops");
    opsReusesHostRelays = ctHost.networking.firewall.interfaces.vz-gaoji-ops.allowedTCPPorts == [18001 18003]
      && lib.hasInfix "ip daddr 10.0.2.2 tcp dport { 18001, 18003 } accept" ctHost.networking.nftables.tables.gaoji-ops.content
      && builtins.all (p: ctHost.systemd.sockets."gaoji-ct-relay-${toString p}".socketConfig.ListenStream == "10.0.2.2:${toString p}") [18001 18003];
    opsHostAddressIsOptIn = ctOps.services.gaoji.sandbox.operations.hostAddress == null
      && !(lib.hasInfix "dnat ip to" ctOps.networking.nftables.tables.gaoji-ops.content)
      && !(lib.hasInfix "dnat ip to" ctOps.systemd.services.gaoji-ops-network.script);
    opsHostAddressTraversesTailnet = lib.hasInfix ''iifname "vz-gaoji-ops" ip saddr 10.237.0.16/28 ip daddr 192.0.2.123 dnat ip to 10.237.0.2'' ctHost.networking.nftables.tables.gaoji-ops.content
      && lib.hasInfix ''iifname "uplink" ip saddr 10.237.0.16/28 ip daddr 10.237.0.2 dnat ip to 192.0.2.123'' ctHost.systemd.services.gaoji-ops-network.script;
    opsTailscaledRunsInNamespace = ctOps.systemd.services.gaoji-ops-tailscaled.serviceConfig.NetworkNamespacePath == "/run/netns/gaoji-ops";
    opsBridgeRoutesThroughNamespace = opsBridge.networkConfig.DHCPServer
      && opsBridge.address == ["10.237.0.17/28"]
      && builtins.any (r: r.From == "10.237.0.16/28" && r.IncomingInterface == "vz-gaoji-ops" && r.Table == 237 && r.Priority == 237) opsBridge.routingPolicyRules
      && builtins.any (r: r.Gateway == "10.237.0.2" && r.Table == 237) opsUplink.routes
      && builtins.any (r: r.Type == "unreachable" && r.Table == 237) opsBridge.routes;
    containerBotUsesBrokerNotLibvirt = ctBot.environment.AI_SANDBOX_BACKEND == "container"
      && ctBot.environment.AI_SANDBOX_CONTAINER_BROKER == "/run/gaoji-ct/broker.sock"
      && builtins.elem "gaoji-ct-broker.socket" ctBot.requires
      && !(ct.systemd.services ? gaoji-vm) && !(ctBot.environment ? AI_SANDBOX_VM_IMAGE)
      && !ct.virtualisation.libvirtd.enable;
    containerBrokerSocketIsBotOnly = ct.systemd.sockets.gaoji-ct-broker.socketConfig.SocketMode == "0600"
      && ct.systemd.sockets.gaoji-ct-broker.socketConfig.SocketUser == ct.services.gaoji.user;
    containerUnitsBootTheContainerSystem = lib.hasInfix "gaoji-ct-start" ct.systemd.services."gaoji-ct@".serviceConfig.ExecStart
      && lib.hasSuffix " plain" ct.systemd.services."gaoji-ct@".serviceConfig.ExecStart
      && lib.hasSuffix " ops" ct.systemd.services."gaoji-ct-ops@".serviceConfig.ExecStart;
    containerBridgeIsHostAtGuestAddress = ct.systemd.network.networks."60-vz-gaoji".address == ["10.0.2.2/24"]
      && builtins.all (port: ct.systemd.sockets ? "gaoji-ct-relay-${toString port}") [18640 18641];
    containerPortsAreIsolated = let
      plain = ctOps.systemd.network.networks."61-gaoji-ct-ports";
      ops = ctOps.systemd.network.networks."61-gaoji-ct-ops-ports";
    in plain.matchConfig.Name == "vb-gct-*" && plain.networkConfig.Bridge == "vz-gaoji" && plain.bridgeConfig.Isolated
      && ops.matchConfig.Name == "ve-gct-*" && ops.networkConfig.Bridge == "vz-gaoji-ops" && ops.bridgeConfig.Isolated;
    containerOpsHasOwnNamespace = ctOps.systemd.services ? gaoji-ops-tailscaled
      && !(ctOps.systemd.services ? gaoji-ops-vm)
      && builtins.elem "gaoji-ops-network.service" opsUnit.requires
      && !(opsUnit.serviceConfig ? NetworkNamespacePath)
      && builtins.elem "gaoji-ct-cdp-ops@%i.service" opsUnit.wants
      && ctOps.systemd.services."gaoji-ct-cdp-ops@".bindsTo == ["gaoji-ct-ops@%i.service"]
      && ctOps.systemd.services.gaoji.environment.AI_SANDBOX_OPERATIONS_OWNERS == owner
      && !(ctOps.systemd.services.gaoji.environment ? AI_SANDBOX_OPERATIONS_SOCKET);
    noSandboxHasNoContainerBroker = !(noSandbox.systemd.sockets ? gaoji-ct-broker)
      && !(builtins.elem "gaoji-ct-broker.socket" noSandbox.systemd.services.gaoji.requires);
  };
in {
  inherit tests;
  failed = builtins.attrNames (lib.filterAttrs (_: value: !value) tests);
}
