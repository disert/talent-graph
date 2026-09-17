/** 岗位录入页 / 已录入岗位页的共用小工具。 */
import type { DepartmentNode, Job } from "../api/client";

/** 按级联路径取部门所在省份（节点已带生效省份，取路径最深的一级即可） */
export function provinceOfPath(nodes: DepartmentNode[], path: string[]): string {
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
export function jobMajors(job: Job): string[] {
  const raw: unknown[] = job.majors?.length ? job.majors : (job.hard_conditions?.majors ?? []);
  return raw.map((m) => String(m).trim()).filter(Boolean);
}
