import { DownloadOutlined, EnvironmentOutlined, UploadOutlined } from "@ant-design/icons";
import {
  Alert, Button, Card, Cascader, Form, Input, List, Modal, Progress, Select, Space,
  Statistic, Tag, Typography, message,
} from "antd";
import type { ChangeEvent } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { jobApi, type DepartmentNode, type JobImportSession } from "../api/client";
import DepartmentAdminCard from "../components/DepartmentAdminCard";
import { provinceOfPath } from "./jobShared";

const EXAMPLE = `例：我们需要一位电力系统自动化方向的博士，35 岁以下，研究方向偏新能源并网或储能调度，
熟悉 PSCAD/MATLAB 仿真，有电网调度或设计院经验优先，能到深圳全职工作。`;

interface FormValues {
  title: string;
  department?: string[];
  majors?: string[];
  raw_text: string;
}

/** 批量导入的异常明细 */
interface ImportError {
  row: number;
  title: string;
  message: string;
}

/** 批量导入的进度状态（逐行调用后端，用于进度条） */
interface ImportTask {
  total: number;
  done: number;
  succeeded: number;
  failed: number;
  current: string;
  errors: ImportError[];
  /** 预校验发现的异常行数（缺岗位名称 / 需求描述） */
  preInvalid: number;
  running: boolean;
  finished: boolean;
}

/**
 * 岗位录入页：组织架构（需求部门）维护 + 单条自然语言录入 + Excel 批量导入。
 * 已录入的岗位列表在「已录入岗位」页（`/jobs/library`）。
 */
export default function JobCreatePage() {
  const [form] = Form.useForm<FormValues>();
  const [submitting, setSubmitting] = useState(false);
  const [deptTree, setDeptTree] = useState<DepartmentNode[]>([]);
  const [importing, setImporting] = useState(false);
  const [importTask, setImportTask] = useState<ImportTask | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const cancelImportRef = useRef(false);

  // 需求部门 -> 省份：省份维护在组织架构上，表单不再单独选择
  const selectedDept = Form.useWatch("department", form) as string[] | undefined;
  const derivedProvince = useMemo(
    () => provinceOfPath(deptTree, selectedDept || []),
    [deptTree, selectedDept],
  );

  const loadDeptTree = () =>
    jobApi
      .departments()
      .then((r) => setDeptTree(r.data))
      .catch((e) => message.error(e?.response?.data?.detail || "部门级联数据加载失败"));

  useEffect(() => {
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
        ? `岗位需求已结构化并入库（省份：${derivedProvince}），可在「已录入岗位」查看`
        : "岗位需求已结构化并入库，可在「已录入岗位」查看");
      form.resetFields();
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "提交失败，请检查内部大模型平台连接");
    } finally {
      setSubmitting(false);
    }
  };

  // ---- Excel 批量导入：先解析建会话，再逐行入库（进度条 + 异常明细） ----
  const triggerImport = () => fileRef.current?.click();

  const appendImportError = (err: ImportError) =>
    setImportTask((t) => (t ? { ...t, errors: [...t.errors, err] } : t));

  const runImport = async (session: JobImportSession) => {
    let done = 0;
    let succeeded = 0;
    let failed = 0;
    for (let i = 0; i < session.items.length; i += 1) {
      if (cancelImportRef.current) break;
      const item = session.items[i];
      const label = item.title || `第 ${item.row} 行`;
      setImportTask((t) => (t ? { ...t, current: label } : t));
      try {
        const r = await jobApi.importStep(session.token, i);
        if (r.data.ok) {
          succeeded += 1;
        } else {
          failed += 1;
          appendImportError({ row: r.data.row, title: r.data.title || label,
                              message: r.data.error || "导入失败" });
        }
      } catch (err: any) {
        failed += 1;
        appendImportError({ row: item.row, title: label,
                            message: err?.response?.data?.detail || "请求失败" });
      }
      done += 1;
      setImportTask((t) => (t ? { ...t, done, succeeded, failed } : t));
    }
    setImportTask((t) => (t ? { ...t, done, succeeded, failed, current: "",
                                running: false, finished: true } : t));
    cancelImportRef.current = false;
    if (failed === 0) {
      message.success(`批量导入完成：${succeeded} 条全部成功`);
    } else {
      message.warning(`导入完成：成功 ${succeeded} 条，失败 ${failed} 条，详见异常明细`);
    }
  };

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
    cancelImportRef.current = false;
    try {
      const r = await jobApi.importPrepare(file); // 只解析不入库：先拿到总行数与预检异常
      const session = r.data;
      setImportTask({
        total: session.total, done: 0, succeeded: 0, failed: 0, current: "",
        errors: [], preInvalid: session.invalid, running: true, finished: false,
      });
      await runImport(session);
    } catch (err: any) {
      message.error(err?.response?.data?.detail || "导入失败，请检查文件格式");
      setImportTask(null);
    } finally {
      setImporting(false);
    }
  };

  const closeImportModal = () => {
    if (importTask?.running) {
      cancelImportRef.current = true;
      message.info("已停止剩余行的导入，已入库的岗位不受影响");
      return;
    }
    setImportTask(null);
  };

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
            <Link to="/jobs/library">
              <Button size="small" type="link">已录入岗位 →</Button>
            </Link>
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
            <Button type="primary" onClick={() => setImportTask(null)}>知道了</Button>
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
          </>
        )}
      </Modal>
    </Space>
  );
}
