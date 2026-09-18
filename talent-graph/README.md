# 智慧引才图谱 · 初版代码（MVP）

高层次人才引进「岗位需求 × 人才简历」智能匹配系统。依据《技术栈选型说明》与《解决方案建议》实现一期演示主线：

**上传 → 解析 → 匹配 → 推送 → 导出**

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | React 18 + TypeScript + Vite + Ant Design 5 |
| 后端 | Python 3.12 + FastAPI + SQLAlchemy 2.0 |
| 数据库 | PostgreSQL 16 + pgvector |
| LLM | 内部大模型平台（内网 API，OpenAI 兼容协议，重试 3 次 + 指数退避） |
| Embedding | 内部平台接口（优先）或本地 BGE-M3 |
| OCR | PaddleOCR（扫描件/图片简历） |
| 文档解析 | PyMuPDF（PDF）+ python-docx（Word） |
| 导出 | openpyxl + zipfile |

> 按选型说明的 MVP 裁剪：用 FastAPI BackgroundTasks 代替 Celery+Redis；原件存本地磁盘；单用户无登录。

## 目录结构

```
talent-graph/
├── docker-compose.yml          # 一键编排：PG/pgvector + 后端 + 前端
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── .env.example            # 复制为 .env 后填内部平台地址
│   └── app/
│       ├── main.py             # FastAPI 入口
│       ├── config.py           # 环境变量配置
│       ├── database.py         # 连接 + 建表（pgvector 扩展）
│       ├── models.py           # resumes / job_requests / match_results / departments
│       ├── schemas.py
│       ├── routers/            # resumes / jobs / match 三组 API
│       └── services/
│           ├── parser.py       # PDF/Word/图片文本提取（扫描件走 OCR）
│           ├── llm.py          # 内部平台客户端 + 结构化抽取 + 匹配理由
│           ├── embedding.py    # 平台接口或本地 BGE-M3
│           ├── matching.py     # 两级匹配引擎（硬过滤→向量粗排→LLM 精排）+ 结果落库 match_results
│           └── exporter.py     # Excel 名单导出 / 名单+简历原件 ZIP 打包
└── frontend/
    ├── Dockerfile / nginx.conf
    └── src/
        ├── api/client.ts       # Axios 封装 + 类型
        └── pages/              # 上传解析 / 岗位录入 / 匹配结果 / 简历库
```

## 快速启动

### 方式一：Docker Compose（推荐）

```bash
cd talent-graph
cp backend/.env.example backend/.env   # 填写内部大模型平台地址
docker compose up -d --build
```

- 前端：http://localhost:20022
- API 文档：http://localhost:20021/docs

> `backend/.env` 在 build 阶段被 `COPY` 进镜像的 `/app/.env`（配置烘焙进镜像），容器启动时由
> pydantic-settings 读取。好处是内网部署只需带镜像（见「内网离线部署」）；
> 代价是**改了它必须重新 `--build`** 才生效。

### 方式二：本地开发

```bash
# 数据库（或本地已有 PG+pgvector）
docker compose up -d db

# 后端
cd backend
pip install -r requirements.txt
cp .env.example .env    # DATABASE_URL db 改为 localhost
uvicorn app.main:app --reload --port 20021
# 如果unicorn指令无法识别 可以使用下列指令
python -m uvicorn app.main:app --reload --port 20021

# 前端
cd frontend
npm install
npm run dev             # http://localhost:20022，已配置 /api 代理
```

## 内网离线部署

配置已随 `backend/.env` 烘焙进镜像（`/app/.env`），内网机器**不需要再提供 `.env`**，
只需要镜像 + 一份 `docker-compose.yml`。

> 交给运维的完整交付流程（环境要求、验收命令、备份恢复、常见报错排查）见 **`部署说明.md`**，
> 连同 `talent-graph-images.tar` 与 `docker-compose.yml` 一起发出即可。

```bash
# 1) 有网机器导出（已 build 过就直接 save；基础镜像 python/node/nginx 不用带，层已包含在产物里）
docker save -o talent-graph-images.tar pgvector/pgvector:pg16 talent-graph-backend:latest talent-graph-frontend:latest

# 2) 拷进内网后导入
docker load -i talent-graph-images.tar

# 3) 把 docker-compose.yml 一起带过去，然后执行（不要加 --build：内网拉不到基础镜像）
docker compose up -d
```

后端镜像约 **2.8GB**（含 PaddleOCR + OCR 模型），tar 包相应变大，导入/拷贝会慢一些。

重建后端镜像（改了 `.env` 或新增 OCR 模型时才需要，有网环境执行）：

