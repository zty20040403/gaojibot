{
  description = "gaoji: reproducible agent runtime, sandbox and NixOS services";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
    };
  };

  outputs = inputs @ {
    self,
    nixpkgs,
    pyproject-nix,
    uv2nix,
    pyproject-build-systems,
    ...
  }: let
    inherit (nixpkgs) lib;
    supportedSystems = [
      "x86_64-linux"
      "aarch64-linux"
      "aarch64-darwin"
      "x86_64-darwin"
    ];
    forAllSystems = lib.genAttrs supportedSystems;
    project = builtins.fromTOML (builtins.readFile ./pyproject.toml);
    workspace = uv2nix.lib.workspace.loadWorkspace {workspaceRoot = ./.;};
    workspaceOverlay = workspace.mkPyprojectOverlay {
      sourcePreference = "wheel";
    };
    applicationSource = lib.fileset.toSource {
      root = ./.;
      fileset = lib.fileset.unions [
        ./bot.py
        ./alembic.ini
        ./pyproject.toml
        ./requirements.txt
        ./uv.lock
        ./README.md
        ./LICENSE
        ./THIRD_PARTY_NOTICES.md
        ./nix/napcat-auth.py
        ./nix/napcat-health.py
        ./nix/napcat-password-login.py
        (lib.fileset.fileFilter (file: file.hasExt "md" || file.hasExt "html") ./docs)
        (lib.fileset.fileFilter (file: file.hasExt "md") ./skills)
        (lib.fileset.fileFilter (
            file:
              file.hasExt "py"
              || file.hasExt "mako"
              || file.hasExt "md"
          )
          ./migrations)
        (lib.fileset.fileFilter (file: file.hasExt "py") ./tests)
        ./tests/fixtures/context_accuracy_cases.json
        ./tests/fixtures/subagent_entry_cases.json
        (lib.fileset.fileFilter (file: file.hasExt "js") ./tools)
        ./tools/context_eval.py
        ./tools/evaluate_subagent_entry.py
        ./tools/live_file_outbox_acceptance.py
        ./tools/qwen_control.py
        (lib.fileset.fileFilter (
            file:
              file.hasExt "py"
              || file.hasExt "swift"
              || file.hasExt "png"
              || file.hasExt "svg"
          )
          ./src)
      ];
    };

    mkPythonSet = system: let
      pkgs = nixpkgs.legacyPackages.${system};
      python = pkgs.python312;
      baseSet = pkgs.callPackage pyproject-nix.build.packages {inherit python;};
    in
      baseSet.overrideScope (
        lib.composeManyExtensions [
          pyproject-build-systems.overlays.wheel
          workspaceOverlay
        ]
      );

    mkPackage = system: let
      pkgs = nixpkgs.legacyPackages.${system};
      pythonSet = mkPythonSet system;
      virtualenv = pythonSet.mkVirtualEnv "gaoji-env" workspace.deps.default;
      adminUi = pkgs.buildNpmPackage {
        pname = "gaoji-admin-ui";
        version = project.project.version;
        src = ./admin-ui;
        npmDepsHash = "sha256-/Qs+7QYbQ7uHibGud6dCk/3vY7+J8ZW5etfUMq2sVZA=";
        npmBuildScript = "build";
        installPhase = ''
          runHook preInstall
          mkdir -p "$out/dist"
          cp -R dist/. "$out/dist/"
          runHook postInstall
        '';
      };
    in
      pkgs.stdenvNoCC.mkDerivation {
        pname = project.project.name;
        version = project.project.version;
        src = applicationSource;
        dontBuild = true;
        nativeBuildInputs = [pkgs.makeWrapper];

        installPhase = ''
          runHook preInstall

          mkdir -p "$out/bin" "$out/share/gaoji"
          cp -R . "$out/share/gaoji"
          mkdir -p "$out/share/gaoji/src/plugins/ai_chat/admin_ui_dist"
          cp -R ${adminUi}/dist/. \
            "$out/share/gaoji/src/plugins/ai_chat/admin_ui_dist/"
          makeWrapper ${virtualenv}/bin/python "$out/bin/gaoji" \
            --add-flags "$out/share/gaoji/bot.py" \
            --chdir "$out/share/gaoji" \
            --set PYTHONDONTWRITEBYTECODE 1 \
            --set PYTHONUNBUFFERED 1 \
            --run 'state_home="''${XDG_STATE_HOME:-''${HOME:-/tmp}/.local/state}"' \
            --run 'cache_home="''${XDG_CACHE_HOME:-''${HOME:-/tmp}/.cache}"' \
            --run 'export AI_STATE_DIR="''${AI_STATE_DIR:-$state_home/gaoji}"' \
            --run 'export AI_CACHE_DIR="''${AI_CACHE_DIR:-$cache_home/gaoji}"' \
            --run '${pkgs.coreutils}/bin/mkdir -p "$AI_STATE_DIR" "$AI_CACHE_DIR"'
          makeWrapper ${virtualenv}/bin/python "$out/bin/gaoji-db" \
            --add-flags "-m src.bot_storage.cli" \
            --chdir "$out/share/gaoji" \
            --set PYTHONDONTWRITEBYTECODE 1 \
            --set PYTHONUNBUFFERED 1
          makeWrapper ${virtualenv}/bin/python "$out/bin/gaoji-admin" \
            --add-flags "-m src.bot_security.cli" \
            --chdir "$out/share/gaoji" \
            --set PYTHONDONTWRITEBYTECODE 1 \
            --set PYTHONUNBUFFERED 1
          makeWrapper ${virtualenv}/bin/python "$out/bin/gaoji-cluster-control" \
            --add-flags "-m src.cluster_control" \
            --chdir "$out/share/gaoji" \
            --set PYTHONDONTWRITEBYTECODE 1 \
            --set PYTHONUNBUFFERED 1
          makeWrapper ${virtualenv}/bin/python "$out/bin/gaoji-cluster-worker" \
            --add-flags "-m src.cluster_worker" \
            --chdir "$out/share/gaoji" \
            --set PYTHONDONTWRITEBYTECODE 1 \
            --set PYTHONUNBUFFERED 1
          makeWrapper ${virtualenv}/bin/python "$out/bin/gaoji-cluster-deployer" \
            --add-flags "-m src.cluster_deployer" \
            --chdir "$out/share/gaoji" \
            --set PYTHONDONTWRITEBYTECODE 1 \
            --set PYTHONUNBUFFERED 1

          runHook postInstall
        '';

        passthru = {inherit virtualenv;};
        meta = {
          description = project.project.description;
          homepage = "https://github.com/zty20040403/gaojibot";
          license = lib.licenses.mit;
          mainProgram = "gaoji";
          platforms = supportedSystems;
        };
      };
    mkSandboxImage = system: let
      pkgs = nixpkgs.legacyPackages.${system};
    in
      import ./nix/sandbox-image.nix {
        inherit pkgs lib;
      };
  in {
    packages = forAllSystems (system: let
      pkgs = nixpkgs.legacyPackages.${system};
    in
      {
        default = mkPackage system;
        gaoji = mkPackage system;
        cluster-worker = import ./nix/cluster-worker-package.nix {inherit pkgs lib;};
        host-control = import ./nix/host-control-package.nix {inherit pkgs lib;};
      }
      // lib.optionalAttrs pkgs.stdenv.isLinux {
        sandbox-image = mkSandboxImage system;
      });

    apps = forAllSystems (system: {
      default = {
        type = "app";
        program = lib.getExe self.packages.${system}.default;
      };
      cluster-control = {
        type = "app";
        program = "${self.packages.${system}.default}/bin/gaoji-cluster-control";
      };
      cluster-worker = {
        type = "app";
        program = "${self.packages.${system}.cluster-worker}/bin/gaoji-cluster-worker";
      };
      cluster-deployer = {
        type = "app";
        program = "${self.packages.${system}.default}/bin/gaoji-cluster-deployer";
      };
    });

    checks = forAllSystems (system: let
      pkgs = nixpkgs.legacyPackages.${system};
      package = self.packages.${system}.default;
      virtualenv = package.passthru.virtualenv;
    in {
      inherit package;
      imports = pkgs.runCommand "gaoji-import-check" {} ''
        cd ${package}/share/gaoji
        ${virtualenv}/bin/python -c 'import alembic, edge_tts, httpx, miniaudio, nonebot, openai, opentelemetry.sdk, playwright, prometheus_client, psycopg, psycopg_pool, pygments, pysilk, sqlalchemy'
        ${virtualenv}/bin/python -c 'import ast, pathlib; [ast.parse(path.read_text(encoding="utf-8"), filename=str(path)) for path in pathlib.Path("src").rglob("*.py")]'
        touch "$out"
      '';
    });

    devShells = forAllSystems (system: let
      pkgs = nixpkgs.legacyPackages.${system};
      pythonSet = mkPythonSet system;
      virtualenv = pythonSet.mkVirtualEnv "gaoji-dev-env" workspace.deps.default;
    in {
      default = pkgs.mkShell {
        packages = [virtualenv pkgs.uv pkgs.nodejs_22];
        env = {
          UV_NO_SYNC = "1";
          UV_PYTHON = pythonSet.python.interpreter;
          UV_PYTHON_DOWNLOADS = "never";
        };
      };
    });

    nixosModules = {
      default = import ./nix/module.nix {inherit self;};
      gaoji = self.nixosModules.default;
      qwen-control = import ./nix/qwen-control.nix;
      cluster-control = import ./nix/cluster-control.nix {inherit self;};
      host-control = import ./nix/host-control.nix {inherit self;};
      cluster-worker = import ./nix/cluster-worker.nix {inherit self;};
      cluster-deployer = import ./nix/cluster-deployer.nix {inherit self;};
    };
  };
}
