import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { environmentValue } from '../../src/acc/env.mjs';
import { E2E_BASE } from '../playwright.config';

export default function teardown() {
  execFileSync(environmentValue('ACC_E2E_PYTHON') ?? fileURLToPath(new URL('../../.venv/bin/python', import.meta.url)),
    [fileURLToPath(new URL('../../tests/owned_tmux.py', import.meta.url)), E2E_BASE], { timeout: 30000, stdio: 'inherit' });
}
