const assert = require('assert');
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

console.log('\n========================================');
console.log('Acceptance Test: Edge Cases');
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

// AC 16: Unknown command error
console.log('Test 1: Unknown command error');
try {
  const result = spawnSync('node', [cliPath, 'invalid-command'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 1, 'exit code should be 1 for unknown command');
  assert(result.stdout.includes('Error:'), 'should display error message');
  assert(result.stdout.includes('Unknown command') || result.stdout.includes('invalid-command'), 'should mention unknown command');

  console.log('✓ Test passed: unknown command error exits 1\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 19: Case insensitivity - uppercase ADD
console.log('Test 2: Uppercase ADD command');
try {
  const result = spawnSync('node', [cliPath, 'ADD', 'Test case insensitive'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 0, 'ADD should work like add');
  assert(result.stdout.includes('Created todo ID:'), 'ADD should create todo');

  console.log('✓ Test passed: ADD works like add\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 19: Case insensitivity - uppercase LIST
console.log('Test 3: Uppercase LIST command');
try {
  const result = spawnSync('node', [cliPath, 'LIST'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 0, 'LIST should work like list');
  assert(result.stdout.includes('|') || result.stdout.includes('No todos found'), 'LIST should display todos');

  console.log('✓ Test passed: LIST works like list\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 19: Case insensitivity - uppercase COMPLETE
console.log('Test 4: Uppercase COMPLETE command');
try {
  const result = spawnSync('node', [cliPath, 'COMPLETE', '1'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 0, 'COMPLETE should work like complete');
  assert(result.stdout.includes('Marked') || result.stdout.includes('already'), 'COMPLETE should complete todo');

  console.log('✓ Test passed: COMPLETE works like complete\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 19: Case insensitivity - uppercase DELETE
console.log('Test 5: Uppercase DELETE command');
try {
  const result = spawnSync('node', [cliPath, 'DELETE', '1'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 0, 'DELETE should work like delete');
  assert(result.stdout.includes('Deleted'), 'DELETE should delete todo');

  console.log('✓ Test passed: DELETE works like delete\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 20: Special characters - quotes
console.log('Test 6: Title with quotes');
try {
  const titleWithQuotes = 'Buy "organic" milk';
  const result = spawnSync('node', [cliPath, 'add', titleWithQuotes], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 0, 'should add todo with quotes');
  assert(result.stdout.includes('Created todo ID:'), 'should display success message');

  // Verify it persists correctly
  const listResult = spawnSync('node', [cliPath, 'list'], {
    cwd: tempDir,
    encoding: 'utf8'
  });
  assert(listResult.stdout.includes('organic'), 'title with quotes should persist');

  // Verify in JSON
  const content = fs.readFileSync(todosFile, 'utf8');
  const todos = JSON.parse(content);
  const foundTodo = todos.find(t => t.title === titleWithQuotes);
  assert(foundTodo, 'todo with quotes should be in JSON');

  console.log('✓ Test passed: title with quotes persists\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 20: Special characters - dollar sign
console.log('Test 7: Title with dollar sign');
try {
  const titleWithDollar = 'Save $100 for vacation';
  const result = spawnSync('node', [cliPath, 'add', titleWithDollar], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 0, 'should add todo with dollar sign');

  // Verify it persists correctly
  const listResult = spawnSync('node', [cliPath, 'list'], {
    cwd: tempDir,
    encoding: 'utf8'
  });
  assert(listResult.stdout.includes('$100'), 'title with dollar sign should persist');

  // Verify in JSON
  const content = fs.readFileSync(todosFile, 'utf8');
  const todos = JSON.parse(content);
  const foundTodo = todos.find(t => t.title === titleWithDollar);
  assert(foundTodo, 'todo with dollar sign should be in JSON');

  console.log('✓ Test passed: title with dollar sign persists\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 20: Special characters - unicode
console.log('Test 8: Title with unicode characters');
try {
  const titleWithUnicode = 'Learn 中文 🎉 emoji';
  const result = spawnSync('node', [cliPath, 'add', titleWithUnicode], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 0, 'should add todo with unicode');

  // Verify it persists correctly
  const listResult = spawnSync('node', [cliPath, 'list'], {
    cwd: tempDir,
    encoding: 'utf8'
  });
  assert(listResult.stdout.includes('中文'), 'title with unicode should persist');

  // Verify in JSON
  const content = fs.readFileSync(todosFile, 'utf8');
  const todos = JSON.parse(content);
  const foundTodo = todos.find(t => t.title === titleWithUnicode);
  assert(foundTodo, 'todo with unicode should be in JSON');

  console.log('✓ Test passed: title with unicode persists\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// Test: Missing command argument
console.log('Test 9: Missing command argument');
try {
  const result = spawnSync('node', [cliPath, 'add'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 1, 'exit code should be 1 for missing argument');
  assert(result.stdout.includes('Error:'), 'should display error message');

  console.log('✓ Test passed: missing argument error exits 1\n');
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
