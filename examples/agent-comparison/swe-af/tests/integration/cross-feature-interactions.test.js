#!/usr/bin/env node

/**
 * Cross-Feature Integration Tests
 *
 * These tests verify interactions between different modules and features:
 * 1. CLI Router + Commands + Store (end-to-end command execution)
 * 2. Store + Utils (data persistence and formatting)
 * 3. Commands + Utils (validation and business logic)
 * 4. Error handling across all layers
 * 5. Data integrity across sequential operations
 */

const assert = require('assert');
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

let testCount = 0;
let passCount = 0;
let failCount = 0;
let testDir = null;

const cliPath = path.resolve(__dirname, '../../cli.js');
const storePath = path.resolve(__dirname, '../../lib/store.js');
const commandsPath = path.resolve(__dirname, '../../lib/commands.js');
const utilsPath = path.resolve(__dirname, '../../lib/utils.js');

// Test helper
function test(name, fn) {
  testCount++;
  try {
    fn();
    console.log(`✓ ${name}`);
    passCount++;
  } catch (error) {
    console.log(`✗ ${name}`);
    console.log(`  Error: ${error.message}`);
    failCount++;
  }
}

function describe(suiteName, fn) {
  console.log(`\n${suiteName}`);
  fn();
}

// Helper to run CLI command
function runCli(args, options = {}) {
  const testWorkDir = options.workDir || testDir;
  const result = spawnSync('node', [cliPath, ...args], {
    cwd: testWorkDir,
    stdio: ['pipe', 'pipe', 'pipe'],
    encoding: 'utf8'
  });

  return {
    exitCode: result.status,
    stdout: result.stdout,
    stderr: result.stderr
  };
}

// Setup and teardown
function beforeEach() {
  testDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cli-test-'));
}

function afterEach() {
  if (testDir && fs.existsSync(testDir)) {
    const files = fs.readdirSync(testDir);
    files.forEach(file => {
      fs.unlinkSync(path.join(testDir, file));
    });
    fs.rmdirSync(testDir);
  }
  testDir = null;
}

console.log('\n' + '='.repeat(70));
console.log('CROSS-FEATURE INTEGRATION TESTS');
console.log('='.repeat(70));

// ===== Test Suite 1: CLI → Commands → Store Chain =====
describe('Suite 1: CLI Router → Commands → Store Integration Chain', () => {
  test('Add command flows through CLI → Commands → Store correctly', () => {
    beforeEach();
    const result = runCli(['add', 'Integration Test Task']);
    afterEach();

    assert.strictEqual(result.exitCode, 0, 'Exit code should be 0 for add success');
    assert(result.stdout.includes('Created todo ID:'), 'Should output success message');
    assert(result.stdout.includes('1'), 'Should assign ID 1');
  });

  test('List command flows through CLI → Commands → Store and retrieves persisted data', () => {
    beforeEach();

    // Add two todos
    runCli(['add', 'First Task']);
    runCli(['add', 'Second Task']);

    // List should retrieve all
    const listResult = runCli(['list']);
    afterEach();

    assert.strictEqual(listResult.exitCode, 0, 'Exit code should be 0 for list');
    assert(listResult.stdout.includes('First Task'), 'Should list first task');
    assert(listResult.stdout.includes('Second Task'), 'Should list second task');
    assert(listResult.stdout.includes('1 |'), 'Should show ID 1');
    assert(listResult.stdout.includes('2 |'), 'Should show ID 2');
  });

  test('Complete command updates state through CLI → Commands → Store', () => {
    beforeEach();

    runCli(['add', 'Complete Test']);
    const completeResult = runCli(['complete', '1']);
    const listResult = runCli(['list']);
    afterEach();

    assert.strictEqual(completeResult.exitCode, 0, 'Complete should exit 0');
    assert(completeResult.stdout.includes('complete'), 'Should output completion message');
    assert(listResult.stdout.includes('[x]'), 'List should show completed status');
  });

  test('Delete command removes from store through CLI → Commands → Store', () => {
    beforeEach();

    runCli(['add', 'Delete Test']);
    const deleteResult = runCli(['delete', '1']);
    const listResult = runCli(['list']);
    afterEach();

    assert.strictEqual(deleteResult.exitCode, 0, 'Delete should exit 0');
    assert(deleteResult.stdout.includes('Deleted todo 1'), 'Should output delete message');
    assert(listResult.stdout.includes('No todos found'), 'List should be empty after delete');
  });
});

