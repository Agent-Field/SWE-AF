const assert = require('assert');
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

console.log('\n========================================');
console.log('Acceptance Test: Delete Command');
console.log('========================================\n');

let testsPassed = 0;
let testsFailed = 0;

// Create temp directory for this test
const tempDir = fs.mkdtempSync(path.join(process.env.TMPDIR || '/tmp', 'cli-test-'));
const cliPath = path.join(process.cwd(), 'cli.js');

function cleanup() {
  try {
    fs.rmSync(tempDir, { recursive: true, force: true });
  } catch (e) {
    // ignore cleanup errors
  }
}

// Setup: Add test todos
spawnSync('node', [cliPath, 'add', 'Buy milk'], { cwd: tempDir, encoding: 'utf8' });
spawnSync('node', [cliPath, 'add', 'Walk dog'], { cwd: tempDir, encoding: 'utf8' });
spawnSync('node', [cliPath, 'add', 'Write code'], { cwd: tempDir, encoding: 'utf8' });

// AC 8: Delete command success
console.log('Test 1: node cli.js delete 1 (success)');
try {
  const result = spawnSync('node', [cliPath, 'delete', '1'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 0, 'exit code should be 0');
  assert(result.stdout.includes('Deleted todo 1'), 'should display success message');

  // Verify todo was removed from list
  const listResult = spawnSync('node', [cliPath, 'list'], {
    cwd: tempDir,
    encoding: 'utf8'
  });
  assert(!listResult.stdout.includes('Buy milk'), 'deleted todo should not appear in list');
  assert(listResult.stdout.includes('Walk dog'), 'other todos should still be in list');
  assert(listResult.stdout.includes('Write code'), 'other todos should still be in list');

  // Verify todos.json is still valid JSON
  const todosFile = path.join(tempDir, 'todos.json');
  const content = fs.readFileSync(todosFile, 'utf8');
  const todos = JSON.parse(content);
  assert.strictEqual(todos.length, 2, 'should have 2 todos remaining');

  console.log('✓ Test passed: delete success with removal from list\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 9: Delete command - invalid ID error
console.log('Test 2: node cli.js delete 999 (non-existent ID)');
try {
  const result = spawnSync('node', [cliPath, 'delete', '999'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 1, 'exit code should be 1');
  assert(result.stdout.includes('Error:'), 'should display error message');
  assert(result.stdout.includes('not found') || result.stdout.includes('999'), 'should mention ID not found');

  console.log('✓ Test passed: delete error exits 1 for non-existent ID\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// Test: Delete with invalid ID format
console.log('Test 3: node cli.js delete abc (invalid ID format)');
try {
  const result = spawnSync('node', [cliPath, 'delete', 'abc'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 1, 'exit code should be 1');
  assert(result.stdout.includes('Error:'), 'should display error message');

  console.log('✓ Test passed: invalid ID format error exits 1\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// Test: Delete all todos
console.log('Test 4: Delete all todos');
try {
  // Delete remaining todos
  spawnSync('node', [cliPath, 'delete', '2'], { cwd: tempDir, encoding: 'utf8' });
  spawnSync('node', [cliPath, 'delete', '3'], { cwd: tempDir, encoding: 'utf8' });

  // Verify list shows "No todos found"
  const listResult = spawnSync('node', [cliPath, 'list'], {
    cwd: tempDir,
    encoding: 'utf8'
  });
  assert(listResult.stdout.includes('No todos found'), 'list should show "No todos found" when all deleted');

  // Verify todos.json is empty array
  const todosFile = path.join(tempDir, 'todos.json');
  const content = fs.readFileSync(todosFile, 'utf8');
  const todos = JSON.parse(content);
  assert.strictEqual(todos.length, 0, 'todos array should be empty');

  console.log('✓ Test passed: delete all todos succeeds\n');
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
