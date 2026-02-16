#!/usr/bin/env node

const commands = require('./lib/commands');
const { printHelp } = require('./lib/utils');

/**
 * Main entry point. Parses command-line arguments, routes to command handlers,
 * formats output, and exits with appropriate code.
 *
 * @param {Array<string>} argv - Command-line arguments (e.g., process.argv.slice(2))
 * @returns {void} - Process exits via process.exit()
 */
function main(argv) {
  // If no arguments, show help and exit with code 0
  if (argv.length === 0) {
    printHelp();
    process.exit(0);
  }

  // Extract command (first argument, case-insensitive)
  const rawCommand = argv[0];
  const command = rawCommand.toLowerCase();

  // Handle help flags
  if (command === '--help' || command === '-h') {
    printHelp();
    process.exit(0);
  }

  // Extract remaining arguments
  const args = argv.slice(1);

  // Route commands
  switch (command) {
    case 'add': {
      // Check if title argument is provided
      if (args.length === 0) {
        console.log('Error: Missing required argument: title');
        process.exit(1);
      }

      // Join remaining args in case title has spaces (first arg is the title)
      const title = args[0];
      const result = commands.addTodo(title);

      console.log(result.message);
      process.exit(result.id !== null ? 0 : 1);
      break;
    }

    case 'list': {
      const result = commands.listTodos();

      // Output each formatted todo on separate line
      result.forEach(line => console.log(line));
      process.exit(0);
      break;
    }

    case 'complete': {
      // Check if ID argument is provided
      if (args.length === 0) {
        console.log('Error: Missing required argument: id');
        process.exit(1);
      }

      const idStr = args[0];
      const result = commands.completeTodo(idStr);

      console.log(result.message);
      process.exit(result.success ? 0 : 1);
      break;
    }

    case 'delete': {
      // Check if ID argument is provided
      if (args.length === 0) {
        console.log('Error: Missing required argument: id');
        process.exit(1);
      }

      const idStr = args[0];
      const result = commands.deleteTodo(idStr);

      console.log(result.message);
      process.exit(result.success ? 0 : 1);
      break;
    }

    default: {
      // Unknown command
      console.log(`Error: Unknown command '${rawCommand}'`);
      process.exit(1);
    }
  }
}

// If invoked directly as a script
if (require.main === module) {
  main(process.argv.slice(2));
}

module.exports = { main };