// ===== Test Suite 2: Store + Utils Integration =====
describe('Suite 2: Store ↔ Utils Integration', () => {
  test('Store persists data in format that Utils can parse', () => {
    beforeEach();

    // Add a todo
    runCli(['add', 'Store Utils Integration']);

    // Verify todos.json exists and is valid
    const todosFile = path.join(testDir, 'todos.json');
    assert(fs.existsSync(todosFile), 'todos.json should exist');

    const content = fs.readFileSync(todosFile, 'utf8');
    const todos = JSON.parse(content);

    assert(Array.isArray(todos), 'Should be an array');
    assert(todos.length === 1, 'Should have one todo');
    assert(todos[0].id === 1, 'Should have correct ID');
    assert(todos[0].title === 'Store Utils Integration', 'Should have correct title');
    assert(todos[0].completed === false, 'Should be uncompleted');

    afterEach();
  });

  test('Utils formatTodo works with Store-persisted data', () => {
    beforeEach();

    runCli(['add', 'Format Test']);
    runCli(['complete', '1']);

    const todosFile = path.join(testDir, 'todos.json');
    const content = fs.readFileSync(todosFile, 'utf8');
    const todos = JSON.parse(content);

    // Import formatTodo
    const utils = require(utilsPath);
    const formatted = utils.formatTodo(todos[0]);

    assert(formatted.includes('1 |'), 'Should have ID');
    assert(formatted.includes('Format Test'), 'Should have title');
    assert(formatted.includes('[x]'), 'Should show completed status');

    afterEach();
  });
});

// ===== Test Suite 3: Commands + Utils Validation =====
describe('Suite 3: Commands ↔ Utils Validation Integration', () => {
  test('Utils validation prevents invalid titles from reaching Store', () => {
    beforeEach();

    const emptyResult = runCli(['add', '']);
    const whitespaceResult = runCli(['add', '   ']);

    afterEach();

    assert.strictEqual(emptyResult.exitCode, 1, 'Empty title should exit 1');
    assert(emptyResult.stdout.includes('Error:'), 'Should output error');
    assert(whitespaceResult.exitCode, 1, 'Whitespace title should exit 1');
  });

  test('Valid titles pass through validation and reach Store', () => {
    beforeEach();

    runCli(['add', 'Valid Title']);

    const todosFile = path.join(testDir, 'todos.json');
    const content = fs.readFileSync(todosFile, 'utf8');
    const todos = JSON.parse(content);

    assert(todos.length === 1, 'Valid title should be stored');
    assert(todos[0].title === 'Valid Title', 'Title should be preserved exactly');

    afterEach();
  });
});

// ===== Test Suite 4: Error Handling Across Layers =====
describe('Suite 4: Error Handling Across Layers', () => {
  test('Invalid command at CLI layer exits with code 1', () => {
    beforeEach();
    const result = runCli(['invalid-command']);
    afterEach();

    assert.strictEqual(result.exitCode, 1, 'Invalid command should exit 1');
    assert(result.stdout.includes('Error:'), 'Should output error message');
  });

  test('Non-existent ID error propagates from Store → Commands → CLI', () => {
    beforeEach();

    const completeResult = runCli(['complete', '999']);
    const deleteResult = runCli(['delete', '999']);
    afterEach();

    assert.strictEqual(completeResult.exitCode, 1, 'Complete non-existent should exit 1');
    assert(completeResult.stdout.includes('not found'), 'Should mention ID not found');
    assert.strictEqual(deleteResult.exitCode, 1, 'Delete non-existent should exit 1');
    assert(deleteResult.stdout.includes('not found'), 'Should mention ID not found');
  });

  test('Error messages are consistent across all operations', () => {
    beforeEach();

    const results = [
      runCli(['complete', '999']),
      runCli(['delete', '999']),
      runCli(['add', '']),
      runCli(['invalid-cmd'])
    ];
    afterEach();

    results.forEach(result => {
      assert(result.stdout.includes('Error:'), 'All errors should be prefixed with "Error:"');
      assert.strictEqual(result.exitCode, 1, 'All errors should exit with code 1');
    });
  });
});

