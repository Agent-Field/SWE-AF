const fs = require('fs');
const path = require('path');

const TODO_FILE = path.join(__dirname, 'todos.json');

function loadTodos() {
  try {
    const data = fs.readFileSync(TODO_FILE, 'utf8');
    return JSON.parse(data);
  } catch (error) {
    return [];
  }
}

function saveTodos(todos) {
  fs.writeFileSync(TODO_FILE, JSON.stringify(todos, null, 2));
}

function addTodo(task) {
  const todos = loadTodos();
  const newTodo = {
    id: todos.length > 0 ? Math.max(...todos.map(t => t.id)) + 1 : 1,
    task,
    completed: false
  };
  todos.push(newTodo);
  saveTodos(todos);
  return newTodo;
}

function listTodos() {
  return loadTodos();
}

function completeTodo(id) {
  const todos = loadTodos();
  const todo = todos.find(t => t.id === id);
  if (!todo) {
    return null;
  }
  todo.completed = true;
  saveTodos(todos);
  return todo;
}

function deleteTodo(id) {
  const todos = loadTodos();
  const index = todos.findIndex(t => t.id === id);
  if (index === -1) {
    return null;
  }
  const deleted = todos.splice(index, 1)[0];
  saveTodos(todos);
  return deleted;
}

module.exports = {
  addTodo,
  listTodos,
  completeTodo,
  deleteTodo,
  loadTodos,
  saveTodos
};
