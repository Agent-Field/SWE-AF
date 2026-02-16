const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { addTodo, listTodos, completeTodo, deleteTodo } = require('../../lib/commands');

// Test helpers
const TODOS_FILE = path.join(process.cwd(), 'todos.json');

function resetStore() {
  // Remove todos.json to start fresh
  if (fs.existsSync(TODOS_FILE)) {
    fs.unlinkSync(TODOS_FILE);
  }
}

function getTodosFromFile() {
  try {
    if (!fs.existsSync(TODOS_FILE)) {
      return [];
    }
    const content = fs.readFileSync(TODOS_FILE, 'utf8');
    return JSON.parse(content);
  } catch (error) {
    return [];
  }
}

let testsPassed = 0;
let testsFailed = 0;

function test(description, fn) {
  try {
    resetStore();
    fn();
    console.log(`✓ ${description}`);
    testsPassed++;
  } catch (error) {
    console.error(`✗ ${description}`);
    console.error(`  ${error.message}`);
    testsFailed++;
  }
}

// ============================================================================
// addTodo Tests
// ============================================================================

test('addTodo with valid title "Buy milk" returns id 1 and success message', () => {
  const result = addTodo('Buy milk');
  assert.strictEqual(result.id, 1);
  assert.strictEqual(result.message, 'Created todo ID: 1');
});

test('addTodo with valid title creates todo in store', () => {
  addTodo('Buy milk');
  const todos = getTodosFromFile();
  assert.strictEqual(todos.length, 1);
  assert.strictEqual(todos[0].id, 1);
  assert.strictEqual(todos[0].title, 'Buy milk');
  assert.strictEqual(todos[0].completed, false);
});

test('addTodo with empty string "" returns null id and error message', () => {
  const result = addTodo('');
  assert.strictEqual(result.id, null);
  assert.strictEqual(result.message, 'Error: Todo title cannot be empty');
});

test('addTodo with whitespace-only "   " returns null id and error message', () => {
  const result = addTodo('   ');
  assert.strictEqual(result.id, null);
  assert.strictEqual(result.message, 'Error: Todo title cannot be empty');
});

test('addTodo with empty string does not save to store', () => {
  addTodo('');
  const todos = getTodosFromFile();
  assert.strictEqual(todos.length, 0);
});

test('Sequential addTodo calls assign IDs 1, 2, 3', () => {
  const result1 = addTodo('Task 1');
  const result2 = addTodo('Task 2');
  const result3 = addTodo('Task 3');

  assert.strictEqual(result1.id, 1);
  assert.strictEqual(result2.id, 2);
  assert.strictEqual(result3.id, 3);

  const todos = getTodosFromFile();
  assert.strictEqual(todos.length, 3);
  assert.strictEqual(todos[0].id, 1);
  assert.strictEqual(todos[1].id, 2);
  assert.strictEqual(todos[2].id, 3);
});

test('Sequential addTodo generates correct success messages', () => {
  const result1 = addTodo('First');
  const result2 = addTodo('Second');

  assert.strictEqual(result1.message, 'Created todo ID: 1');
  assert.strictEqual(result2.message, 'Created todo ID: 2');
});

test('addTodo persists across multiple calls (store persistence)', () => {
  addTodo('Task 1');
  const todos1 = getTodosFromFile();
  assert.strictEqual(todos1.length, 1);

  addTodo('Task 2');
  const todos2 = getTodosFromFile();
  assert.strictEqual(todos2.length, 2);
});

// ============================================================================
// listTodos Tests
// ============================================================================

test('listTodos with empty store returns ["No todos found"]', () => {
  const result = listTodos();
  assert.deepStrictEqual(result, ['No todos found']);
});

test('listTodos with one todo returns formatted string array', () => {
  addTodo('Buy milk');
  const result = listTodos();

  assert.strictEqual(result.length, 1);
  assert.strictEqual(result[0], '1 | Buy milk | [ ]');
});

test('listTodos with two todos returns two formatted strings', () => {
  addTodo('Buy milk');
  addTodo('Walk dog');
  const result = listTodos();

  assert.strictEqual(result.length, 2);
  assert.strictEqual(result[0], '1 | Buy milk | [ ]');
  assert.strictEqual(result[1], '2 | Walk dog | [ ]');
});

test('listTodos shows incomplete todo with [ ] status', () => {
  addTodo('Incomplete task');
  const result = listTodos();
  assert(result[0].includes('[ ]'));
});

test('listTodos shows completed todo with [x] status', () => {
  addTodo('Task');
  completeTodo('1');
  const result = listTodos();
  assert(result[0].includes('[x]'));
});

