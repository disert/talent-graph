import { Button, Card, Cascader, Drawer, Modal, Progress, Space, Table, Tag, Typography, message } from "antd";
import { useEffect, useMemo, useState } from "react";
import {
  displayName, jobApi, matchApi, resumeApi,
  type DepartmentNode, type Job, type MatchResult,
} from "../api/client";

/** 匹配结果表 match_results 的展示文案：触发来源 */
const SOURCE_MAP: Record<string, string> = {
  resume_upload: "简历上传时匹配",
  job_create: "岗位新建时匹配",
  manual: "手动重跑",
};

// ---------- 岗位选择：按需求部门层级的多层级级联 ----------
/** 级联节点 value 前缀：d: 部门节点 / j: 岗位叶节点（避免部门名与岗位 id 互相冲突） */
const DEPT_PREFIX = "d:";
const JOB_PREFIX = "j:";
/** 未填需求部门的岗位统一挂到这个虚拟根节点下 */
const UNASSIGNED_KEY = "__unassigned__";

interface JobCascaderOption {
  value: string;
  label: string;
  children?: JobCascaderOption[];
}

interface JobCascaderData {
  options: JobCascaderOption[];
  /** 部门路径（/ 分隔）-> 该部门及其下级部门的岗位 */
  jobsUnder: Map<string, Job[]>;
  /** 岗位 id -> 级联完整 value（用于回填选中态） */
  valuesOfJob: Map<number, string[]>;
}

/** 岗位的需求部门层级路径：优先结构化字段 department_path，回退拆分旧的 department 字符串 */
function jobDeptPath(j: Job): string[] {
  const path = (j.department_path || []).map((s) => String(s).trim()).filter(Boolean);
  if (path.length) return path;
  return (j.department || "").split("/").map((s) => s.trim()).filter(Boolean);
}

/** 同级内部门节点排前面、岗位叶节点排后面（sort 稳定，组内保持原有顺序） */
function sortOptions(nodes: JobCascaderOption[]): void {
  nodes.sort((a, b) => Number(b.value.startsWith(DEPT_PREFIX)) - Number(a.value.startsWith(DEPT_PREFIX)));
  nodes.forEach((n) => n.children && sortOptions(n.children));
}

/**
 * 组织架构树 + 岗位列表 -> 「需求部门层级 -> 岗位」级联数据。
 *
 * - 部门节点可选中（changeOnSelect=true）：选中部门 = 定位到该部门（含下级）的岗位；
 * - 岗位为叶节点：选中即确定当前分析的岗位；
 * - 旧数据里部门路径已不在组织架构中的，自动补建缺失层级，保证岗位始终能被选到。
 */
function buildJobCascader(tree: DepartmentNode[], jobs: Job[]): JobCascaderData {
  const options: JobCascaderOption[] = [];
  const index = new Map<string, JobCascaderOption>();

  const buildDept = (nodes: DepartmentNode[] | undefined, prefix: string[]): JobCascaderOption[] =>
    (nodes || []).map((n) => {
      const path = [...prefix, n.label];
      const node: JobCascaderOption = {
        value: DEPT_PREFIX + path.join("/"),
        label: n.label,
        children: buildDept(n.children, path),
      };
      index.set(path.join("/"), node);
      return node;
    });
  options.push(...buildDept(tree, []));

  /** 按路径逐级查找部门节点，缺失的层级即时补建，返回最深层节点 */
  const ensureDept = (path: string[]): JobCascaderOption => {
    let siblings = options;
    let acc: string[] = [];
    let node: JobCascaderOption | undefined;
    for (const name of path) {
      acc = [...acc, name];
      const key = acc.join("/");
      let found = index.get(key);
      if (!found) {
        found = { value: DEPT_PREFIX + key, label: name, children: [] };
        index.set(key, found);
        siblings.push(found);
      }
      found.children = found.children || [];
      node = found;
      siblings = found.children;
    }
    return node as JobCascaderOption;
  };

  let unassigned: JobCascaderOption | undefined;
  const unassignedNode = (): JobCascaderOption => {
    if (!unassigned) {
      unassigned = { value: DEPT_PREFIX + UNASSIGNED_KEY, label: "（未填需求部门）", children: [] };
      options.push(unassigned);
    }
    return unassigned;
  };

  const jobsUnder = new Map<string, Job[]>();
  const valuesOfJob = new Map<number, string[]>();
  /** 同一岗位按每个上级部门路径登记一次，便于「选中部门」时统计其下岗位数 */
  const register = (deptKey: string, job: Job) => {
    const list = jobsUnder.get(deptKey);
    if (list) list.push(job);
    else jobsUnder.set(deptKey, [job]);
  };

  for (const job of jobs) {
    const deptPath = jobDeptPath(job);
    const leaf: JobCascaderOption = { value: JOB_PREFIX + job.id, label: job.title };
    const values: string[] = [];

    if (deptPath.length === 0) {
      const node = unassignedNode();
      (node.children = node.children || []).push(leaf);
      values.push(node.value);
      register(UNASSIGNED_KEY, job);
    } else {
      const node = ensureDept(deptPath);
      (node.children = node.children || []).push(leaf);
      let acc: string[] = [];
      for (const name of deptPath) {
        acc = [...acc, name];
        values.push(DEPT_PREFIX + acc.join("/"));
        register(acc.join("/"), job);
      }
    }

    values.push(leaf.value);
    valuesOfJob.set(job.id, values);
  }

  // 部门节点标签补上「岗位数量（含下级部门）」，方便在展开前就看清该层级的岗位规模
  for (const [key, node] of index) {
    node.label = `${node.label}（${jobsUnder.get(key)?.length ?? 0}）`;
  }
  if (unassigned) {
    unassigned.label = `${unassigned.label}（${jobsUnder.get(UNASSIGNED_KEY)?.length ?? 0}）`;
  }

  sortOptions(options);
  return { options, jobsUnder, valuesOfJob };
}

