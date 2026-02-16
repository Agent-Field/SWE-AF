#!/usr/bin/env node

import TodoStore from './store.js';

const store = new TodoStore();
const args = process.argv.slice(2);
const command = args[0];

function printTodo(todo) {
  const status = todo.completed ? '✓' : ' ';
  console.log(`[${status}] ${todo.id}. ${todo.title}`);
}

function showUsage() {
  console.log(`Usage:
  todo add <title>      - Add a new todo
  todo list             - List all todos
  todo complete <id>    - Mark a todo as complete
  todo delete <id>      - Delete a todo`);
}

try {
  switch (command) {
    case 'add': {
      if (args.length < 2) {
        console.error('Error: Please provide a todo title');
        process.exit(1);
      }
      const title = args.slice(1).join(' ');
      const todo = store.add(title);
      console.log(`Added todo #${todo.id}: ${todo.title}`);
      break;
    }

    case 'list': {
      const todos = store.list();
      if (todos.length === 0) {
        console.log('No todos yet!');
      } else {
        console.log('Todos:');
        todos.forEach(printTodo);
      }
      break;
    }

    case 'complete': {
      if (args.length < 2) {
        console.error('Error: Please provide a todo id');
        process.exit(1);
      }
      const id = parseInt(args[1], 10);
      if (isNaN(id)) {
        console.error('Error: Todo id must be a number');
        process.exit(1);
      }
      const todo = store.complete(id);
      console.log(`Completed: ${todo.title}`);
      break;
    }

    case 'delete': {
      if (args.length < 2) {
        console.error('Error: Please provide a todo id');
        process.exit(1);
      }
      const id = parseInt(args[1], 10);
      if (isNaN(id)) {
        console.error('Error: Todo id must be a number');
        process.exit(1);
      }
      const todo = store.delete(id);
      console.log(`Deleted: ${todo.title}`);
      break;
    }

    default: {
      if (command) {
        console.error(`Unknown command: ${command}`);
      }
      showUsage();
      process.exit(command ? 1 : 0);
    }
  }
} catch (error) {
  console.error(`Error: ${error.message}`);
  process.exit(1);
}
