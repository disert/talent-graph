import {
  ApartmentOutlined, DownloadOutlined, EnvironmentOutlined, ReloadOutlined, UploadOutlined,
} from "@ant-design/icons";
import {
  Alert, Button, Card, Cascader, Col, Descriptions, Form, Input, List, Modal,
  Popconfirm, Progress, Row, Select, Space, Statistic, Tag, Tree, Typography, message,
} from "antd";
import type { ChangeEvent, ReactNode } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  departmentApi, jobApi,
  type DepartmentImportResult, type DepartmentNode, type Job,
} from "../api/client";
import PieChart from "../components/PieChart";
import { useJobImport } from "../stores/jobImport";

const EXAMPLE = `例：我们需要一位电力系统自动化方向的博士，35 岁以下，研究方向偏新能源并网或储能调度，
熟悉 PSCAD/MATLAB 仿真，有电网调度或设计院经验优先，能到深圳全职工作。`;

const PROVINCES = [
  "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
  "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
  "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海",
  "内蒙古", "广西", "西藏", "宁夏", "新疆", "香港", "澳门", "台湾",
];

// ---- 组织架构（需求部门）管理：树形转换 / 节点统计 ----
type AdminTreeNode = {
  key: string;
  title: string;
  rawId?: number;
  isLeaf: boolean;
  province: string;          // 节点自身省份（空 = 继承上级）
  effectProvince: string;    // 生效省份（已含继承）
  children?: AdminTreeNode[];
};

function toAdminTree(nodes: DepartmentNode[]): AdminTreeNode[] {
  return (nodes || []).map((n) => ({
    key: `d-${n.id ?? n.value}`,
    title: n.label,
    rawId: n.id,
    isLeaf: !(n.children && n.children.length > 0),
    province: n.province || "",
    effectProvince: n.effect_province || n.province || "",
    children: n.children?.length ? toAdminTree(n.children) : undefined,
  }));
}

/** 按级联路径取部门所在省份（节点已带生效省份，取路径最深的一级即可） */
function provinceOfPath(nodes: DepartmentNode[], path: string[]): string {
  let level: DepartmentNode[] = nodes || [];
  let province = "";
  for (const seg of path) {
    const node = level.find((n) => n.value === seg);
    if (!node) break;
    if (node.effect_province) province = node.effect_province;
    level = node.children || [];
  }
  return province;
}

/** 岗位所需专业：优先显式录入，其次取 AI 从需求描述中抽取的 hard_conditions.majors */
function jobMajors(job: Job): string[] {
  const raw: unknown[] = job.majors?.length ? job.majors : (job.hard_conditions?.majors ?? []);
  return raw.map((m) => String(m).trim()).filter(Boolean);
}

function countNodes(nodes?: DepartmentNode[]): number {
  return (nodes || []).reduce((s, n) => s + 1 + countNodes(n.children), 0);
}

