import {
  ApartmentOutlined, EnvironmentOutlined, PlusOutlined, ReloadOutlined,
} from "@ant-design/icons";
import {
  Button, Card, Col, Descriptions, List, Popconfirm, Row, Space, Statistic, Tag, message,
} from "antd";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { jobApi, type Job } from "../api/client";
import PieChart from "../components/PieChart";
import { jobMajors } from "./jobShared";

/**
 * 已录入岗位页：在招岗位总量 + 省份/专业分布看板 + 岗位明细列表。
 * 新增岗位与 Excel 导入在「岗位录入」页（`/jobs/create`）。
 */
export default function JobLibraryPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(false);

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

  // ---- 看板统计（饼图口径）：按省份 / 按专业 ----
  const provinceDist = useMemo(() => {
    const map: Record<string, number> = {};
    jobs.forEach((j) => {
      const p = (j.province || "").trim() || "未填省份";
      map[p] = (map[p] || 0) + 1;
    });
    return Object.entries(map)
      .map(([name, value]) => ({ name, value }))
      .sort((a, b) => b.value - a.value);
  }, [jobs]);

  const majorDist = useMemo(() => {
    const map: Record<string, number> = {};
    jobs.forEach((j) => {
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
  }, [jobs]);

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
            <Statistic title="在招岗位总数" value={jobs.length} />
            <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
              <Col xs={24} lg={12}>
                <Card
                  size="small"
                  title={<Space><EnvironmentOutlined />按省份分布</Space>}
                >
                  <PieChart data={provinceDist} centerLabel="岗位数" />
                </Card>
              </Col>
              <Col xs={24} lg={12}>
                <Card
                  size="small"
                  title={<Space><ApartmentOutlined />按专业分布</Space>}
                >
                  <PieChart data={majorDist} centerLabel="专业需求数" />
                </Card>
              </Col>
            </Row>
          </>
        )}
        <List
          loading={loading}
          dataSource={jobs}
          locale={{
            emptyText: (
              <>
                暂无岗位，请到
                <Link to="/jobs/create">「岗位录入」</Link>
                新增或导入 Excel
              </>
            ),
          }}
          renderItem={(job) => {
            const dept = job.department || "";
            const majors = jobMajors(job);
            return (
              <List.Item
                actions={[
                  <Popconfirm
                    key="del"
                    title="删除该岗位及其匹配记录？"
                    onConfirm={() => jobApi.remove(job.id).then(refresh)}
                  >
                    <a style={{ color: "#ff4d4f" }}>删除</a>
                  </Popconfirm>,
                ]}
              >
                <List.Item.Meta
                  title={
                    <Space wrap>
                      {job.title}
                      {dept && (
                        <Tag color="geekblue">{dept}</Tag>
                      )}
                      {job.province ? (
                        <Tag color="blue" icon={<EnvironmentOutlined />}>
                          {job.province}
                        </Tag>
                      ) : (
                        <Tag>未填省份</Tag>
                      )}
                    </Space>
                  }
                  description={
                    <Descriptions size="small" column={1} style={{ marginTop: 8 }}>
                      <Descriptions.Item label="所需专业">
                        {majors.length ? (
                          <Space wrap size={[0, 4]}>
                            {majors.map((m, i) => (
                              <Tag key={i}>{m}</Tag>
                            ))}
                          </Space>
                        ) : (
                          "（由 AI 从需求描述中抽取）"
                        )}
                      </Descriptions.Item>
                      <Descriptions.Item label="硬性条件">
                        {JSON.stringify(job.hard_conditions)}
                      </Descriptions.Item>
                      <Descriptions.Item label="择优条件">
                        {JSON.stringify(job.soft_conditions)}
                      </Descriptions.Item>
                    </Descriptions>
                  }
                />
              </List.Item>
            );
          }}
        />
      </Card>
    </Space>
  );
}
