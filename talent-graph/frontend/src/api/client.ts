import axios from "axios";

/**
 * 后端接口前缀。
 *
 * 默认 "/api"（直连后端或容器内 nginx 反代）。
 * 二级路径部署时由构建参数注入，例如 VITE_API_BASE=/talent-graph/backend/api，
 * 这样浏览器请求 /talent-graph/backend/api/... 经代理机转发后由容器内 nginx 剥掉前缀。
 *
 * 注意：模板下载 / 导出这类**拼字符串给浏览器**的地址也必须用这个前缀，不能写死 /api。
 */
export const API_BASE = (import.meta.env.VITE_API_BASE || "/api").replace(/\/+$/, "");

export const api = axios.create({ baseURL: API_BASE, timeout: 300000 });

/**
 * 上传类请求的报错文案。
 *
 * 被 nginx / 反向代理拦下的 413 返回的是 HTML 错误页（没有 `detail` 字段），
 * 直接取 `e.response.data.detail` 只会得到一句「上传失败」，从提示上看不出
 * 「文件太大被网关挡了」。这里统一转成可操作的中文提示。
 */
export const uploadErrorMessage = (e: any, fallback = "上传失败"): string => {
  if (e?.response?.status === 413) {
    return "文件超过服务器允许的上传大小（HTTP 413），请让运维放开反向代理/网关的 client_max_body_size";
  }
  return e?.response?.data?.detail || e?.message || fallback;
};

// ---------- 类型 ----------
export interface Resume {
  id: number;
  name: string;
  structured: Record<string, any>;
  confidence: Record<string, any>;
  status: string;
  source: string;
  created_at: string;
}

export interface Job {
  id: number;
  title: string;
  department: string;
  department_path?: string[];
  province: string;
  majors: string[];
  raw_text: string;
  hard_conditions: Record<string, any>;
  soft_conditions: Record<string, any>;
  status: string;
  created_at: string;
}

/** 组织架构节点（需求部门多层级级联） */
export interface DepartmentNode {
  id?: number;
  label: string;
  value: string;
  /** 节点自身省份（空串 = 继承上级） */
  province?: string;
  /** 节点生效省份（已按祖先链推导，直接展示用） */
  effect_province?: string;
  children?: DepartmentNode[];
}

/** 部门 Excel 批量导入结果 */
export interface DepartmentImportResult {
  total: number;
  succeeded: number;
  failed: number;
  errors: { row: number; message: string }[];
}

/** Excel 批量导入结果 */
export interface JobImportResult {
  total: number;
  succeeded: number;
  failed: number;
  errors: { row: number; message: string }[];
}

/** 导入会话中的一行（预校验后的原始数据） */
export interface JobImportItem {
  row: number;
  title: string;
  department: string;
  province: string;
  majors: string[];
  raw_text: string;
  /** 预校验发现的问题（空 = 可导入） */
  error: string;
}

/** 上传解析后的导入会话：前端据此渲染进度条与异常清单 */
export interface JobImportSession {
  token: string;
  total: number;
  valid: number;
  invalid: number;
  items: JobImportItem[];
}

/** 单行导入结果（进度条每推进一步返回一条） */
export interface JobImportStep {
  row: number;
  title: string;
  ok: boolean;
  job_id?: number;
  error: string;
}

/** 匹配结果表（match_results）的一行：简历 × 岗位的预计算匹配结果 */
export interface MatchResult {
  id: number;
  job_id: number;
  resume_id: number;
  resume_name: string;
  vector_score: number;
  score: number;
  reason: string;
  /** 触发来源：resume_upload（简历上传）/ job_create（岗位新建）/ manual（手动重跑） */
  match_source: string;
  push_status: string;
  created_at: string;
  updated_at?: string;
}

/** 简历库检索条件（列表与看板共用同一套条件，保证两者口径一致） */
export interface ResumeFilters {
  keyword?: string;
  status?: string;
  low_confidence_only?: boolean;
  /** 学历（博士/硕士/本科，包含匹配） */
  education?: string;
  /** 专业，多个用逗号分隔（任一命中即可） */
  major?: string;
  /** 毕业院校关键词 */
  school?: string;
  /** 渠道 */
  source?: string;
  age_min?: number;
  age_max?: number;
}

/** 饼图数据项（与 PieChart 组件同结构） */
export interface DistributionItem {
  name: string;
  value: number;
}

/** 简历库看板：学校/专业分布 + 筛选项候选值（候选值取自全库） */
export interface ResumeStats {
  total: number;
  by_school: DistributionItem[];
  by_major: DistributionItem[];
  filters: {
    educations: string[];
    majors: string[];
    sources: string[];
  };
}

