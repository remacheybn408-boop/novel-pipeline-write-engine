import { defineConfig } from "@playwright/test";

// Minimal config for the compose e2e service: the specs under tests/e2e only
// need a base URL. Browsers and @playwright/test come from the Playwright
// docker image; apps/web intentionally carries no Playwright dependency.
export default defineConfig({
  testDir: ".",
  // Keep artifacts off the mounted repo (Windows mounts reject them with
  // EACCES); compose.test.yaml mounts a tmpfs at this path.
  outputDir: "/tmp/playwright-results",
  retries: 1,
  use: {
    baseURL: process.env.BASE_URL ?? "http://web",
  },
});
