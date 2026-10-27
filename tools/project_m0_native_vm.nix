# Test-only NixOS guest; not imported by any product flake or server module.
{ nixpkgs }:
let
  pkgs = import nixpkgs { system = "aarch64-linux"; };
  evaluated = import (nixpkgs + "/nixos/lib/eval-config.nix") {
    system = "aarch64-linux";
    modules = [ ({ modulesPath, ... }: {
      imports = [ (modulesPath + "/profiles/qemu-guest.nix") ];
      system.stateVersion = "26.05";
      networking.hostName = "gaoji-m0-nixos";
      networking.useDHCP = false;
      services.openssh.enable = false;
      services.getty.autologinUser = "root";
      boot.loader.grub.enable = false;
      boot.kernelParams = [ "console=ttyAMA0" ];
      boot.initrd.availableKernelModules = [ "virtio_pci" "virtio_blk" "erofs" ];
      boot.kernelModules = [ "loop" ];
      fileSystems."/" = { device = "/dev/disk/by-label/m0-root"; fsType = "ext4"; };
      fileSystems."/nix/store" = {
        device = "/dev/disk/by-label/m0-store";
        fsType = "erofs";
        neededForBoot = true;
        options = [ "ro" ];
      };
      environment.etc."gaoji-m0-disposable".text = "gaoji-m0\n";
      environment.systemPackages = with pkgs; [ python3 e2fsprogs util-linux ];
      virtualisation.docker.enable = true;
      virtualisation.docker.daemon.settings = { iptables = false; bridge = "none"; };
      documentation.enable = false;
      documentation.nixos.enable = false;
    }) ];
  };
  system = evaluated.config.system.build.toplevel;
  closure = pkgs.closureInfo { rootPaths = [ system ]; };
in pkgs.runCommand "gaoji-m0-native-nixos" {
  nativeBuildInputs = with pkgs; [ gnutar erofs-utils e2fsprogs qemu-utils ];
} ''
  mkdir -p $out
  tar --create --absolute-names --verbatim-files-from \
    --transform 'flags=rSh;s|/nix/store/||' --files-from ${closure}/store-paths \
    | mkfs.erofs --quiet --force-uid=0 --force-gid=0 -L m0-store \
      --hard-dereference --tar=f $out/store.img
  truncate -s 4G root.raw
  mkfs.ext4 -q -F -L m0-root root.raw
  qemu-img convert -f raw -O qcow2 root.raw $out/root.qcow2
  cp -L ${system}/kernel $out/kernel
  cp -L ${system}/initrd $out/initrd
  printf '%s init=${system}/init console=ttyAMA0' "$(cat ${system}/kernel-params)" > $out/kernel-params
''
