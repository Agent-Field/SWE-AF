/**
 * Validate a todo title.
 *
 * @param {string} title - The title to validate
 * @returns {{valid: boolean, error?: string}} - Validation result
 *   - If valid: {valid: true}
 *   - If invalid: {valid: false, error: "Todo title cannot be empty"}
 */
function validateTitle(title) {
  // Check if title is empty string or whitespace-only
  if (title.length === 0 || title.trim().length === 0) {
    return {
      valid: false,
      error: 'Todo title cannot be empty'
    };
  }
  return {
    valid: true
  };
}

/**
 * Format a todo object for display.
 *
 * @param {{id: number, title: string, completed: boolean}} todo - The todo to format
 * @returns {string} - Formatted string: "1 | Buy milk | [x]"
 */
function formatTodo(todo) {
  const status = todo.completed ? 'x' : ' ';
  return `${todo.id} | ${todo.title} | [${status}]`;
}

/**
 * Print usage information to stdout.
 *
 * @returns {void}
 */
function printHelp() {
  console.log('Usage: node cli.js <command> [arguments]');
  console.log('');
  console.log('Commands:');
  console.log('  add <title>       Create a new todo item');
  console.log('  list              Display all todo items');
  console.log('  complete <id>     Mark a todo as completed');
  console.log('  delete <id>       Delete a todo item');
  console.log('  --help            Show this message');
}

module.exports = {
  validateTitle,
  formatTodo,
  printHelp
};
