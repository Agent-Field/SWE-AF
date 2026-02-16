const assert = require('assert');
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

console.log('\n========================================');
console.log('Acceptance Test: Data Persistence');
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

// AC 10: todos.json is valid JSON
console.log('Test 1: todos.json is valid JSON');
try {
  // Add a todo to create the file
  spawnSync('node', [cliPath, 'add', 'Buy milk'], { cwd: tempDir, encoding: 'utf8' });

  // Verify file exists
  assert(fs.existsSync(todosFile), 'todos.json should exist');

  // Verify it's valid JSON
  const content = fs.readFileSync(todosFile, 'utf8');
  const todos = JSON.parse(content);

  // Verify structure
  assert(Array.isArray(todos), 'todos.json should contain an array');
  assert(todos.length > 0, 'todos array should not be empty');
  assert(todos[0].id !== undefined, 'todo should have id property');
  assert(todos[0].title !== undefined, 'todo should have title property');
  assert(todos[0].completed !== undefined, 'todo should have completed property');

  console.log('✓ Test passed: todos.json is valid JSON with correct structure\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 11: Persistence across process restart
console.log('Test 2: Todos persist across process restart');
try {
  // Add a todo in first process
  spawnSync('node', [cliPath, 'add', 'First todo'], { cwd: tempDir, encoding: 'utf8' });

  // Simulate process restart by spawning new process
  const listResult = spawnSync('node', [cliPath, 'list'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  // Verify todo still exists
  assert(listResult.stdout.includes('Buy milk'), 'first todo should persist');
  assert(listResult.stdout.includes('First todo'), 'second todo should persist');

  console.log('✓ Test passed: todos persist across process restart\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 12: File location is ./todos.json (working directory)
console.log('Test 3: todos.json exists in working directory');
try {
  // Verify file exists at expected location
  assert(fs.existsSync(todosFile), 'todos.json should exist in current working directory');

  // Verify it's the right file by checking content
  const content = fs.readFileSync(todosFile, 'utf8');
  const todos = JSON.parse(content);
  assert(todos.length >= 2, 'todos.json should contain our test todos');

  console.log('✓ Test passed: todos.json exists at ./todos.json\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// Test: JSON validity after complete operation
console.log('Test 4: todos.json remains valid JSON after complete');
try {
  // Complete a todo
  spawnSync('node', [cliPath, 'complete', '1'], { cwd: tempDir, encoding: 'utf8' });

  // Verify JSON is still valid
  const content = fs.readFileSync(todosFile, 'utf8');
  const todos = JSON.parse(content);
  assert(Array.isArray(todos), 'todos.json should still be an array');

  // Verify the completed flag was set
  const completedTodo = todos.find(t => t.id === 1);
  assert(completedTodo && completedTodo.completed === true, 'todo should be marked as completed');

  console.log('✓ Test passed: todos.json valid after complete operation\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// Test: JSON validity after delete operation
console.log('Test 5: todos.json remains valid JSON after delete');
try {
  // Delete a todo
  spawnSync('node', [cliPath, 'delete', '1'], { cwd: tempDir, encoding: 'utf8' });

  // Verify JSON is still valid
  const content = fs.readFileSync(todosFile, 'utf8');
  const todos = JSON.parse(content);
  assert(Array.isArray(todos), 'todos.json should still be an array');

  // Verify the todo was deleted
  const deletedTodo = todos.find(t => t.id === 1);
  assert(!deletedTodo, 'deleted todo should not exist');

  console.log('✓ Test passed: todos.json valid after delete operation\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// Test: Empty todos.json is valid
console.log('Test 6: Empty todos.json is valid JSON');
try {
  // Delete all remaining todos
  const listResult = spawnSync('node', [cliPath, 'list'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  // If there are todos, find their IDs and delete
  const lines = listResult.stdout.trim().split('\n');
  if (lines[0] !== 'No todos found') {
    for (let i = 2; i <= 10; i++) {
      spawnSync('node', [cliPath, 'delete', String(i)], { cwd: tempDir, encoding: 'utf8' });
    }
  }

  // Verify JSON is still valid when empty
  const content = fs.readFileSync(todosFile, 'utf8');
  const todos = JSON.parse(content);
  assert(Array.isArray(todos), 'empty todos.json should still be an array');
  assert(todos.length === 0, 'todos array should be empty');

  console.log('✓ Test passed: empty todos.json is valid JSON\n');
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
