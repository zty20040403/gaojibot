{self, pkgs}: pkgs.testers.runNixOSTest {
  name = "gaoji-public-search-browser";
  nodes.machine = {lib, ...}: {
    imports = [self.nixosModules.default];
    virtualisation.memorySize = 3072;
    environment.systemPackages = [pkgs.curl pkgs.util-linux pkgs.nftables];
    networking.nftables.enable = true;
    security.unprivilegedUsernsClone = true;
    boot.kernelParams = ["lsm=landlock,lockdown,yama,bpf"];
    services.gaoji = {enable = true; package = pkgs.hello; searchBrowser.enable = true;
      qqOfficial = {enable = true; appIdFile = "/run/TEST-app-id"; appSecretFile = "/run/TEST-secret"; bindPepperFile = "/run/TEST-pepper";};};
    systemd.services.gaoji.wantedBy = lib.mkForce [];
    systemd.timers.gaoji-interest.wantedBy = lib.mkForce [];
  };
  testScript = ''
    machine.start()
    unit = "gaoji-search-browser.service"
    machine.wait_for_unit(unit)
    sock = "/run/gaoji-search-browser/search.sock"
    request = f"curl -sS --max-time 15 --unix-socket {sock} --data-binary @/dev/null -o /dev/null -w '%{{http_code}}' http://localhost/search"
    machine.wait_until_succeeds(f"test $(runuser -u gaoji -- {request}) = 400", timeout=90)
    machine.fail(f"runuser -u nobody -- {request}")
    machine.succeed(f"test $(stat -c '%G:%a' {sock}) = gaoji:660")
    pid = machine.succeed(f"systemctl show -p MainPID --value {unit}").strip()
    machine.succeed("touch /srv/TEST-other-workspace /run/TEST-secret")
    machine.succeed(f"test ! -e /proc/{pid}/root/srv/TEST-other-workspace && test ! -e /proc/{pid}/root/run/TEST-secret")
    machine.succeed(f"test $(stat -c %u /proc/{pid}) = $(id -u gaoji-search-browser)")
    machine.succeed("nft list table inet gaoji-search-browser | grep -F 'fib daddr type local drop'")
    machine.succeed(f"test $(systemctl show -p NRestarts --value {unit}) = 0")
    machine.fail("systemctl is-active --quiet gaoji.service")
  '';
}
