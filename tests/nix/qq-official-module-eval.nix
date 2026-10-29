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
        codesnap.enable = false;
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
  legacy = evaluate {};
  dormant = evaluate {qqOfficial.hostConfigFile = "/nix/store/TEST-dormant";};
  dual = evaluate {qqOfficial = official;};
  native = evaluate {oneBot.enable = false; qqOfficial = official // {hostConfigFile = "/run/secrets/TEST-host.json";};};
  delivery = evaluate {qqOfficial = official // {
    markdown = false; maxTextChars = 300; maxTextParts = 2; maxTextBytes = 600;
    deferMaxHours = 2; allowNewerWindows = true; rateGroupProactivePerMinute = 5;
    projectGrantTtlSeconds = 300;
    fileLinkTtlSeconds = 120;
  };};
  noPlatform = evaluate {oneBot.enable = false;};
  nativeWithNapcat = evaluate {oneBot.enable = false; qqOfficial = official; napcat.enable = true;};
  badHost = value: evaluate {qqOfficial = official // {hostConfigFile = value;};};
  tests = {
    legacyHasNoOfficialEnvironment = lib.all (name: !(lib.hasPrefix "AI_QQ_" name) && name != "AI_ONEBOT_ENABLED") (builtins.attrNames (environment legacy));
    dormantDoesNotChangeLegacyEnvironment = environment dormant == environment legacy;
    dormantDoesNotLoadHostCredential = credentials dormant == credentials legacy;
    legacyAssertionsPass = failures legacy == [];
    dualAssertionsPass = failures dual == [];
    dualPreservesDefaultOneBot = !(environment dual ? AI_ONEBOT_ENABLED);
    nativeAssertionsPass = failures native == [];
    deliveryAssertionsPass = failures delivery == [];
    deliveryRendersExplicitLimits = (environment delivery).AI_QQ_OFFICIAL_MARKDOWN == "false"
      && (environment delivery).AI_QQ_OFFICIAL_MAX_TEXT_CHARS == "300"
      && (environment delivery).AI_QQ_OFFICIAL_MAX_TEXT_PARTS == "2"
      && (environment delivery).AI_QQ_OFFICIAL_MAX_TEXT_BYTES == "600";
    deliveryPreservesOriginalDeadlineSetting = (environment delivery).AI_QQ_OFFICIAL_DEFER_MAX_HOURS == "2"
      && (environment delivery).AI_QQ_OFFICIAL_ALLOW_NEWER_WINDOWS == "true";
    deliveryUsesSelectedLocalRateCap = (environment delivery).AI_QQ_OFFICIAL_RATE_GROUP_PROACTIVE_PER_MINUTE == "5";
    deliveryBoundsNewProjectGrants = (environment delivery).AI_QQ_OFFICIAL_PROJECT_GRANT_TTL_SECONDS == "300";
    deliveryBoundsNewFileLinks = (environment delivery).AI_QQ_OFFICIAL_FILE_LINK_TTL_SECONDS == "120";
    unsetRateCapsRemainEmpty = (environment native).AI_QQ_OFFICIAL_RATE_PASSIVE_PER_SECOND == "";

    nativeDisablesOneBot = (environment native).AI_ONEBOT_ENABLED == "false";
    nativeLoadsProtectedHostPath = (environment native).AI_QQ_OFFICIAL_HOST_CONFIG_FILE == "/run/credentials/gaoji.service/qq-official-host-config";
    nativeHasExactlyFourOfficialCredentials = builtins.length (builtins.filter (value: lib.hasPrefix "qq-official-" value) (credentials native)) == 4;
    nativeDoesNotStartNapcat = !(native.virtualisation.oci-containers.containers ? napcat-gaoji);
    noPlatformRejected = failures noPlatform != [];
    nativeNapcatConflictRejected = failures nativeWithNapcat != [];
    storeHostPathRejected = failures (badHost "/nix/store/TEST-host.json") != [];
    relativeHostPathRejected = failures (badHost "TEST-host.json") != [];
    traversingHostPathRejected = failures (badHost "/run/secrets/../TEST-host.json") != [];
    contextHostPathRejected = failures (badHost "${self}/TEST-host.json") != [];
  };
in {
  inherit tests;
  failed = builtins.attrNames (lib.filterAttrs (_: value: !value) tests);
}
