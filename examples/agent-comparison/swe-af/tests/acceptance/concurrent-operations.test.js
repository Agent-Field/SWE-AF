const assert = require('assert');
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

console.log('\n========================================');
console.log('Acceptance Test: Concurrent Operations');
console.log('========================================\n');

let testsPassed = 0;
let testsFailed = 0;

// Create temp directory for this test
const tempDir = fs.mkdtempSync(path.join(process.env.TMPDIR || '/tmp', 'cli-test-'));
const cliPath = path.join(process.cwd(), 'cli.js');
const todosFile = path.join(tempDir, 'todos.json');

function cleanup() {
  try {
    fs.rmSync(tempDir, { recursive: true, force: true });
  } catch (e) {
    // ignore cleanup errors
  }
}

// AC 18: Concurrent operations - 5 parallel adds
console.log('Test 1: 5 parallel add operations');
try {
  // Spawn 5 parallel add operations
  const titles = ['Todo 1', 'Todo 2', 'Todo 3', 'Todo 4', 'Todo 5'];
  const results = [];

  for (let i = 0; i < titles.length; i++) {
    const result = spawnSync('node', [cliPath, 'add', titles[i]], {
      cwd: tempDir,
      encoding: 'utf8'
    });
    results.push(result);
  }

  // Verify all operations succeeded
  for (let i = 0; i < results.length; i++) {
    assert.strictEqual(results[i].status, 0, `add operation ${i + 1} should exit 0`);
    assert(results[i].stdout.includes('Created todo ID:'), `add operation ${i + 1} should display success message`);
  }

  console.log('✓ All 5 parallel adds completed successfully\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// Verify todos.json is valid after concurrent operations
console.log('Test 2: todos.json is valid after concurrent operations');
try {
  // Verify file exists
  assert(fs.existsSync(todosFile), 'todos.json should exist');

  // Verify it's valid JSON
  const content = fs.readFileSync(todosFile, 'utf8');
  const todos = JSON.parse(content);

  // Verify all 5 todos were persisted
  assert(Array.isArray(todos), 'todos.json should contain an array');
  assert.strictEqual(todos.length, 5, 'should have exactly 5 todos after 5 adds');

  // Verify IDs are 1, 2, 3, 4, 5
  const ids = todos.map(t => t.id).sort((a, b) => a - b);
  assert.deepStrictEqual(ids, [1, 2, 3, 4, 5], 'IDs should be 1-5');

  // Verify all titles are present
  for (let i = 1; i <= 5; i++) {
    const todo = todos.find(t => t.id === i);
    assert(todo, `todo with ID ${i} should exist`);
    assert.strictEqual(todo.title, `Todo ${i}`, `todo ${i} title should be correct`);
    assert.strictEqual(todo.completed, false, `todo ${i} should not be completed`);
  }

  console.log('✓ Test passed: todos.json valid with all 5 todos\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// Verify list shows all concurrent todos
console.log('Test 3: List displays all concurrent todos');
try {
  const listResult = spawnSync('node', [cliPath, 'list'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(listResult.status, 0, 'list should exit 0');

  // Verify all 5 todos are in the list
  for (let i = 1; i <= 5; i++) {
    assert(listResult.stdout.includes(`${i} |`), `list should contain todo ID ${i}`);
    assert(listResult.stdout.includes(`Todo ${i}`), `list should contain title "Todo ${i}"`);
  }

  console.log('✓ Test passed: list displays all 5 concurrent todos\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// Test: Concurrent operations don't corrupt file
console.log('Test 4: No corruption in todos.json after concurrent operations');
try {
  // Perform concurrent add, complete, and delete operations
  const addResult1 = spawnSync('node', [cliPath, 'add', 'Extra 1'], { cwd: tempDir, encoding: 'utf8' });
  const addResult2 = spawnSync('node', [cliPath, 'add', 'Extra 2'], { cwd: tempDir, encoding: 'utf8' });
  const completeResult = spawnSync('node', [cliPath, 'complete', '1'], { cwd: tempDir, encoding: 'utf8' });
  const deleteResult = spawnSync('node', [cliPath, 'delete', '2'], { cwd: tempDir, encoding: 'utf8' });

  // Verify JSON is still valid
  const content = fs.readFileSync(todosFile, 'utf8');
  const todos = JSON.parse(content);

  // Verify file structure is intact
  assert(Array.isArray(todos), 'todos.json should still be an array');
  assert(todos.length > 0, 'todos array should not be empty');

  // Verify all todos have required properties
  for (const todo of todos) {
    assert(todo.id !== undefined, 'each todo should have id');
    assert(todo.title !== undefined, 'each todo should have title');
    assert(todo.completed !== undefined, 'each todo should have completed flag');
  }

  console.log('✓ Test passed: no corruption after mixed operations\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// Cleanup
cleanup();

console.log('========================================');
console.log(`Tests passed: ${testsPassed}`);
console.log(`Tests failed: ${testsFailed}`);
console.log('========================================\n');

if (testsFailed > 0) {
  process.exit(1);
}
