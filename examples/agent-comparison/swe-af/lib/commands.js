const store = require('./store');
const { validateTitle, formatTodo } = require('./utils');

/**
 * Add a new todo with the given title.
 *
 * @param {string} title - The todo title
 * @returns {{id: number|null, message: string}} - Result object
 *   - success case: {id: <number>, message: "Created todo ID: <id>"}
 *   - error case: {id: null, message: "Error: <reason>"}
 */
function addTodo(title) {
  // Validate title
  const validation = validateTitle(title);
  if (!validation.valid) {
    return {
      id: null,
      message: `Error: ${validation.error}`
    };
  }

  // Load todos from store
  const todos = store.loadTodos();

  // Get next ID
  const nextId = store.getNextId(todos);

  // Create new todo object
  const newTodo = {
    id: nextId,
    title: title,
    completed: false
  };

  // Append to todos
  todos.push(newTodo);

  // Save to store
  store.saveTodos(todos);

  // Return success response
  return {
    id: nextId,
    message: `Created todo ID: ${nextId}`
  };
}

/**
 * List all todos in display format.
 *
 * @returns {Array<string>} - Array of formatted todo lines
 *   - If empty: ["No todos found"]
 *   - Otherwise: ["1 | Buy milk | [x]", "2 | Walk dog | [ ]", ...]
 */
function listTodos() {
  try {
    // Load todos from store
    const todos = store.loadTodos();

    // If empty, return no todos message
    if (todos.length === 0) {
      return ['No todos found'];
    }

    // Format each todo and return array
    return todos.map(todo => formatTodo(todo));
  } catch (error) {
    return ['Error: Failed to load todos'];
  }
}

/**
 * Mark a todo as completed.
 *
 * @param {string} idStr - The todo ID as a string (from command-line)
 * @returns {{success: boolean, message: string}} - Result object
 *   - success case: {success: true, message: "Marked todo <id> as complete"}
 *                  OR {success: true, message: "Todo <id> is already complete"}
 *   - error case: {success: false, message: "Error: <reason>"}
 */
function completeTodo(idStr) {
  // Parse ID from string
  const id = parseInt(idStr, 10);
  if (isNaN(id)) {
    return {
      success: false,
      message: 'Error: Invalid ID format'
    };
  }

  try {
    // Load todos from store
    const todos = store.loadTodos();

    // Find todo by ID
    const todoIndex = todos.findIndex(t => t.id === id);
    if (todoIndex === -1) {
      return {
        success: false,
        message: `Error: Todo ID ${id} not found`
      };
    }

    const todo = todos[todoIndex];

    // Check if already completed
    if (todo.completed) {
      return {
        success: true,
        message: `Todo ${id} is already complete`
      };
    }

    // Mark as completed
    todo.completed = true;

    // Save to store
    store.saveTodos(todos);

    return {
      success: true,
      message: `Marked todo ${id} as complete`
    };
  } catch (error) {
    return {
      success: false,
      message: 'Error: Failed to save todo'
    };
  }
}

/**
 * Delete a todo by ID.
 *
 * @param {string} idStr - The todo ID as a string (from command-line)
 * @returns {{success: boolean, message: string}} - Result object
 *   - success case: {success: true, message: "Deleted todo <id>"}
 *   - error case: {success: false, message: "Error: <reason>"}
 */
function deleteTodo(idStr) {
  // Parse ID from string
  const id = parseInt(idStr, 10);
  if (isNaN(id)) {
    return {
      success: false,
      message: 'Error: Invalid ID format'
    };
  }

  try {
    // Load todos from store
    const todos = store.loadTodos();

    // Find todo by ID
    const todoIndex = todos.findIndex(t => t.id === id);
    if (todoIndex === -1) {
      return {
        success: false,
        message: `Error: Todo ID ${id} not found`
      };
    }

    // Remove from array
    todos.splice(todoIndex, 1);

    // Save to store
    store.saveTodos(todos);

    return {
      success: true,
      message: `Deleted todo ${id}`
    };
  } catch (error) {
    return {
      success: false,
      message: 'Error: Failed to save todo'
    };
  }
}

module.exports = {
  addTodo,
  listTodos,
  completeTodo,
  deleteTodo
};
