const fs = require('node:fs');

function loadTodos(filePath) {
  if (!fs.existsSync(filePath)) {
    return [];
  }

  const raw = fs.readFileSync(filePath, 'utf8').trim();
  if (!raw) {
    return [];
  }

  let todos;
  try {
    todos = JSON.parse(raw);
  } catch (error) {
    throw new Error(`Failed to parse todo data from ${filePath}.`);
  }

  if (!Array.isArray(todos)) {
    throw new Error(`Todo data at ${filePath} is invalid.`);
  }

  return todos;
}

function saveTodos(filePath, todos) {
  fs.writeFileSync(filePath, `${JSON.stringify(todos, null, 2)}\n`, 'utf8');
}

function getNextId(todos) {
  const maxId = todos.reduce((max, todo) => {
    if (typeof todo.id === 'number' && Number.isFinite(todo.id) && todo.id > max) {
      return todo.id;
    }
    return max;
  }, 0);

  return maxId + 1;
}

function addTodo(filePath, text) {
  const todos = loadTodos(filePath);
  const todo = {
    id: getNextId(todos),
    text,
    completed: false,
    createdAt: new Date().toISOString()
  };

  todos.push(todo);
  saveTodos(filePath, todos);
  return todo;
}

function listTodos(filePath) {
  return loadTodos(filePath);
}

function completeTodo(filePath, id) {
  const todos = loadTodos(filePath);
  const todo = todos.find((item) => item.id === id);

  if (!todo) {
    return { todo: null, alreadyCompleted: false };
  }

  const alreadyCompleted = Boolean(todo.completed);
  if (!alreadyCompleted) {
    todo.completed = true;
    todo.completedAt = new Date().toISOString();
    saveTodos(filePath, todos);
  }

  return { todo, alreadyCompleted };
}

function deleteTodo(filePath, id) {
  const todos = loadTodos(filePath);
  const index = todos.findIndex((item) => item.id === id);

  if (index === -1) {
    return null;
  }

  const [removed] = todos.splice(index, 1);
  saveTodos(filePath, todos);
  return removed;
}

module.exports = {
  addTodo,
  listTodos,
  completeTodo,
  deleteTodo
};
