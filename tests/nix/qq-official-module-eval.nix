{self}: let
  lib = self.inputs.nixpkgs.lib;
  evaluate = overrides: evaluateWith overrides {};
  evaluateWith = overrides: extra: import ./evaluate.nix {inherit self;} [({config, ...}: {
    networking.nftables.enable = true;
    security.unprivilegedUsernsClone = lib.mkDefault (config.services.gaoji.runBot && config.services.gaoji.searchBrowser.enable);
    services.gaoji = overrides;
  }) extra];
  failures = config: map (item: item.message) (builtins.filter (item: !item.assertion) config.assertions);
  contains = items: list: lib.all (item: builtins.elem item list) items;
  isolated = service: let c = service.serviceConfig; in
    !(c ? EnvironmentFile) && !(c ? LoadCredential) && !(service.environment ? TEST_PRIVATE) && !(service.environment ? AI_POSTGRES_DSN)
    && c.NoNewPrivileges && c.PrivateTmp && c.ProtectHome && c.ProtectSystem == "strict" && c.CapabilityBoundingSet == ""
    && contains ["localhost" "10.0.0.0/8" "fc00::/7"] c.IPAddressDeny;
  official = {enable = true; appIdFile = "/run/secrets/TEST-app-id";
    appSecretFile = "/run/secrets/TEST-app-secret"; bindPepperFile = "/run/secrets/TEST-bind-pepper";};
  native = evaluate {
    searchBrowser.enable = true;
    qqOfficial = official // {hostConfigFile = "/run/secrets/TEST-host.json";};
    stateDirectory = "TEST-state";
    environmentFile = "/TEST-protected.env";
    environment = {TEST_PRIVATE = "TEST_ONLY_BOT"; AI_GLOBAL_INTEREST_STATE_DIR = "/TEST-other";};
  };
  interest = native.systemd.services.gaoji-interest; ic = interest.serviceConfig;
  timer = native.systemd.timers.gaoji-interest;
  bot = native.systemd.services.gaoji;
  browser = native.systemd.services.gaoji-search-browser; bc = browser.serviceConfig;
  noPlatform = evaluate {};
  badHost = value: evaluate {qqOfficial = official // {hostConfigFile = value;};};
  searchConfig = {qqOfficial = official; searchBrowser.enable = true;};
  isolationFailure = "The public search browser requires nftables, unprivileged user namespaces and a dedicated account without supplementary groups";
  noBrowser = config: !(config.systemd.services ? gaoji-search-browser) && !(config.systemd.sockets ? gaoji-search-browser)
    && !(config.users.users ? gaoji-search-browser) && !(config.users.groups ? gaoji-search-browser)
    && !(config.networking.nftables.tables ? gaoji-search-browser);
  tests = {
    nativeAssertionsPass = failures native == [];
    nativeLoadsProtectedHostPath = bot.environment.AI_QQ_OFFICIAL_HOST_CONFIG_FILE == "/run/credentials/gaoji.service/qq-official-host-config";
    nativeHasExactlyFourOfficialCredentials = builtins.length (builtins.filter (value: lib.hasPrefix "qq-official-" value) bot.serviceConfig.LoadCredential) == 4;
    noPlatformRejected = failures noPlatform != [];
    badHostPathsRejected = lib.all (path: failures (badHost path) != []) [
      "/nix/store/TEST-host.json" "TEST-host.json" "/run/secrets/../TEST-host.json" "${self}/TEST-host.json"
    ];
    interestTimer = builtins.elem "timers.target" timer.wantedBy && timer.timerConfig.Persistent
      && timer.timerConfig.OnCalendar == "*-*-* 05:00:00 Asia/Shanghai" && timer.timerConfig.Unit == "gaoji-interest.service";
    interestAccount = ic.Type == "oneshot" && ic.User == "gaoji-interest" && ic.Group == "gaoji-interest"
      && native.users.users.gaoji-interest.isSystemUser && builtins.elem "gaoji-interest" bot.serviceConfig.SupplementaryGroups;
    interestState = ic.StateDirectory == "TEST-state-interest" && ic.StateDirectoryMode == "0750"
      && bot.environment.AI_GLOBAL_INTEREST_STATE_DIR == "/var/lib/TEST-state-interest"
      && lib.hasInfix "AI_GLOBAL_INTEREST_STATE_DIR=/var/lib/TEST-state-interest" bot.serviceConfig.ExecStart
      && lib.hasSuffix "/bin/gaoji-interest /var/lib/TEST-state-interest" ic.ExecStart;
    interestIsolation = isolated interest && ic.TimeoutStartSec == 30 && builtins.length ic.BindReadOnlyPaths == 2
      && contains ["-/var/lib/TEST-state" "-/run/postgresql" "-/run/credentials"] ic.InaccessiblePaths;
    browserIsolation = isolated browser && bc.IPAddressDeny == ic.IPAddressDeny && bc.PrivateIPC
      && browser.confinement.enable && !browser.confinement.fullUnit && lib.hasInfix "-gaoji-search-scope " bc.ExecStart
      && bc.MemoryMax == "2G" && bc.TasksMax == 256 && !(builtins.elem "gaoji-search-browser.service" bot.requires);
    browserRequiresIsolation = lib.all (extra: builtins.elem isolationFailure (failures (evaluateWith searchConfig extra))) [
      {networking.nftables.enable = lib.mkForce false;} {security.unprivilegedUsernsClone = lib.mkForce false;}
      {security.allowUserNamespaces = false;} {boot.kernel.sysctl."user.max_user_namespaces" = 0;}
      {users.users.gaoji-search-browser.extraGroups = ["gaoji"];} {services.gaoji.group = "gaoji-search-browser";}
    ];
    browserIds = let changed = evaluateWith searchConfig {users.users.gaoji-search-browser.uid = 498; users.groups.gaoji-search-browser.gid = 498;}; in
      failures changed == [] && lib.hasInfix "meta skuid != 498 return" changed.networking.nftables.tables.gaoji-search-browser.content
      && builtins.elem "The search browser UID/GID conflicts; override users.users.gaoji-search-browser.uid and users.groups.gaoji-search-browser.gid"
        (failures (evaluateWith searchConfig {users.groups.TEST-conflict.gid = 497;}));
    browserOffByDefault = let config = evaluateWith {qqOfficial = official;} {networking.nftables.enable = lib.mkForce false;}; in
      noBrowser config && !config.security.unprivilegedUsernsClone && failures config == [];
    noInterestWithoutBot = let config = evaluateWith {runBot = false; searchBrowser.enable = true;} {networking.nftables.enable = lib.mkForce false;}; in
      !(config.systemd.services ? gaoji-interest) && !(config.systemd.timers ? gaoji-interest) && noBrowser config && failures config == [];
  };
in {
  inherit tests;
  failed = builtins.attrNames (lib.filterAttrs (_: value: !value) tests);
}
