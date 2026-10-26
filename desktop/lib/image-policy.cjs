'use strict';

const { UserError, shellLiteral } = require('./contracts.cjs');
const DIGEST = /^[a-z0-9][a-z0-9.:-]*\/[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}$/;

function options(value = {}) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).some(key => !['mode', 'source'].includes(key))) throw new UserError('image_options', '安装选项无效');
  const result = { mode: value.mode || 'auto', source: value.source || 'official' };
  if (!['auto', 'prebuilt', 'source'].includes(result.mode) || !['official', 'tuna'].includes(result.source)) throw new UserError('image_options', '安装来源无效');
  return result;
}
function catalog(manifest) {
  const value = manifest.runtime_images || { schema: 1, checksum: manifest.files['gaoji-runtime.tar.gz'].sha256, images: {} };
  if (value.schema !== 1 || value.checksum !== manifest.files['gaoji-runtime.tar.gz'].sha256 || !value.images || typeof value.images !== 'object' || Array.isArray(value.images)) throw new UserError('image_catalog', '镜像与运行包版本不一致');
  for (const [platform, images] of Object.entries(value.images)) {
    if (!['linux/amd64', 'linux/arm64'].includes(platform) || !images || Object.keys(images).sort().join(',') !== 'controller,runtime'
      || !Object.values(images).every(image => typeof image === 'string' && DIGEST.test(image))) throw new UserError('image_catalog', '预构建镜像必须固定摘要并匹配处理器架构');
  }
  return value;
}
function script(manifest, value) {
  return `test ! -L "$work/runtime-images.json"\nprintf '%s\\n' ${shellLiteral(JSON.stringify(catalog(manifest)))} > "$work/runtime-images.json"\ntest ! -L "$work/install-options.json"\nprintf '%s\\n' ${shellLiteral(JSON.stringify(options(value)))} > "$work/install-options.json"`;
}
module.exports = { options, catalog, script };
