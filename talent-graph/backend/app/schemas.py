"""Pydantic 出入参模型。"""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


# ---------- 简历 ----------
class ResumeOut(BaseModel):
    id: int
    name: str
    structured: dict[str, Any]
    confidence: dict[str, Any]
    status: str
    source: str
    created_at: datetime

    class Config:
        from_attributes = True


class ResumeUpdate(BaseModel):
    """人工修正低置信字段 / 更新状态。"""
    structured: Optional[dict[str, Any]] = None
    status: Optional[str] = None
    source: Optional[str] = None


# ---------- 岗位需求 ----------
class JobCreate(BaseModel):
    title: str
    department: str = ""                     # 部门级联路径字符串（/ 分隔），兼容旧客户端直接传单级名称
    department_path: list[str] = []          # 部门级联路径（["集团总部","人力资源部",...]）
    province: str = ""                       # 岗位所在省份；留空时由 department_path 在组织架构中的省份推导
    majors: list[str] = []                   # 岗位所需专业（显式录入）
    raw_text: str                            # 对话式访谈汇总的自然语言需求


class JobOut(BaseModel):
    id: int
    title: str
    department: str
    department_path: list[str]
    province: str
    majors: list[str]
    raw_text: str
    hard_conditions: dict[str, Any]
    soft_conditions: dict[str, Any]
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


class JobImportResult(BaseModel):
    """Excel 批量导入结果。"""
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    errors: list[dict[str, Any]] = []        # [{row: 行号, message: 原因}]

    class Config:
        from_attributes = True


class JobImportItem(BaseModel):
    """导入会话中的一行（预校验后的原始数据）。"""
    row: int                                 # Excel 行号
    title: str = ""
    department: str = ""
    province: str = ""
    majors: list[str] = []
    raw_text: str = ""
    error: str = ""                          # 预校验发现的问题（空 = 可导入）


class JobImportSessionOut(BaseModel):
    """上传解析后的导入会话：前端据此渲染进度条与异常清单。"""
    token: str                               # 会话 token，供逐行导入时引用
    total: int = 0
    valid: int = 0                           # 预校验通过行数
    invalid: int = 0                         # 预校验异常行数
    items: list[JobImportItem] = []


class JobImportStepIn(BaseModel):
    token: str
    index: int                               # items 中的下标（从 0 开始）


class JobImportStepOut(BaseModel):
    """单行导入结果（进度条每推进一步返回一条）。"""
    row: int
    title: str = ""
    ok: bool = False
    job_id: Optional[int] = None
    error: str = ""


# ---------- 组织架构 ----------
class DepartmentProvinceUpdate(BaseModel):
    """修改部门节点所在省份（空串 = 恢复为继承上级）。"""
    province: str = ""


# ---------- 匹配 ----------
class MatchOut(BaseModel):
    """匹配结果表（match_results）的一条简历×岗位记录。"""
    id: int
    job_id: int
    resume_id: int
    resume_name: str
    vector_score: float
    score: float
    reason: str
    push_status: str
    match_source: str = ""                   # 触发来源：resume_upload / job_create / manual
    created_at: datetime
    updated_at: Optional[datetime] = None


class MatchRunOut(BaseModel):
    job_id: int
    total_after_hard_filter: int
    candidates: list[MatchOut]


class PushRequest(BaseModel):
    match_ids: list[int]


class FeedbackRequest(BaseModel):
    """二期预留：用人单位一键反馈 有意向/无意向。"""
    match_id: int
    result: str  # selected / rejected
