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
  native = evaluate {
    qqOfficial = official // {hostConfigFile = "/run/secrets/TEST-host.json";};
    stateDirectory = "TEST-state";
    environmentFile = "/run/secrets/TEST-bot.env";
    environment = {TEST_PRIVATE = "TEST_ONLY_BOT"; AI_GLOBAL_INTEREST_STATE_DIR = "/TEST-other";};
  };
  interest = native.systemd.services.gaoji-interest;
  timer = native.systemd.timers.gaoji-interest;
  bot = native.systemd.services.gaoji;
  noPlatform = evaluate {};
  badHost = value: evaluate {qqOfficial = official // {hostConfigFile = value;};};
  tests = {
    nativeAssertionsPass = failures native == [];
    nativeLoadsProtectedHostPath = (environment native).AI_QQ_OFFICIAL_HOST_CONFIG_FILE == "/run/credentials/gaoji.service/qq-official-host-config";
    nativeHasExactlyFourOfficialCredentials = builtins.length (builtins.filter (value: lib.hasPrefix "qq-official-" value) (credentials native)) == 4;
    noPlatformRejected = failures noPlatform != [];
    storeHostPathRejected = failures (badHost "/nix/store/TEST-host.json") != [];
    relativeHostPathRejected = failures (badHost "TEST-host.json") != [];
    traversingHostPathRejected = failures (badHost "/run/secrets/../TEST-host.json") != [];
    contextHostPathRejected = failures (badHost "${self}/TEST-host.json") != [];
    interestTimer = builtins.elem "timers.target" timer.wantedBy && timer.timerConfig.Persistent
      && timer.timerConfig.OnCalendar == "*-*-* 05:00:00 Asia/Shanghai" && timer.timerConfig.Unit == "gaoji-interest.service";
    interestAccount = interest.serviceConfig.Type == "oneshot" && interest.serviceConfig.User == "gaoji-interest"
      && interest.serviceConfig.Group == "gaoji-interest" && native.users.users.gaoji-interest.isSystemUser
      && builtins.elem "gaoji-interest" bot.serviceConfig.SupplementaryGroups;
    interestState = interest.serviceConfig.StateDirectory == "TEST-state-interest" && interest.serviceConfig.StateDirectoryMode == "0750"
      && bot.environment.AI_GLOBAL_INTEREST_STATE_DIR == "/var/lib/TEST-state-interest"
      && lib.hasInfix "AI_GLOBAL_INTEREST_STATE_DIR=/var/lib/TEST-state-interest" bot.serviceConfig.ExecStart
      && lib.hasSuffix "/bin/gaoji-interest /var/lib/TEST-state-interest" interest.serviceConfig.ExecStart;
    interestIsolation = !(interest.serviceConfig ? EnvironmentFile) && !(interest.serviceConfig ? LoadCredential)
      && !(interest.environment ? TEST_PRIVATE) && !(interest.environment ? AI_POSTGRES_DSN)
      && interest.serviceConfig.NoNewPrivileges && interest.serviceConfig.ProtectSystem == "strict"
      && interest.serviceConfig.ProtectHome && interest.serviceConfig.PrivateTmp && interest.serviceConfig.CapabilityBoundingSet == ""
      && interest.serviceConfig.TimeoutStartSec == 30 && builtins.elem "-/var/lib/TEST-state" interest.serviceConfig.InaccessiblePaths
      && builtins.elem "-/run/postgresql" interest.serviceConfig.InaccessiblePaths && builtins.elem "-/run/credentials" interest.serviceConfig.InaccessiblePaths
      && builtins.elem "localhost" interest.serviceConfig.IPAddressDeny && builtins.elem "10.0.0.0/8" interest.serviceConfig.IPAddressDeny
      && builtins.elem "fc00::/7" interest.serviceConfig.IPAddressDeny && builtins.length interest.serviceConfig.BindReadOnlyPaths == 2;
    noInterestWithoutBot = let config = evaluate {runBot = false;}; in
      !(config.systemd.services ? gaoji-interest) && !(config.systemd.timers ? gaoji-interest);
  };
in {
  inherit tests;
  failed = builtins.attrNames (lib.filterAttrs (_: value: !value) tests);
}
