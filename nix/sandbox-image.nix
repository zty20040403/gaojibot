{pkgs, lib}:
let
  sandboxToolchainVersion = "toolchain-v3-gaoji";
  python = pkgs.python312.withPackages (ps:
    with ps; [
      aiohttp
      beautifulsoup4
      httpx
      jinja2
      lxml
      odfpy
      openpyxl
      pillow
      pymupdf
      pypdf
      pytest
      pip
      python-docx
      python-pptx
      pyyaml
      reportlab
      requests
      setuptools
      wheel
      xlrd
    ]);

  # Keep the pinned package catalog in the image without merging the nixpkgs
  # source tree into /. Package preparation can therefore evaluate offline and
  # only needs the network to fetch missing binary closures.
  nixpkgsPin = pkgs.runCommand "gaoji-nixpkgs-pin" {} ''
    mkdir -p "$out/share"
    ln -s ${pkgs.path} "$out/share/nixpkgs"
  '';

  nixConfigPackage = pkgs.writeTextDir "etc/nix/nix.conf" ''
    sandbox = false
    build-users-group =
    experimental-features = nix-command
    warn-dirty = false
    keep-outputs = true
    keep-derivations = true
  '';

  cacheSeed = pkgs.writeShellScriptBin "gaoji-cache-seed" ''
    set -euo pipefail
    ${pkgs.nix}/bin/nix --extra-experimental-features read-only-local-store \
      copy --from 'local?read-only=true' \
      --to 'local?root=/cache' --all --no-check-sigs
    ${pkgs.coreutils}/bin/mkdir -p /cache/nix/var/nix/gcroots
    ${pkgs.coreutils}/bin/cp -a /nix/var/nix/gcroots/. /cache/nix/var/nix/gcroots/
    ${pkgs.coreutils}/bin/touch /cache/nix/.gaoji-cache-ready
  '';

  sandboxFontConfig = pkgs.makeFontsConf {
    fontDirectories = [
      pkgs.wqy_microhei
    ];
  };

  sandboxFontConfigPackage = pkgs.runCommand "gaoji-fontconfig" {} ''
    mkdir -p $out/etc/gaoji
    cp ${sandboxFontConfig} $out/etc/gaoji/fonts.conf
  '';

  cjkPdfTool = pkgs.writeTextFile {
    name = "gaoji-pdf";
    destination = "/bin/gaoji-pdf";
    executable = true;
    text = ''
      #!${pkgs.runtimeShell}
      export GAOJI_PDF_FONT=${pkgs.wqy_microhei}/share/fonts/truetype/wqy-microhei.ttc
      exec ${python}/bin/python ${../src/plugins/ai_chat/sandbox_pdf.py} "$@"
    '';
  };

  # The base is intentionally boring and useful. Large specialist stacks are
  # supplied per command through sandbox_exec.packages and cached in /nix.
  tools = with pkgs; [
    nix
    bashInteractive
    coreutils
    gnused
    gawk
    gnugrep
    findutils
    diffutils
    patch
    file
    tree
    bc
    less
    which
    procps
    psmisc
    lsof
    util-linux
    hostname
    gnutar
    gzip
    xz
    bzip2
    zstd
    zip
    unzip
    p7zip
    curl
    wget
    openssl
    rsync
    socat
    netcat-gnu
    iproute2
    iputils
    dnsutils
    openssh
    cacert
    git
    vim
    nano
    jq
    ripgrep
    gnumake
    gcc
    cmake
    ninja
    pkg-config
    shellcheck
    python
    nodejs_22
    sqlite
    poppler-utils
    qpdf
    fontconfig
    wqy_microhei
    cjkPdfTool
    cacheSeed
  ];

  baseEnvironment = pkgs.buildEnv {
    name = "gaoji-sandbox-base";
    paths = tools;
    pathsToLink = ["/bin" "/share"];
  };
in
pkgs.dockerTools.buildLayeredImageWithNixDb {
  name = "gaoji-sandbox";
  tag = "latest";
  maxLayers = 120;
  contents = [
    baseEnvironment
    sandboxFontConfigPackage
    nixConfigPackage
    nixpkgsPin
    pkgs.dockerTools.binSh
    pkgs.dockerTools.usrBinEnv
  ];

  extraCommands = ''
    mkdir -p workspace home/sandbox tmp etc nix/var/nix/gcroots/gaoji-packages
    # Nix builders cannot materialize arbitrary numeric ownership in every
    # sandbox backend. The container is isolated and runs as uid 1000, so make
    # its private workspace and home writable without a build-time chown.
    chmod 0777 workspace home/sandbox
    chmod 1777 tmp
    printf 'sandbox:x:1000:1000:gaoji sandbox:/home/sandbox:/bin/sh\n' > etc/passwd
    printf 'sandbox:x:1000:\n' > etc/group
    printf 'hosts: files dns\n' > etc/nsswitch.conf
    ln -s ${baseEnvironment} nix/var/nix/gcroots/gaoji-base
    printf '%s\n' \
      'Languages: Python 3.12 with pip, Node.js 22 with npm' \
      'Development: git, gcc, make, pkg-config, shellcheck' \
      'Documents: poppler, qpdf, Python PDF and Office libraries' \
      'CJK PDF: gaoji-pdf input.md output.pdf (embedded Chinese font)' \
      'Data: SQLite, JSON/YAML/XML parsers' \
      'On demand: pass nixpkgs attributes in sandbox_exec.packages' \
      > etc/gaoji-sandbox-tools
  '';

  config = {
    User = "1000:1000";
    WorkingDir = "/workspace";
    Cmd = ["${pkgs.coreutils}/bin/sleep" "infinity"];
    Env = [
      "PATH=${lib.makeBinPath tools}:/bin:/usr/bin"
      "HOME=/home/sandbox"
      "USER=sandbox"
      "LANG=C.UTF-8"
      "LC_ALL=C.UTF-8"
      "NIX_PATH=nixpkgs=${nixpkgsPin}/share/nixpkgs"
      "NIX_PAGER=cat"
      "FONTCONFIG_FILE=/etc/gaoji/fonts.conf"
      "PDF_CJK_FONT=${pkgs.wqy_microhei}/share/fonts/truetype/wqy-microhei.ttc"
      "PDF_CJK_BOLD_FONT=${pkgs.wqy_microhei}/share/fonts/truetype/wqy-microhei.ttc"
      "PYTHONUNBUFFERED=1"
      "MPLCONFIGDIR=/tmp/matplotlib"
      "XDG_CACHE_HOME=/tmp/cache"
      "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
    ];
    Labels = {
      "org.opencontainers.image.title" = "gaoji advanced sandbox";
      # Keep this independent from the Bot release. Bump it only when the
      # sandbox toolchain itself has a compatibility-breaking change.
      "org.opencontainers.image.version" = sandboxToolchainVersion;
      "io.gaoji.sandbox" = "advanced";
    };
  };
}