```bash
cd talent-graph
docker compose build backend        # 默认走清华 PyPI 源
docker compose build backend --build-arg PIP_INDEX=https://pypi.org/simple   # 需要换源时
```

- **内网要改配置**：改 `backend/.env` → `docker compose build backend && docker compose up -d --force-recreate backend`。
  配置在镜像里，**必须重建**才生效（重启容器没用）。
- **`DATABASE_URL` 是例外**：它由 `docker-compose.yml` 的 `environment` 在运行时注入并覆盖镜像内的值
  （镜像里写的是 `localhost`，容器里必须走 `db` 服务名），改它只要 `docker compose up -d --force-recreate backend`。
- **数据**（可选）：命名卷 `talent-graph_pgdata`（库）+ `talent-graph_upload_data`（简历原件）。
  要带历史数据需一并迁移；只迁库不迁原件，导出 ZIP 会标「（原件缺失）」。
- **安全**：镜像内含明文密钥，不要推到不可信的仓库或外发。
- **OCR（扫描件/图片简历）**：后端镜像内已装 `paddlepaddle 3.3.1` + `paddleocr 3.7.0`，
  OCR 模型（PP-OCRv6 检测/识别 + 文本行方向）已烘焙到 `/opt/paddlex_models`，运行时**不联网**。
  ⚠️ 新加模型文件后必须重新 `docker compose build backend`，否则镜像里还是旧缓存。
- **OCR 提速**：文档方向分类（`PP-LCNet_x1_0_doc_ori`）与 UVDoc 文档矫正已在
  `services/parser.py::_build_ocr_engine` 里主动关闭（`use_doc_orientation_classify=False`
  + `use_doc_unwarping=False`）：初始化少载 2 个模型、每页少跑两次网络，简历扫描件几乎无损失。
  要恢复这两步，改回 `True` 并把对应模型目录补回 `/opt/paddlex_models/official_models`。
- **离线开关**：`OFFLINE_MODE=true`（`.env` 已烘焙进镜像）。开启后本地 BGE-M3 若不在本地缓存
  会直接报错，而不是去连 huggingface.co——内网这种请求会一直挂着、把整条解析队列拖死。
- **挂在二级路径下访问**（例：应用在 `44.149`，内网只能经 `74.32` 的 nginx 转发到 `/talent-graph/...`）：
  前端必须带构建参数重出镜像，否则页面能打开但 `#root` 空白（资源 `/assets/xxx.js` 在代理机上 404）：
  ```bash
  docker compose build frontend       # args 已写在 docker-compose.yml（VITE_BASE_PATH / VITE_API_BASE）
  ```
  代理机 nginx 用 `location /talent-graph/frontend { proxy_pass http://<应用机>:20022; }`（**结尾不加斜杠**），
  并在 server 块加 `client_max_body_size 100m;`（默认 1m，不加则上传附件一律 413；前端容器自带的 nginx
  也已同步放开）。完整步骤与验收见 `部署说明.md` 附录 B。

## 使用流程（对应演示主线）

1. **简历上传解析**：批量上传 PDF/Word/图片 → 后台解析 → 低置信字段高亮；
2. **简历库检索**：人工修正低置信字段（保存后自动重新生成向量）；
3. **岗位需求录入**：多层级级联选择需求部门，填写省份/专业，粘贴与业务部门的对话访谈内容 → LLM 结构化为硬性/择优条件；支持下载 Excel 模板「批量导入」（`GET /api/jobs/import/template` / `POST /api/jobs/import`），已录入岗位按省份分布看板展示；
4. **匹配结果**：简历上传解析完成、岗位新建/导入时即自动跑两级匹配并写入 `match_results` 表；匹配页选岗位即秒出结果（只查该表，不重算）→ 查看 Top 候选与逐条理由（可点开简历原文溯源）→ 勾选精选 → 推送 → 导出（名单+简历原件 ZIP 或仅 Excel）。需要刷新时点「重新匹配」手动重跑并覆盖结果。

## 对接内部平台前需确认（选型说明遗留 4 问）

| # | 确认项 | 代码位置 |
|---|---|---|
| 1 | 接口协议是否 OpenAI 兼容 | `services/llm.py` 的 `_chat()` |
| 2 | 是否支持 JSON 模式 | `llm.py` 已做不支持时自动降级 |
| 3 | 是否提供 Embedding 接口 | `.env` 的 `EMBEDDING_BASE_URL` |
| 4 | 并发限制 | `matching.py` 的 `ThreadPoolExecutor(max_workers=4)` |

## 二期/三期预留

- `POST /api/match/feedback` 反馈回流接口已实现（匹配页有模拟回填按钮）；
- Celery+Redis 异步队列、MinIO、SSO 登录按选型说明二期接入。

