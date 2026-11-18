# The NixOS system every conversation container boots (systemd-nspawn).
# Stateless: the root is recreated on every start; only /workspace (also
# /work) is the conversation's own, bind-mounted from the host. Tools come
# from the host's read-only /nix/store, so a deploy updates every container.
{
  lib,
  pkgs,
  source,
  pi,
  ...
}: let
  tools = import ./tools.nix {inherit pkgs pi source;};
in {
  boot.isContainer = true;
  system.stateVersion = "26.05";
  networking.hostName = "gaoji-sandbox";

  # host0 is the veth into the host's isolated bridge; ops containers share
  # the operations namespace instead and have no host0.
  networking.useDHCP = false;
  networking.useNetworkd = true;
  systemd.network.config.networkConfig = {
    ManageForeignRoutes = false;
    ManageForeignRoutingPolicyRules = false;
  };
  systemd.network.networks."10-host0" = {
    matchConfig.Name = "host0";
    networkConfig.DHCP = "ipv4";
    dhcpV4Config.UseDNS = false;
  };
  # The host binds /etc/resolv.conf in: public resolvers, or MagicDNS on the
  # operations network.
  networking.resolvconf.enable = false;
  services.resolved.enable = false;
  networking.firewall.enable = false;

  users.groups.sandbox.gid = 1000;
  users.users.sandbox = {
    isNormalUser = true;
    uid = 1000;
    group = "sandbox";
    home = "/workspace";
    createHome = false;
  };
  security.sudo.enable = false;
  services.openssh.enable = false;
  # On the operations network the host writes the fleet's ssh_config here.
  # Copy ssh_config as container uid 0 and disable systemd-ssh-proxy's /nix/store
  # include: under --private-users=pick, /nix/store files map to nobody (65534),
  # which OpenSSH rejects on Include targets.
  programs.ssh.systemd-ssh-proxy.enable = false;
  programs.ssh.extraConfig = "Include /etc/ssh/ssh_config.d/*.conf";
  environment.etc."ssh/ssh_config".mode = "0644";

  # The kernel's boot id is the host's; VM credentials need one per boot.
  systemd.services.gaoji-boot-id = {
    wantedBy = ["multi-user.target"];
    serviceConfig.Type = "oneshot";
    script = "umask 022; cat /proc/sys/kernel/random/uuid > /run/gaoji-boot-id";
  };

  # Resident headless Chromium shared across commands and jobs over CDP (9222).
  # Running in its own service keeps browser tabs alive after transient
  # systemd-run command units finish.
  systemd.services.gaoji-browser = {
    restartIfChanged = false;
    unitConfig.RequiresMountsFor = "/work";
    environment.TZ = "Asia/Shanghai";
    serviceConfig = {
      ExecStartPre = [
        "${pkgs.coreutils}/bin/mkdir -p /work/.gaoji/chromium"
        "${pkgs.coreutils}/bin/rm -f /work/.gaoji/chromium/SingletonLock /work/.gaoji/chromium/SingletonSocket /work/.gaoji/chromium/SingletonCookie"
      ];
      ExecStart = lib.escapeShellArgs [
        "${pkgs.chromium}/bin/chromium"
        "--headless=new"
        "--no-sandbox"
        "--disable-dev-shm-usage"
        "--remote-debugging-address=127.0.0.1"
        "--remote-debugging-port=9222"
        "--user-data-dir=/work/.gaoji/chromium"
        "--no-first-run"
        "--no-default-browser-check"
        "--lang=zh-CN"
        "--accept-lang=zh-CN,zh"
        "--window-size=1280,900"
        "about:blank"
      ];
      ExecStartPost = pkgs.writeShellScript "gaoji-browser-ready" ''
        for _ in $(seq 200); do
          ${pkgs.curl}/bin/curl -sf http://127.0.0.1:9222/json/version >/dev/null && exit 0
          sleep 0.1
        done
        exit 1
      '';
      SuccessExitStatus = 143;
      Restart = "on-failure";
      MemoryMax = "2G";
    };
  };

  systemd.settings.Manager.DefaultEnvironment = "PATH=/run/wrappers/bin:/run/current-system/sw/bin HOME=/work";
  users.users.root.home = lib.mkForce "/work";
  systemd.tmpfiles.rules =
    map
    (name: "L+ ${name} - - - - /run/current-system/sw/bin/${baseNameOf name}")
    tools.compatPaths;
  environment.systemPackages = tools.packages;
  environment.etc."gaoji/skills".source = source + "/skills";
  environment.etc."gaoji/pi".source = tools.piFiles;
  environment.variables = tools.variables // {HOME = "/workspace";};
  programs.nix-ld.enable = true;
  programs.nix-ld.libraries = tools.ldLibraries;
  nix.settings.experimental-features = ["nix-command" "flakes"];
  documentation.enable = false;
  time.timeZone = "Asia/Shanghai";
  i18n.defaultLocale = "C.UTF-8";
}
