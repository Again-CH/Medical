import { defineConfig, devices } from "@playwright/test";

// E2E 冒烟测试配置
// - baseURL 指向由 run_e2e_server.sh 拉起的本地后端（端口 8137，避免与手动 demo 的 8000 冲突）
// - webServer.reuseExistingServer: 若 8137 已有服务则复用，否则自动拉起并负责回收
// - 后端以 sqlite + fake LLM 运行，启动时 seed 出 alice / drwang 演示账号，测试可直接登录
export default defineConfig({
  testDir: "./tests",
  timeout: 30000,
  expect: { timeout: 10000 },
  fullyParallel: false,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:8137",
    headless: true,
    trace: "retain-on-failure",
  },
  webServer: {
    command: "bash run_e2e_server.sh",
    url: "http://127.0.0.1:8137/",
    reuseExistingServer: true,
    timeout: 60000,
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
});
