# Todo CLI App

A simple command-line todo application built with Node.js.

## Installation

```bash
npm install
```

## Usage

```bash
# Add a new todo
node cli.js add Buy groceries

# List all todos
node cli.js list

# Complete a todo (replace 1 with the todo ID)
node cli.js complete 1

# Delete a todo (replace 1 with the todo ID)
node cli.js delete 1
```

## Running Tests

```bash
npm test
```

## Features

- Add new todos
- List all todos with completion status
- Mark todos as complete
- Delete todos
- Data persists to a JSON file
