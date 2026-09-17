import {
  FileSearchOutlined, FileTextOutlined, FormOutlined, PartitionOutlined, UploadOutlined,
} from "@ant-design/icons";
import { Layout, Menu, Typography } from "antd";
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import JobCreatePage from "./pages/JobCreate";
import JobLibraryPage from "./pages/JobLibrary";
import MatchResultPage from "./pages/MatchResult";
import ResumeLibraryPage from "./pages/ResumeLibrary";
import ResumeUploadPage from "./pages/ResumeUpload";

const { Header, Sider, Content } = Layout;

function Shell() {
  const location = useLocation();
  const items = [
    { key: "/upload", icon: <UploadOutlined />, label: <Link to="/upload">简历上传解析</Link> },
    { key: "/jobs/create", icon: <FormOutlined />, label: <Link to="/jobs/create">岗位录入</Link> },
    { key: "/jobs/library", icon: <FileTextOutlined />, label: <Link to="/jobs/library">已录入岗位</Link> },
    { key: "/match", icon: <PartitionOutlined />, label: <Link to="/match">匹配结果</Link> },
    { key: "/library", icon: <FileSearchOutlined />, label: <Link to="/library">简历库检索</Link> },
  ];
  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header style={{ display: "flex", alignItems: "center", background: "#1F4E79" }}>
        <Typography.Title level={4} style={{ color: "#fff", margin: 0 }}>
          智慧引才图谱 · 高层次人才智能匹配
        </Typography.Title>
      </Header>
      <Layout>
        <Sider width={200} theme="light">
          <Menu mode="inline" selectedKeys={[location.pathname]} items={items} style={{ height: "100%" }} />
        </Sider>
        <Content style={{ padding: 24, background: "#f5f7fa" }}>
          <Routes>
            <Route path="/" element={<Navigate to="/upload" replace />} />
            <Route path="/upload" element={<ResumeUploadPage />} />
            {/* 旧路径 /jobs 保留兼容：重定向到岗位录入页 */}
            <Route path="/jobs" element={<Navigate to="/jobs/create" replace />} />
            <Route path="/jobs/create" element={<JobCreatePage />} />
            <Route path="/jobs/library" element={<JobLibraryPage />} />
            <Route path="/match" element={<MatchResultPage />} />
            <Route path="/library" element={<ResumeLibraryPage />} />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Shell />
    </BrowserRouter>
  );
}