function DepartmentAdminCard({ tree, onChanged }: { tree: DepartmentNode[]; onChanged: () => void }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<DepartmentImportResult | null>(null);
  const total = useMemo(() => countNodes(tree), [tree]);
  const treeData = useMemo(() => toAdminTree(tree), [tree]);
  // 省份维护：点击节点上的省份标签弹出选择框
  const [editing, setEditing] = useState<{ id: number; name: string; province: string } | null>(null);
  const [provinceInput, setProvinceInput] = useState<string | undefined>(undefined);
  const [savingProvince, setSavingProvince] = useState(false);

  const openProvinceEditor = (n: AdminTreeNode) => {
    if (n.rawId == null) return;
    setEditing({ id: n.rawId, name: n.title, province: n.province });
    setProvinceInput(n.province || undefined);
  };

  const saveProvince = async () => {
    if (!editing) return;
    setSavingProvince(true);
    try {
      await departmentApi.setProvince(editing.id, provinceInput || "");
      message.success(provinceInput
        ? `「${editing.name}」省份已设为 ${provinceInput}`
        : `「${editing.name}」已改为继承上级省份`);
      setEditing(null);
      onChanged();
    } catch (err: any) {
      message.error(err?.response?.data?.detail || "省份保存失败");
    } finally {
      setSavingProvince(false);
    }
  };

  const onPickImportFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const ext = file.name.split(".").pop()?.toLowerCase();
    if (!ext || !["xlsx", "xlsm"].includes(ext)) {
      message.warning("仅支持 .xlsx 格式，请先下载模板填写");
      return;
    }
    setImporting(true);
    try {
      const r = await departmentApi.importExcel(file);
      setImportResult(r.data);
      onChanged();
      if (r.data.failed === 0) {
        message.success(`组织架构导入成功 ${r.data.succeeded} 个节点`);
      } else {
        message.warning(`导入完成：成功 ${r.data.succeeded} 个，失败 ${r.data.failed} 个，详见明细`);
      }
    } catch (err: any) {
      message.error(err?.response?.data?.detail || "导入失败，请检查文件格式");
    } finally {
      setImporting(false);
    }
  };

  const resetDefault = () => {
    Modal.confirm({
      title: "恢复默认示例组织架构",
      content: "将清空当前全部部门节点并恢复为内置默认示例树，确定继续？",
      okText: "恢复",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        await departmentApi.reset();
        message.success("已恢复默认示例组织架构");
        onChanged();
      },
    });
  };

  const deleteNode = async (node: AdminTreeNode) => {
    if (node.rawId == null) return;
    try {
      await departmentApi.remove(node.rawId);
      message.success("已删除部门节点");
      onChanged();
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "删除失败");
    }
  };

  return (
    <Card
      size="small"
      title={
        <Space>
          <ApartmentOutlined /> 组织架构（需求部门）管理
        </Space>
      }
      extra={<Statistic title="部门节点总数" value={total} valueStyle={{ fontSize: 18 }} />}
    >
      <Space direction="vertical" style={{ width: "100%" }} size={12}>
        <Alert
          type="info"
          showIcon
          message="部门层级保存于后端数据库 departments 表，供上方「需求部门」级联选择。省份直接维护在组织架构上：点击右侧省份标签即可修改，子部门留空则自动继承上级，因此录入岗位时不再需要单独选择省份。Excel 批量导入：每行填一个节点的完整路径（如：集团总部/人力资源部）及可选省份，父级不存在会自动创建，重复导入不重复建。"
        />
        <Space wrap>
          <Button size="small" icon={<DownloadOutlined />} href={departmentApi.importTemplateUrl} target="_blank">
            下载导入模板
          </Button>
          <Button size="small" type="dashed" icon={<UploadOutlined />} loading={importing} onClick={() => fileRef.current?.click()}>
            导入 Excel
          </Button>
          <Button size="small" icon={<ReloadOutlined />} onClick={resetDefault}>
            恢复默认示例
          </Button>
        </Space>
        <input ref={fileRef} type="file" accept=".xlsx,.xlsm" style={{ display: "none" }} onChange={onPickImportFile} />
        <div style={{ maxHeight: 320, overflow: "auto", border: "1px solid #f0f0f0", borderRadius: 6, padding: 8 }}>
          {treeData.length === 0 ? (
            <Typography.Text type="secondary">暂无部门数据，请导入 Excel 或点击「恢复默认示例」。</Typography.Text>
          ) : (
            <Tree
              showLine
              defaultExpandAll
              treeData={treeData}
              titleRender={(node) => {
                const n = node as unknown as AdminTreeNode;
                return (
                  <Space size="small">
                    <span>{n.title as ReactNode}</span>
                    <Tag
                      color={n.effectProvince ? "blue" : "default"}
                      style={{ cursor: n.rawId != null ? "pointer" : "default", marginInlineEnd: 0 }}
                      onClick={() => openProvinceEditor(n)}
                      title="点击设置 / 修改该部门所在省份（留空=继承上级）"
                    >
                      {n.effectProvince
                        ? `${n.effectProvince}${n.province ? "" : "·继承"}`
                        : "未设省份"}
                    </Tag>
                    {n.isLeaf && n.rawId != null && (
                      <Popconfirm
                        title="删除该部门节点？"
                        okText="删除"
                        okButtonProps={{ danger: true }}
                        cancelText="取消"
                        onConfirm={() => deleteNode(n)}
                      >
                        <Typography.Text type="danger" style={{ fontSize: 12 }}>
                          删除
                        </Typography.Text>
                      </Popconfirm>
                    )}
                  </Space>
                );
              }}
            />
          )}
        </div>
      </Space>
      <Modal
        open={importResult !== null}
        onCancel={() => setImportResult(null)}
        footer={<Button type="primary" onClick={() => setImportResult(null)}>知道了</Button>}
        title="组织架构 Excel 导入结果"
        width={520}
      >
        {importResult && (
          <>
            <Space size="large" style={{ marginBottom: 16 }}>
              <Statistic title="总行数" value={importResult.total} />
              <Statistic title="成功" value={importResult.succeeded} valueStyle={{ color: "#52c41a" }} />
              <Statistic title="失败" value={importResult.failed} valueStyle={{ color: importResult.failed ? "#ff4d4f" : undefined }} />
            </Space>
            {importResult.failed > 0 && (
              <List
                size="small"
                header="失败明细（其余行已成功导入）"
                dataSource={importResult.errors}
                renderItem={(e) => (
                  <List.Item>
                    <span>第 {e.row} 行：{e.message}</span>
                  </List.Item>
                )}
              />
            )}
          </>
        )}
      </Modal>
      <Modal
        open={editing !== null}
        onCancel={() => setEditing(null)}
        title="设置部门所在省份"
        width={420}
        okText="保存"
        cancelText="取消"
        confirmLoading={savingProvince}
        onOk={saveProvince}
      >
        {editing && (
          <Space direction="vertical" style={{ width: "100%" }} size={12}>
            <Typography.Text>
              部门：<Typography.Text strong>{editing.name}</Typography.Text>
            </Typography.Text>
            <Select
              style={{ width: "100%" }}
              showSearch
              allowClear
              placeholder="选择省份（留空 = 继承上级省份）"
              value={provinceInput}
              onChange={(v) => setProvinceInput(v)}
              options={PROVINCES.map((p) => ({ value: p, label: p }))}
              optionFilterProp="label"
            />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              该部门下的子部门留空时会自动继承本省份；岗位录入时省份由所选部门自动带出。
            </Typography.Text>
          </Space>
        )}
      </Modal>
    </Card>
  );
}

