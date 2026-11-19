{self}: modules: (import (self.inputs.nixpkgs + "/nixos/lib/eval-config.nix") {
  system = "x86_64-linux";
  modules = [self.nixosModules.default ({pkgs, ...}: {
    system.stateVersion = "26.05";
    boot.isContainer = true;
    fileSystems."/" = {device = "none"; fsType = "tmpfs";};
    services.gaoji = {enable = true; package = pkgs.hello;};
  })] ++ modules;
}).config