test('listTodos with mixed completed and incomplete todos shows correct statuses', () => {
  addTodo('Task 1');
  addTodo('Task 2');
  addTodo('Task 3');
  completeTodo('1');
  completeTodo('3');

  const result = listTodos();
  assert.strictEqual(result.length, 3);
  assert(result[0].includes('[x]')); // Task 1 completed
  assert(result[1].includes('[ ]')); // Task 2 incomplete
  assert(result[2].includes('[x]')); // Task 3 completed
});

test('listTodos returns array type', () => {
  const result = listTodos();
  assert(Array.isArray(result));
});

// ============================================================================
// completeTodo Tests
// ============================================================================

test('completeTodo on incomplete todo returns success true', () => {
  addTodo('Task');
  const result = completeTodo('1');
  assert.strictEqual(result.success, true);
});

test('completeTodo on incomplete todo returns correct message', () => {
  addTodo('Task');
  const result = completeTodo('1');
  assert.strictEqual(result.message, 'Marked todo 1 as complete');
});

test('completeTodo on incomplete todo marks it completed in store', () => {
  addTodo('Task');
  completeTodo('1');

  const todos = getTodosFromFile();
  assert.strictEqual(todos[0].completed, true);
});

test('completeTodo on already-completed todo returns success true', () => {
  addTodo('Task');
  completeTodo('1');
  const result = completeTodo('1');
  assert.strictEqual(result.success, true);
});

test('completeTodo on already-completed todo returns info message', () => {
  addTodo('Task');
  completeTodo('1');
  const result = completeTodo('1');
  assert.strictEqual(result.message, 'Todo 1 is already complete');
});

test('completeTodo on already-completed todo does not change data', () => {
  addTodo('Task');
  completeTodo('1');
  const todos1 = getTodosFromFile();

  completeTodo('1');
  const todos2 = getTodosFromFile();

  assert.deepStrictEqual(todos1, todos2);
});

test('completeTodo with non-existent ID returns success false', () => {
  addTodo('Task');
  const result = completeTodo('999');
  assert.strictEqual(result.success, false);
});

test('completeTodo with non-existent ID returns error message', () => {
  addTodo('Task');
  const result = completeTodo('999');
  assert.strictEqual(result.message, 'Error: Todo ID 999 not found');
});

test('completeTodo with invalid ID format "abc" returns success false', () => {
  addTodo('Task');
  const result = completeTodo('abc');
  assert.strictEqual(result.success, false);
});

test('completeTodo with invalid ID format returns error message', () => {
  addTodo('Task');
  const result = completeTodo('abc');
  assert.strictEqual(result.message, 'Error: Invalid ID format');
});

test('completeTodo with empty string returns error message', () => {
  addTodo('Task');
  const result = completeTodo('');
  assert.strictEqual(result.success, false);
  assert.strictEqual(result.message, 'Error: Invalid ID format');
});

test('completeTodo persists data after completion', () => {
  addTodo('Task');
  completeTodo('1');
  const todos = getTodosFromFile();

  assert.strictEqual(todos.length, 1);
  assert.strictEqual(todos[0].completed, true);
});

test('completeTodo with string number "1" parses correctly', () => {
  addTodo('Task');
  const result = completeTodo('1');
  assert.strictEqual(result.success, true);
  assert.strictEqual(result.message, 'Marked todo 1 as complete');
});

test('completeTodo on empty store with valid ID returns not found', () => {
  const result = completeTodo('1');
  assert.strictEqual(result.success, false);
  assert.strictEqual(result.message, 'Error: Todo ID 1 not found');
});

// ============================================================================
// deleteTodo Tests
// ============================================================================

test('deleteTodo on existing todo returns success true', () => {
  addTodo('Task');
  const result = deleteTodo('1');
  assert.strictEqual(result.success, true);
});

test('deleteTodo on existing todo returns correct message', () => {
  addTodo('Task');
  const result = deleteTodo('1');
  assert.strictEqual(result.message, 'Deleted todo 1');
});

test('deleteTodo on existing todo removes from store', () => {
  addTodo('Task');
  deleteTodo('1');

  const todos = getTodosFromFile();
  assert.strictEqual(todos.length, 0);
});

test('deleteTodo with non-existent ID returns success false', () => {
  addTodo('Task');
  const result = deleteTodo('999');
  assert.strictEqual(result.success, false);
});

test('deleteTodo with non-existent ID returns error message', () => {
  addTodo('Task');
  const result = deleteTodo('999');
  assert.strictEqual(result.message, 'Error: Todo ID 999 not found');
});

