import { defineConfig } from "vite";

// 前端工程化构建配置
// - root 即 client/ 目录（与后端同仓库，前端源码随仓库提交，构建产物 dist/ 不入库）
// - base: "./" 产出相对路径资源，使 gateway 在 `/` 与 `/review` 两条路由下都能正确解析 /assets
// - 多入口：chat.html（患者端）、review.html（医护端），各自引用 /src/*.js 模块
// - dev 模式把 /api /auth 代理到后端 8000，本地联调无需跨域
export default defineConfig({
  root: ".",
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        chat: "chat.html",
        review: "review.html",
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/auth": "http://127.0.0.1:8000",
    },
  },
});
