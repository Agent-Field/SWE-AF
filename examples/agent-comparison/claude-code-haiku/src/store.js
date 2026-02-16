import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DATA_FILE = path.join(__dirname, '..', 'todos.json');

class TodoStore {
  constructor() {
    this.todos = this.loadTodos();
  }

  loadTodos() {
    try {
      if (fs.existsSync(DATA_FILE)) {
        const data = fs.readFileSync(DATA_FILE, 'utf-8');
        return JSON.parse(data);
      }
      return [];
    } catch (error) {
      console.error('Error loading todos:', error.message);
      return [];
    }
  }

  saveTodos() {
    try {
      fs.writeFileSync(DATA_FILE, JSON.stringify(this.todos, null, 2), 'utf-8');
    } catch (error) {
      console.error('Error saving todos:', error.message);
      throw error;
    }
  }

  add(title) {
    const id = this.todos.length > 0 ? Math.max(...this.todos.map(t => t.id)) + 1 : 1;
    const todo = {
      id,
      title,
      completed: false,
      createdAt: new Date().toISOString()
    };
    this.todos.push(todo);
    this.saveTodos();
    return todo;
  }

  list() {
    return this.todos;
  }

  complete(id) {
    const todo = this.todos.find(t => t.id === id);
    if (!todo) {
      throw new Error(`Todo with id ${id} not found`);
    }
    todo.completed = true;
    this.saveTodos();
    return todo;
  }

  delete(id) {
    const index = this.todos.findIndex(t => t.id === id);
    if (index === -1) {
      throw new Error(`Todo with id ${id} not found`);
    }
    const deleted = this.todos.splice(index, 1);
    this.saveTodos();
    return deleted[0];
  }
}

export default TodoStore;
