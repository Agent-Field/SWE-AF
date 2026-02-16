const fs = require('fs');
const path = require('path');
const { addTodo, listTodos, completeTodo, deleteTodo, loadTodos, saveTodos } = require('./todo');

const TEST_TODO_FILE = path.join(__dirname, 'todos.json');

describe('Todo App', () => {
  beforeEach(() => {
    if (fs.existsSync(TEST_TODO_FILE)) {
      fs.unlinkSync(TEST_TODO_FILE);
    }
  });

  afterEach(() => {
    if (fs.existsSync(TEST_TODO_FILE)) {
      fs.unlinkSync(TEST_TODO_FILE);
    }
  });

  describe('addTodo', () => {
    test('should add a new todo', () => {
      const todo = addTodo('Test task');
      expect(todo.id).toBe(1);
      expect(todo.task).toBe('Test task');
      expect(todo.completed).toBe(false);
    });

    test('should increment IDs for multiple todos', () => {
      const todo1 = addTodo('Task 1');
      const todo2 = addTodo('Task 2');
      expect(todo1.id).toBe(1);
      expect(todo2.id).toBe(2);
    });

    test('should persist todos to file', () => {
      addTodo('Task 1');
      const todos = loadTodos();
      expect(todos).toHaveLength(1);
      expect(todos[0].task).toBe('Task 1');
    });
  });

  describe('listTodos', () => {
    test('should return empty array when no todos exist', () => {
      const todos = listTodos();
      expect(todos).toEqual([]);
    });

    test('should return all todos', () => {
      addTodo('Task 1');
      addTodo('Task 2');
      const todos = listTodos();
      expect(todos).toHaveLength(2);
    });
  });

  describe('completeTodo', () => {
    test('should mark a todo as completed', () => {
      const todo = addTodo('Test task');
      const completed = completeTodo(todo.id);
      expect(completed.completed).toBe(true);
    });

    test('should return null for non-existent todo', () => {
      const result = completeTodo(999);
      expect(result).toBeNull();
    });

    test('should persist completion status', () => {
      const todo = addTodo('Test task');
      completeTodo(todo.id);
      const todos = loadTodos();
      expect(todos[0].completed).toBe(true);
    });
  });

  describe('deleteTodo', () => {
    test('should delete a todo', () => {
      const todo = addTodo('Test task');
      const deleted = deleteTodo(todo.id);
      expect(deleted.id).toBe(todo.id);
      expect(deleted.task).toBe('Test task');
    });

    test('should return null for non-existent todo', () => {
      const result = deleteTodo(999);
      expect(result).toBeNull();
    });

    test('should remove todo from file', () => {
      const todo = addTodo('Test task');
      deleteTodo(todo.id);
      const todos = loadTodos();
      expect(todos).toHaveLength(0);
    });

    test('should handle deleting from multiple todos', () => {
      addTodo('Task 1');
      const todo2 = addTodo('Task 2');
      addTodo('Task 3');
      deleteTodo(todo2.id);
      const todos = loadTodos();
      expect(todos).toHaveLength(2);
      expect(todos.find(t => t.id === todo2.id)).toBeUndefined();
    });
  });

  describe('File persistence', () => {
    test('should load todos from existing file', () => {
      const testTodos = [
        { id: 1, task: 'Task 1', completed: false },
        { id: 2, task: 'Task 2', completed: true }
      ];
      saveTodos(testTodos);
      const loaded = loadTodos();
      expect(loaded).toEqual(testTodos);
    });

    test('should handle corrupted file gracefully', () => {
      fs.writeFileSync(TEST_TODO_FILE, 'invalid json');
      const todos = loadTodos();
      expect(todos).toEqual([]);
    });
  });
});
