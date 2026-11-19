# gaoji 官网 (`site/`)

基于 [Astro](https://astro.build/) + [Starlight](https://starlight.astro.build/) 构建的纯静态文档与介绍站点。

## 本地预览与构建

需要 Node.js 22.23.4+（推荐 Node.js 24）。

```sh
cd site
npm ci
npm run dev -- --host 127.0.0.1 --port 4321
```

在浏览器打开 `http://127.0.0.1:4321/` 即可实时预览（Astro 7 后台启动时可用 `npx astro dev stop` 停止）。

验证生产静态构建并本地预览产物：

```sh
cd site
npm run build
npm run preview -- --host 127.0.0.1 --port 4322
```

构建产物输出在 `site/dist/`。
