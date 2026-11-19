{self}: let
  lib = self.inputs.nixpkgs.lib;
  evaluate = module: import ./evaluate.nix {inherit self;} [{services.gaoji.qqOfficial = {
    enable = true; appIdFile = "/run/secrets/TEST-app-id";
    appSecretFile = "/run/secrets/TEST-app-secret"; bindPepperFile = "/run/secrets/TEST-bind-pepper";
  };} module];
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
  ctOps = container {
    services.gaoji.sandbox.operations = {
      enable = true;
      owners = [owner];
      loginServer = "https://TEST.invalid";
      domain = "TEST.invalid";
      authKeyFile = "/run/secrets/TEST-authkey";
    };
  };
  ctBot = ct.systemd.services.gaoji;
  tests = {
    defaultBackendIsContainer = ct.services.gaoji.sandbox.backend == "container";
    relayPortsDefault = ct.services.gaoji.sandbox.vmHostPorts == [18640 18641];
    opsOffByDefault = !(ct.systemd.services ? gaoji-ops-tailscaled)
      && !(ct.systemd.sockets ? gaoji-ops-relay-18640);
    opsRelaysEveryHostPort = builtins.all (port: ctOps.systemd.sockets ? "gaoji-ops-relay-${toString port}"
      && ctOps.systemd.services ? "gaoji-ops-relay-${toString port}") [18640 18641];
    opsTailscaledRunsInNamespace = ctOps.systemd.services.gaoji-ops-tailscaled.serviceConfig.NetworkNamespacePath == "/run/netns/gaoji-ops"
      && ctOps.systemd.services.gaoji-ops-network.unitConfig.DefaultDependencies == false;
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
      && ct.systemd.network.networks."61-gaoji-ct-ports".bridgeConfig.Isolated
      && builtins.all (port: ct.systemd.sockets ? "gaoji-ct-relay-${toString port}") [18640 18641];
    containerOpsSharesNamespace = ctOps.systemd.services ? gaoji-ops-tailscaled
      && !(ctOps.systemd.services ? gaoji-ops-vm)
      && builtins.elem "gaoji-ops-network.service" ctOps.systemd.services."gaoji-ct-ops@".requires
      && ctOps.systemd.services."gaoji-ct-ops@".serviceConfig.NetworkNamespacePath == "/run/netns/gaoji-ops"
      && ctOps.systemd.services.gaoji.environment.AI_SANDBOX_OPERATIONS_OWNERS == owner
      && !(ctOps.systemd.services.gaoji.environment ? AI_SANDBOX_OPERATIONS_SOCKET);
    noSandboxHasNoContainerBroker = !(noSandbox.systemd.sockets ? gaoji-ct-broker)
      && !(builtins.elem "gaoji-ct-broker.socket" noSandbox.systemd.services.gaoji.requires);
  };
in {
  inherit tests;
  failed = builtins.attrNames (lib.filterAttrs (_: value: !value) tests);
}
