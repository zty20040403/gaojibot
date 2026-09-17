{self}: {config, lib, pkgs, ...}: let
  cfg = config.services.gaoji-host-control;
  settings = (pkgs.formats.json {}).generate "gaoji-host-control.json" {
    host_id = cfg.hostId;
    shell = "${pkgs.bash}/bin/bash";
    systemctl = "${pkgs.systemd}/bin/systemctl";
    path = "/run/current-system/sw/bin:/run/wrappers/bin";
    default_cwd = "/";
    boot_id_file = "/proc/sys/kernel/random/boot_id";
    receipt_directory = "/var/lib/gaoji-host-control";
    job_state_root = "/var/lib/maxops-jobs";
    cgroup_file = "/proc/self/cgroup";
  };
  entry = pkgs.writeShellScriptBin "gaoji-host-control" ''
    if [ "$#" -ne 1 ]; then
      echo 'Expected one JSON request argument' >&2
      exit 64
    fi
    exec ${pkgs.python3}/bin/python3 -I ${cfg.package}/libexec/host_control.py \
      --request-json "$1" --config ${settings}
  '';
  sshSettings = (pkgs.formats.json {}).generate "gaoji-ssh-operations.json" {
    host_id = cfg.hostId;
    shell = "${pkgs.bash}/bin/bash";
    systemctl = "${pkgs.systemd}/bin/systemctl";
    systemd_run = "${pkgs.systemd}/bin/systemd-run";
    journalctl = "${pkgs.systemd}/bin/journalctl";
    path = "/run/current-system/sw/bin:/run/wrappers/bin";
    boot_id_file = "/proc/sys/kernel/random/boot_id";
    jobs_directory = "/var/lib/gaoji-ssh-operations";
    entry = "/run/current-system/sw/bin/gaoji-ssh-operations";
    mounts = cfg.ssh.mounts;
  };
  sshEntry = pkgs.writeShellScriptBin "gaoji-ssh-operations" ''
    exec ${pkgs.python3}/bin/python3 -I -c \
      'import sys; sys.path.insert(0, "${cfg.package}/libexec"); from ssh_operations import main; raise SystemExit(main())' \
      --config ${sshSettings} "$@"
  '';
in {
  options.services.gaoji-host-control = {
    enable = lib.mkEnableOption "target-side checks for authorized gaoji host operations";
    hostId = lib.mkOption {
      type = lib.types.strMatching "[A-Za-z0-9][A-Za-z0-9_-]{0,63}";
      default = config.networking.hostName;
      description = "Exact host identity shared with the Ops catalog.";
    };
    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.host-control;
    };
    ssh = {
      enable = lib.mkEnableOption "dedicated SSH operations endpoint with durable systemd jobs";
      user = lib.mkOption {type = lib.types.strMatching "[a-z_][a-z0-9_-]*"; default = "gaoji-operator";};
      authorizedKeys = lib.mkOption {type = lib.types.listOf lib.types.str; default = [];};
      mounts = lib.mkOption {type = lib.types.listOf lib.types.str; default = ["/"];};
    };
  };
  config = lib.mkIf cfg.enable {
    environment.systemPackages = [entry] ++ lib.optional cfg.ssh.enable sshEntry;
    systemd.tmpfiles.rules = ["d /var/lib/gaoji-host-control 0700 root root -"]
      ++ lib.optional cfg.ssh.enable "d /var/lib/gaoji-ssh-operations 0700 root root -";
    users.groups = lib.mkIf cfg.ssh.enable {${cfg.ssh.user} = {};};
    users.users = lib.mkIf cfg.ssh.enable {
      ${cfg.ssh.user} = {
        isSystemUser = true;
        group = cfg.ssh.user;
        shell = pkgs.bash;
        openssh.authorizedKeys.keys = map (key:
          ''restrict,command="/run/wrappers/bin/sudo -n -- /run/current-system/sw/bin/gaoji-ssh-operations" ${key}''
        ) cfg.ssh.authorizedKeys;
      };
    };
    security.sudo.extraRules = lib.optional cfg.ssh.enable {
      users = [cfg.ssh.user];
      commands = [{command = ''/run/current-system/sw/bin/gaoji-ssh-operations ""''; options = ["NOPASSWD"]; }];
    };
  };
}
