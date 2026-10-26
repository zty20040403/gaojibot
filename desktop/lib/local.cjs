'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { spawn } = require('node:child_process');
const { randomUUID, randomBytes } = require('node:crypto');
const { EventEmitter } = require('node:events');
const deploy = require('./deploy.cjs');
const control = require('./control.cjs');
const imagePolicy = require('./image-policy.cjs');
const { UserError } = require('./contracts.cjs');

const LOCAL_ID = 'desktop-local';
const ID = /^[a-f0-9]{32}$/;
const HASH = /^[a-f0-9]{64}$/;
function endpoint(platform = process.platform, home = os.homedir()) {
  if (platform === 'darwin') return { binary: '/Applications/Docker.app/Contents/Resources/bin/docker',
    host: 'unix://' + path.join(home, '.docker/run/docker.sock') };
  if (platform === 'win32') return { binary: path.win32.join(process.env.ProgramFiles || 'C:\\Program Files', 'Docker/Docker/resources/bin/docker.exe'),
    host: 'npipe:////./pipe/dockerDesktopLinuxEngine' };
  throw new UserError('local_platform', '本机模式支持 macOS 和 Windows；Linux 请使用服务器部署');
}

function run(binary, args, { input, timeout = 30000, onOutput } = {}) {
  return new Promise((resolve, reject) => {
    const env = { ...process.env };
    for (const key of Object.keys(env)) if (key.startsWith('DOCKER_') || key.startsWith('COMPOSE_')) delete env[key];
    env.PATH = path.dirname(binary) + path.delimiter + (env.PATH || '');
    const child = spawn(binary, args, { env, shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    const chunks = []; let size = 0; let expired = false; let diagnostic = '';
    const timer = setTimeout(() => { expired = true; child.kill(); }, timeout);
    child.stdout.on('data', data => { size += data.length; if (size <= 2 * 1024 ** 2) chunks.push(data); onOutput?.(data.toString('utf8')); });
    child.stderr.on('data', data => { diagnostic = (diagnostic + data.toString('utf8')).slice(-32768); onOutput?.(data.toString('utf8')); });
    child.stdin.on('error', () => {});
    child.on('error', () => { clearTimeout(timer); reject(new UserError('docker_missing', '找不到 Docker Desktop，请先安装并打开')); });
    child.on('close', code => { clearTimeout(timer); resolve({ code: expired ? -1 : code,
      stdout: Buffer.concat(chunks).toString('utf8'), diagnostic }); });
    child.stdin.end(input);
  });
}

function localClient() {
  const client = new EventEmitter();
  client.forwardOut = (_address, _source, host, port, callback) => {
    if (host !== '127.0.0.1' || !control.validPort(port)) return callback(new Error('Invalid local destination'));
    const socket = net.createConnection({ host, port });
    const fail = error => callback(error);
    socket.once('error', fail);
    socket.once('connect', () => { socket.removeListener('error', fail); callback(null, socket); });
  };
  return client;
}
async function freePort(excluded = []) {
  const server = net.createServer();
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return excluded.includes(port) || [5432, 6099, 8090].includes(port) ? freePort(excluded) : port;
}

class LocalDeployment {
  constructor(directory, bundleDirectory, safeStorage) {
    this.directory = directory; this.bundleDirectory = bundleDirectory; this.safeStorage = safeStorage;
    this.filename = path.join(directory, 'instance.json'); this.running = null;
  }
  read() {
    if (!fs.existsSync(this.filename)) return null;
    if (fs.lstatSync(this.filename).isSymbolicLink()) throw new UserError('local_record', '本机记录路径异常，未操作 Docker');
    const value = JSON.parse(fs.readFileSync(this.filename, 'utf8'));
    if (value.schema !== 1 || !ID.test(value.job) || !HASH.test(value.checksum) || !control.validPort(value.port)
        || !control.validPort(value.botPort) || value.port === value.botPort || typeof value.credential !== 'string'
        || typeof value.engineId !== 'string'
        || value.controllerChecksum !== undefined && !HASH.test(value.controllerChecksum)
        || value.storageState !== undefined && !['new', 'created', 'ready'].includes(value.storageState)) throw new UserError('local_record', '本机安装记录损坏，请保留数据，勿重复安装');
    return value;
  }
  write(value) {
    fs.mkdirSync(this.directory, { recursive: true, mode: 0o700 });
    const temp = this.filename + '.tmp';
    fs.writeFileSync(temp, JSON.stringify(value), { mode: 0o600 });
    fs.renameSync(temp, this.filename);
  }
  async docker(args, options) {
    const target = endpoint();
    return run(target.binary, ['--host', target.host, ...args], options);
  }
  async checked(args, options) {
    const result = await this.docker(args, options);
    if (result.code !== 0) {
      // Classify diagnostics, but never send raw Docker output/URLs to the renderer.
      const hint = /no space left|disk quota exceeded/i.test(result.diagnostic) ? 'Docker 磁盘空间不足'
        : /address already in use|port is already allocated/i.test(result.diagnostic) ? '本机端口被其他程序占用'
        : /no matching manifest|exec format error/i.test(result.diagnostic) ? '依赖镜像不支持当前处理器架构'
        : /resolve|tls handshake|connection timed out|failed to fetch|network is unreachable/i.test(result.diagnostic) ? '依赖下载网络不可用'
        : args[0] === 'build' ? '运行镜像构建失败' : 'Docker 组件操作未完成';
      throw new UserError('local_docker_failed', hint + '；请检查 Docker Desktop 后继续安装，已有数据保留');
    }
    return result.stdout;
  }
  async inspect() {
    const record = this.read();
    const target = endpoint();
    const blockers = [];
    let engine = null;
    const installed = fs.existsSync(target.binary);
    if (!installed) blockers.push('尚未安装 Docker Desktop');
    else {
      try {
        const result = await this.docker(['info', '--format', '{{json .}}'], { timeout: 10000 });
        if (result.code === 0) engine = JSON.parse(result.stdout);
      } catch { /* Show the prerequisite failure without engine diagnostics. */ }
      if (!engine) blockers.push('Docker Desktop 尚未就绪；Windows 请使用 WSL2 / Linux 容器');
      else {
        if (engine.OSType !== 'linux' || !/docker desktop/i.test(engine.OperatingSystem || '')) blockers.push('需要本机 Docker Desktop 的 Linux 引擎，未连接其他 Docker 主机');
        if (Number.parseInt(engine.ServerVersion, 10) < 26) blockers.push('请升级 Docker Desktop，本机数据卷需要 Docker 26 或更高版本');
        if (engine.MemTotal < 4 * 1024 ** 3) blockers.push('请在 Docker Desktop 中至少分配 4 GB 内存');
        if (record && engine.ID !== record.engineId) blockers.push('Docker 引擎与原安装不同；请先恢复原 Docker 数据，未创建替代数据库');
      }
    }
    if (!this.safeStorage.isEncryptionAvailable()) blockers.push('系统凭据存储不可用，请解锁系统账户后重试');
    const disk = fs.statfsSync(fs.existsSync(this.directory) ? this.directory : path.dirname(this.directory));
    const freeBytes = Number(disk.bavail) * Number(disk.bsize);
    if (!record && freeBytes < 15 * 1024 ** 3) blockers.push('首次安装请至少留出 15 GB 磁盘空间');
    return { installed, ready: blockers.length === 0, blockers, engineId: engine?.ID || '', freeBytes,
      memoryBytes: engine?.MemTotal || 0, platform: process.platform, recorded: Boolean(record),
      volume: record ? `gaoji-desktop-${record.job}-data` : '', status: record?.progress || null };
  }
  access(record) {
    return { port: record.port, token: this.safeStorage.decryptString(Buffer.from(record.credential, 'base64')) };
  }
  async request(action, payload) {
    const record = this.read();
    if (!record) throw new UserError('local_not_installed', '请先安装本机环境');
    return control.request(localClient(), this.access(record), action, payload);
  }
  async status() {
    const record = this.read();
    if (!record) return null;
    if (this.running) return record.progress;
    try { return (await this.request('status')).desktop_installation; }
    catch {
      if (record.update && record.storageState === 'ready') return { status: 'awaiting_setup', stage: 'update',
        message: '原本机实例正在更新或等待恢复，请查看更新与回退' };
      if (record.progress?.status === 'failed' || record.progress?.status === 'stopped') return record.progress;
      return { status: 'unconfirmed', stage: record.progress?.stage || 'prepare',
        message: '本机服务未连接，请打开 Docker Desktop 后点击继续；不会创建第二个实例' };
    }
  }
  async start(options) {
    if (this.starting) return this.starting;
    this.starting = this.prepareStart(options).finally(() => { this.starting = null; });
    return this.starting;
  }
  async prepareStart(options) {
    if (this.running) return this.read().progress;
    const inspection = await this.inspect();
    if (!inspection.ready) throw new UserError('local_not_ready', inspection.blockers.join('；'));
    let record = this.read();
    if (record?.update && record.updateProgress?.finished !== true && record.updateProgress?.status !== 'completed') {
      throw new UserError('update_busy', '原实例有待核对的更新，请到更新与回退继续原任务');
    }
    if (record) {
      const volume = await this.owned('volume', `gaoji-desktop-${record.job}-data`, record);
      if (!volume && record.storageState !== 'new') {
        throw new UserError('local_data_missing', '原本机数据卷不存在，已停止操作；请恢复原 Docker 数据或备份，不会创建空数据库代替');
      }
      if (volume && await this.resumeController(record)) return record.progress;
    }
    const manifest = deploy.bundle(this.bundleDirectory);
    const checksum = manifest.files['gaoji-runtime.tar.gz'].sha256;
    const imageOptions = imagePolicy.options(options || record?.imageOptions);
    if (record && checksum !== record.checksum) throw new UserError('local_version', '原运行组件缺失或安装未完成，需要匹配原运行包才能修复；已有数据保留，未自动迁移或创建新实例');
    if (!record) {
      const port = await freePort();
      record = { schema: 1, job: randomUUID().replaceAll('-', ''), checksum, port,
        botPort: await freePort([port]), engineId: inspection.engineId, storageState: 'new',
        credential: this.safeStorage.encryptString(randomBytes(32).toString('base64url')).toString('base64') };
    }
    record.imageOptions = imageOptions;
    record.progress = { status: 'running', stage: 'runtime', message: '正在准备本机运行镜像，首次安装可能需要较长时间' };
    this.write(record);
    this.running = this.install(record).catch(error => {
      record.progress = { ...record.progress, status: 'failed', updated_at: Math.floor(Date.now()/1000), message: error instanceof UserError ? error.message : '本机安装未完成，已有数据保留；请点击继续安装' };
      this.write(record);
    }).finally(() => { this.running = null; });
    return record.progress;
  }
  async owned(kind, name, record) {
    const listed = await this.checked(kind === 'container' ? ['ps', '-a', '--format', '{{.Names}}'] : [kind, 'ls', '--format', '{{.Name}}']);
    if (!listed.trim().split('\n').includes(name)) return null;
    const row = JSON.parse(await this.checked([kind, 'inspect', name]))[0];
    const labels = kind === 'container' ? row.Config.Labels : row.Labels;
    if (labels?.['gaoji.desktop.job'] !== record.job) throw new UserError('local_ownership', '同名 Docker 资源不属于此实例，已停止操作');
    return row;
  }
  async resumeController(record) {
    const name = `gaoji-desktop-${record.job}-controller`;
    const controller = await this.owned('container', name, record);
    if (!controller) return false;
    const network = await this.owned('network', `gaoji-desktop-${record.job}`, record);
    const volume = `gaoji-desktop-${record.job}-data`;
    const data = controller.Mounts?.filter(mount => mount.Destination === '/var/lib/gaoji-app');
    const ports = controller.HostConfig?.PortBindings?.['8090/tcp'];
    const attachedNetwork = network && controller.NetworkSettings?.Networks?.[network.Name]?.NetworkID;
    if (!HASH.test(controller.Id || '') || !/^sha256:[a-f0-9]{64}$/.test(controller.Image || '')
        || controller.Config.Image !== `gaoji-desktop-controller:${record.controllerChecksum || record.checksum}`
        || JSON.stringify(controller.Config.Cmd) !== '["serve"]'
        || data?.length !== 1 || data[0].Type !== 'volume' || data[0].Name !== volume || !data[0].RW
        || !network || controller.HostConfig?.NetworkMode !== network.Name
        || attachedNetwork && attachedNetwork !== network.Id
        || ports?.length !== 1 || ports[0].HostIp !== '127.0.0.1' || ports[0].HostPort !== String(record.port)) {
      throw new UserError('local_ownership', '原管理组件的挂载、网络或端口与安装记录不符，未启动或替换');
    }
    const image = JSON.parse(await this.checked(['image', 'inspect', controller.Image]))[0];
    if (image.Id !== controller.Image || image.Config.Labels?.['gaoji.desktop.runtime-digest'] !== (record.controllerChecksum || record.checksum)) {
      throw new UserError('local_image', '原管理组件的运行镜像无法核实，未启动或替换');
    }
    if (controller.State.Running) {
      let current, version;
      try { current = await this.request('status'); version = await this.request('versions'); }
      catch { throw new UserError('local_controller_unavailable', '原管理组件仍在运行，但暂时无法连接；未强制重启，请稍后重新连接'); }
      if (version.checksum !== record.checksum || !current.desktop_installation) {
        throw new UserError('local_version', '管理接口与原实例的运行版本不符，未操作');
      }
      if (current.desktop_installation.status !== 'failed') {
        record.progress = current.desktop_installation; this.write(record); return true;
      }
      if (current.maintenance_pending || current.operation && ['queued', 'running'].includes(current.operation.status)) {
        throw new UserError('local_busy', '原实例有未完成的维护或配置任务，未强制重启');
      }
    }
    // Start the verified immutable container, never rebuild an old instance from a new bundle.
    await this.checked([controller.State.Running ? 'restart' : 'start', controller.Id]);
    record.progress = { status: 'running', stage: 'reconnect', message: '正在连接原本机实例，数据与运行版本保持不变' };
    this.write(record);
    return true;
  }
  async prepareImages(record, progress) {
    const archive = fs.readFileSync(path.join(this.bundleDirectory, 'gaoji-runtime.tar.gz'));
    const manifest = deploy.bundle(this.bundleDirectory);
    const images = imagePolicy.catalog(manifest).images[`linux/${process.arch === 'arm64' ? 'arm64' : 'amd64'}`];
    const options = imagePolicy.options(record.imageOptions);
    for (const [image, file] of [[`gaoji-app-runtime:${record.checksum}`, 'Dockerfile'], [`gaoji-desktop-controller:${record.checksum}`, 'Desktop.Dockerfile']]) {
      progress('runtime', file === 'Dockerfile' ? '正在准备本机机器人镜像，首次需要下载依赖' : '正在准备本机管理组件');
      const inspected = await this.docker(['image', 'inspect', image]);
      if (inspected.code !== 0) {
        const remote = images?.[file === 'Dockerfile' ? 'runtime' : 'controller'];
        const started = Date.now(); let lastOutput = started, detail = '连接下载源';
        const timer = setInterval(() => progress('runtime', `${detail} · 已用 ${Math.floor((Date.now() - started) / 60000)} 分 ${Math.floor((Date.now() - started) / 1000) % 60} 秒${Date.now() - lastOutput > 60000 ? ' · 等待新输出' : ''}`), 1000);
        const onOutput = text => { lastOutput = Date.now();
          if (/Get:|Downloading|Pulling/i.test(text)) detail = '下载镜像或运行依赖';
          else if (/Building CXX|Linking/i.test(text)) detail = '编译语音组件';
          else if (/Unpacking|Extracting/i.test(text)) detail = '解压依赖';
          else if (/Setting up|uv sync|Prepared .* packages/i.test(text)) detail = '安装运行依赖';
          else if (/exporting/i.test(text)) detail = '保存运行镜像';
        };
        try {
          if (remote && options.mode !== 'source') {
            detail = '下载预构建镜像';
            await this.checked(['pull', remote], { timeout: 1800000, onOutput });
            const metadata = JSON.parse(await this.checked(['image', 'inspect', remote]))[0];
            if (metadata.Config.Labels?.['gaoji.desktop.runtime-digest'] !== record.checksum || metadata.Architecture !== (process.arch === 'arm64' ? 'arm64' : 'amd64')) throw new UserError('local_image', '预构建镜像的版本或架构不匹配');
            await this.checked(['tag', remote, image]);
          } else if (options.mode === 'prebuilt') throw new UserError('missing_image', '此版本尚未附带已发布的预构建镜像，请选择源码构建或更换发行版');
          else {
            detail = '源码构建 · ' + (options.source === 'tuna' ? '清华 Debian 镜像源' : '官方 HTTPS 源');
            await this.checked(['build', '--progress=plain', '--build-arg', `DOWNLOAD_SOURCE=${options.source}`, '--label', `gaoji.desktop.runtime-digest=${record.checksum}`,
              '--tag', image, '--file', file, '-'], { input: archive, timeout: 3600000, onOutput });
          }
        } finally { clearInterval(timer); }
      }
      else if (JSON.parse(inspected.stdout)[0].Config.Labels?.['gaoji.desktop.runtime-digest'] !== record.checksum) {
        throw new UserError('local_image', '同名运行镜像来源无法核实，未覆盖或使用');
      }
    }
    if (!record.runtimeVerified) {
      progress('verify', '正在检查运行依赖、中文字体和离线浏览器');
      await this.checked(['run', '--rm', '--network', 'none', `gaoji-app-runtime:${record.checksum}`, 'check-runtime'], { timeout: 300000 });
      record.runtimeVerified = true;
    }
  }
  async install(record) {
    const progress = (stage, message) => {
      const stages = ['runtime', 'verify', 'initialize', 'database', 'migrate', 'qq'];
      record.progress = { status: 'running', stage, message, completed: stages.slice(0, Math.max(0, stages.indexOf(stage))),
        stage_started_at: record.progress?.stage === stage ? record.progress.stage_started_at || Math.floor(Date.now()/1000) : Math.floor(Date.now()/1000),
        updated_at: Math.floor(Date.now()/1000) };
      this.write(record);
    };
    await this.prepareImages(record, progress);
    this.write(record);
    const volume = `gaoji-desktop-${record.job}-data`, network = `gaoji-desktop-${record.job}`;
    const name = `gaoji-desktop-${record.job}-controller`, label = `gaoji.desktop.job=${record.job}`;
    if (!await this.owned('volume', volume, record)) {
      if (record.storageState !== 'new') throw new UserError('local_data_missing', '原本机数据卷不存在，未创建空数据库');
      await this.checked(['volume', 'create', '--label', label, volume]);
    }
    if (record.storageState === 'new') { record.storageState = 'created'; this.write(record); }
    if (!await this.owned('network', network, record)) await this.checked(['network', 'create', '--label', label, network]);
    const mounts = ['--mount', `type=volume,source=${volume},target=/var/lib/gaoji-app`];
    const image = `gaoji-desktop-controller:${record.checksum}`;
    progress('initialize', '正在初始化本机专属数据卷和管理凭据');
    await this.checked(['run', '--rm', '-i', '--network', 'none', ...mounts, image, 'init'], {
      input: JSON.stringify({ job: record.job, checksum: record.checksum, token: this.access(record).token, bot_port: record.botPort }) });
    record.storageState = 'ready'; this.write(record);
    if (await this.resumeController(record)) return;
    else {
      await this.checked(['run', '-d', '--name', name, '--label', label, '--restart', 'unless-stopped',
        '--network', network, '--publish', `127.0.0.1:${record.port}:8090`,
        '--memory', '512m', '--cpus', '1', '--log-driver', 'local', '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3',
        ...mounts, '--mount', 'type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock', image, 'serve']);
    }
    record.progress = { status: 'running', stage: 'database', message: '管理组件已启动，正在初始化本机数据库与 QQ' };
    this.write(record);
  }
}

module.exports = { LocalDeployment, LOCAL_ID, endpoint, run, localClient };
