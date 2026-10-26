'use strict';

const http = require('node:http');
const net = require('node:net');
const { UserError } = require('./contracts.cjs');
const { jobId } = require('./deploy.cjs');
const { execute } = require('./ssh.cjs');

const ROUTES = Object.freeze({ status: '/status', services: '/services', qqStatus: '/qq/status', qqRefresh: '/qq/refresh',
  save: '/settings', modelTest: '/model/test', modelList: '/model/list', embeddingTest: '/embedding/test', clusterTest: '/cluster/test', matrixTest: '/matrix/test', imessageTest: '/imessage/test', operation: '/operation', backups: '/backups', versions: '/versions', console: '/console',
  storageStatus: '/storage/status', storageScan: '/storage/scan', storagePlan: '/storage/plan', storagePolicy: '/storage/policy', storageCleanup: '/storage/cleanup' });

function validPort(port) { return Number.isInteger(port) && port >= 1024 && port <= 65535; }
async function accessFor(client, job) {
  const id = jobId(job);
  const result = await execute(client, `cat /var/lib/gaoji-app/control-access/${id}.json`, { maxBytes: 8192 });
  if (result.code !== 0) throw new UserError('setup_unavailable', '配置入口尚未安装完成，或当前 SSH 账户不是原安装账户');
  let access;
  try { access = JSON.parse(result.stdout); } catch { throw new UserError('setup_unavailable', '配置入口记录暂不可读'); }
  if (access.schema !== 1 || !validPort(access.port) || !/^[a-f0-9]{32}$/.test(access.instance_id)
      || typeof access.token !== 'string' || !/^[A-Za-z0-9_-]{40,100}$/.test(access.token)) {
    throw new UserError('setup_unavailable', '配置入口记录无效，未发送任何密钥');
  }
  return access;
}
function forward(client, port) {
  return new Promise((resolve, reject) => {
    let expired = false;
    const timer = setTimeout(() => { expired = true; reject(new UserError('tunnel_timeout', '连接配置入口超时，请检查服务器网络')); }, 15000);
    // The destination always comes from the authenticated per-instance control
    // record, never from the renderer or a web page.
    client.forwardOut('127.0.0.1', 0, '127.0.0.1', port, (error, stream) => {
      clearTimeout(timer);
      if (expired) { stream?.destroy(); return; }
      if (error) reject(new UserError('tunnel_failed', '服务器配置入口尚未运行，请稍后重试'));
      else resolve(stream);
    });
  });
}
async function request(client, access, action, payload = {}) {
  if (!Object.hasOwn(ROUTES, action)) throw new UserError('invalid_operation', '不支持此管理操作');
  const data = JSON.stringify(payload);
  if (typeof data !== 'string' || Buffer.byteLength(data) > 256 * 1024) throw new UserError('invalid_operation', '提交的数据过大');
  if (!validPort(access.port)) throw new UserError('setup_unavailable', '配置入口无效');
  const stream = await forward(client, access.port);
  return new Promise((resolve, reject) => {
    const agent = new http.Agent({ keepAlive: false });
    agent.createConnection = () => stream;
    const req = http.request({ hostname: '127.0.0.1', port: access.port, path: ROUTES[action], method: 'POST', agent,
      headers: { Authorization: 'Bearer ' + access.token, 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) } }, res => {
      const chunks = []; let size = 0;
      res.on('data', chunk => {
        size += chunk.length;
        if (size > 2 * 1024 * 1024) { req.destroy(); reject(new UserError('invalid_response', '配置响应过大，已停止读取')); }
        else chunks.push(chunk);
      });
      res.on('end', () => {
        try {
          const value = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          if (res.statusCode !== 200) reject(new UserError(res.statusCode === 404 ? 'setup_unsupported' : 'setup_failed', typeof value.error === 'string' ? value.error.slice(0,500) : '配置尚未完成'));
          else resolve(value.data);
        } catch { reject(new UserError('invalid_response', '配置入口没有返回有效数据')); }
      });
      res.on('error', () => reject(new UserError('connection_lost', '连接中断，请重新查看状态；已提交的任务会继续')));
    });
    const timer = setTimeout(() => req.destroy(new Error('request timeout')), 70000);
    req.on('error', () => reject(new UserError('connection_lost', '连接中断或超时，请重新查看状态；不要重复安装')));
    req.once('close', () => { clearTimeout(timer); agent.destroy(); stream.destroy(); });
    req.end(data);
  });
}

async function consoleTunnel(client, port) {
  if (!validPort(port)) throw new UserError('invalid_port', '控制台端口无效');
  const sockets = new Set();
  const server = net.createServer(socket => {
    sockets.add(socket);
    socket.on('error', () => socket.destroy());
    socket.once('close', () => sockets.delete(socket));
    void forward(client, port).then(stream => {
      if (socket.destroyed) { stream.destroy(); return; }
      sockets.add(stream);
      stream.on('error', () => socket.destroy());
      stream.once('close', () => { sockets.delete(stream); socket.destroy(); });
      socket.once('close', () => stream.destroy());
      socket.pipe(stream).pipe(socket);
    }).catch(() => socket.destroy());
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  const close = () => { for (const socket of sockets) socket.destroy(); server.close(); client.removeListener('close', close); };
  client.once('close', close);
  return { url: `http://127.0.0.1:${server.address().port}/bot-admin`, close };
}

module.exports = { ROUTES, validPort, accessFor, request, forward, consoleTunnel };