## 匹配结果表（match_results）

匹配结果不再每次实时计算，统一落库到 `match_results`（`job_id + resume_id` 唯一）：

| 触发时机 | 入口 | `match_source` |
| --- | --- | --- |
| 简历上传解析完成 / 重新解析 | `resumes._process_resume` | `resume_upload` |
| 简历人工修正字段 | `resumes.update_resume`（后台） | `resume_upload` |
| 岗位新建 / Excel 导入 | `jobs._match_job_task`（后台） | `job_create` |
| 匹配页「重新匹配」 | `POST /api/match/run/{job_id}` | `manual` |

- 简历侧：先对该简历 × 全部「招聘中」岗位做硬性过滤，再按向量相似度取最相关
  `MATCH_RESUME_JOB_TOP_K`（默认 10）个岗位交 LLM 精排，避免岗位多时无谓的大模型调用。
- 岗位侧：硬性过滤 → 向量粗排 `MATCH_VECTOR_TOP_K` → LLM 精排 `MATCH_FINAL_TOP_N`，与原有策略一致。
- 写入为幂等 upsert：重跑只刷新分数/理由/来源/简历路径，**保留 `push_status`**（已推送/有意向/无意向不被覆盖）。
- `resume_path`：匹配时把简历原件的完整路径快照进表，导出打包时直接用它定位文件
  （旧记录该列为空时自动回退到 `resumes.file_path`）。
- 查询接口只读该表：`GET /api/match/{job_id}`（按岗位）、`GET /api/match/resume/{resume_id}`（按简历）。
- 单条 LLM 精排失败不影响整批（记 0 分并写明失败原因），匹配失败也不会把简历误标为「解析失败」。
- 旧库中的 `match_records` 表已废弃（模型中的 `MatchRecord` 已由 `MatchResult` 取代），
  不影响运行，确认无用后可手动 `DROP TABLE match_records;` 清理。

## 导出（名单 / 名单+简历）

| 接口 | 内容 |
| --- | --- |
| `GET /api/match/{job_id}/export`（默认） | ZIP：`候选名单_岗位_时间.xlsx` + `简历原件/姓名_专业.pdf` 等原件 |
| `GET /api/match/{job_id}/export?with_files=false` | 仅候选名单 xlsx |

- 名单新增「简历文件」列，写明打包内的相对路径；原件已丢失/被手工删除的记录标注「（原件缺失）」，
  打包时自动跳过（后端打 WARNING 日志），不影响其余文件。
- ZIP 内简历文件名统一为「姓名_专业.原后缀」，同名自动加序号；文件名与 ZIP 内条目都会过滤
  `/ : * ? " < > |` 等非法字符，避免解压出多余目录或保存失败。

## 联调测试（已通过 20/20）

无内网平台时可用 Mock 跑通全链路（后端已完成冒烟验证）：

```bash
cd backend
pip install -r requirements.txt
pip install requests

# 终端1：启动 Mock 内部大模型平台（OpenAI 兼容，返回固定 JSON + 确定性向量）
python tests/mock_llm_server.py          # 监听 :9000

# 终端2：以 SQLite 演示模式启动后端（无需 PostgreSQL）
# Git Bash:  set -a && . ./.env.test && set +a
# PowerShell: 手动设置 .env.test 中的同名环境变量
uvicorn app.main:app --port 20021

# 终端3：全链路冒烟测试
python tests/smoke_test.py
# 覆盖：健康检查→上传docx→后台解析→置信度标记→人工修正→关键词检索
#       →岗位结构化→两级匹配(理由)→推送(状态流转)→反馈回流→导出Excel
```

SQLite 演示模式说明：`DATABASE_URL=sqlite:///...` 时无需 PG/pgvector，embedding 以 JSON 存储、
向量粗排退化为 Python 余弦计算；切回 PostgreSQL 后自动使用 pgvector SQL 检索。

> 「需求部门」多层级级联的组织树在后端 `backend/app/services/departments.py` 统一维护
> （前端经 `GET /api/jobs/departments` 异步加载），请按实际组织架构替换 `DEPARTMENT_TREE`。

## 已验证记录（2026-08-21）

- 后端 12 条 API 路由全部注册，OpenAPI 文档正常生成；
- 冒烟测试 20/20 通过（Mock LLM + SQLite 演示模式）；
- 联调中修复的两个真实缺陷：
  1. `generate_match_reason` 的 prompt 模板含 JSON 大括号，误用 `str.format` 导致精排失败 → 改为 `replace` 注入；
  2. 导出接口中文文件名未按 RFC 5987 URL 编码导致 500 → 已加 `urllib.parse.quote()`。
