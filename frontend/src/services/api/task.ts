import {
  type TaskCancelResponse,
  type TaskExecutionResponse,
  type TaskInfo,
} from "@/__generated__";
import api from "@/services/api";
import type { TaskStatusResponse } from "@/utils/tasks";

async function getTasks() {
  return api.get<Record<string, TaskInfo[]>>("/tasks");
}

async function getTaskById(taskId: string) {
  return api.get<TaskStatusResponse>(`/tasks/${taskId}`);
}

async function runTask(taskName: string, body?: Record<string, unknown>) {
  return api.post<TaskExecutionResponse>(`/tasks/run/${taskName}`, body);
}

async function getTaskStatus() {
  return api.get<TaskStatusResponse[]>("/tasks/status");
}

/** Drop a queued task, or ask a running one to stop. */
async function cancelTask(taskId: string) {
  return api.delete<TaskCancelResponse>(`/tasks/${taskId}`);
}

export default {
  getTasks,
  getTaskById,
  runTask,
  getTaskStatus,
  cancelTask,
};
