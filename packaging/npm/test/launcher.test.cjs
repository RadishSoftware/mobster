const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const { mkdtempSync, readFileSync, rmSync, writeFileSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join, resolve } = require('node:path');
const { test } = require('node:test');

const root = resolve(__dirname, '..');
const launcher = join(root, 'bin/mobster');
const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));

function tools(t) {
  const dir = mkdtempSync(join(tmpdir(), 'mobster-npm-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return dir;
}

test('forwards arguments, stdin, stdout, stderr and exit status', (t) => {
  const dir = tools(t);
  writeFileSync(join(dir, 'uvx'), '#!/bin/sh\nprintf "%s\\0" "$@"\n/bin/cat\nprintf "uvx diagnostic\\n" >&2\nexit 37\n', { mode: 0o755 });
  const args = ['mcp', '--keyless', 'space here', '$(do not execute)', 'line\nbreak', ''];
  const input = '{"jsonrpc":"2.0","id":1}\n';
  const result = spawnSync(launcher, args, { env: { PATH: dir }, input, encoding: 'utf8', timeout: 5000 });
  assert.equal(result.error, undefined);
  assert.equal(result.status, 37);
  assert.equal(result.stdout, ['--python', '3.12', '--from', `mobster-cli==${pkg.version}`, 'mobster', ...args].join('\0') + '\0' + input);
  assert.equal(result.stderr, 'uvx diagnostic\n');
});

test('missing uvx refuses clearly without writing to protocol stdout', (t) => {
  const result = spawnSync(launcher, ['mcp'], { env: { PATH: tools(t) }, encoding: 'utf8', timeout: 5000 });
  assert.equal(result.error, undefined);
  assert.equal(result.status, 127);
  assert.equal(result.stdout, '');
  assert.match(result.stderr, /uvx is missing from PATH/);
  assert.match(result.stderr, /https:\/\/docs\.astral\.sh\/uv\/getting-started\/installation\//);
});

test('npm and its Python pin use the core version', () => {
  const core = readFileSync(resolve(root, '../../mobile_agent/__init__.py'), 'utf8');
  const version = core.match(/^__version__ = "([^"]+)"$/m)[1];
  assert.equal(pkg.version, version);
  assert.ok(readFileSync(launcher, 'utf8').includes(`--from mobster-cli==${version} mobster`));
});
