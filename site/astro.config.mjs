import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
  site: process.env.SITE_URL || 'https://gaoji.pages.dev',
  integrations: [
    starlight({
      title: 'gaoji',
      description: '住在你 QQ 群里的 AI 群友：能聊天、会查资料、帮你做文件。',
      favicon: '/favicon.svg',
      logo: {
        src: './public/favicon.svg',
        alt: 'gaoji logo',
      },
      defaultLocale: 'root',
      locales: {
        root: {
          label: '简体中文',
          lang: 'zh-CN',
        },
      },
      social: [
        {
          icon: 'github',
          label: 'GitHub',
          href: 'https://github.com/zty20040403/gaojibot',
        },
      ],
      customCss: ['./src/styles/theme.css'],
      head: [
        {
          tag: 'meta',
          attrs: {
            property: 'og:image',
            content: '/og-image.png',
          },
        },
      ],
      sidebar: [
        { label: '首页', link: '/' },
        {
          label: '使用指南',
          items: [
            { label: '指南概览', slug: 'guide' },
            { label: '怎么跟它说话', slug: 'guide/basics' },
            { label: '命令速查', slug: 'guide/commands' },
            { label: '功能说明', slug: 'guide/features' },
            { label: '常见问题', slug: 'guide/faq' },
          ],
        },
        {
          label: '信任与项目',
          items: [
            { label: '隐私与安全', slug: 'privacy' },
            { label: '关于 gaoji', slug: 'about' },
          ],
        },
      ],
    }),
  ],
});
