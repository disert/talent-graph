import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * 静态资源前缀。
 *
 * 默认 "/"：打包出来的 index.html 引用 /assets/xxx.js，适合「直连 20022 端口」或
 * 「反向代理挂在根路径」的场景。
 *
 * 挂在二级路径下（例如 nginx 反代成 https://<代理机>/talent-graph/frontend/）时必须改掉，
 * 否则浏览器会去请求 https://<代理机>/assets/xxx.js —— 代理机上没有这个 location，
 * 直接 404，JS 加载不到，页面就只剩下一个空的 <div id="root">。
 * 传参方式：docker build --build-arg VITE_BASE_PATH=/talent-graph/frontend/
 */
const basePath = process.env.VITE_BASE_PATH || "/";

export default defineConfig({
  base: basePath,
  plugins: [react()],
  server: {
    port: 20022,
    proxy: {
      // 用 127.0.0.1 而非 localhost：Node 会把 localhost 解析成 IPv6 ::1，
      // 而后端 uvicorn 只监听 IPv4 127.0.0.1，会导致 ECONNREFUSED ::1:20021
      "/api": { target: "http://127.0.0.1:20021", changeOrigin: true },
    },
  },
});