interface FormValues {
  title: string;
  department?: string[];
  majors?: string[];
  raw_text: string;
}

export default function JobRequestPage() {
  const [form] = Form.useForm<FormValues>();
  const [jobs, setJobs] = useState<Job[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [deptTree, setDeptTree] = useState<DepartmentNode[]>([]);
  const [importing, setImporting] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  // 批量导入进度放在全局 store：切走/切回页面不丢进度，仍可查看与中止
  const importTask = useJobImport((s) => s.task);

  // 需求部门 -> 省份：省份维护在组织架构上，表单不再单独选择
  const selectedDept = Form.useWatch("department", form) as string[] | undefined;
  const derivedProvince = useMemo(
    () => provinceOfPath(deptTree, selectedDept || []),
    [deptTree, selectedDept],
  );

  const refresh = () => jobApi.list().then((r) => setJobs(r.data));
  const loadDeptTree = () =>
    jobApi
      .departments()
      .then((r) => setDeptTree(r.data))
      .catch((e) => message.error(e?.response?.data?.detail || "部门级联数据加载失败"));

  useEffect(() => {
    refresh();
    loadDeptTree();
  }, []);

  const onFinish = async (values: FormValues) => {
    setSubmitting(true);
    try {
      const path = Array.isArray(values.department) ? values.department : [];
      await jobApi.create({
        title: values.title,
        department: path.join("/"),
        department_path: path,
        // 省份留空：由后端按部门在组织架构中的省份推导，避免前端缓存过期造成不一致
        province: "",
        majors: values.majors || [],
        raw_text: values.raw_text,
      });
      message.success(derivedProvince
        ? `岗位需求已结构化并入库（省份：${derivedProvince}）`
        : "岗位需求已结构化并入库");
      form.resetFields();
      refresh();
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "提交失败，请检查内部大模型平台连接");
    } finally {
      setSubmitting(false);
    }
  };

  // ---- Excel 批量导入：先解析建会话，store 内逐行入库（进度条 + 异常明细，跨页面保持） ----
  const triggerImport = () => fileRef.current?.click();

  // 导入在后台 store 循环执行，这里监听其"运行中 -> 结束/中止"的转换：
  // 结束后续刷岗位列表并给出汇总提示；切回本页时已有挂载时的初始化刷新兜底。
  const prevImportRunning = useRef<boolean | undefined>(undefined);
  useEffect(() => {
    const t = importTask;
    if (!t) return;
    if (prevImportRunning.current === true && !t.running) {
      if (t.failed === 0) {
        message.success(`批量导入完成：${t.succeeded} 条全部成功`);
      } else {
        message.warning(`导入完成：成功 ${t.succeeded} 条，失败 ${t.failed} 条，详见异常明细`);
      }
      refresh();
    }
    prevImportRunning.current = t.running;
  }, [importTask]);

  const onPickImportFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // 允许重复选择同一文件
    if (!file) return;
    const ext = file.name.split(".").pop()?.toLowerCase();
    if (!ext || !["xlsx", "xlsm"].includes(ext)) {
      message.warning("仅支持 .xlsx 格式，请先下载模板填写");
      return;
    }
    setImporting(true);
    try {
      const r = await jobApi.importPrepare(file); // 只解析不入库：先拿到总行数与预检异常
      useJobImport.getState().start(r.data);
    } catch (err: any) {
      message.error(err?.response?.data?.detail || "导入失败，请检查文件格式");
    } finally {
      setImporting(false);
    }
  };

  const closeImportModal = () => {
    if (importTask?.running) {
      useJobImport.getState().stop();
      message.info("已停止剩余行的导入，已入库的岗位不受影响");
      return;
    }
    useJobImport.getState().dismiss();
  };

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
      <DepartmentAdminCard tree={deptTree} onChanged={loadDeptTree} />
      <Card
        title="岗位需求录入（自然语言描述，AI 自动结构化为硬性 + 择优条件）"
        extra={
          <Space>
            <Button
              icon={<DownloadOutlined />}
              href={jobApi.importTemplateUrl}
              target="_blank"
              size="small"
            >
              下载导入模板
            </Button>
            <Button
              icon={<UploadOutlined />}
              size="small"
              type="dashed"
              loading={importing}
              onClick={triggerImport}
            >
              导入 Excel
            </Button>
          </Space>
        }
      >
        <input
          ref={fileRef}
          type="file"
          accept=".xlsx,.xlsm"
          style={{ display: "none" }}
          onChange={onPickImportFile}
        />
        <Form form={form} layout="vertical" onFinish={onFinish}>
          <Space direction="vertical" style={{ width: "100%" }} size={0}>
            <Form.Item name="title" label="岗位名称" rules={[{ required: true, message: "请填写岗位名称" }]}>
              <Input placeholder="如：新能源并网高级工程师" />
            </Form.Item>
            <Form.Item name="department" label="需求部门（多层级级联选择，省份随部门自动带出）">
              <Cascader
                options={deptTree}
                placeholder="选择组织层级部门（如：集团总部 / 人力资源部 / 人才引进处）"
                changeOnSelect={false}
                expandTrigger="hover"
                allowClear
                style={{ width: "100%" }}
              />
            </Form.Item>
            <div style={{ marginTop: -12, marginBottom: 16 }}>
              <Typography.Text type="secondary">岗位省份：</Typography.Text>{" "}
              {derivedProvince ? (
                <Tag color="blue" icon={<EnvironmentOutlined />}>{derivedProvince}</Tag>
              ) : (
                <Tag color="orange">未带出省份，请在组织架构管理中为该部门设置省份</Tag>
              )}
            </div>
            <Form.Item name="majors" label="岗位所需专业（可多个）">
              <Select
                mode="tags"
                placeholder="输入专业后回车（如：电气工程）"
                tokenSeparators={["、", ",", "，", " "]}
                open={false}
                suffixIcon={null}
              />
            </Form.Item>
            <Form.Item
              name="raw_text"
              label="需求描述（把和业务部门对话访谈的内容粘贴在这里）"
              rules={[{ required: true, message: "请填写需求描述" }]}
            >
              <Input.TextArea rows={5} placeholder={EXAMPLE} />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={submitting}>
              结构化并入库
            </Button>
          </Space>
        </Form>
      </Card>

      <Card title="已录入岗位">
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
          dataSource={jobs}
          locale={{ emptyText: "暂无岗位，请在上方录入或通过 Excel 批量导入" }}
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

      <Modal
        open={importTask !== null}
        onCancel={closeImportModal}
        title="Excel 批量导入"
        width={680}
        maskClosable={!importTask?.running}
        footer={
          importTask?.running ? (
            <Button danger onClick={closeImportModal}>停止剩余导入</Button>
          ) : (
            <Button type="primary" onClick={closeImportModal}>知道了</Button>
          )
        }
      >
        {importTask && (
          <>
            <Space size="large" style={{ marginBottom: 12 }} wrap>
              <Statistic title="总行数" value={importTask.total} />
              <Statistic title="已处理" value={importTask.done} />
              <Statistic title="成功" value={importTask.succeeded} valueStyle={{ color: "#52c41a" }} />
              <Statistic title="失败" value={importTask.failed}
                         valueStyle={{ color: importTask.failed ? "#ff4d4f" : undefined }} />
            </Space>
            <Progress
              percent={importTask.total
                ? Math.round((importTask.done / importTask.total) * 100)
                : 100}
              status={importTask.running ? "active" : importTask.failed ? "exception" : "success"}
            />
            <div style={{ minHeight: 22, margin: "6px 0 10px", fontSize: 12, color: "#8c8c8c" }}>
              {importTask.running
                ? `正在 AI 结构化并入库：${importTask.current}（每行一次大模型调用，请稍候）`
                : importTask.finished
                  ? `导入完成：成功 ${importTask.succeeded} 条，失败 ${importTask.failed} 条`
                  : ""}
            </div>
            {importTask.preInvalid > 0 && (
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 10 }}
                message={`预检发现 ${importTask.preInvalid} 行数据不完整（缺「岗位名称」或「需求描述」），这些行会失败并记入下方异常明细。`}
              />
            )}
            {importTask.errors.length > 0 && (
              <List
                size="small"
                header={`异常数据（${importTask.errors.length} 条，其余行已成功入库）`}
                dataSource={importTask.errors}
                style={{ maxHeight: 240, overflow: "auto" }}
                renderItem={(e) => (
                  <List.Item>
                    <span>
                      第 {e.row} 行{e.title ? `「${e.title}」` : ""}：{e.message}
                    </span>
                  </List.Item>
                )}
              />
            )}
            {importTask.finished && importTask.errors.length === 0 && (
              <Alert type="success" showIcon message={`全部 ${importTask.total} 行导入成功`} />
            )}
          </>
        )}
      </Modal>
    </Space>
  );
}
