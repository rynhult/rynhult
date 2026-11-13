// Test-only oracle: extract only into the caller's disposable directory. Never
// execute package code. Python supplies only the corpus accepted by its inspector.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const tar = require(process.argv[2]);
const request = JSON.parse(fs.readFileSync(0, 'utf8'));
function walk(directory, relative = '', files = {}, directories = []) {
  for (const name of fs.readdirSync(directory)) {
    const target = path.join(directory, name), key = relative + name;
    const stat = fs.lstatSync(target);
    if (stat.isDirectory()) {
      directories.push(key);
      walk(target, key + '/', files, directories);
    } else if (stat.isFile()) {
      // Compare paths/bytes, not npm's umask-dependent modes: the trusted
      // extractor deliberately normalizes modes. Read owner-unreadable mutants
      // only after extraction, inside this disposable observation directory.
      fs.chmodSync(target, stat.mode | 0o400);
      const bytes = fs.readFileSync(target);
      files[key] = {sha256: crypto.createHash('sha256').update(bytes).digest('hex'), size: bytes.length};
    } else {
      throw new Error(`Unexpected extracted type at ${key}`);
    }
  }
  return {files, directories: directories.sort()};
}
(async () => {
  const results = [];
  for (const [index, candidate] of request.cases.entries()) {
    const cwd = path.join(request.destination, String(index));
    fs.mkdirSync(cwd);
    const entries = [], warnings = [];
    await tar.t({file: candidate.file, onwarn: code => warnings.push(code),
      onReadEntry: entry => {entries.push({path: entry.path, type: entry.type}); entry.resume();}});
    await tar.x({file: candidate.file, cwd, strip: 1, preserveOwner: false,
      onwarn: code => warnings.push(code)});
    results.push({id: candidate.id, entries, warnings, ...walk(cwd)});
  }
  process.stdout.write(JSON.stringify(results));
})().catch(error => {console.error(error); process.exitCode = 1;});
