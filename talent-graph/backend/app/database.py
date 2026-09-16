"""数据库连接与会话管理。"""
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _add_missing_columns() -> None:
    """给已存在的旧表补齐新增列。

    create_all 只建表、不会给已存在的表加列；这里在启动时对旧库升级，
    逐列补齐模型新增的字段，避免写入时报"column ... does not exist"
    （如岗位录入结构化入库 500）。
    - PostgreSQL：用 ADD COLUMN IF NOT EXISTS（列不存在才加，幂等）；
      并对历史行把新增列 NULL 回填为默认值，避免读取时被响应校验拒绝。
    - SQLite：无该语法，先 PRAGMA 查列再 ALTER TABLE ADD COLUMN；
      SQLite 无原生 JSON 列，统一用 TEXT 存储。
    """
    additions = {
        "job_requests": [
            ("department_path", "JSON", "[]"),
            ("province", "VARCHAR(64)", ""),
            ("majors", "JSON", "[]"),
        ],
        "match_results": [
            ("resume_path", "VARCHAR(512)", ""),
        ],
        "departments": [
            ("province", "VARCHAR(64)", ""),
        ],
    }
    with engine.begin() as conn:
        for table, cols in additions.items():
            if engine.dialect.name == "postgresql":
                clauses = ", ".join(f"ADD COLUMN IF NOT EXISTS {name} {ddl}"
                                    for name, ddl, _default in cols)
                conn.execute(text(f"ALTER TABLE {table} {clauses}"))
                # 回填历史行：新增列对旧行是 NULL，把 JSON/字符串默认值补上
                for name, _ddl, default in cols:
                    conn.execute(text(
                        f"UPDATE {table} SET {name} = :v WHERE {name} IS NULL"
                    ).bindparams(v=default))
            elif engine.dialect.name == "sqlite":
                existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
                for name, ddl, default in cols:
                    if name in existing:
                        continue
                    sqlite_type = "TEXT" if ddl == "JSON" else ddl
                    literal = "''" if default == "" else f"'{default}'"
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN {name} {sqlite_type} DEFAULT {literal}"))


def _drop_deprecated_tables() -> None:
    """清理遗留旧表。

    match_records 是早期 schema 的匹配记录表，已被 match_results 取代（代码不再引用，
    见 README），其 job_id/resume_id 外键无级联，会阻塞岗位/简历删除。启动时幂等清理。
    """
    if engine.dialect.name not in ("postgresql", "sqlite"):
        return
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS match_records"))


def init_db() -> None:
    """启动时初始化：PG 启用 pgvector 扩展并建表（一期简化，正式环境用 Alembic 迁移）。

    SQLite 模式（DATABASE_URL=sqlite:///...）跳过扩展创建，embedding 以 JSON 存储、
    向量检索退化为 Python 计算；.db 文件所在目录不存在时自动创建。
    """
    if engine.dialect.name == "sqlite":
        # sqlite:///./data/talent_graph.db -> ./data 目录须存在，否则 SQLAlchemy 建库失败
        db_file = settings.database_url.replace("sqlite:///", "", 1)
        if db_file and not db_file.startswith(":"):
            Path(db_file).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    elif engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    from . import models  # noqa: F401  确保模型已注册
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    _drop_deprecated_tables()
