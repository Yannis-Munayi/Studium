import { startMockBackend, type MockBackend } from "./mock-backend";

/**
 * Start the Tier 2 mocked-LLM backend before Next boots (spec §17).
 *
 * Order matters: the Next proxy reads `STUDIUM_BACKEND_ORIGIN` at module load,
 * so the mock has to be listening on a known port before `next start` runs.
 * That is why the port is fixed here rather than assigned by the OS.
 */

export const MOCK_PORT = Number(process.env["STUDIUM_MOCK_PORT"] ?? 8099);
export const MOCK_ORIGIN = `http://127.0.0.1:${MOCK_PORT}`;

let backend: MockBackend | null = null;

export default async function globalSetup(): Promise<() => Promise<void>> {
  // Tier 3 talks to the real FastAPI app; starting a mock alongside it would
  // bind a port for nothing and invite a test to hit the wrong one.
  if (process.env["STUDIUM_RUN_PAID_TESTS"] === "1") {
    return async () => {};
  }

  backend = await startMockBackend({}, MOCK_PORT);
  process.env["STUDIUM_BACKEND_ORIGIN"] = backend.origin;

  return async () => {
    await backend?.close();
    backend = null;
  };
}
