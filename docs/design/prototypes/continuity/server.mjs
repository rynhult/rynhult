import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { patchXtermComposition } from '../../../../web/scripts/xterm-composition.mjs';
const require = createRequire(new URL('../../../../web/package.json', import.meta.url));

export async function startServer() {
  const child = spawn(process.env.CONTINUITY_PYTHON || '/srv/acc/app/.venv/bin/python',
    [fileURLToPath(new URL('fake-pty.py', import.meta.url))], { stdio: ['ignore', 'pipe', 'pipe'] });
  const stats = { connections: 0, closed: 0, outputs: 0, inputs: [], resizes: [] };
  let stderr = '';
  child.stderr.on('data', chunk => { stderr += chunk; });
  const lines = createInterface({ input: child.stdout });
  let timer;
  let port;
  try {
    port = await new Promise((resolve, reject) => {
      timer = setTimeout(() => reject(new Error(`fake PTY startup timed out: ${stderr}`)), 10_000);
      child.once('exit', code => reject(new Error(`fake PTY exited ${code}: ${stderr}`)));
      lines.on('line', line => {
        const value = JSON.parse(line);
        if (value.kind === 'ready') resolve(value.port);
        if (value.kind === 'connection') stats.connections++;
        if (value.kind === 'closed') stats.closed++;
        if (value.kind === 'output') stats.outputs++;
        if (value.kind === 'client') {
          if (value.message.type === 'input') stats.inputs.push(value.message.text);
          if (value.message.type === 'resize') stats.resizes.push(value.message);
        }
      });
    });
  } catch (error) { child.kill('SIGTERM'); throw error; }
  finally { clearTimeout(timer); }
  const root = dirname(require.resolve('@xterm/xterm'));
  const assets = new Map([
    ['/xterm.mjs', [join(root, 'xterm.mjs'), 'text/javascript']],
    ['/fit.mjs', [join(dirname(require.resolve('@xterm/addon-fit')), 'addon-fit.mjs'), 'text/javascript']],
    ['/xterm.css', [join(root, '../css/xterm.css'), 'text/css']],
    ['/prototype.mjs', [fileURLToPath(new URL('prototype.mjs', import.meta.url)), 'text/javascript']],
    ['/', [fileURLToPath(new URL('index.html', import.meta.url)), 'text/html']],
  ]);
  const http = createServer(async (req, res) => {
    const path = new URL(req.url, 'http://localhost').pathname;
    if (path === '/stats') { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(stats)); return; }
    if (path === '/connection') { res.end(JSON.stringify({ url: `ws://127.0.0.1:${port}` })); return; }
    const asset = assets.get(path);
    if (!asset) { res.writeHead(404); res.end(); return; }
    try {
      let body = await readFile(asset[0], 'utf8');
      if (path === '/xterm.mjs') body = patchXtermComposition(body);
      res.setHeader('Content-Type', asset[1]); res.end(body);
    } catch (error) { res.writeHead(500); res.end(String(error)); }
  });
  await new Promise(resolve => http.listen(0, '127.0.0.1', resolve));
  return {
    url: `http://127.0.0.1:${http.address().port}`, stats,
    async close() {
      const exited = new Promise(resolve => child.once('exit', resolve));
      child.kill('SIGTERM'); await exited; lines.close();
      http.closeAllConnections(); await new Promise(resolve => http.close(resolve));
    },
  };
}
