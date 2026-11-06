import { environmentValue } from '../../src/acc/env.mjs';

// Kept free of Playwright test declarations so the fixture and helpers share paths without a cycle.
export const BASE = environmentValue('ACC_E2E_BASE') ?? '/tmp/acc-e2e';
export const AUTH_FILE = `${BASE}/auth.json`;
