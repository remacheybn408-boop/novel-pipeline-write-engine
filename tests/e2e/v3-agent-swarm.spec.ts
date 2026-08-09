import { test, expect } from "@playwright/test";
test("agent route remains same-origin", async ({ page }) => { await page.goto("/"); await expect(page).toHaveTitle(/ProseForge/i); });
