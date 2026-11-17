# The NixOS container system's tools and immutable gaoji scripts.
{
  pkgs,
  pi ? null,
  source ? ../..,
}: let
  inherit (pkgs) lib;
  # courlan's own test suite fails in the build sandbox on this nixpkgs pin.
  python3 = pkgs.python3.override {
    packageOverrides = _: super: {
      courlan = super.courlan.overridePythonAttrs (_: {doCheck = false;});
    };
  };
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
    ]);
  scripts = [
    "sandbox_browser.py"
    "sandbox_browser_actions.py"
    "sandbox_browser_state.py"
    "sandbox_webread.py"
    "sandbox_transcribe.py"
    "sandbox_video.py"
    "sandbox_pdf.py"
    "sandbox_gaoji.py"
    "sandbox_commands.py"
    "sandbox_command_process.py"
    "sandbox_identity.py"
  ];
  share = pkgs.runCommand "gaoji-sandbox-tools" {} ''
    mkdir -p "$out/share/gaoji"
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
    GAOJI_SHARE = "${share}/share/gaoji";
    GAOJI_COMMAND_PATH = "/run/current-system/sw/bin:/usr/local/bin:/usr/bin:/bin";
    PLAYWRIGHT_BROWSERS_PATH = "${pkgs.playwright-driver.browsers}";
    FONTCONFIG_FILE = "${fontConfig}";
    GAOJI_PDF_FONT = "${pkgs.wqy_microhei}/share/fonts/truetype/wqy-microhei.ttc";
    SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
    PYTHONUNBUFFERED = "1";
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
in {
  inherit python packages commands share variables fontConfig;
  # The guest receives exactly these immutable files, never a host directory.
  piFiles = pkgs.runCommand "gaoji-pi-files" {} ''
    mkdir -p "$out"
    cp ${source}/src/plugins/ai_chat/sandbox_pi_tools.ts "$out/sandbox_pi_tools.ts"
    cp ${source}/src/plugins/ai_chat/sandbox_agent_bridge.py "$out/sandbox_agent_bridge.py"
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
