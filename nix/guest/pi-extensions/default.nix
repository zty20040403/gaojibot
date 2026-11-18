{
  lib,
  buildNpmPackage,
  importNpmLock,
}:
buildNpmPackage {
  pname = "gaoji-pi-extensions";
  version = "1.0.0";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./package.json
      ./package-lock.json
    ];
  };

  npmDeps = importNpmLock {
    npmRoot = ./.;
  };
  npmConfigHook = importNpmLock.npmConfigHook;
  npmFlags = ["--legacy-peer-deps" "--ignore-scripts"];
  dontNpmBuild = true;

  installPhase = ''
    runHook preInstall
    mkdir -p "$out"
    rm -f node_modules/pi-web-access/pi-web-fetch-demo.mp4 node_modules/pi-web-access/banner.png
    cp -r node_modules package.json "$out/"
    runHook postInstall
  '';

  meta = {
    description = "Pinned Pi extensions (rpiv-todo and pi-web-access) for gaoji containers";
    license = lib.licenses.mit;
    platforms = lib.platforms.unix;
  };
}
