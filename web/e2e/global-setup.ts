import type { FullConfig } from '@playwright/test';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { assertSafeWorkers, baseTestImports } from './isolation-policy';

export default function setup(config: FullConfig) {
  assertSafeWorkers(config);
  const root = new URL('.', import.meta.url);
  const invalid = readdirSync(root, { recursive: true }).filter((file): file is string => typeof file === 'string' && file.endsWith('.ts') && !file.split('/').some(part => part.startsWith('.')))
    .flatMap(file => baseTestImports(readFileSync(new URL(file, root), 'utf8'), file));
  if (invalid.length) throw new Error(`E2E test modules must import test from ./fixtures: ${invalid.map(file => join('e2e', file)).join(', ')}`);
}
