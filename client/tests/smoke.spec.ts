import { test, expect, type Page } from "@playwright/test";

// 等待前端模块（chat.js / review.js）完成执行并注册 data-action 委托，
// 避免点击时事件处理器尚未就绪的竞态。
async function waitForActions(page: Page, name: string) {
  await page.waitForFunction(
    (n) => (window as any).__ACTIONS && typeof (window as any).__ACTIONS[n] === "function",
    name,
    { timeout: 10000 }
  );
}

test("患者端页面可加载并展示登录遮罩", async ({ page }) => {
  await page.goto("/");
  // 品牌与登录遮罩可见
  await expect(page.getByText("康宁健康", { exact: false }).first()).toBeVisible();
  await expect(page.locator("#loginMask")).toBeVisible();
  // 演示账号预填（源码硬编码），便于一键登录
  await expect(page.locator("#lu")).toHaveValue("alice");
});

test("患者使用演示账号可登录进入服务台", async ({ page }) => {
  await page.goto("/");
  await waitForActions(page, "login");
  await page.fill("#lu", "alice");
  await page.fill("#lp", "alice123");
  await page.click('[data-action="login"]');
  // 登录成功后顶栏用户盒与用户名显示
  await expect(page.locator("#userbox")).toBeVisible();
  await expect(page.locator("#uname")).toHaveText("alice");
});

test("医护端页面可加载并展示登录遮罩", async ({ page }) => {
  await page.goto("/review");
  await expect(page.getByText("康宁健康", { exact: false }).first()).toBeVisible();
  await expect(page.locator("#loginMask")).toBeVisible();
  await expect(page.locator("#lu")).toHaveValue("drwang");
});
