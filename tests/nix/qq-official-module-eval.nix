{self}: let
  lib = self.inputs.nixpkgs.lib;
  evaluate = overrides: (import (self.inputs.nixpkgs + "/nixos/lib/eval-config.nix") {
    system = "x86_64-linux";
    modules = [self.nixosModules.default ({pkgs, ...}: {
      system.stateVersion = "26.05";
      boot.isContainer = true;
      fileSystems."/" = {device = "none"; fsType = "tmpfs";};
      services.gaoji = {
        enable = true;
        package = pkgs.hello;
      };
    }) {services.gaoji = overrides;}];
  }).config;
  failures = config: map (item: item.message) (builtins.filter (item: !item.assertion) config.assertions);
  environment = config: config.systemd.services.gaoji.environment;
  credentials = config: config.systemd.services.gaoji.serviceConfig.LoadCredential;
  official = {
    enable = true;
    appIdFile = "/run/secrets/TEST-app-id";
    appSecretFile = "/run/secrets/TEST-app-secret";
    bindPepperFile = "/run/secrets/TEST-bind-pepper";
  };
  native = evaluate {qqOfficial = official // {hostConfigFile = "/run/secrets/TEST-host.json";};};
  noPlatform = evaluate {};
  ops = evaluate {qqOfficial = official; opsRead = {
    baseUrl = "http://127.0.0.1:8091"; tokenFile = "/run/secrets/TEST-ops";
  };};
  badHost = value: evaluate {qqOfficial = official // {hostConfigFile = value;};};
  tests = {
    nativeAssertionsPass = failures native == [];
    nativeLoadsProtectedHostPath = (environment native).AI_QQ_OFFICIAL_HOST_CONFIG_FILE == "/run/credentials/gaoji.service/qq-official-host-config";
    nativeHasExactlyFourOfficialCredentials = builtins.length (builtins.filter (value: lib.hasPrefix "qq-official-" value) (credentials native)) == 4;
    opsLoadsHostCredential = builtins.elem "ops-read-token:/run/secrets/TEST-ops" (credentials ops)
      && (environment ops).AI_OPS_READ_TOKEN_FILE == "/run/credentials/gaoji.service/ops-read-token";
    noPlatformRejected = failures noPlatform != [];
    storeHostPathRejected = failures (badHost "/nix/store/TEST-host.json") != [];
    relativeHostPathRejected = failures (badHost "TEST-host.json") != [];
    traversingHostPathRejected = failures (badHost "/run/secrets/../TEST-host.json") != [];
    contextHostPathRejected = failures (badHost "${self}/TEST-host.json") != [];
  };
in {
  inherit tests;
  failed = builtins.attrNames (lib.filterAttrs (_: value: !value) tests);
}
