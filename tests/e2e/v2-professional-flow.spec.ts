import { test, expect } from "@playwright/test";
test("professional shell exposes a same-origin entry", async ({ page }) => { await page.goto("/"); await expect(page).toHaveTitle(/ProseForge/i); });