export default function MatchResultPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [deptTree, setDeptTree] = useState<DepartmentNode[]>([]);
  const [jobId, setJobId] = useState<number>();
  /** 级联选择器受控值：由「部门节点 value」逐级拼到「岗位叶节点 value」 */
  const [cascadeValue, setCascadeValue] = useState<string[]>([]);
  const [matches, setMatches] = useState<MatchResult[]>([]);
  const [running, setRunning] = useState(false);
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([]);
  const [rawText, setRawText] = useState<string>();
  const [reasonDetail, setReasonDetail] = useState<MatchResult>();
  /** resume_id -> 解析姓名：匹配记录里的 resume_name 是库字段快照（可能是文件名），姓名展示统一以 structured.name 为准 */
  const [nameMap, setNameMap] = useState<Record<number, string>>({});

  const loadNames = () =>
    resumeApi.list({ limit: 500 }).then((r) =>
      setNameMap(Object.fromEntries(r.data.map((x) => [x.id, displayName(x)])))
    );
  const nameOf = (m: MatchResult) => nameMap[m.resume_id] || m.resume_name;

  useEffect(() => {
    jobApi.list().then((r) => setJobs(r.data));
    jobApi.departments().then((r) => setDeptTree(r.data)); // 需求部门层级：岗位级联选择的数据源
    loadNames();
  }, []);

  const cascader = useMemo(() => buildJobCascader(deptTree, jobs), [deptTree, jobs]);
  const currentJob = useMemo(() => jobs.find((j) => j.id === jobId), [jobs, jobId]);

  const refresh = (id: number) => matchApi.list(id).then((r) => setMatches(r.data));

  /** 确定当前分析岗位：刷新匹配结果并清掉上一岗位的勾选 */
  const selectJob = (id: number) => {
    setJobId(id);
    setSelectedRowKeys([]);
    refresh(id);
  };

  /** 级联选择回调：路径末级是岗位则直接选定；停在部门层级则提示该部门（含下级）的岗位 */
  const pickFromCascade = (value: string[]) => {
    const last = value.length ? String(value[value.length - 1]) : "";
    setCascadeValue(value);

    if (last.startsWith(JOB_PREFIX)) {
      selectJob(Number(last.slice(JOB_PREFIX.length)));
      return;
    }

    // 停在部门层级（或清空）：清空结果，避免表格与当前选择不一致
    setJobId(undefined);
    setMatches([]);
    setSelectedRowKeys([]);
    if (!last.startsWith(DEPT_PREFIX)) return;

    const under = cascader.jobsUnder.get(last.slice(DEPT_PREFIX.length)) || [];
    if (under.length === 0) {
      message.warning("该部门（含下级）暂无岗位");
    } else if (under.length === 1) {
      message.success(`该部门（含下级）仅 1 个岗位，已自动选中：${under[0].title}`);
      setCascadeValue(cascader.valuesOfJob.get(under[0].id) || value);
      selectJob(under[0].id);
    } else {
      message.info(`该部门（含下级）共 ${under.length} 个岗位，请继续展开选择具体岗位`);
    }
  };

  const run = async () => {
    if (!jobId) return message.warning("请先按需求部门层级选择岗位");
    setRunning(true);
    try {
      const r = await matchApi.run(jobId);
      setMatches(r.data.candidates);
      loadNames(); // 匹配前可能刚修正过姓名，同步最新映射
      message.success(`硬性过滤通过 ${r.data.total_after_hard_filter} 份，输出 Top ${r.data.candidates.length} 候选`);
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "匹配执行失败");
    } finally {
      setRunning(false);
    }
  };

  const push = async () => {
    if (selectedRowKeys.length === 0) return message.warning("请先勾选要推送的候选人");
    await matchApi.push(selectedRowKeys);
    message.success(`已推送 ${selectedRowKeys.length} 人，简历状态更新为「已推送」`);
    setSelectedRowKeys([]);
    if (jobId) refresh(jobId);
  };

  const columns = [
    { title: "姓名", width: 100, render: (_: unknown, r: MatchResult) => nameOf(r) },
    {
      title: "综合分",
      dataIndex: "score",
      width: 130,
      sorter: (a: MatchResult, b: MatchResult) => a.score - b.score,
      render: (v: number) => <Progress percent={v} size="small" status={v >= 80 ? "success" : "normal"} />,
    },
    { title: "向量相似度", dataIndex: "vector_score", width: 100, render: (v: number) => v.toFixed(3) },
    {
      title: "匹配理由（可追溯）",
      dataIndex: "reason",
      ellipsis: true,
      render: (v: string, r: MatchResult) => <a onClick={() => setReasonDetail(r)}>{v || "（无）"}</a>,
    },
    {
      title: "结果来源",
      dataIndex: "match_source",
      width: 120,
      render: (v: string) => SOURCE_MAP[v] || v || "-",
    },
    {
      title: "推送状态",
      dataIndex: "push_status",
      width: 100,
      render: (v: string) => {
        const map: Record<string, [string, string]> = {
          pending: ["待推送", "default"], pushed: ["已推送", "orange"],
          selected: ["有意向", "green"], rejected: ["无意向", "red"],
        };
        const [label, color] = map[v] || [v, "default"];
        return <Tag color={color}>{label}</Tag>;
      },
    },
    {
      title: "反馈（模拟单位回填）",
      width: 170,
      render: (_: unknown, r: MatchResult) =>
        r.push_status === "pushed" ? (
          <Space>
            <a onClick={() => matchApi.feedback(r.id, "selected").then(() => { if (jobId) refresh(jobId); })}>有意向</a>
            <a onClick={() => matchApi.feedback(r.id, "rejected").then(() => { if (jobId) refresh(jobId); })}>无意向</a>
          </Space>
        ) : null,
    },
  ];

  return (
    <Card title="匹配结果（简历上传 / 岗位新建时已自动匹配落库，本页仅查询结果表）">
      <Space style={{ marginBottom: 12 }} wrap>
        <Cascader
          style={{ width: 520 }}
          options={cascader.options}
          value={cascadeValue}
          onChange={pickFromCascade}
          changeOnSelect
          expandTrigger="hover"
          showSearch={{ limit: 200 }}
          allowClear
          placeholder="按需求部门层级选择岗位（括号内为该层级岗位数量，可搜索）"
        />
        <Button type="primary" onClick={run} loading={running}>
          重新匹配
        </Button>
        <Button onClick={push} disabled={selectedRowKeys.length === 0}>
          推送精选（{selectedRowKeys.length}）
        </Button>
        {jobId && (
          <>
            <Button href={matchApi.exportUrl(jobId)} target="_blank">
              导出名单 + 简历（ZIP）
            </Button>
            <Button href={matchApi.exportUrl(jobId, false)} target="_blank">
              仅导出 Excel 名单
            </Button>
          </>
        )}
      </Space>

      {currentJob && (
        <Space size={8} wrap style={{ marginBottom: 12 }}>
          <Tag color="blue">岗位：{currentJob.title}</Tag>
          <Tag>需求部门：{currentJob.department || "未填"}</Tag>
          {currentJob.province && <Tag>省份：{currentJob.province}</Tag>}
          {currentJob.majors?.length > 0 && <Tag>专业：{currentJob.majors.join("、")}</Tag>}
        </Space>
      )}

      <Table
        rowKey="id"
        size="small"
        columns={columns}
        dataSource={matches}
        pagination={{ pageSize: 20, showSizeChanger: false }}
        rowSelection={{ selectedRowKeys, onChange: (keys) => setSelectedRowKeys(keys as number[]) }}
      />

      <Modal open={!!reasonDetail} footer={null} onCancel={() => setReasonDetail(undefined)}
        title={`匹配理由 · ${reasonDetail ? nameOf(reasonDetail) : ""}`} width={640}>
        <Typography.Paragraph>{reasonDetail?.reason}</Typography.Paragraph>
        <a onClick={() => reasonDetail && resumeApi.raw(reasonDetail.resume_id).then((r) => setRawText(r.data.raw_text))}>
          查看简历原文（溯源）
        </a>
      </Modal>

      <Drawer open={rawText !== undefined} onClose={() => setRawText(undefined)} title="简历解析原文" width={560}>
        <pre style={{ whiteSpace: "pre-wrap" }}>{rawText}</pre>
      </Drawer>
    </Card>
  );
}