// ===== Test Suite 5: Data Integrity Across Operations =====
describe('Suite 5: Data Integrity Across Sequential Operations', () => {
  test('ID sequence remains consistent after adds/deletes/adds', () => {
    beforeEach();

    // Add 3 todos
    runCli(['add', 'Task 1']);
    runCli(['add', 'Task 2']);
    runCli(['add', 'Task 3']);

    // Delete middle one
    runCli(['delete', '2']);

    // Add new one - should get ID 4 (not reused)
    const addResult = runCli(['add', 'Task 4']);

    // Verify in list
    const listResult = runCli(['list']);
    afterEach();

    assert(addResult.stdout.includes('Created todo ID: 4'), 'Should assign ID 4, not reuse 2');
    assert(listResult.stdout.includes('1 |'), 'Should have ID 1');
    assert(!listResult.stdout.includes('2 |'), 'Should not have ID 2 (deleted)');
    assert(listResult.stdout.includes('3 |'), 'Should have ID 3');
    assert(listResult.stdout.includes('4 |'), 'Should have ID 4');
  });

  test('Complete status persists across multiple operations', () => {
    beforeEach();

    // Add two todos
    runCli(['add', 'Task A']);
    runCli(['add', 'Task B']);

    // Complete first one
    runCli(['complete', '1']);

    // Add a third one
    runCli(['add', 'Task C']);

    // List and verify status
    const listResult = runCli(['list']);
    const lines = listResult.stdout.split('\n').filter(line => line.includes('|'));

    afterEach();

    assert(lines[0].includes('[x]'), 'Task 1 should be completed');
    assert(lines[1].includes('[ ]'), 'Task 2 should be incomplete');
    assert(lines[2].includes('[ ]'), 'Task 3 should be incomplete');
  });

  test('Completing already-completed todo returns info message with exit 0', () => {
    beforeEach();

    runCli(['add', 'Test']);
    runCli(['complete', '1']);

    const secondCompleteResult = runCli(['complete', '1']);
    afterEach();

    assert.strictEqual(secondCompleteResult.exitCode, 0, 'Already-completed should exit 0');
    assert(secondCompleteResult.stdout.includes('already complete'), 'Should mention already complete');
  });

  test('Data consistency with todos.json matches CLI output', () => {
    beforeEach();

    runCli(['add', 'Consistency Test 1']);
    runCli(['add', 'Consistency Test 2']);
    runCli(['complete', '1']);

    const listResult = runCli(['list']);

    const todosFile = path.join(testDir, 'todos.json');
    const todos = JSON.parse(fs.readFileSync(todosFile, 'utf8'));

    afterEach();

    // Verify file and CLI output agree
    assert(todos.length === 2, 'File should have 2 todos');
    assert(todos[0].completed === true, 'First todo should be completed in file');
    assert(todos[1].completed === false, 'Second todo should be incomplete in file');

    assert(listResult.stdout.includes('[x]'), 'CLI should show completed status');
    assert(listResult.stdout.includes('[ ]'), 'CLI should show incomplete status');
  });
});

