{ tag }:
assert builtins.match "[a-f0-9]{64}" tag != null;
let
  source = builtins.fetchTarball {
    url = "https://github.com/NixOS/nixpkgs/archive/8b8c811c7c2541c30382c5de7ed26be055569c60.tar.gz";
    sha256 = "sha256-CiOTEjmwAmG2AWnaIno9YaCJJmpca2FXPhMAsnrolCg=";
  };
  pkgs = import source {
    system = "x86_64-linux";
    overlays = [(final: prev: {
      python312 = prev.python312.override {
        packageOverrides = pyFinal: pyPrev: {
          pymupdf = pyPrev.pymupdf.overridePythonAttrs (old: {
            # Upstream test_4435 deliberately renders an oversized image and
            # exceeds the App builder's 4 GiB limit. Keep all other checks.
            disabledTestPaths = (old.disabledTestPaths or []) ++ [
              "tests/test_pixmap.py::test_4435"
            ];
          });
        };
      };
    })];
  };
in import ./image.nix {
  inherit pkgs;
  inherit (pkgs) lib;
  imageName = "gaoji-app-sandbox";
  imageTag = tag;
}
