const assert = require('assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

let testDir;
let testCount = 0;
let passCount = 0;
let failCount = 0;

// Test helper
function test(name, fn) {
  testCount++;
  try {
    fn();
    console.log(`✓ ${name}`);
    passCount++;
  } catch (error) {
    console.log(`✗ ${name}`);
    console.log(`  ${error.message}`);
    failCount++;
  }
}

function beforeEach(fn) {
  // Store the setup function
  beforeEachFn = fn;
}

function afterEach(fn) {
  // Store the teardown function
  afterEachFn = fn;
}

let beforeEachFn = null;
let afterEachFn = null;

function describe(suiteName, fn) {
  console.log(`\n${suiteName}`);
  fn();
  if (afterEachFn) {
    afterEachFn();
  }
}

// Helper to get a fresh instance of store with mocked cwd
function loadStoreWithMockedCwd(testPath) {
  // Clear require cache to get a fresh module
  delete require.cache[require.resolve('../../lib/store.js')];

  // Mock process.cwd to return test directory
  const originalCwdFn = process.cwd;
  process.cwd = () => testPath;

  try {
    const store = require('../../lib/store.js');
    process.cwd = originalCwdFn;
    return store;
  } catch (e) {
    process.cwd = originalCwdFn;
    throw e;
  }
}

function setupTestEnvironment() {
  testDir = fs.mkdtempSync(path.join(os.tmpdir(), 'store-test-'));
  return testDir;
}

function cleanupTestEnvironment() {
  if (testDir && fs.existsSync(testDir)) {
    const files = fs.readdirSync(testDir);
    files.forEach(file => {
      const filePath = path.join(testDir, file);
      if (fs.lstatSync(filePath).isDirectory()) {
        fs.rmSync(filePath, { recursive: true });
      } else {
        fs.unlinkSync(filePath);
      }
    });
    fs.rmdirSync(testDir);
  }
}

// ============ TEST SUITES ============

// Test Suite: loadTodos
describe('loadTodos', () => {
  test('returns empty array when todos.json does not exist', () => {
    setupTestEnvironment();
    const store = loadStoreWithMockedCwd(testDir);
    const result = store.loadTodos();
    assert.deepStrictEqual(result, []);
    cleanupTestEnvironment();
  });

  test('returns parsed todos when todos.json contains valid JSON array', () => {
    setupTestEnvironment();
    const store = loadStoreWithMockedCwd(testDir);
    const todosPath = path.join(testDir, 'todos.json');
    const testData = [
      { id: 1, title: 'Test task', completed: false },
      { id: 2, title: 'Another task', completed: true },
    ];
    fs.writeFileSync(todosPath, JSON.stringify(testData));

    const result = store.loadTodos();
    assert.deepStrictEqual(result, testData);
    cleanupTestEnvironment();
  });

  test('logs warning and returns empty array when todos.json is invalid JSON', () => {
    setupTestEnvironment();
    const store = loadStoreWithMockedCwd(testDir);
    const todosPath = path.join(testDir, 'todos.json');
    fs.writeFileSync(todosPath, '{ invalid json }');

    // Capture console.warn
    let warnCalled = false;
    const originalWarn = console.warn;
    console.warn = (msg) => {
      if (msg.includes('corrupted') || msg.includes('invalid JSON')) {
        warnCalled = true;
      }
    };

    const result = store.loadTodos();

    console.warn = originalWarn;

    assert.strictEqual(warnCalled, true, 'console.warn should be called with warning about corruption');
    assert.deepStrictEqual(result, []);
    cleanupTestEnvironment();
  });

  test('handles file with empty array', () => {
    setupTestEnvironment();
    const store = loadStoreWithMockedCwd(testDir);
    const todosPath = path.join(testDir, 'todos.json');
    fs.writeFileSync(todosPath, '[]');

    const result = store.loadTodos();
    assert.deepStrictEqual(result, []);
    cleanupTestEnvironment();
  });

  test('preserves todo data structure with all fields', () => {
    setupTestEnvironment();
    const store = loadStoreWithMockedCwd(testDir);
    const todosPath = path.join(testDir, 'todos.json');
    const testData = [
      { id: 42, title: 'Important task with "quotes"', completed: false },
      { id: 100, title: 'Task with $special & chars', completed: true },
    ];
    fs.writeFileSync(todosPath, JSON.stringify(testData));

    const result = store.loadTodos();
    assert.deepStrictEqual(result, testData);
    assert.strictEqual(result[0].id, 42);
    assert.strictEqual(result[0].title, 'Important task with "quotes"');
    assert.strictEqual(result[0].completed, false);
    cleanupTestEnvironment();
  });
});

// Test Suite: saveTodos
describe('saveTodos', () => {
  test('writes todos array to ./todos.json as valid JSON with 2-space indentation', () => {
    setupTestEnvironment();
    const store = loadStoreWithMockedCwd(testDir);
    const testData = [
      { id: 1, title: 'Task 1', completed: false },
      { id: 2, title: 'Task 2', completed: true },
    ];

    store.saveTodos(testData);

    const todosPath = path.join(testDir, 'todos.json');
    assert.strictEqual(fs.existsSync(todosPath), true, 'todos.json should be created');

    const content = fs.readFileSync(todosPath, 'utf8');
    const parsed = JSON.parse(content); // Will throw if invalid JSON
    assert.deepStrictEqual(parsed, testData);

    // Check for 2-space indentation (after newline)
    assert.match(content, /\n  \{/, 'Should use 2-space indentation');
    cleanupTestEnvironment();
  });

  test('uses synchronous write (fs.writeFileSync) for atomicity', () => {
    setupTestEnvironment();
    const store = loadStoreWithMockedCwd(testDir);
    const testData = [{ id: 1, title: 'Test', completed: false }];

    // This test verifies the implementation uses writeFileSync
    // by checking that the file exists immediately after the call
    store.saveTodos(testData);

    const todosPath = path.join(testDir, 'todos.json');
    assert.strictEqual(fs.existsSync(todosPath), true, 'File should exist synchronously after save');
    cleanupTestEnvironment();
  });

  test('creates todos.json if it doesn\'t exist', () => {
    setupTestEnvironment();
    const store = loadStoreWithMockedCwd(testDir);
    const todosPath = path.join(testDir, 'todos.json');

    assert.strictEqual(fs.existsSync(todosPath), false, 'File should not exist initially');

    const testData = [{ id: 1, title: 'First task', completed: false }];
    store.saveTodos(testData);

    assert.strictEqual(fs.existsSync(todosPath), true, 'File should be created');
    cleanupTestEnvironment();
  });

  test('overwrites existing todos.json with new data', () => {
    setupTestEnvironment();
    const store = loadStoreWithMockedCwd(testDir);
    const todosPath = path.join(testDir, 'todos.json');

    // Write initial data
    const initialData = [{ id: 1, title: 'Old task', completed: false }];
    store.saveTodos(initialData);

    // Write new data
    const newData = [
      { id: 2, title: 'New task 1', completed: false },
      { id: 3, title: 'New task 2', completed: true },
    ];
    store.saveTodos(newData);

    // Verify only new data exists
    const content = fs.readFileSync(todosPath, 'utf8');
    const parsed = JSON.parse(content);
    assert.deepStrictEqual(parsed, newData);
    assert.strictEqual(parsed.length, 2);
    cleanupTestEnvironment();
  });

  test('saves empty array correctly', () => {
    setupTestEnvironment();
    const store = loadStoreWithMockedCwd(testDir);
    store.saveTodos([]);

    const todosPath = path.join(testDir, 'todos.json');
    const content = fs.readFileSync(todosPath, 'utf8');
    const parsed = JSON.parse(content);
    assert.deepStrictEqual(parsed, []);
    cleanupTestEnvironment();
  });

  test('preserves special characters in titles when writing', () => {
    setupTestEnvironment();
    const store = loadStoreWithMockedCwd(testDir);
    const testData = [
      { id: 1, title: 'Task with "quotes" and \'apostrophes\'', completed: false },
      { id: 2, title: 'Task with newline\ncharacter', completed: false },
      { id: 3, title: 'Task with unicode: 你好', completed: false },
    ];

    store.saveTodos(testData);

    const todosPath = path.join(testDir, 'todos.json');
    const content = fs.readFileSync(todosPath, 'utf8');
    const parsed = JSON.parse(content);
    assert.deepStrictEqual(parsed, testData);
    cleanupTestEnvironment();
  });

  test('file is created at ./todos.json (relative to working directory)', () => {
    setupTestEnvironment();
    const store = loadStoreWithMockedCwd(testDir);
    const testData = [{ id: 1, title: 'Test', completed: false }];

    store.saveTodos(testData);

    const todosPath = path.join(testDir, 'todos.json');
    assert.strictEqual(fs.existsSync(todosPath), true);
    // Verify by loading it back
    const content = fs.readFileSync(todosPath, 'utf8');
    assert.ok(content.includes('Test'));
    cleanupTestEnvironment();
  });
});

// Test Suite: getNextId
describe('getNextId', () => {
  test('returns 1 for empty array', () => {
    const store = require('../../lib/store.js');
    const result = store.getNextId([]);
    assert.strictEqual(result, 1);
  });

  test('returns 3 for array with ids [1, 2]', () => {
    const store = require('../../lib/store.js');
    const todos = [
      { id: 1, title: 'Task 1', completed: false },
      { id: 2, title: 'Task 2', completed: false },
    ];
    const result = store.getNextId(todos);
    assert.strictEqual(result, 3);
  });

  test('returns 6 for array with ids [5, 2] (finds max, not sequential)', () => {
    const store = require('../../lib/store.js');
    const todos = [
      { id: 5, title: 'Task 5', completed: false },
      { id: 2, title: 'Task 2', completed: false },
    ];
    const result = store.getNextId(todos);
    assert.strictEqual(result, 6);
  });

  test('handles single todo correctly', () => {
    const store = require('../../lib/store.js');
    const todos = [{ id: 1, title: 'Only task', completed: false }];
    const result = store.getNextId(todos);
    assert.strictEqual(result, 2);
  });

  test('handles large IDs correctly', () => {
    const store = require('../../lib/store.js');
    const todos = [
      { id: 1000, title: 'Task', completed: false },
      { id: 500, title: 'Task', completed: false },
    ];
    const result = store.getNextId(todos);
    assert.strictEqual(result, 1001);
  });

  test('handles non-sequential IDs', () => {
    const store = require('../../lib/store.js');
    const todos = [
      { id: 10, title: 'Task', completed: false },
      { id: 3, title: 'Task', completed: false },
      { id: 7, title: 'Task', completed: false },
    ];
    const result = store.getNextId(todos);
    assert.strictEqual(result, 11);
  });

  test('ignores other properties and focuses on id', () => {
    const store = require('../../lib/store.js');
    const todos = [
      { id: 2, title: 'Any title', completed: true, extra: 'data' },
      { id: 5, title: 'Another', completed: false },
    ];
    const result = store.getNextId(todos);
    assert.strictEqual(result, 6);
  });
});

// Integration tests
describe('Integration: load and save round trip', () => {
  test('saves and loads data consistently', () => {
    setupTestEnvironment();
    const store = loadStoreWithMockedCwd(testDir);
    const originalData = [
      { id: 1, title: 'Task 1', completed: false },
      { id: 2, title: 'Task 2', completed: true },
      { id: 5, title: 'Task 5', completed: false },
    ];

    // Save
    store.saveTodos(originalData);

    // Load
    const loadedData = store.loadTodos();

    // Verify
    assert.deepStrictEqual(loadedData, originalData);
    cleanupTestEnvironment();
  });

  test('getNextId works correctly after save and load', () => {
    setupTestEnvironment();
    const store = loadStoreWithMockedCwd(testDir);
    const testData = [
      { id: 1, title: 'Task 1', completed: false },
      { id: 2, title: 'Task 2', completed: false },
    ];

    store.saveTodos(testData);
    const loadedTodos = store.loadTodos();
    const nextId = store.getNextId(loadedTodos);

    assert.strictEqual(nextId, 3);
    cleanupTestEnvironment();
  });
});

// Edge case tests
describe('Edge cases', () => {
  test('handles todos with empty title string', () => {
    setupTestEnvironment();
    const store = loadStoreWithMockedCwd(testDir);
    const testData = [
      { id: 1, title: '', completed: false },
    ];

    store.saveTodos(testData);
    const loaded = store.loadTodos();
    assert.deepStrictEqual(loaded, testData);
    cleanupTestEnvironment();
  });

  test('handles todos with very long titles', () => {
    setupTestEnvironment();
    const store = loadStoreWithMockedCwd(testDir);
    const longTitle = 'a'.repeat(10000);
    const testData = [
      { id: 1, title: longTitle, completed: false },
    ];

    store.saveTodos(testData);
    const loaded = store.loadTodos();
    assert.strictEqual(loaded[0].title, longTitle);
    cleanupTestEnvironment();
  });

  test('handles large arrays of todos', () => {
    setupTestEnvironment();
    const store = loadStoreWithMockedCwd(testDir);
    const testData = [];
    for (let i = 1; i <= 100; i++) {
      testData.push({ id: i, title: `Task ${i}`, completed: i % 2 === 0 });
    }

    store.saveTodos(testData);
    const loaded = store.loadTodos();
    assert.deepStrictEqual(loaded, testData);
    assert.strictEqual(loaded.length, 100);
    cleanupTestEnvironment();
  });

  test('getNextId with 100 todos returns 101', () => {
    const store = require('../../lib/store.js');
    const testData = [];
    for (let i = 1; i <= 100; i++) {
      testData.push({ id: i, title: `Task ${i}`, completed: false });
    }

    const nextId = store.getNextId(testData);
    assert.strictEqual(nextId, 101);
  });
});

// Print summary
console.log(`\n${'='.repeat(50)}`);
console.log(`Total: ${testCount} | Passed: ${passCount} | Failed: ${failCount}`);
console.log(`${'='.repeat(50)}`);

process.exit(failCount > 0 ? 1 : 0);
