{self}: let
  evaluated = import (self.inputs.nixpkgs + "/nixos/lib/eval-config.nix") {
    system = "x86_64-linux";
    modules = [
      self.nixosModules.host-control
      self.nixosModules.cluster-control
      ({pkgs, ...}: {
        system.stateVersion = "26.05";
        boot.isContainer = true;
        fileSystems."/" = {device = "none"; fsType = "tmpfs";};
        services.gaoji-host-control = {
          enable = true;
          hostId = "test";
          ssh.enable = true;
        };
        services.gaoji-cluster-control = {
          enable = true;
          apiTokenFile = "/run/secrets/test-token";
          environmentFile = "/run/secrets/test-db";
          localHostId = "test";
          inventory = [{host_id = "test"; observe = true; operate = true;}];
          ssh = {
            enable = true;
            knownHostsFile = pkgs.writeText "test-host-pins" "example.invalid test-only";
            targets.test.destination = "gaoji-operator@example.invalid";
            managementHosts = ["test"];
            administrators = ["admin:test"];
          };
        };
      })
    ];
  };
in {
  failedAssertions = map (item: item.message) (builtins.filter (item: !item.assertion) evaluated.config.assertions);
  account = evaluated.config.users.users.gaoji-operator.isSystemUser;
  sudo = evaluated.config.security.sudo.extraRules;
  environment = evaluated.config.systemd.services.gaoji-cluster-control.environment;
  credentials = evaluated.config.systemd.services.gaoji-cluster-control.serviceConfig.LoadCredential;
}
