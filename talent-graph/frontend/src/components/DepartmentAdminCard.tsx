import {
  ApartmentOutlined, DownloadOutlined, ReloadOutlined, UploadOutlined,
} from "@ant-design/icons";
import {
  Alert, Button, Card, List, Modal, Popconfirm, Select, Space, Statistic, Tag, Tree,
  Typography, message,
} from "antd";
import type { ChangeEvent, ReactNode } from "react";
import { useMemo, useRef, useState } from "react";
import {
  departmentApi, type DepartmentImportResult, type DepartmentNode,
} from "../api/client";

/** 省份候选（部门省份维护用） */
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

function countNodes(nodes?: DepartmentNode[]): number {
  return (nodes || []).reduce((s, n) => s + 1 + countNodes(n.children), 0);
}

/**
 * 组织架构（需求部门）维护卡片：层级树 + 省份维护 + Excel 批量导入。
 *
 * 岗位录入的「需求部门」级联与省份都来自这里，因此放在「岗位录入」页顶部。
 */
export default function DepartmentAdminCard({ tree, onChanged }: {
  tree: DepartmentNode[];
  onChanged: () => void;
}) {
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
          message="部门层级保存于后端数据库 departments 表，供下方「需求部门」级联选择。省份直接维护在组织架构上：点击右侧省份标签即可修改，子部门留空则自动继承上级，因此录入岗位时不再需要单独选择省份。Excel 批量导入：每行填一个节点的完整路径（如：集团总部/人力资源部）及可选省份，父级不存在会自动创建，重复导入不重复建。"
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
