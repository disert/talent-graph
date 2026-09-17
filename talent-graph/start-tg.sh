#!/bin/bash
# 智慧引才图谱 · 无 docker compose 环境的一键启动脚本
#
# 用法：把本文件与镜像放在同一目录，执行  bash start-tg.sh
# 特点：可重复执行（幂等）；所有命令均为整行，规避多行粘贴被命令管控拦截的问题。
#
# 注意：若报 $'\r': command not found，说明文件是 Windows 换行，先执行：
#   sed -i 's/\r$//' start-tg.sh

set -e

NET=talent-graph
DB=talent-graph-db
BE=talent-graph-backend
FE=talent-graph-frontend

echo "==> 1/5 准备网络"
if docker network inspect "$NET" >/dev/null 2>&1; then
  echo "    网络 $NET 已存在，跳过"
else
  docker network create "$NET"
fi

echo "==> 2/5 启动数据库"
if docker ps -a --format '{{.Names}}' | grep -qx "$DB"; then
  echo "    容器 $DB 已存在，直接启动（数据在卷里，不会丢）"
  docker start "$DB" >/dev/null 2>&1 || true
else
  docker run -d --name "$DB" --network "$NET" --restart unless-stopped -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=talent_graph -v talent-graph_pgdata:/var/lib/postgresql/data pgvector/pgvector:pg16 >/dev/null
fi

echo "==> 3/5 等待数据库就绪"
until docker exec "$DB" pg_isready -U postgres >/dev/null 2>&1; do sleep 2; done
echo "    数据库已就绪"

echo "==> 4/5 启动后端"
if docker ps -a --format '{{.Names}}' | grep -qx "$BE"; then
  echo "    重建容器 $BE（使用最新镜像，卷内数据不受影响）"
  docker rm -f "$BE" >/dev/null
fi
docker run -d --name "$BE" --network "$NET" --network-alias backend --restart unless-stopped -p 20021:8000 -e DATABASE_URL=postgresql+psycopg2://postgres:postgres@"$DB":5432/talent_graph -v talent-graph_upload_data:/app/data/uploads talent-graph-backend:latest >/dev/null

echo "==> 5/5 启动前端"
if docker ps -a --format '{{.Names}}' | grep -qx "$FE"; then
  docker rm -f "$FE" >/dev/null
fi
docker run -d --name "$FE" --network "$NET" --restart unless-stopped -p 20022:80 talent-graph-frontend:latest >/dev/null

echo
echo "==== 当前状态 ===="
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E 'NAMES|talent-graph' || true
echo
echo "前端页面 : http://<服务器IP>:20022"
echo "API 文档 : http://<服务器IP>:20021/docs"
echo "后端日志 : docker logs -f $BE"
echo "健康检查 : curl http://127.0.0.1:20021/api/health"
