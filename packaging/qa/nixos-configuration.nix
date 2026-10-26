{ modulesPath, ... }:
let
  identity = builtins.fromJSON (builtins.readFile ./identity.json);
in {
  imports = [ (modulesPath + "/profiles/qemu-guest.nix") ];
  boot.loader.grub.device = "/dev/disk/by-id/virtio-gaoji-qa-disk";
  boot.kernelParams = [ "console=ttyS0,115200n8" ];
  fileSystems."/" = {
    device = "/dev/disk/by-label/gaoji-qa-root";
    fsType = "ext4";
  };
  networking.hostName = "gaoji-local-qa";
  networking.useDHCP = true;
  services.openssh = {
    enable = true;
    settings.PasswordAuthentication = false;
    settings.KbdInteractiveAuthentication = false;
    settings.PermitRootLogin = "no";
    hostKeys = [{ path = "/etc/ssh/ssh_host_ed25519_key"; type = "ed25519"; }];
  };
  users.users.qa = {
    isNormalUser = true;
    extraGroups = [ "wheel" ];
    openssh.authorizedKeys.keys = [ identity.publicKey ];
  };
  security.sudo.extraRules = [{
    users = [ "qa" ];
    commands = [{ command = "ALL"; options = [ "NOPASSWD" ]; }];
  }];
  environment.etc."gaoji-local-qa".text = identity.id + "\n";
  nix.settings = { max-jobs = 1; cores = 2; builders = ""; };
  system.stateVersion = "26.05";
}
