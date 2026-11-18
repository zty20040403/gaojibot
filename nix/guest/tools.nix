# The NixOS container system's tools and immutable gaoji scripts.
{
  pkgs,
  pi ? null,
  source ? ../..,
  appSource ? source,
  gaojiCommit ? "unknown",
}: let
  inherit (pkgs) lib;
  # courlan's own test suite fails in the build sandbox on this nixpkgs pin.
  python3 = pkgs.python3.override {
    packageOverrides = _: super: {
      courlan = super.courlan.overridePythonAttrs (_: {doCheck = false;});
    };
  };
  # The store is read-only and pip wheels clash with Nix's numpy, so the
  # libraries people routinely ask for are preinstalled here.
  python = python3.withPackages (ps:
    with ps; [
      aiohttp
      av
      beautifulsoup4
      faster-whisper
      httpx
      jinja2
      lxml
      odfpy
      openpyxl
      pillow
      pip
      playwright
      pymupdf
      pypdf
      pytest
      python-docx
      python-pptx
      pyyaml
      reportlab
      requests
      setuptools
      trafilatura
      wheel
      xlrd
      # Data, plotting and science.
      imageio
      matplotlib
      networkx
      numpy
      opencv4
      pandas
      plotly
      pyarrow
      scikit-image
      scikit-learn
      scipy
      seaborn
      statsmodels
      sympy
      # Everyday helpers.
      chardet
      markdown
      pydantic
      python-dateutil
      qrcode
      regex
      rich
      tabulate
      tqdm
    ]);
  # Headless plotting with Chinese labels and a correct minus sign by default.
  matplotlibrc = pkgs.writeText "matplotlibrc" ''
    backend: Agg
    font.family: sans-serif
    font.sans-serif: WenQuanYi Micro Hei, DejaVu Sans
    axes.unicode_minus: False
  '';
  scripts = [
    "sandbox_browser.py"
    "sandbox_search.py"
    "sandbox_browser_actions.py"
    "sandbox_browser_state.py"
    "sandbox_webread.py"
    "sandbox_transcribe.py"
    "sandbox_video.py"
    "sandbox_pdf.py"
    "sandbox_gaoji.py"
    "sandbox_gaoji_units.py"
    "sandbox_identity.py"
  ];
  playwrightCore = pkgs.fetchurl {
    url = "https://registry.npmjs.org/playwright-core/-/playwright-core-1.59.0.tgz";
    hash = "sha512-PW/X/IoZ6BMUUy8rpwHEZ8Kc0IiLIkgKYGNFaMs5KmQhcfLILNx9yCQD0rnWeWfz1PNeqcFP1BsihQhDOBCwZw==";
  };
  share = pkgs.runCommand "gaoji-sandbox-tools" {} ''
    mkdir -p "$out/share/gaoji/runner/node_modules/playwright-core"
    tar -xzf ${playwrightCore} --strip-components=1 \
      -C "$out/share/gaoji/runner/node_modules/playwright-core"
    cp ${source}/src/plugins/ai_chat/sandbox_runner.js "$out/share/gaoji/runner/runner.js"
    ${lib.concatMapStringsSep "\n" (name: ''
        cp ${source}/src/plugins/ai_chat/${name} "$out/share/gaoji/${
          if name == "sandbox_browser.py"
          then "gaoji_browser.py"
          else name
        }"
      '')
      scripts}
  '';
  fontConfig = pkgs.makeFontsConf {fontDirectories = [pkgs.wqy_microhei];};
  variables = {
    GAOJI_COMMIT = gaojiCommit;
    GAOJI_LIVE_SOURCE = "/run/gaoji/source";
    GAOJI_SHARE = "${share}/share/gaoji";
    GAOJI_COMMAND_PATH = "/run/current-system/sw/bin:/usr/local/bin:/usr/bin:/bin";
    PLAYWRIGHT_BROWSERS_PATH = "${pkgs.playwright-driver.browsers}";
    FONTCONFIG_FILE = "${fontConfig}";
    GAOJI_PDF_FONT = "${pkgs.wqy_microhei}/share/fonts/truetype/wqy-microhei.ttc";
    SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
    PYTHONUNBUFFERED = "1";
    MATPLOTLIBRC = "${matplotlibrc}";
    MPLBACKEND = "Agg";
  };
  environment = lib.concatStringsSep "\n" (lib.mapAttrsToList
    (name: value: "export ${name}=${lib.escapeShellArg value}")
    variables);
  wrapper = name: command:
    pkgs.writeShellScriptBin name ''
      ${environment}
      export PYTHONPATH="${share}/share/gaoji''${PYTHONPATH:+:$PYTHONPATH}"
      exec ${python}/bin/python ${command} "$@"
    '';
  commands = [
    (wrapper "gaoji" "${share}/share/gaoji/sandbox_gaoji.py")
    (wrapper "gaoji-python" "")
    (wrapper "gaoji-video" "${share}/share/gaoji/sandbox_video.py")
    (wrapper "gaoji-transcribe" "${share}/share/gaoji/sandbox_transcribe.py")
    (wrapper "gaoji-pdf" "${share}/share/gaoji/sandbox_pdf.py")
    (wrapper "webread" "${share}/share/gaoji/sandbox_webread.py")
  ];
  packages =
    (with pkgs; [
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
      fd
      gnumake
      gcc
      cmake
      ninja
      pkg-config
      shellcheck
      nodejs_22
      sqlite
      poppler-utils
      chromium
      ffmpeg-headless
      qpdf
      fontconfig
      wqy_microhei
      yt-dlp
      uv
      e2fsprogs
    ])
    ++ [python share] ++ commands ++ lib.optional (pi != null) pi;
  piExtensions = pkgs.callPackage ./pi-extensions {};
in {
  inherit python packages commands share variables fontConfig piExtensions;
  # The guest receives exactly these immutable files, never a host directory.
  sourceTree = pkgs.runCommand "gaoji-live-source" {} ''
    mkdir -p "$out"
    cp -R ${appSource}/. "$out/"
    chmod -R u+w "$out"
    printf '%s\n' ${lib.escapeShellArg gaojiCommit} > "$out/.gaoji-commit"
  '';
  piFiles = pkgs.runCommand "gaoji-pi-files" {} ''
    mkdir -p "$out"
    cp ${source}/src/plugins/ai_chat/sandbox_pi_tools.ts "$out/sandbox_pi_tools.ts"
    cp ${source}/src/plugins/ai_chat/sandbox_pi_agent.ts "$out/sandbox_pi_agent.ts"
    ln -s ${piExtensions}/node_modules "$out/node_modules"
  '';
  compatPaths = [
    "/bin/sh"
    "/bin/rm"
    "/usr/bin/rm"
    "/usr/bin/python3"
    "/usr/bin/stat"
    "/usr/bin/sync"
    "/usr/bin/touch"
    "/usr/bin/find"
    "/usr/bin/timeout"
    "/usr/bin/prlimit"
    "/usr/sbin/runuser"
  ];
  ldLibraries = with pkgs; [
    stdenv.cc.cc.lib
    zlib
    openssl
    glib
    nss
    nspr
    dbus
    expat
    libdrm
    libxkbcommon
    mesa
    alsa-lib
    cups
    at-spi2-atk
    at-spi2-core
    cairo
    pango
    libx11
    libxcomposite
    libxdamage
    libxext
    libxfixes
    libxrandr
    libxcb
  ];
}
