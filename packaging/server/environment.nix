let
  source = builtins.fetchTarball {
    url = "https://github.com/NixOS/nixpkgs/archive/8b8c811c7c2541c30382c5de7ed26be055569c60.tar.gz";
    sha256 = "sha256-CiOTEjmwAmG2AWnaIno9YaCJJmpca2FXPhMAsnrolCg=";
  };
  pkgs = import source { system = "x86_64-linux"; };
in pkgs.buildEnv {
  name = "gaoji-app-tools";
  paths = with pkgs; [ python312 docker docker-compose iptables iproute2 kmod coreutils util-linux ];
  pathsToLink = [ "/bin" "/libexec" ];
}
