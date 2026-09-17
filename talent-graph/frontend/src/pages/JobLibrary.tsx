import {
  ApartmentOutlined, EnvironmentOutlined, PlusOutlined, ReloadOutlined,
} from "@ant-design/icons";
import {
  Button, Card, Col, DatePicker, Descriptions, Input, Popconfirm, Row, Select,
  Space, Statistic, Table, Tag, Typography, message,
} from "antd";
import type { Dayjs } from "dayjs";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { jobApi, type Job } from "../api/client";
import PieChart from "../components/PieChart";
import { jobMajors } from "./jobShared";

/** 省份筛选中「未填写」项的内部值（不能用空串，否则 Select 会当成未选中） */
const NO_PROVINCE = "__none__";

/** 条件对象转展示文本（空对象不展示 null） */
const fmtConditions = (v: Record<string, any> | undefined) =>
  v && Object.keys(v).length ? JSON.stringify(v, null, 2) : "—";

/** 看板卡片统一样式：等高 + 图表在卡片内垂直居中 */
const DASH_CARD_STYLE = { height: "100%", display: "flex", flexDirection: "column" } as const;
const DASH_CARD_BODY = {
  flex: 1, display: "flex", alignItems: "center", justifyContent: "center",
} as const;

/**
 * 已录入岗位页：多条件搜索（关键词 / 省份 / 部门 / 专业 / 录入时间）+ 在招岗位总量
 * + 省份/专业分布看板 + 岗位明细表格。
 * 饼图与表格共用同一份筛选结果，保证数字与明细口径一致。
 * 新增岗位与 Excel 导入在「岗位录入」页（`/jobs/create`）。
 */
