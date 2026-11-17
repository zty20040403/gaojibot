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
  vmSandbox = {pkgs, ...}: {
    services.gaoji.sandbox = {
      enable = true;
      vmImage = pkgs.hello;
      vmRoot = "/var/lib/TEST/vms";
    };
  };
  vm = evaluate vmSandbox;
  vmNftables = evaluate {imports = [vmSandbox]; networking.nftables.enable = true;};
  noSandbox = evaluate {};
  owner = "qqo:conversation:" + lib.fixedWidthString 64 "0" "1";
  ops = evaluate {
    imports = [vmSandbox];
    networking.nftables.enable = true;
    services.gaoji.sandbox.operations = {
      enable = true;
      owners = [owner];
      loginServer = "https://TEST.invalid";
      domain = "TEST.invalid";
      authKeyFile = "/run/secrets/TEST-authkey";
    };
  };
  opsBot = ops.systemd.services.gaoji;
  unit = config: config.systemd.services.gaoji-vm;
  hook = value: lib.isList value && lib.hasInfix "gaoji-vm-network" (lib.last value)
    && lib.hasSuffix "--if-running" (lib.last value);
  nftables = vmNftables.systemd.services.nftables.serviceConfig;
  tests = {
    vmUnitInstallsRulesBeforeDaemon = lib.hasPrefix "+" (unit vm).serviceConfig.ExecStartPre
      && lib.hasInfix "gaoji-vm-network" (unit vm).serviceConfig.ExecStartPre
      && lib.hasSuffix "/sbin/virtqemud" (unit vm).serviceConfig.ExecStart;
    vmUnitSharesLibvirtSession = (unit vm).environment.XDG_RUNTIME_DIR == vm.systemd.services.gaoji.environment.XDG_RUNTIME_DIR
      && (unit vm).environment.HOME == vm.systemd.services.gaoji.environment.HOME;
    botRequiresVmUnit = builtins.elem "gaoji-vm.service" vm.systemd.services.gaoji.requires
      && builtins.elem "gaoji-vm.service" vm.systemd.services.gaoji.after;
    # Runs last in the same job, after nftables has loaded (or flushed) the ruleset.
    nftablesReloadReinstalls = hook nftables.ExecReload && hook nftables.ExecStartPost;
    relayPortsDefault = vm.services.gaoji.sandbox.vmHostPorts == [18640 18641];
    opsBotUsesSecondDaemon = opsBot.environment.AI_SANDBOX_OPERATIONS_OWNERS == owner
      && opsBot.environment.AI_SANDBOX_OPERATIONS_SOCKET == ops.systemd.services.gaoji-ops-vm.environment.XDG_RUNTIME_DIR + "/libvirt/virtqemud-sock"
      && builtins.elem "gaoji-ops-vm.service" opsBot.requires;
    opsDaemonRunsInNamespace = ops.systemd.services.gaoji-ops-vm.serviceConfig.NetworkNamespacePath == "/run/netns/gaoji-ops"
      && ops.systemd.services.gaoji-ops-tailscaled.serviceConfig.NetworkNamespacePath == "/run/netns/gaoji-ops";
    opsRelaysEveryHostPort = builtins.all (port: ops.systemd.sockets ? "gaoji-ops-relay-${toString port}"
      && ops.systemd.services ? "gaoji-ops-relay-${toString port}") [18640 18641];
    opsOffByDefault = !(vm.systemd.services ? gaoji-ops-vm) && !(vm.systemd.sockets ? gaoji-ops-relay-18640);
    noSandboxHasNoVmUnit = !(noSandbox.systemd.services ? gaoji-vm);
  };
in {
  inherit tests;
  failed = builtins.attrNames (lib.filterAttrs (_: value: !value) tests);
}
