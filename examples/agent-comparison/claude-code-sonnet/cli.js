#!/usr/bin/env node

const { addTodo, listTodos, completeTodo, deleteTodo } = require('./todo');

const args = process.argv.slice(2);
const command = args[0];

switch (command) {
  case 'add':
    const task = args.slice(1).join(' ');
    if (!task) {
      console.log('Error: Please provide a task');
      process.exit(1);
    }
    const newTodo = addTodo(task);
    console.log(`Added: [${newTodo.id}] ${newTodo.task}`);
    break;

  case 'list':
    const todos = listTodos();
    if (todos.length === 0) {
      console.log('No todos found');
    } else {
      todos.forEach(todo => {
        const status = todo.completed ? '✓' : ' ';
        console.log(`[${todo.id}] [${status}] ${todo.task}`);
      });
    }
    break;

  case 'complete':
    const completeId = parseInt(args[1]);
    if (!completeId) {
      console.log('Error: Please provide a todo ID');
      process.exit(1);
    }
    const completed = completeTodo(completeId);
    if (!completed) {
      console.log(`Error: Todo with ID ${completeId} not found`);
      process.exit(1);
    }
    console.log(`Completed: [${completed.id}] ${completed.task}`);
    break;

  case 'delete':
    const deleteId = parseInt(args[1]);
    if (!deleteId) {
      console.log('Error: Please provide a todo ID');
      process.exit(1);
    }
    const deleted = deleteTodo(deleteId);
    if (!deleted) {
      console.log(`Error: Todo with ID ${deleteId} not found`);
      process.exit(1);
    }
    console.log(`Deleted: [${deleted.id}] ${deleted.task}`);
    break;

  default:
    console.log('Usage:');
    console.log('  todo add <task>       Add a new todo');
    console.log('  todo list             List all todos');
    console.log('  todo complete <id>    Mark a todo as complete');
    console.log('  todo delete <id>      Delete a todo');
    process.exit(command ? 1 : 0);
}
