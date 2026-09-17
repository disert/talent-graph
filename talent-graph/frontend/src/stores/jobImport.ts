/* 岗位 Excel 批量导入的全局状态（zustand 模块级单例）。

   导入循环放在 store 内异步执行，而不是组件里：用户中途切到其他页面（如简历库检索）
   时 JobRequestPage 组件虽被卸载，但 store 仍保有进度与中止标志，切回来进度条照常显示、
   仍可停止。JobRequestPage 通过 useJobImport 订阅读取。
*/
import { create } from "zustand";
import { jobApi, type JobImportSession } from "../api/client";

/** 批量导入的一条异常明细 */
export interface ImportError {
  row: number;
  title: string;
  message: string;
}

/** 批量导入的进度状态（全局，跨页面保持） */
export interface ImportTask {
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

interface JobImportStore {
  task: ImportTask | null;
  /** 开始逐行导入（会重置中止标志并启动后台循环） */
  start: (session: JobImportSession) => void;
  /** 停止剩余行的导入 */
  stop: () => void;
  /** 结束/中止后关闭导入面板（清空任务） */
  dismiss: () => void;
}

export const useJobImport = create<JobImportStore>((set, get) => {
  let cancel = false;

  async function run(session: JobImportSession) {
    const items = session.items;
    let done = 0;
    let succeeded = 0;
    let failed = 0;
    const errors: ImportError[] = [];
    for (let i = 0; i < items.length; i += 1) {
      if (cancel) break;
      const item = items[i];
      const label = item.title || `第 ${item.row} 行`;
      const cur = get().task;
      if (cur) set({ task: { ...cur, current: label } });
      try {
        const r = await jobApi.importStep(session.token, i);
        if (r.data.ok) {
          succeeded += 1;
        } else {
          failed += 1;
          errors.push({ row: r.data.row, title: r.data.title || label,
                        message: r.data.error || "导入失败" });
        }
      } catch (err: any) {
        failed += 1;
        errors.push({ row: item.row, title: label,
                      message: err?.response?.data?.detail || "请求失败" });
      }
      done += 1;
      const now = get().task;
      if (now) set({ task: { ...now, done, succeeded, failed, errors } });
    }
    const tail = get().task;
    if (tail) set({ task: { ...tail, done, succeeded, failed, errors,
                            current: "", running: false, finished: true } });
  }

  return {
    task: null,
    start: (session) => {
      cancel = false;
      set({
        task: {
          total: session.total, done: 0, succeeded: 0, failed: 0, current: "",
          errors: [], preInvalid: session.invalid, running: true, finished: false,
        },
      });
      void run(session);
    },
    stop: () => { cancel = true; },
    dismiss: () => set({ task: null }),
  };
});