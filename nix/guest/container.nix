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
  programs.ssh.extraConfig = "Include /etc/ssh/ssh_config.d/*.conf";

  # The kernel's boot id is the host's; VM credentials need one per boot.
  systemd.services.gaoji-boot-id = {
    wantedBy = ["multi-user.target"];
    serviceConfig.Type = "oneshot";
    script = "umask 022; cat /proc/sys/kernel/random/uuid > /run/gaoji-boot-id";
  };
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
