"""全局配置：全部通过环境变量注入，默认值为本地开发用。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 数据库：PostgreSQL 16 + pgvector（docker-compose 已编排 PG 服务）
    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/talent_graph"
    # docker-compose 内部访问：postgresql+psycopg2://postgres:postgres@db:5432/talent_graph

    # 连接池：SQLAlchemy 默认只有 5 + 10 溢出。批量上传简历时，每个后台解析任务
    # 在匹配阶段都要占用连接，默认值会被瞬间打爆并抛
    # `QueuePool limit of size 5 overflow 10 reached`，这里放宽并让取不到连接时快速失败。
    db_pool_size: int = 20            # 常驻连接数
    db_max_overflow: int = 30         # 峰值可临时超出数（合计 50）
    db_pool_timeout: float = 5.0      # 取连接的等待秒数（默认 30s 会让请求全挂死）
    db_pool_recycle: int = 1800       # 连接最长复用秒数，避免中间件断开空闲连接

    # 重负载后台任务（OCR / LLM / embedding）并发上限。上传简历按文件起后台任务，
    # 不限并发会同时抢 CPU、抢连接、触发上游限流。单实例内部工具，进程内队列够用。
    heavy_task_concurrency: int = 1

    # 内部大模型平台（内网 API，默认按 OpenAI 兼容协议对接）
    llm_base_url: str = "http://internal-llm-platform.local/v1"
    llm_api_key: str = "internal-key"
    llm_model: str = "internal-model"
    llm_timeout: float = 120.0        # 结构化抽取输出较长（工作经历+科研成果），60s 容易超时
    llm_max_retries: int = 3          # 稳定性兜底：失败重试 3 次 + 指数退避

    # Embedding：优先走内部平台接口；未配置则本地加载 BGE-M3
    embedding_base_url: str = ""      # 留空 = 使用本地 BGE-M3
    embedding_api_key: str = ""
    embedding_model: str = "bge-m3"
    embedding_dim: int = 1024         # BGE-M3 向量维度
    embedding_local_path: str = "BAAI/bge-m3"  # 本地模型目录（推荐）或 HuggingFace 仓库名

    # 本地 Embedding 模型下载相关（仅当模型不在本地缓存、确实需要联网下载时生效）
    # 内网/国内直连 huggingface.co 会被黑洞丢包，必须走镜像，否则会一直挂着
    hf_endpoint: str = "https://hf-mirror.com"  # 置空则用官方源；显式设 HF_ENDPOINT 环境变量优先生效
    hf_timeout: float = 20.0          # HF 元数据/下载单次超时秒数（不给无限等待的机会）

    # 离线模式（内网镜像默认开，见 .env 的 OFFLINE_MODE）：
    # 开启后，本地 Embedding 模型不在本地缓存时**直接报错**，而不去连 huggingface.co。
    # 内网既连不上、也常被黑洞丢包，这种请求会一直挂着把整条解析队列拖死。
    offline_mode: bool = False

    # 上游报文日志（内网联调排查用，见 app/upstream_log.py）：
    # 开启后把 LLM / Embedding 的**真实请求 URL + 请求体预览 + 响应状态码/耗时/响应体预览**
    # 打到 INFO（超长自动截断、api key 自动脱敏、失败与 4xx/5xx 必打）。
    # 逐条精排一次匹配会调用成百上千次 LLM，日志量较大；排查完可改 false。
    debug_upstream: bool = False
    upstream_log_max_chars: int = 800   # 单段报文预览最大字符数（0 = 不截断，慎用）

    # 文件存储（一期存本地磁盘，二期可换 MinIO）
    upload_dir: str = "./data/uploads"

    # 匹配参数
    match_vector_top_k: int = 30      # 向量粗排取 Top K，再交 LLM 精排
    match_final_top_n: int = 10       # 最终输出候选人数量
    match_resume_job_top_k: int = 10  # 简历侧上传后，只对向量最相关的 K 个在招岗位做 LLM 精排


settings = Settings()
