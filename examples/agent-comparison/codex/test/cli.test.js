const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const binPath = path.resolve(__dirname, '..', 'bin', 'todo.js');

function makeTempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'todo-cli-'));
}

function runCli(args, cwd, todoFileName = 'todos.json') {
  return spawnSync(process.execPath, [binPath, ...args], {
    cwd,
    env: {
      ...process.env,
      TODO_FILE: path.join(cwd, todoFileName)
    },
    encoding: 'utf8'
  });
}

test('add and list todos', () => {
  const cwd = makeTempDir();

  const add = runCli(['add', 'Buy milk'], cwd);
  assert.equal(add.status, 0);
  assert.match(add.stdout, /Added todo 1\./);

  const list = runCli(['list'], cwd);
  assert.equal(list.status, 0);
  assert.match(list.stdout, /\[ \] 1: Buy milk/);

  const data = JSON.parse(fs.readFileSync(path.join(cwd, 'todos.json'), 'utf8'));
  assert.equal(data.length, 1);
  assert.equal(data[0].text, 'Buy milk');
  assert.equal(data[0].completed, false);
});

test('complete marks todo done', () => {
  const cwd = makeTempDir();

  assert.equal(runCli(['add', 'Write tests'], cwd).status, 0);
  const complete = runCli(['complete', '1'], cwd);
  assert.equal(complete.status, 0);
  assert.match(complete.stdout, /Completed todo 1\./);

  const list = runCli(['list'], cwd);
  assert.match(list.stdout, /\[x\] 1: Write tests/);
});

test('delete removes todo', () => {
  const cwd = makeTempDir();

  assert.equal(runCli(['add', 'One'], cwd).status, 0);
  assert.equal(runCli(['add', 'Two'], cwd).status, 0);

  const del = runCli(['delete', '1'], cwd);
  assert.equal(del.status, 0);
  assert.match(del.stdout, /Deleted todo 1\./);

  const list = runCli(['list'], cwd);
  assert.doesNotMatch(list.stdout, /1: One/);
  assert.match(list.stdout, /\[ \] 2: Two/);
});

test('errors for unknown command and missing todo', () => {
  const cwd = makeTempDir();

  const unknown = runCli(['wat'], cwd);
  assert.equal(unknown.status, 1);
  assert.match(unknown.stderr, /Unknown command: wat/);

  const missing = runCli(['complete', '999'], cwd);
  assert.equal(missing.status, 1);
  assert.match(missing.stderr, /Todo 999 not found\./);
});
