FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 可选：本地 BGE-M3 + PaddleOCR（内网 GPU 镜像中安装，注释掉可大幅缩小镜像）
# RUN pip install --no-cache-dir sentence-transformers paddlepaddle paddleocr

COPY app ./app

# 配置烘焙进镜像：pydantic-settings 会在 WORKDIR(/app) 下自动读取 .env，
# 所以内网部署时无需再提供 backend/.env 文件。
# 代价：改了 .env 里的地址/密钥必须重新 build 镜像才会生效。
COPY .env ./.env

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
