'use strict';

const deploy = require('./deploy.cjs');
const control = require('./control.cjs');
const lifecycle = require('./lifecycle.cjs');
const updates = require('./updates.cjs');

const active = status => ['running', 'queued', 'waiting'].includes(status);
function taskState(task, label) {
  if (!task) return null;
  const message = typeof task.message === 'string' ? task.message.slice(0, 300) : '';
  if (active(task.status)) return { state: 'working', label, message };
  if (['failed', 'interrupted'].includes(task.status)) return { state: 'attention', label: '任务需处理', message };
  if (task.status === 'unconfirmed') return { state: 'unknown', label: '任务待确认', message };
  return null;
}

function websiteState(value) {
  const task = taskState(value.task, '网站任务进行中');
  if (task) return task;
  const state = value.site?.status;
  if (state === 'running') return value.health === 'ready'
    ? { state: 'ready', label: '网站可访问', message: '首页已通过真实 HTTP 与内容校验' }
    : { state: value.health === 'unhealthy' ? 'attention' : 'unknown', label: '访问未通过确认', message: '容器状态不能代替页面访问检查' };
  if (['retained', 'removed', 'stopped'].includes(state)) return { state,
    label: { retained: '已卸载 · 数据保留', removed: '数据已删除', stopped: '已停止' }[state], message: '' };
  return { state: 'unknown', label: '部署待确认', message: '已有任务记录，尚未读取到网站状态；不会重复创建实例' };
}

function botState(settings, health) {
  const operation = taskState(settings.operation, '正在应用配置');
  if (operation) return operation;
  if (settings.maintenance_pending) return { state: 'attention', label: '维护待完成', message: '请在服务维护中核对原任务' };
  const services = health.services;
  if (!Array.isArray(services)) throw Error('Invalid services');
  const service = name => services.find(item => item.name === name);
  if (service('database')?.state !== 'running' || service('database')?.health !== 'healthy') {
    return { state: 'attention', label: '数据库未就绪', message: '数据库容器或就绪检查尚未通过' };
  }
  const config = settings.instance?.settings;
  if (!config || !Array.isArray(config.models)) throw Error('Invalid settings');
  if (!config.bot_qq || !config.models.some(model => model.id === config.default_model)) {
    return { state: 'setup', label: '等待配置', message: '配置 QQ 和默认模型后再启用' };
  }
  if (settings.desired?.bot_enabled === false) return { state: 'stopped', label: '机器人未启用', message: '主动停止不等于故障' };
  if (settings.desired?.bot_enabled !== true) throw Error('Unknown desired state');
  if (service('bot')?.state !== 'running' || service('napcat')?.state !== 'running') {
    return { state: 'attention', label: '组件未运行', message: '机器人或 QQ 接入组件没有处于运行状态' };
  }
  if (settings.desired.revision !== settings.instance.revision) return { state: 'attention', label: '配置尚未应用', message: '保存的配置与当前运行版本不同' };
  return health.connection === 'connected'
    ? { state: 'ready', label: 'QQ 已接通', message: '已确认近期连接心跳；未进行付费模型调用' }
    : { state: ['disconnected', 'wrong_account'].includes(health.connection) ? 'attention' : 'unknown', label: 'QQ 连接待确认', message: '没有有效的近期连接心跳，不能仅凭容器运行判断机器人在线' };
}

class ApplicationMonitor {
  constructor(store, websites, clientFor, io = { deploy, control, lifecycle, updates }) {
    Object.assign(this, { store, websites, clientFor, io });
    this.pending = new Map();
  }

  status(id) {
    this.store.instance(id);
    if (this.pending.has(id)) return this.pending.get(id);
    const pending = this.read(id).then(value => ({ ...value, checkedAt: Date.now() })).catch(() => ({
      state: 'unknown', label: '状态读取失败', message: '连接中断或管理入口未响应，不能判断实例已停止', checkedAt: Date.now(),
    })).finally(() => this.pending.delete(id));
    this.pending.set(id, pending);
    return pending;
  }

  async read(id) {
    const instance = this.store.instance(id);
    if (!instance.deployment) return { state: 'draft', label: '待部署', message: '仅保存了本机记录，尚未向服务器提交安装任务' };
    if (instance.appId === 'static-site') return websiteState(await this.websites.status(id));
    const client = await this.clientFor(id);
    if (instance.lifecycle?.dispatched) {
      const value = await this.io.lifecycle.status(client, instance.lifecycle);
      const task = taskState(value, '正在维护');
      if (task) return task;
      if (['retained', 'removed'].includes(value.service_state)) return { state: value.service_state,
        label: value.service_state === 'retained' ? '已卸载 · 数据保留' : '数据已删除', message: '' };
    }
    if (instance.update) {
      const task = taskState(await this.io.updates.status(client, instance.update), '正在更新版本');
      if (task) return task;
    }
    const installed = await this.io.deploy.status(client, instance.deployment.job);
    const task = taskState(installed, '正在安装');
    if (task) return task;
    if (!['awaiting_setup', 'completed'].includes(installed.status)) throw Error('Unknown installation state');
    const access = await this.io.control.accessFor(client, instance.deployment.job);
    const settings = await this.io.control.request(client, access, 'status');
    const health = await this.io.control.request(client, access, 'services');
    return botState(settings, health);
  }
}

module.exports = { ApplicationMonitor, websiteState, botState };