test('deleteTodo with invalid ID format "abc" returns success false', () => {
  addTodo('Task');
  const result = deleteTodo('abc');
  assert.strictEqual(result.success, false);
});

test('deleteTodo with invalid ID format returns error message', () => {
  addTodo('Task');
  const result = deleteTodo('abc');
  assert.strictEqual(result.message, 'Error: Invalid ID format');
});

test('deleteTodo with empty string returns error message', () => {
  addTodo('Task');
  const result = deleteTodo('');
  assert.strictEqual(result.success, false);
  assert.strictEqual(result.message, 'Error: Invalid ID format');
});

test('deleteTodo deletes only specified todo from multiple', () => {
  addTodo('Task 1');
  addTodo('Task 2');
  addTodo('Task 3');

  deleteTodo('2');

  const todos = getTodosFromFile();
  assert.strictEqual(todos.length, 2);
  assert.strictEqual(todos[0].id, 1);
  assert.strictEqual(todos[1].id, 3);
});

test('deleteTodo persists data after deletion', () => {
  addTodo('Task 1');
  addTodo('Task 2');
  deleteTodo('1');

  const todos = getTodosFromFile();
  assert.strictEqual(todos.length, 1);
  assert.strictEqual(todos[0].id, 2);
});

test('deleteTodo with string number "1" parses correctly', () => {
  addTodo('Task');
  const result = deleteTodo('1');
  assert.strictEqual(result.success, true);
  assert.strictEqual(result.message, 'Deleted todo 1');
});

test('deleteTodo on empty store with valid ID returns not found', () => {
  const result = deleteTodo('1');
  assert.strictEqual(result.success, false);
  assert.strictEqual(result.message, 'Error: Todo ID 1 not found');
});

// ============================================================================
// Edge Cases and Integration Tests
// ============================================================================

test('Delete todo and re-add generates correct new ID', () => {
  addTodo('Task 1');
  addTodo('Task 2');
  deleteTodo('1');
  const result3 = addTodo('Task 3');

  assert.strictEqual(result3.id, 3);
});

test('Complete, delete, and list shows correct state', () => {
  addTodo('Task 1');
  addTodo('Task 2');
  completeTodo('1');
  deleteTodo('2');

  const result = listTodos();
  assert.strictEqual(result.length, 1);
  assert(result[0].includes('[x]')); // Task 1 is completed
  assert(result[0].includes('1 |'));
});

test('Multiple operations maintain data integrity', () => {
  addTodo('A');
  addTodo('B');
  completeTodo('1');
  addTodo('C');
  deleteTodo('2');

  const todos = getTodosFromFile();
  assert.strictEqual(todos.length, 2);
  assert.strictEqual(todos[0].id, 1);
  assert.strictEqual(todos[0].completed, true);
  assert.strictEqual(todos[1].id, 3);
  assert.strictEqual(todos[1].completed, false);
});

test('addTodo with title containing special characters', () => {
  const result = addTodo('Task with "quotes" and $vars');
  assert.strictEqual(result.id, 1);

  const todos = getTodosFromFile();
  assert.strictEqual(todos[0].title, 'Task with "quotes" and $vars');
});

test('listTodos after multiple additions shows all todos in order', () => {
  addTodo('First');
  addTodo('Second');
  addTodo('Third');

  const result = listTodos();
  assert.strictEqual(result.length, 3);
  assert(result[0].includes('First'));
  assert(result[1].includes('Second'));
  assert(result[2].includes('Third'));
});

test('completeTodo with large ID number', () => {
  addTodo('Task');
  const result = completeTodo('1');
  assert.strictEqual(result.success, true);

  const result2 = completeTodo('999999');
  assert.strictEqual(result2.success, false);
});

test('Title with leading and trailing spaces is preserved', () => {
  const result = addTodo('  Task with spaces  ');
  assert.strictEqual(result.id, 1);

  const todos = getTodosFromFile();
  assert.strictEqual(todos[0].title, '  Task with spaces  ');
});

test('addTodo with title containing newlines', () => {
  const result = addTodo('Task\nwith\nnewlines');
  assert.strictEqual(result.id, 1);

  const todos = getTodosFromFile();
  assert.strictEqual(todos[0].title, 'Task\nwith\nnewlines');
});

// ============================================================================
// Test Summary
// ============================================================================

// Clean up
resetStore();

console.log('\n' + '='.repeat(70));
console.log(`Tests passed: ${testsPassed}`);
console.log(`Tests failed: ${testsFailed}`);
console.log('='.repeat(70));

if (testsFailed > 0) {
  process.exit(1);
}
