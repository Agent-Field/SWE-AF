const fs = require('fs');
const path = require('path');

const TODOS_FILE = path.join(process.cwd(), 'todos.json');

/**
 * Load all todos from ./todos.json.
 *
 * @returns {Array<{id: number, title: string, completed: boolean}>} - Array of todos
 *   - If file doesn't exist: returns []
 *   - If file is corrupted JSON: logs warning and returns []
 *   - Otherwise: returns parsed array
 */
function loadTodos() {
  try {
    if (!fs.existsSync(TODOS_FILE)) {
      return [];
    }

    const fileContent = fs.readFileSync(TODOS_FILE, 'utf8');
    return JSON.parse(fileContent);
  } catch (error) {
    if (error instanceof SyntaxError) {
      console.warn('Warning: todos.json is corrupted or invalid JSON. Reinitializing with empty list.');
      return [];
    }
    throw error;
  }
}

/**
 * Save todos to ./todos.json atomically.
 *
 * @param {Array<{id: number, title: string, completed: boolean}>} todos - Todos to save
 * @returns {void}
 * @throws {Error} - If file write fails
 */
function saveTodos(todos) {
  const jsonContent = JSON.stringify(todos, null, 2);
  fs.writeFileSync(TODOS_FILE, jsonContent, 'utf8');
}

/**
 * Get the next available ID (max existing ID + 1, or 1 if empty).
 *
 * @param {Array<{id: number, ...}>} todos - Array of todos
 * @returns {number} - Next available ID
 */
function getNextId(todos) {
  if (todos.length === 0) {
    return 1;
  }

  const maxId = Math.max(...todos.map(todo => todo.id));
  return maxId + 1;
}

module.exports = {
  loadTodos,
  saveTodos,
  getNextId,
};
