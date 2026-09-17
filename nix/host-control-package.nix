{pkgs, lib}: pkgs.stdenvNoCC.mkDerivation {
  pname = "gaoji-host-control";
  version = "2";
  src = lib.fileset.toSource {
    root = ../src;
    fileset = lib.fileset.unions [../src/host_control.py ../src/ssh_operations.py ../src/ssh_ops_protocol.py];
  };
  dontUnpack = true;
  installPhase = ''
    mkdir -p $out/libexec
    cp $src/*.py $out/libexec/
  '';
  meta = {
    description = "Target-side command preflight and durable execution receipts";
    license = lib.licenses.mit;
    platforms = lib.platforms.unix;
  };
}
