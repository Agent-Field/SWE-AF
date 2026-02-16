const assert = require('assert');
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

console.log('\n========================================');
console.log('Acceptance Test: Add Command');
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

// AC 2: Add command success
console.log('Test 1: node cli.js add "Test"');
try {
  const result = spawnSync('node', [cliPath, 'add', 'Test'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 0, 'exit code should be 0');
  assert(result.stdout.includes('Created todo ID:'), 'should display success message');
  assert(result.stdout.includes('1'), 'should display ID 1');

  // Verify todos.json exists and is valid JSON
  assert(fs.existsSync(todosFile), 'todos.json should be created');
  const content = fs.readFileSync(todosFile, 'utf8');
  const todos = JSON.parse(content);
  assert(Array.isArray(todos), 'todos.json should contain an array');
  assert.strictEqual(todos.length, 1, 'should have one todo');
  assert.strictEqual(todos[0].id, 1, 'todo id should be 1');
  assert.strictEqual(todos[0].title, 'Test', 'todo title should be "Test"');
  assert.strictEqual(todos[0].completed, false, 'todo should not be completed');

  console.log('✓ Test passed: add success with ID 1\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 3: ID generation - sequential IDs
console.log('Test 2: Sequential ID generation (1, 2, 3)');
try {
  // Add second todo
  const result2 = spawnSync('node', [cliPath, 'add', 'Second'], {
    cwd: tempDir,
    encoding: 'utf8'
  });
  assert.strictEqual(result2.status, 0, 'second add should exit 0');
  assert(result2.stdout.includes('Created todo ID: 2'), 'second todo should have ID 2');

  // Add third todo
  const result3 = spawnSync('node', [cliPath, 'add', 'Third'], {
    cwd: tempDir,
    encoding: 'utf8'
  });
  assert.strictEqual(result3.status, 0, 'third add should exit 0');
  assert(result3.stdout.includes('Created todo ID: 3'), 'third todo should have ID 3');

  // Verify via list
  const listResult = spawnSync('node', [cliPath, 'list'], {
    cwd: tempDir,
    encoding: 'utf8'
  });
  assert(listResult.stdout.includes('1 |'), 'list should show ID 1');
  assert(listResult.stdout.includes('2 |'), 'list should show ID 2');
  assert(listResult.stdout.includes('3 |'), 'list should show ID 3');

  console.log('✓ Test passed: sequential IDs 1, 2, 3\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 13: Empty title error
console.log('Test 3: node cli.js add "" (empty title)');
try {
  // Clean temp directory for this test
  fs.rmSync(tempDir, { recursive: true, force: true });
  fs.mkdirSync(tempDir);

  const result = spawnSync('node', [cliPath, 'add', ''], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 1, 'exit code should be 1 for empty title');
  assert(result.stdout.includes('Error:'), 'should display error message');
  assert(result.stdout.includes('cannot be empty'), 'should mention empty title');

  // Verify todos.json was not created or is empty
  if (fs.existsSync(todosFile)) {
    const content = fs.readFileSync(todosFile, 'utf8');
    const todos = JSON.parse(content);
    assert(todos.length === 0, 'todos.json should be empty after failed add');
  }

  console.log('✓ Test passed: empty title error exits 1\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 14: Whitespace-only title error
console.log('Test 4: node cli.js add "   " (whitespace only)');
try {
  const result = spawnSync('node', [cliPath, 'add', '   '], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 1, 'exit code should be 1 for whitespace title');
  assert(result.stdout.includes('Error:'), 'should display error message');
  assert(result.stdout.includes('cannot be empty'), 'should mention empty/whitespace title');

  console.log('✓ Test passed: whitespace title error exits 1\n');
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