// ===== Test Suite 6: Case-Insensitivity Across Layers =====
describe('Suite 6: Case-Insensitive Commands Through All Layers', () => {
  test('All command variations work identically', () => {
    beforeEach();

    const variants = [
      ['add', 'Test'],
      ['ADD', 'Test'],
      ['Add', 'Test'],
      ['aDd', 'Test']
    ];

    let firstId = null;
    variants.forEach((args, index) => {
      const result = runCli(args);
      if (index === 0) {
        const match = result.stdout.match(/Created todo ID: (\d+)/);
        firstId = match ? match[1] : null;
      } else {
        assert.strictEqual(result.exitCode, 0, `${args[0]} should work`);
      }
    });

    afterEach();
    assert(firstId, 'At least one add should succeed');
  });
});

// ===== Test Suite 7: Special Characters and Edge Cases =====
describe('Suite 7: Special Characters Through All Layers', () => {
  test('Titles with special characters persist correctly through all layers', () => {
    beforeEach();

    const specialTitle = 'Task with "quotes" and $variables and #hashtags';
    runCli(['add', specialTitle]);

    const listResult = runCli(['list']);
    const todosFile = path.join(testDir, 'todos.json');
    const todos = JSON.parse(fs.readFileSync(todosFile, 'utf8'));

    afterEach();

    assert(listResult.stdout.includes(specialTitle), 'CLI should display special characters');
    assert(todos[0].title === specialTitle, 'File should preserve special characters');
  });

  test('Unicode characters handled correctly through all layers', () => {
    beforeEach();

    const unicodeTitle = 'Buy 🍕 and 📚 and café';
    runCli(['add', unicodeTitle]);

    const listResult = runCli(['list']);
    const todosFile = path.join(testDir, 'todos.json');
    const todos = JSON.parse(fs.readFileSync(todosFile, 'utf8'));

    afterEach();

    assert(listResult.stdout.includes('Buy'), 'Should display unicode text');
    assert(todos[0].title === unicodeTitle, 'Should preserve unicode exactly');
  });
});

// ===== Test Suite 8: Concurrent Access via CLI =====
describe('Suite 8: Multiple Commands Sequentially (Simulated Concurrent)', () => {
  test('Multiple add operations maintain sequential IDs', () => {
    beforeEach();

    const ids = [];
    for (let i = 1; i <= 5; i++) {
      const result = runCli(['add', `Task ${i}`]);
      const match = result.stdout.match(/ID: (\d+)/);
      if (match) ids.push(parseInt(match[1]));
    }

    const listResult = runCli(['list']);
    afterEach();

    assert.deepStrictEqual(ids, [1, 2, 3, 4, 5], 'Should assign sequential IDs');
    for (let i = 1; i <= 5; i++) {
      assert(listResult.stdout.includes(`${i} |`), `Should list ID ${i}`);
    }
  });

  test('Mixed operations maintain data integrity', () => {
    beforeEach();

    runCli(['add', 'Task 1']);
    runCli(['add', 'Task 2']);
    runCli(['add', 'Task 3']);
    runCli(['complete', '2']);
    runCli(['delete', '1']);
    runCli(['add', 'Task 4']);
    runCli(['complete', '3']);

    const listResult = runCli(['list']);
    const todosFile = path.join(testDir, 'todos.json');
    const todos = JSON.parse(fs.readFileSync(todosFile, 'utf8'));

    afterEach();

    assert.strictEqual(todos.length, 3, 'Should have 3 todos (1 deleted)');
    assert.deepStrictEqual(
      todos.map(t => ({ id: t.id, completed: t.completed })),
      [
        { id: 2, completed: true },
        { id: 3, completed: true },
        { id: 4, completed: false }
      ],
      'Should maintain correct state'
    );
  });
});

// Run tests
console.log('\n' + '='.repeat(70));
console.log(`Tests run: ${testCount}, Passed: ${passCount}, Failed: ${failCount}`);
console.log('='.repeat(70) + '\n');

process.exit(failCount > 0 ? 1 : 0);