export default function JobLibraryPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(false);
  // keywordInput 为输入框即时值，keyword 为回车/点击搜索后的生效值
  const [keywordInput, setKeywordInput] = useState("");
  const [keyword, setKeyword] = useState("");
  // ---- 其余筛选项 ----
  const [province, setProvince] = useState<string>();
  const [department, setDepartment] = useState<string>();
  const [majorsFilter, setMajorsFilter] = useState<string[]>([]);
  const [dateRange, setDateRange] = useState<[Dayjs, Dayjs] | null>(null);

  const refresh = () => {
    setLoading(true);
    return jobApi
      .list()
      .then((r) => setJobs(r.data))
      .catch((e) => message.error(e?.response?.data?.detail || "岗位列表加载失败"))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    refresh();
  }, []);

  /** 多条件筛选：省份 + 需求部门 + 所需专业（任一命中）+ 录入时间范围 + 关键词，全部满足才保留 */
  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return jobs.filter((job) => {
      // 省份（含「未填写」枚举）
      if (province) {
        const p = (job.province || "").trim();
        if (province === NO_PROVINCE ? p !== "" : p !== province) return false;
      }
      // 需求部门（精确匹配）
      if (department && (job.department || "") !== department) return false;
      // 所需专业：命中所选专业中任意一个即保留
      if (majorsFilter.length) {
        const majors = jobMajors(job);
        if (!majorsFilter.some((m) => majors.includes(m))) return false;
      }
      // 录入时间（含首尾两天）
      if (dateRange) {
        const t = new Date(job.created_at).getTime();
        if (!Number.isFinite(t)) return false;
        if (t < dateRange[0].startOf("day").valueOf() || t > dateRange[1].endOf("day").valueOf()) {
          return false;
        }
      }
      // 关键词：岗位名称 / 部门 / 省份 / 专业 / 需求原文
      if (kw) {
        const haystack = [job.title, job.department, job.province, jobMajors(job).join(" "), job.raw_text];
        if (!haystack.some((v) => (v || "").toLowerCase().includes(kw))) return false;
      }
      return true;
    });
  }, [jobs, keyword, province, department, majorsFilter, dateRange]);

  /** 是否存在生效的筛选条件（控制「筛选结果」统计与重置按钮） */
  const hasFilter = !!(keyword || province || department || majorsFilter.length || dateRange);

  const resetFilters = () => {
    setKeywordInput("");
    setKeyword("");
    setProvince(undefined);
    setDepartment(undefined);
    setMajorsFilter([]);
    setDateRange(null);
  };

  // ---- 筛选项候选值：取自全量岗位（不受当前筛选影响，避免选中一个条件后其他选项消失）----
  const toOptions = (values: string[]) => values.map((v) => ({ value: v, label: v }));
  const provinceOptions = useMemo(() => {
    const values = Array.from(
      new Set(jobs.map((j) => (j.province || "").trim()).filter(Boolean))
    ).sort();
    const opts = toOptions(values);
    // 存在未填省份的岗位时，补一个「未填写」选项便于排查脏数据
    if (jobs.some((j) => !(j.province || "").trim())) {
      opts.push({ value: NO_PROVINCE, label: "未填写" });
    }
    return opts;
  }, [jobs]);
  const departmentOptions = useMemo(
    () => toOptions(Array.from(new Set(jobs.map((j) => j.department).filter(Boolean))).sort()),
    [jobs]
  );
  const majorOptions = useMemo(
    () => toOptions(Array.from(new Set(jobs.flatMap((j) => jobMajors(j)))).sort()),
    [jobs]
  );

  // ---- 看板统计（饼图口径）：按省份 / 按专业，统计范围 = 当前筛选结果 ----
  const provinceDist = useMemo(() => {
    const map: Record<string, number> = {};
    filtered.forEach((j) => {
      const p = (j.province || "").trim() || "未填省份";
      map[p] = (map[p] || 0) + 1;
    });
    return Object.entries(map)
      .map(([name, value]) => ({ name, value }))
      .sort((a, b) => b.value - a.value);
  }, [filtered]);

  const majorDist = useMemo(() => {
    const map: Record<string, number> = {};
    filtered.forEach((j) => {
      const unique = Array.from(new Set(jobMajors(j)));
      if (!unique.length) {
        map["未填专业"] = (map["未填专业"] || 0) + 1; // 由 AI 从描述中抽取，可能为空
        return;
      }
      unique.forEach((m) => { map[m] = (map[m] || 0) + 1; });
    });
    return Object.entries(map)
      .map(([name, value]) => ({ name, value }))
      .sort((a, b) => b.value - a.value);
  }, [filtered]);

  /** 表格列：省份 / 专业用 Tag 展示，条件类长文本放在展开行 */
  const columns = [
    {
      title: "岗位名称", dataIndex: "title", width: 220, ellipsis: true,
      render: (v: string) => <Typography.Text strong>{v}</Typography.Text>,
    },
    {
      title: "需求部门", dataIndex: "department", width: 180, ellipsis: true,
      render: (v: string) => v || "-",
    },
    {
      title: "省份", dataIndex: "province", width: 120,
      render: (v: string) =>
        v ? <Tag color="blue" icon={<EnvironmentOutlined />}>{v}</Tag> : <Tag>未填省份</Tag>,
    },
    {
      title: "所需专业", width: 300,
      render: (_: unknown, job: Job) => {
        const majors = jobMajors(job);
        if (!majors.length) {
          return <Typography.Text type="secondary">待 AI 抽取</Typography.Text>;
        }
        const head = majors.slice(0, 3);
        return (
          <Space size={[0, 4]} wrap>
            {head.map((m, i) => <Tag key={i}>{m}</Tag>)}
            {majors.length > head.length && <Tag>+{majors.length - head.length}</Tag>}
          </Space>
        );
      },
    },
    {
      title: "录入时间", dataIndex: "created_at", width: 180,
      render: (v: string) => (v ? new Date(v).toLocaleString() : "-"),
    },
    {
      title: "操作", width: 80, fixed: "right" as const,
      render: (_: unknown, job: Job) => (
        <Popconfirm
          title="删除该岗位及其匹配记录？"
          okText="删除"
          okButtonProps={{ danger: true }}
          cancelText="取消"
          onConfirm={() => jobApi.remove(job.id).then(refresh)}
        >
          <a style={{ color: "#ff4d4f" }}>删除</a>
        </Popconfirm>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card
        title="已录入岗位"
        extra={
          <Space>
            <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={refresh}>
              刷新
            </Button>
            <Link to="/jobs/create">
              <Button size="small" type="primary" icon={<PlusOutlined />}>岗位录入</Button>
            </Link>
          </Space>
        }
      >
        {jobs.length > 0 && (
          <>
            <Space style={{ marginBottom: 16 }} wrap size={8}>
              <Input.Search
                placeholder="岗位名称 / 部门 / 省份 / 专业 / 需求原文"
                style={{ width: 320 }}
                value={keywordInput}
                onChange={(e) => {
                  setKeywordInput(e.target.value);
                  if (!e.target.value) setKeyword(""); // 清空时立即恢复全量
                }}
                onSearch={(v) => setKeyword(v.trim())}
                allowClear
              />
              <Select
                placeholder="省份" allowClear showSearch style={{ width: 140 }}
                options={provinceOptions} value={province} onChange={setProvince}
              />
              <Select
                placeholder="需求部门" allowClear showSearch optionFilterProp="label"
                style={{ minWidth: 200 }} options={departmentOptions}
                value={department} onChange={setDepartment}
              />
              <Select
                placeholder="所需专业（任一命中）" mode="multiple" allowClear showSearch
                maxTagCount="responsive" optionFilterProp="label" style={{ minWidth: 240 }}
                options={majorOptions} value={majorsFilter} onChange={setMajorsFilter}
              />
              <DatePicker.RangePicker
                placeholder={["录入开始日期", "录入结束日期"]}
                value={dateRange}
                onChange={(dates) =>
                  setDateRange(dates && dates[0] && dates[1] ? [dates[0], dates[1]] : null)
                }
              />
              {hasFilter && (
                <Button type="link" onClick={resetFilters}>重置筛选</Button>
              )}
              <span style={{ color: "#999" }}>
                共 {filtered.length} 个岗位{hasFilter ? "（已按条件筛选）" : ""}
              </span>
            </Space>

            <Statistic
              title={hasFilter ? `筛选后岗位数（全库 ${jobs.length} 个）` : "在招岗位总数"}
              value={filtered.length}
            />
            {/* align="stretch" + 卡片 flex 布局：两张饼图卡片等高，图在卡片内垂直居中 */}
            <Row gutter={[16, 16]} align="stretch" style={{ marginTop: 16 }}>
              <Col xs={24} lg={12}>
                <Card
                  size="small"
                  style={DASH_CARD_STYLE}
                  styles={{ body: DASH_CARD_BODY }}
                  title={<Space><EnvironmentOutlined />按省份分布</Space>}
                >
                  <PieChart data={provinceDist} centerLabel="岗位数" />
                </Card>
              </Col>
              <Col xs={24} lg={12}>
                <Card
                  size="small"
                  style={DASH_CARD_STYLE}
                  styles={{ body: DASH_CARD_BODY }}
                  title={<Space><ApartmentOutlined />按专业分布</Space>}
                >
                  <PieChart data={majorDist} centerLabel="专业需求数" />
                </Card>
              </Col>
            </Row>
          </>
        )}
        <Table
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={filtered}
          scroll={{ x: 1080 }}
          style={{ marginTop: jobs.length > 0 ? 16 : 0 }}
          pagination={{ pageSize: 10, showSizeChanger: true, showTotal: (t) => `共 ${t} 个岗位` }}
          locale={{
            emptyText: hasFilter ? (
              <>没有符合筛选条件的岗位，试试「重置筛选」</>
            ) : (
              <>
                暂无岗位，请到
                <Link to="/jobs/create">「岗位录入」</Link>
                新增或导入 Excel
              </>
            ),
          }}
          expandable={{
            expandedRowRender: (job: Job) => (
              <Descriptions size="small" column={1} style={{ marginBottom: 0 }}>
                <Descriptions.Item label="所需专业">
                  {jobMajors(job).join("、") || "（由 AI 从需求描述中抽取，暂未识别）"}
                </Descriptions.Item>
                <Descriptions.Item label="硬性条件">
                  <Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>
                    {fmtConditions(job.hard_conditions)}
                  </Typography.Paragraph>
                </Descriptions.Item>
                <Descriptions.Item label="择优条件">
                  <Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>
                    {fmtConditions(job.soft_conditions)}
                  </Typography.Paragraph>
                </Descriptions.Item>
                <Descriptions.Item label="需求原文">
                  <Typography.Paragraph
                    style={{ whiteSpace: "pre-wrap", marginBottom: 0, maxHeight: 240, overflow: "auto" }}
                  >
                    {job.raw_text || "—"}
                  </Typography.Paragraph>
                </Descriptions.Item>
              </Descriptions>
            ),
          }}
        />
      </Card>
    </Space>
  );
}
