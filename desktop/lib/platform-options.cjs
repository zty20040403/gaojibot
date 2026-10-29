'use strict';
const { UserError } = require('./contracts.cjs');
function platforms(value) {
  if (value === undefined) return { onebot: true, qq_official: false };
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || Object.keys(value).sort().join(',') !== 'onebot,qq_official'
      || typeof value.onebot !== 'boolean' || typeof value.qq_official !== 'boolean'
      || !(value.onebot || value.qq_official)) throw new UserError('platform_options', '请选择至少一个机器人平台');
  return { onebot: value.onebot, qq_official: value.qq_official };
}
module.exports = { platforms };
