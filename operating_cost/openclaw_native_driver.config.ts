/** Use pinned OpenClaw source aliases, without its broad unit-test setup mocks. */
import path from "node:path";
import { fileURLToPath } from "node:url";
import { sharedVitestConfig } from "../../openclaw/test/vitest/vitest.shared.config.ts";

const directory = path.dirname(fileURLToPath(import.meta.url));
const openclawRoot = path.resolve(directory, "../../openclaw");

export default {
  ...sharedVitestConfig,
  root: path.resolve(directory, ".."),
  resolve: {
    ...sharedVitestConfig.resolve,
    alias: [
      { find: /^vitest$/, replacement: path.join(openclawRoot, "node_modules/vitest/dist/index.js") },
      ...sharedVitestConfig.resolve.alias,
    ],
  },
  test: {
    ...sharedVitestConfig.test,
    dir: path.resolve(directory, ".."),
    name: "openclaw-recording-cost",
    include: ["operating_cost/openclaw_native_driver.test.ts"],
    setupFiles: [],
    globalSetup: [],
    isolate: true,
    pool: "forks",
    maxWorkers: 1,
    fileParallelism: false,
    testTimeout: 3_600_000,
    hookTimeout: 30_000,
  },
};