// ---------- 状态标签 ----------
export const STATUS_MAP: Record<string, { label: string; color: string }> = {
  in_pool: { label: "在库", color: "blue" },
  pushed: { label: "已推送", color: "orange" },
  selected: { label: "被选中", color: "green" },
  rejected: { label: "未选中", color: "default" },
  withdrawn: { label: "已退出", color: "red" },
};

/** 展示用姓名：一律优先取解析/修正后的 structured.name，仅在其为空时回退到 resume.name（文件名兜底） */
export const displayName = (r: Pick<Resume, "name" | "structured">) => r.structured?.name || r.name;

// ---------- API ----------
export const resumeApi = {
  /** 单文件上传：逐文件请求，成功/失败可精确对应到具体文件 */
  upload: (file: File, source: string) => {
    const fd = new FormData();
    fd.append("files", file);
    return api.post<Resume[]>(`/resumes/upload?source=${encodeURIComponent(source)}`, fd);
  },
  list: (params: ResumeFilters & { limit?: number }) =>
    api.get<Resume[]>("/resumes", { params }),
  /** 简历库看板：按同一套筛选条件聚合学校/专业分布（不受列表 limit 截断影响） */
  stats: (params: ResumeFilters) => api.get<ResumeStats>("/resumes/stats", { params }),
  update: (id: number, body: Partial<Pick<Resume, "structured" | "status" | "source">>) =>
    api.patch<Resume>(`/resumes/${id}`, body),
  raw: (id: number) => api.get<{ raw_text: string }>(`/resumes/${id}/raw`),
  reparse: (id: number) => api.post<Resume>(`/resumes/${id}/reparse`),
  remove: (id: number) => api.delete(`/resumes/${id}`),
};

export const jobApi = {
  create: (body: {
    title: string;
    department: string;
    department_path?: string[];
    province?: string;
    majors?: string[];
    raw_text: string;
  }) => api.post<Job>("/jobs", body),
  list: () => api.get<Job[]>("/jobs"),
  remove: (id: number) => api.delete(`/jobs/${id}`),
  /** 组织架构树（需求部门多层级级联） */
  departments: () => api.get<DepartmentNode[]>("/jobs/departments"),
  /** 标准 Excel 导入模板下载地址 */
  importTemplateUrl: `${API_BASE}/jobs/import/template`,
  /** Excel 批量导入岗位（一次性提交，脚本/接口直调用） */
  importJobs: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return api.post<JobImportResult>("/jobs/import", fd);
  },
  /** 解析 Excel 并建立导入会话（只解析不入库，用于展示进度条与异常数据） */
  importPrepare: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return api.post<JobImportSession>("/jobs/import/prepare", fd);
  },
  /** 导入会话中的第 index 行（逐行推进进度条） */
  importStep: (token: string, index: number) =>
    api.post<JobImportStep>("/jobs/import/step", { token, index }),
};

/** 组织架构（需求部门层级）管理 */
export const departmentApi = {
  list: () => api.get<DepartmentNode[]>("/departments"),
  stats: () => api.get<{ total: number }>("/departments/stats"),
  /** 部门层级 Excel 导入模板下载地址 */
  importTemplateUrl: `${API_BASE}/departments/import/template`,
  /** Excel 批量导入部门层级 */
  importExcel: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return api.post<DepartmentImportResult>("/departments/import", fd);
  },
  /** 清空并恢复内置默认示例组织架构 */
  reset: () => api.post("/departments/reset"),
  /** 删除部门节点（存在下级时后端会拒绝） */
  remove: (id: number) => api.delete(`/departments/${id}`),
  /** 设置部门节点所在省份（空串 = 恢复为继承上级） */
  setProvince: (id: number, province: string) =>
    api.patch<{ ok: boolean; id: number; province: string }>(`/departments/${id}`, { province }),
};

export const matchApi = {
  /** 手动重跑匹配引擎（覆盖 match_results）；日常无需调用，结果已在上传/建岗时预计算 */
  run: (jobId: number) =>
    api.post<{ total_after_hard_filter: number; candidates: MatchResult[] }>(`/match/run/${jobId}`),
  /** 按岗位查询匹配结果（只读 match_results，不触发计算） */
  list: (jobId: number) => api.get<MatchResult[]>(`/match/${jobId}`),
  /** 按简历反向查询匹配结果 */
  listByResume: (resumeId: number) => api.get<MatchResult[]>(`/match/resume/${resumeId}`),
  push: (matchIds: number[]) => api.post("/match/push", { match_ids: matchIds }),
  feedback: (matchId: number, result: "selected" | "rejected") =>
    api.post("/match/feedback", { match_id: matchId, result }),
  exportUrl: (jobId: number, withFiles = true) =>
    `${API_BASE}/match/${jobId}/export?with_files=${withFiles}`,
};
