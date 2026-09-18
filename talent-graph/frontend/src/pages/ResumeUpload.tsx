import { InboxOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Popconfirm, Select, Space, Table, Tag, Upload, message } from "antd";
import type { UploadFile } from "antd";
import { useEffect, useState } from "react";
import { STATUS_MAP, displayName, resumeApi, uploadErrorMessage, type Resume } from "../api/client";

const SOURCE_OPTIONS = ["猎头直推", "进校园", "邮箱", "交流会"].map((v) => ({ value: v, label: v }));

const FIELD_LABELS: Record<string, string> = {
  name: "姓名", education: "学历", major: "专业", birth_date: "出生年月", age: "年龄",
  phone: "电话", email: "邮箱", research: "研究方向",
};

export default function ResumeUploadPage() {
  const [source, setSource] = useState("邮箱");
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [recent, setRecent] = useState<Resume[]>([]);

  const refresh = () => resumeApi.list({}).then((r) => setRecent(r.data.slice(0, 20)));
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000); // 轮询解析进度
    return () => clearInterval(t);
  }, []);

  /** 逐文件上传：成功一个就从待上传栏移除一个，失败的保留并提示原因。
   *  不再整批一个请求——整批模式下任何一个文件失败都会让成功的文件滞留在列表里。 */
  const doUpload = async () => {
    if (fileList.length === 0) return message.warning("请先选择文件");
    setUploading(true);
    let okCount = 0;
    const failed: string[] = [];
    for (const uf of fileList) {
      const file = uf.originFileObj as File;
      if (!file) {
        // 已从列表移除的占位项，直接清掉
        setFileList((prev) => prev.filter((p) => p.uid !== uf.uid));
        continue;
      }
      try {
        await resumeApi.upload(file, source);
        okCount += 1;
        setFileList((prev) => prev.filter((p) => p.uid !== uf.uid));
      } catch (e: any) {
        failed.push(`${uf.name}：${uploadErrorMessage(e)}`);
      }
    }
    setUploading(false);
    if (okCount > 0) message.success(`已提交 ${okCount} 份简历，后台解析中`);
    if (failed.length > 0) message.error(`以下文件上传失败：${failed.join("；")}`, 8);
    refresh();
  };

  const isParsing = (r: Resume) => !!(r.confidence || {})._parsing;
  const hasError = (r: Resume) => !!(r.confidence || {})._error;
  const lowConfFields = (r: Resume) =>
    Object.entries(r.confidence || {})
      .filter(([k, v]) => typeof v === "number" && v < 0.7)
      .map(([k]) => FIELD_LABELS[k] || k);

  const columns = [
    { title: "姓名", width: 110, render: (_: unknown, r: Resume) => displayName(r) },
    { title: "学历", width: 80, render: (_: unknown, r: Resume) => r.structured?.education || "-" },
    { title: "专业", render: (_: unknown, r: Resume) => r.structured?.major || "-" },
    {
      title: "状态",
      width: 140,
      render: (_: unknown, r: Resume) => {
        if (isParsing(r)) return <Tag color="processing">解析中…</Tag>;
        if (hasError(r)) return <Tag color="error">解析失败</Tag>;
        const s = STATUS_MAP[r.status] || { label: r.status, color: "default" };
        return <Tag color={s.color}>{s.label}</Tag>;
      },
    },
    {
      title: "待修正字段",
      render: (_: unknown, r: Resume) =>
        lowConfFields(r).length ? (
          <span style={{ color: "#faad14" }}>{lowConfFields(r).join("、")}（低置信，请到简历库修正）</span>
        ) : (
          <span style={{ color: "#52c41a" }}>无</span>
        ),
    },
    {
      title: "操作",
      width: 160,
      render: (_: unknown, r: Resume) => (
        <Space size="small">
          {isParsing(r) ? null : (
            <a onClick={() => resumeApi.reparse(r.id).then(refresh).catch((e) => message.error(e?.response?.data?.detail || "重新解析失败"))}>
              重新解析
            </a>
          )}
          <Popconfirm
            title="删除简历"
            description={`确认删除「${displayName(r)}」？关联的匹配记录会一并删除，且不可恢复。`}
            okText="删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => resumeApi.remove(r.id).then(() => { message.success("已删除"); refresh(); })}
          >
            <a style={{ color: "#ff4d4f" }}>删除</a>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <Card title="简历批量上传解析" extra={<span>支持 PDF / Word / 图片（扫描件自动走 OCR）</span>}>
      <div style={{ marginBottom: 16 }}>
        简历渠道：
        <Select value={source} options={SOURCE_OPTIONS} onChange={setSource} style={{ width: 160, marginLeft: 8 }} />
      </div>
      <Upload.Dragger
        multiple
        fileList={fileList}
        beforeUpload={() => false}
        onChange={({ fileList }) => setFileList(fileList)}
        accept=".pdf,.docx,.png,.jpg,.jpeg,.bmp,.webp"
      >
        <p className="ant-upload-drag-icon">
          <InboxOutlined />
        </p>
        <p className="ant-upload-text">点击或拖拽简历文件到此区域</p>
        <p className="ant-upload-hint">支持批量上传，上传后 AI 自动抽取结构化字段并入库</p>
      </Upload.Dragger>
      <div style={{ marginTop: 16 }}>
        <Button type="primary" loading={uploading} disabled={fileList.length === 0} onClick={doUpload}>
          {uploading ? "提交中…" : `开始上传（${fileList.length} 个文件）`}
        </Button>
      </div>
      <Alert style={{ marginTop: 16 }} type="info" showIcon
        message="解析结果带置信度，低置信字段会高亮提示，请前往「简历库检索」页人工修正后再参与匹配。" />
      <Table style={{ marginTop: 16 }} rowKey="id" size="small" columns={columns} dataSource={recent}
        pagination={false} title={() => "最近入库"} />
    </Card>
  );
}
