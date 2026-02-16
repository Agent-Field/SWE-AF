const assert = require('assert');
const fs = require('fs');
const path = require('path');

// Clean up todos.json before and after test
function cleanupTodos() {
  const TODOS_FILE = path.join(process.cwd(), 'todos.json');
  if (fs.existsSync(TODOS_FILE)) {
    fs.unlinkSync(TODOS_FILE);
  }
}

console.log('\n========================================');
console.log('Smoke Test: Module Integration');
console.log('========================================\n');

// Test 1: All four modules can be imported without error
console.log('Test 1: Importing all modules...');
try {
  const store = require('../../lib/store');
  const utils = require('../../lib/utils');
  const commands = require('../../lib/commands');
  const cli = require('../../cli');

  assert(store, 'store module is importable');
  assert(utils, 'utils module is importable');
  assert(commands, 'commands module is importable');
  assert(cli, 'cli module is importable');

  assert(store.loadTodos, 'store has loadTodos');
  assert(store.saveTodos, 'store has saveTodos');
  assert(store.getNextId, 'store has getNextId');

  assert(utils.validateTitle, 'utils has validateTitle');
  assert(utils.formatTodo, 'utils has formatTodo');
  assert(utils.printHelp, 'utils has printHelp');

  assert(commands.addTodo, 'commands has addTodo');
  assert(commands.listTodos, 'commands has listTodos');
  assert(commands.completeTodo, 'commands has completeTodo');
  assert(commands.deleteTodo, 'commands has deleteTodo');

  assert(cli.main, 'cli has main');

  console.log('✓ All modules imported successfully\n');
} catch (error) {
  console.error('✗ Module import failed');
  console.error(`  ${error.message}`);
  process.exit(1);
}

// Get fresh references to modules
const store = require('../../lib/store');
const commands = require('../../lib/commands');
const TODOS_FILE = path.join(process.cwd(), 'todos.json');

// Clean up before test
cleanupTodos();

// Test 2: Add a todo and verify ID is returned
console.log('Test 2: Adding a todo...');
try {
  const addResult = commands.addTodo('Buy groceries');

  assert(addResult.id !== null, 'addTodo returns a non-null ID');
  assert.strictEqual(addResult.id, 1, 'First todo has ID 1');
  assert(addResult.message.includes('Created todo'), 'addTodo returns success message');

  console.log(`✓ Todo added with ID ${addResult.id}\n`);
} catch (error) {
  console.error('✗ Adding todo failed');
  console.error(`  ${error.message}`);
  cleanupTodos();
  process.exit(1);
}

// Test 3: List todos and verify output contains the added todo
console.log('Test 3: Listing todos...');
try {
  const listResult = commands.listTodos();

  assert(Array.isArray(listResult), 'listTodos returns an array');
  assert(listResult.length > 0, 'listTodos returns non-empty array');
  assert(listResult[0].includes('Buy groceries'), 'List contains the added todo');
  assert(listResult[0].includes('1'), 'List contains todo ID');

  console.log(`✓ Todos listed: ${listResult[0]}\n`);
} catch (error) {
  console.error('✗ Listing todos failed');
  console.error(`  ${error.message}`);
  cleanupTodos();
  process.exit(1);
}

// Test 4: Mark todo as complete and verify status changes
console.log('Test 4: Completing a todo...');
try {
  const completeResult = commands.completeTodo('1');

  assert(completeResult.success === true, 'completeTodo returns success=true');
  assert(completeResult.message.includes('complete'), 'Complete message contains "complete"');

  // Verify the todo is marked as complete in listing
  const listAfterComplete = commands.listTodos();
  assert(listAfterComplete[0].includes('[x]'), 'Completed todo shows [x] status');

  console.log(`✓ Todo marked as complete\n`);
} catch (error) {
  console.error('✗ Completing todo failed');
  console.error(`  ${error.message}`);
  cleanupTodos();
  process.exit(1);
}

// Test 5: Delete todo and verify it is removed from list
console.log('Test 5: Deleting a todo...');
try {
  const deleteResult = commands.deleteTodo('1');

  assert(deleteResult.success === true, 'deleteTodo returns success=true');
  assert(deleteResult.message.includes('Deleted'), 'Delete message contains "Deleted"');

  // Verify the todo is removed from list
  const listAfterDelete = commands.listTodos();
  assert(listAfterDelete.length === 1, 'List shows "No todos found" after deletion');
  assert(listAfterDelete[0] === 'No todos found', 'Empty list returns "No todos found"');

  console.log('✓ Todo deleted successfully\n');
} catch (error) {
  console.error('✗ Deleting todo failed');
  console.error(`  ${error.message}`);
  cleanupTodos();
  process.exit(1);
}

// Test 6: Verify todos.json is created after first write
console.log('Test 6: Verifying todos.json file creation...');
try {
  // Clean up and start fresh
  cleanupTodos();

  // Add a todo to trigger file write
  commands.addTodo('Test file creation');

  // Verify file exists
  assert(fs.existsSync(TODOS_FILE), 'todos.json file is created after write');

  console.log('✓ todos.json file created\n');
} catch (error) {
  console.error('✗ File creation verification failed');
  console.error(`  ${error.message}`);
  cleanupTodos();
  process.exit(1);
}

// Test 7: Verify todos.json contains valid JSON
console.log('Test 7: Verifying todos.json contains valid JSON...');
try {
  const fileContent = fs.readFileSync(TODOS_FILE, 'utf8');
  const parsed = JSON.parse(fileContent);

  assert(Array.isArray(parsed), 'todos.json parses to an array');
  assert(parsed.length > 0, 'todos.json contains at least one todo');
  assert(parsed[0].id !== undefined, 'Todo object has id property');
  assert(parsed[0].title !== undefined, 'Todo object has title property');
  assert(parsed[0].completed !== undefined, 'Todo object has completed property');

  console.log('✓ todos.json contains valid JSON\n');
} catch (error) {
  console.error('✗ JSON validation failed');
  console.error(`  ${error.message}`);
  cleanupTodos();
  process.exit(1);
}

// Test 8: Final todos array is empty after deleting the only todo
console.log('Test 8: Verifying empty state after deleting all todos...');
try {
  // Clean up and start fresh
  cleanupTodos();

  // Add two todos
  commands.addTodo('First todo');
  commands.addTodo('Second todo');

  // Delete both
  commands.deleteTodo('1');
  commands.deleteTodo('2');

  // Verify both are gone and todos array is empty
  const todos = store.loadTodos();
  assert(todos.length === 0, 'todos array is empty after deleting all todos');

  const listResult = commands.listTodos();
  assert(listResult[0] === 'No todos found', 'List shows "No todos found" when todos array is empty');

  console.log('✓ Final todos array is empty after deleting all todos\n');
} catch (error) {
  console.error('✗ Empty state verification failed');
  console.error(`  ${error.message}`);
  cleanupTodos();
  process.exit(1);
}

// Cleanup
cleanupTodos();

console.log('========================================');
console.log('✓ All smoke tests passed!');
console.log('========================================\n');
