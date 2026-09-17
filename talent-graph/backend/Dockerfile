FROM python:3.12-slim

# PaddlePaddle / PaddleOCR 的系统级依赖（缺一个就会 import 失败）：
#   libgomp1            - Paddle 的 OpenMP 运行时（libgomp.so.1）
#   libgl1, libglib2.0-0 - opencv（paddlex[ocr-core] 依赖）动态链接的 libGL / glib
# 这三个库 python:3.12-slim 默认没有，内网又装不了，必须烤进镜像。
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# pip 源：与前端 Dockerfile 用 npmmirror 同理，默认走国内镜像——
# paddlepaddle 轮子有 100MB+，直连 files.pythonhosted.org 实测会出现 ReadTimeout，
# 整个镜像构建直接失败。需要换源时：
#   docker build --build-arg PIP_INDEX=https://pypi.org/simple -t talent-graph-backend:latest ./backend
# --retries/--timeout 是为慢速/不稳定网络兜底。
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

COPY requirements.txt .
RUN pip install --no-cache-dir --index-url ${PIP_INDEX} \
    --retries 10 --timeout 120 -r requirements.txt

# OCR（扫描件/图片简历）：版本与本地开发环境对齐，避免「本机好用、镜像里报错」。
# 需要 GPU 时把 paddlepaddle 换成 paddlepaddle-gpu（宿主机需 NVIDIA Container Toolkit）。
RUN pip install --no-cache-dir --index-url ${PIP_INDEX} \
    --retries 10 --timeout 120 paddlepaddle==3.3.1 paddleocr==3.7.0

# ---------- 离线化：模型烤进镜像，运行时绝不联网 ----------
# PADDLE_PDX_CACHE_HOME：OCR 模型缓存根；official_models/ 由下面 COPY 提供。
#   放 /opt 而不是 /app/data，避免被 upload_data 数据卷盖掉。
# PADDLE_PDX_MODEL_SOURCE：paddlex 3.7 的默认值是 huggingface！内网连不上就会去 HF 下载，
#   这里改成 bos（百度官方源）；只要模型命中本地缓存就完全不会用到它。
# PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK：跳过首次初始化时的「联网探测可用模型源」
#   （默认会去连 huggingface.co，内网必卡）。
# HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE：本地 BGE-M3 万一启用，也只读本地缓存。
ENV PADDLE_PDX_CACHE_HOME=/opt/paddlex_models \
    PADDLE_PDX_MODEL_SOURCE=bos \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1

# OCR 模型（PP-OCRv6 检测/识别 + 文本行方向），约 145MB，来自 backend/data/paddlex_models。
# UVDoc 与 PP-LCNet_x1_0_doc_ori 已被代码主动关闭（services/parser.py::_build_ocr_engine），
# 不参与推理，故不烤进镜像；日后要恢复文档矫正需把这两个目录也拷进来。
COPY data/paddlex_models/official_models/PP-OCRv6_medium_det /opt/paddlex_models/official_models/PP-OCRv6_medium_det
COPY data/paddlex_models/official_models/PP-OCRv6_medium_rec /opt/paddlex_models/official_models/PP-OCRv6_medium_rec
COPY data/paddlex_models/official_models/PP-LCNet_x1_0_textline_ori /opt/paddlex_models/official_models/PP-LCNet_x1_0_textline_ori

COPY app ./app

# 配置烘焙进镜像：pydantic-settings 会在 WORKDIR(/app) 下自动读取 .env，
# 所以内网部署时无需再提供 backend/.env 文件。
# 代价：改了 .env 里的地址/密钥必须重新 build 镜像才会生效。
COPY .env ./.env

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
