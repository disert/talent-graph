FROM node:22-alpine AS build
WORKDIR /app

# 部署形态（仅构建期生效，会被编进 JS）：
#   直连 20022 端口（或反代挂在根路径）  —— 不传参数，用默认值
#   反代挂在二级路径 /talent-graph/frontend/ ——
#     docker build --build-arg VITE_BASE_PATH=/talent-graph/frontend/ \
#                  --build-arg VITE_API_BASE=/talent-graph/backend/api ./frontend
ARG VITE_BASE_PATH=/
ARG VITE_API_BASE=/api
ENV VITE_BASE_PATH=$VITE_BASE_PATH \
    VITE_API_BASE=$VITE_API_BASE

# 只复制 package.json（不复制 package-lock.json）：lock 文件是在 Windows 上生成的，
# 会漏装 Linux 平台的 rollup 原生二进制（@rollup/rollup-linux-x64-musl，npm/cli#4828）
COPY package.json ./

# 使用国内镜像源（npmmirror）加速，规避 npmjs 官方源慢/超时问题。
# 需要换源时：docker build --build-arg NPM_REGISTRY=https://registry.npmjs.org ...
# --legacy-peer-deps：没有 lock 文件时 npm 会重新跑一遍 peer 依赖求解，npmmirror 的
#   元数据偶发返回不完整，会报 `ERESOLVE ... Found: vite@undefined` 直接中断构建。
#   本项目依赖很简单，绕过 peer 校验不影响产物（出错时 npm 自己也是这么建议的）。
ARG NPM_REGISTRY=https://registry.npmmirror.com
RUN npm config set registry ${NPM_REGISTRY} \
    && npm install --no-audit --no-fund --legacy-peer-deps \
       --fetch-retries=5 --fetch-retry-maxtimeout=120000

COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
