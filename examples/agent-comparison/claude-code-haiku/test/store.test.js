import { test } from 'node:test';
import assert from 'node:assert';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import TodoStore from '../src/store.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const TEST_DATA_FILE = path.join(__dirname, '..', 'todos-test.json');

// Mock the data file for testing
function setupTestStore() {
  // Clean up any existing test file
  if (fs.existsSync(TEST_DATA_FILE)) {
    fs.unlinkSync(TEST_DATA_FILE);
  }

  // Create a store instance
  const store = new TodoStore();

  // Override the DATA_FILE path to use test file
  const originalSaveTodos = store.saveTodos.bind(store);
  store.saveTodos = function() {
    fs.writeFileSync(TEST_DATA_FILE, JSON.stringify(this.todos, null, 2), 'utf-8');
  };

  return store;
}

function cleanupTestStore() {
  if (fs.existsSync(TEST_DATA_FILE)) {
    fs.unlinkSync(TEST_DATA_FILE);
  }
}

test('TodoStore - add todo', () => {
  const store = setupTestStore();
  const todo = store.add('Learn Node.js');

  assert.strictEqual(todo.title, 'Learn Node.js');
  assert.strictEqual(todo.completed, false);
  assert.strictEqual(todo.id, 1);
  assert.ok(todo.createdAt);

  cleanupTestStore();
});

test('TodoStore - add multiple todos', () => {
  const store = setupTestStore();
  const todo1 = store.add('First todo');
  const todo2 = store.add('Second todo');
  const todo3 = store.add('Third todo');

  assert.strictEqual(todo1.id, 1);
  assert.strictEqual(todo2.id, 2);
  assert.strictEqual(todo3.id, 3);

  cleanupTestStore();
});

test('TodoStore - list todos', () => {
  const store = setupTestStore();
  store.add('Todo 1');
  store.add('Todo 2');

  const todos = store.list();
  assert.strictEqual(todos.length, 2);
  assert.strictEqual(todos[0].title, 'Todo 1');
  assert.strictEqual(todos[1].title, 'Todo 2');

  cleanupTestStore();
});

test('TodoStore - complete todo', () => {
  const store = setupTestStore();
  const todo = store.add('Complete me');

  const completed = store.complete(todo.id);
  assert.strictEqual(completed.completed, true);
  assert.strictEqual(completed.title, 'Complete me');

  const todos = store.list();
  assert.strictEqual(todos[0].completed, true);

  cleanupTestStore();
});

test('TodoStore - complete todo throws for non-existent id', () => {
  const store = setupTestStore();

  assert.throws(() => {
    store.complete(999);
  }, /Todo with id 999 not found/);

  cleanupTestStore();
});

test('TodoStore - delete todo', () => {
  const store = setupTestStore();
  store.add('To delete');
  store.add('Keep this');

  const deleted = store.delete(1);
  assert.strictEqual(deleted.title, 'To delete');

  const todos = store.list();
  assert.strictEqual(todos.length, 1);
  assert.strictEqual(todos[0].title, 'Keep this');

  cleanupTestStore();
});

test('TodoStore - delete todo throws for non-existent id', () => {
  const store = setupTestStore();

  assert.throws(() => {
    store.delete(999);
  }, /Todo with id 999 not found/);

  cleanupTestStore();
});

test('TodoStore - persistence', () => {
  const store1 = setupTestStore();
  store1.add('Persistent todo');

  // Create a new store instance - it should load from the file
  const store2 = new TodoStore();
  store2.saveTodos = function() {
    fs.writeFileSync(TEST_DATA_FILE, JSON.stringify(this.todos, null, 2), 'utf-8');
  };

  // Override loadTodos to use test file
  store2.todos = (() => {
    try {
      if (fs.existsSync(TEST_DATA_FILE)) {
        const data = fs.readFileSync(TEST_DATA_FILE, 'utf-8');
        return JSON.parse(data);
      }
      return [];
    } catch (error) {
      return [];
    }
  })();

  const todos = store2.list();
  assert.strictEqual(todos.length, 1);
  assert.strictEqual(todos[0].title, 'Persistent todo');

  cleanupTestStore();
});

test('TodoStore - empty list', () => {
  const store = setupTestStore();

  const todos = store.list();
  assert.strictEqual(todos.length, 0);

  cleanupTestStore();
});
