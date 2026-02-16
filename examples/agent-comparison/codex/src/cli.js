const path = require('node:path');
const { addTodo, listTodos, completeTodo, deleteTodo } = require('./todoStore');

function resolveTodoFile(cwd) {
  return process.env.TODO_FILE || path.join(cwd, 'todos.json');
}

function parseId(value) {
  const id = Number.parseInt(value, 10);
  if (!Number.isInteger(id) || id <= 0) {
    return null;
  }
  return id;
}

function printHelp(stderr) {
  stderr.write('Usage:\n');
  stderr.write('  todo add <text>\n');
  stderr.write('  todo list\n');
  stderr.write('  todo complete <id>\n');
  stderr.write('  todo delete <id>\n');
}

function run(argv, io = { stdout: process.stdout, stderr: process.stderr }, cwd = process.cwd()) {
  const [command, ...args] = argv;
  const filePath = resolveTodoFile(cwd);

  if (!command) {
    printHelp(io.stderr);
    return 1;
  }

  if (command === 'add') {
    const text = args.join(' ').trim();
    if (!text) {
      io.stderr.write('Error: todo text is required.\n');
      printHelp(io.stderr);
      return 1;
    }

    const todo = addTodo(filePath, text);
    io.stdout.write(`Added todo ${todo.id}.\n`);
    return 0;
  }

  if (command === 'list') {
    const todos = listTodos(filePath);
    if (todos.length === 0) {
      io.stdout.write('No todos found.\n');
      return 0;
    }

    for (const todo of todos) {
      const marker = todo.completed ? 'x' : ' ';
      io.stdout.write(`[${marker}] ${todo.id}: ${todo.text}\n`);
    }
    return 0;
  }

  if (command === 'complete') {
    const id = parseId(args[0]);
    if (!id) {
      io.stderr.write('Error: valid todo id is required.\n');
      printHelp(io.stderr);
      return 1;
    }

    const result = completeTodo(filePath, id);
    if (!result.todo) {
      io.stderr.write(`Todo ${id} not found.\n`);
      return 1;
    }

    if (result.alreadyCompleted) {
      io.stdout.write(`Todo ${id} is already completed.\n`);
      return 0;
    }

    io.stdout.write(`Completed todo ${id}.\n`);
    return 0;
  }

  if (command === 'delete') {
    const id = parseId(args[0]);
    if (!id) {
      io.stderr.write('Error: valid todo id is required.\n');
      printHelp(io.stderr);
      return 1;
    }

    const removed = deleteTodo(filePath, id);
    if (!removed) {
      io.stderr.write(`Todo ${id} not found.\n`);
      return 1;
    }

    io.stdout.write(`Deleted todo ${id}.\n`);
    return 0;
  }

  io.stderr.write(`Unknown command: ${command}\n`);
  printHelp(io.stderr);
  return 1;
}

module.exports = {
  run
};
