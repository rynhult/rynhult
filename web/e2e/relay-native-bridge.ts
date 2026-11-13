import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rename, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { environmentValue } from '../../src/acc/env.mjs';

/** Fixed offline native lab. All browser RPC replies come from its real Hub. */
export async function nativeRelayBridge(sessionId: string) {
  const directory = await mkdtemp(`${tmpdir()}/relay-browser-native-`);
  const root = resolve('..');
  const child = spawn(environmentValue('ACC_E2E_PYTHON') ?? resolve(root, '.acc/relay-r1b-venv/bin/python'), [
    'scripts/session_relay_qualification/installed_security_probe.py', '--bridge', directory, '--session-id', sessionId,
  ], { cwd: root, env: { PATH: process.env.PATH, PYTHONPATH: `${root}/src`,
    PYTHONDONTWRITEBYTECODE: '1', PLAYWRIGHT_BROWSERS_PATH: environmentValue('PLAYWRIGHT_BROWSERS_PATH') } });
  let stderr = ''; let stdout = '';
  child.stderr.on('data', data => { stderr = (stderr + String(data)).slice(-18000); });
  child.stdout.on('data', data => { stdout += String(data); });
  const exited = new Promise<number | null>((done, reject) => { child.once('error', reject); child.once('exit', done); });
  async function jsonFile(name: string, timeout = 25000): Promise<any> {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      try { return JSON.parse(await readFile(`${directory}/${name}`, 'utf8')); }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
      if (child.exitCode !== null) throw new Error(`Offline native lab exited ${child.exitCode}: ${stderr}`);
      await new Promise(done => setTimeout(done, 20));
    }
    throw new Error(`Offline native lab timeout for ${name}: ${stderr}`);
  }
  await jsonFile('ready.json');
  let sequence = 0; let queue = Promise.resolve();
  function call(method: string, path: string, body: unknown = null, headers: Record<string, string> = {}) {
    const result = queue.then(async () => {
      const id = ++sequence;
      await writeFile(`${directory}/request-${id}.tmp`, JSON.stringify({ method, path, body, headers }));
      await rename(`${directory}/request-${id}.tmp`, `${directory}/request-${id}.json`);
      return jsonFile(`reply-${id}.json`);
    });
    queue = result.then(() => undefined);
    return result;
  }
  return { directory, call,
    reset: () => writeFile(`${directory}/reset`, ''),
    async stop() {
      await queue;
      await writeFile(`${directory}/stop`, '');
      const code = await exited;
      if (code !== 0) throw new Error(`Offline native lab failed ${code}: ${stderr}`);
      return JSON.parse(stdout.trim());
    },
    async abort() { if (child.exitCode === null) { child.kill('SIGTERM'); await exited; } },
  };
}
