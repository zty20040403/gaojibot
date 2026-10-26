'use strict';

const { app, nativeImage } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

// Static icon rendering does not need a GPU compositor.
app.disableHardwareAcceleration();

app.whenReady().then(async () => {
  const output = path.resolve(__dirname, '../build');
  fs.mkdirSync(output, { recursive: true });
  const icon = nativeImage.createFromPath(path.resolve(__dirname, '../public/brand.png')).resize({ width: 1024, height: 1024 });
  if (icon.isEmpty()) throw new Error('Icon rendering returned an empty image');
  fs.writeFileSync(path.join(output, 'icon.png'), icon.toPNG());
  const sizes = [16, 32, 48, 64, 128, 256];
  const images = sizes.map(size => icon.resize({ width: size, height: size }).toPNG());
  const header = Buffer.alloc(6 + 16 * sizes.length);
  header.writeUInt16LE(1, 2); header.writeUInt16LE(sizes.length, 4);
  let offset = header.length;
  sizes.forEach((size, i) => {
    const position = 6 + i * 16;
    header[position] = size === 256 ? 0 : size; header[position + 1] = header[position];
    header.writeUInt16LE(1, position + 4); header.writeUInt16LE(32, position + 6);
    header.writeUInt32LE(images[i].length, position + 8); header.writeUInt32LE(offset, position + 12);
    offset += images[i].length;
  });
  fs.writeFileSync(path.join(output, 'icon.ico'), Buffer.concat([header, ...images]));
  if (process.platform === 'darwin') {
    const iconset = path.join(output, 'Gaoji.iconset');
    fs.mkdirSync(iconset, { recursive: true });
    for (const size of [16, 32, 128, 256, 512]) {
      for (const scale of [1, 2]) {
        fs.writeFileSync(path.join(iconset, `icon_${size}x${size}${scale === 2 ? '@2x' : ''}.png`),
          icon.resize({ width: size * scale, height: size * scale }).toPNG());
      }
    }
    execFileSync('/usr/bin/iconutil', ['-c', 'icns', '-o', path.join(output, 'icon.icns'), iconset]);
    fs.rmSync(iconset, { recursive: true });
  }
  console.log('Generated application PNG/ICO' + (process.platform === 'darwin' ? '/ICNS' : '') + ' from public/brand.png.');
  app.quit();
}).catch(error => { console.error(error); app.exit(1); });
