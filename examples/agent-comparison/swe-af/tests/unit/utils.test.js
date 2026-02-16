const assert = require('assert');
const { validateTitle, formatTodo, printHelp } = require('../../lib/utils');

let testResults = {
  passed: 0,
  failed: 0,
  errors: []
};

function test(description, fn) {
  try {
    fn();
    console.log(`✓ ${description}`);
    testResults.passed++;
  } catch (error) {
    console.log(`✗ ${description}`);
    console.log(`  Error: ${error.message}`);
    testResults.failed++;
    testResults.errors.push({ description, error });
  }
}

// ============================================================================
// validateTitle() Tests
// ============================================================================

test('validateTitle("Buy milk") returns {valid: true}', () => {
  const result = validateTitle('Buy milk');
  assert.strictEqual(result.valid, true);
  assert.strictEqual(result.error, undefined);
});

test('validateTitle("") returns {valid: false, error: "Todo title cannot be empty"}', () => {
  const result = validateTitle('');
  assert.strictEqual(result.valid, false);
  assert.strictEqual(result.error, 'Todo title cannot be empty');
});

test('validateTitle("   ") returns {valid: false, error: "Todo title cannot be empty"}', () => {
  const result = validateTitle('   ');
  assert.strictEqual(result.valid, false);
  assert.strictEqual(result.error, 'Todo title cannot be empty');
});

test('validateTitle("  Buy milk  ") returns {valid: true} (preserves whitespace)', () => {
  const result = validateTitle('  Buy milk  ');
  assert.strictEqual(result.valid, true);
  assert.strictEqual(result.error, undefined);
});

test('validateTitle with only tabs returns invalid', () => {
  const result = validateTitle('\t\t\t');
  assert.strictEqual(result.valid, false);
  assert.strictEqual(result.error, 'Todo title cannot be empty');
});

test('validateTitle with mixed whitespace returns invalid', () => {
  const result = validateTitle('  \t \n  ');
  assert.strictEqual(result.valid, false);
  assert.strictEqual(result.error, 'Todo title cannot be empty');
});

test('validateTitle with single character returns valid', () => {
  const result = validateTitle('a');
  assert.strictEqual(result.valid, true);
});

test('validateTitle with very long title returns valid', () => {
  const longTitle = 'a'.repeat(1000);
  const result = validateTitle(longTitle);
  assert.strictEqual(result.valid, true);
});

test('validateTitle with unicode characters returns valid', () => {
  const result = validateTitle('买牛奶');
  assert.strictEqual(result.valid, true);
});

test('validateTitle with special characters returns valid', () => {
  const result = validateTitle('Test with "quotes" and $vars');
  assert.strictEqual(result.valid, true);
});

// ============================================================================
// formatTodo() Tests
// ============================================================================

test('formatTodo({id: 1, title: "Buy milk", completed: false}) returns "1 | Buy milk | [ ]"', () => {
  const todo = { id: 1, title: 'Buy milk', completed: false };
  const result = formatTodo(todo);
  assert.strictEqual(result, '1 | Buy milk | [ ]');
});

test('formatTodo({id: 2, title: "Walk dog", completed: true}) returns "2 | Walk dog | [x]"', () => {
  const todo = { id: 2, title: 'Walk dog', completed: true };
  const result = formatTodo(todo);
  assert.strictEqual(result, '2 | Walk dog | [x]');
});

test('formatTodo uses correct separator " | " (space-pipe-space)', () => {
  const todo = { id: 1, title: 'Test', completed: false };
  const result = formatTodo(todo);
  // Should have exactly " | " separators, not "|" or "| " or " |"
  assert.strictEqual(result.includes(' | '), true);
  assert.strictEqual(result.includes('|'), true);
  const parts = result.split(' | ');
  assert.strictEqual(parts.length, 3);
});

test('formatTodo uses correct bracket format [ ] for incomplete', () => {
  const todo = { id: 1, title: 'Test', completed: false };
  const result = formatTodo(todo);
  assert.strictEqual(result.endsWith('[ ]'), true);
});

test('formatTodo uses correct bracket format [x] for complete', () => {
  const todo = { id: 1, title: 'Test', completed: true };
  const result = formatTodo(todo);
  assert.strictEqual(result.endsWith('[x]'), true);
});

test('formatTodo handles titles with quotes', () => {
  const todo = { id: 1, title: 'Buy "organic" milk', completed: false };
  const result = formatTodo(todo);
  assert.strictEqual(result, '1 | Buy "organic" milk | [ ]');
});

test('formatTodo handles titles with dollar signs', () => {
  const todo = { id: 1, title: 'Save $100', completed: false };
  const result = formatTodo(todo);
  assert.strictEqual(result, '1 | Save $100 | [ ]');
});

test('formatTodo handles titles with pipes', () => {
  const todo = { id: 1, title: 'Buy milk | eggs', completed: false };
  const result = formatTodo(todo);
  assert.strictEqual(result, '1 | Buy milk | eggs | [ ]');
});

test('formatTodo handles titles with brackets', () => {
  const todo = { id: 1, title: '[urgent] Buy milk', completed: false };
  const result = formatTodo(todo);
  assert.strictEqual(result, '1 | [urgent] Buy milk | [ ]');
});

test('formatTodo handles titles with backslashes', () => {
  const todo = { id: 1, title: 'C:\\path\\to\\file', completed: false };
  const result = formatTodo(todo);
  assert.strictEqual(result, '1 | C:\\path\\to\\file | [ ]');
});

test('formatTodo handles titles with newlines (stored as-is)', () => {
  const todo = { id: 1, title: 'Line1\nLine2', completed: false };
  const result = formatTodo(todo);
  // Should preserve newline in output
  assert.strictEqual(result.includes('Line1\nLine2'), true);
});

test('formatTodo handles very long titles', () => {
  const longTitle = 'a'.repeat(1000);
  const todo = { id: 1, title: longTitle, completed: false };
  const result = formatTodo(todo);
  assert.strictEqual(result.includes(longTitle), true);
});

test('formatTodo handles unicode characters', () => {
  const todo = { id: 1, title: '买牛奶', completed: false };
  const result = formatTodo(todo);
  assert.strictEqual(result, '1 | 买牛奶 | [ ]');
});

test('formatTodo handles emoji characters', () => {
  const todo = { id: 1, title: '🛒 Buy milk', completed: false };
  const result = formatTodo(todo);
  assert.strictEqual(result, '1 | 🛒 Buy milk | [ ]');
});

test('formatTodo handles large ID numbers', () => {
  const todo = { id: 999999, title: 'Test', completed: false };
  const result = formatTodo(todo);
  assert.strictEqual(result, '999999 | Test | [ ]');
});

test('formatTodo handles whitespace in titles', () => {
  const todo = { id: 1, title: '  leading and trailing  ', completed: false };
  const result = formatTodo(todo);
  assert.strictEqual(result, '1 |   leading and trailing   | [ ]');
});

// ============================================================================
// printHelp() Tests
// ============================================================================

test('printHelp() outputs text containing "Usage:"', () => {
  const originalLog = console.log;
  let output = '';
  console.log = function(...args) {
    output += args.join(' ') + '\n';
  };

  printHelp();
  console.log = originalLog;

  assert.strictEqual(output.includes('Usage:'), true);
});

test('printHelp() outputs text containing "add"', () => {
  const originalLog = console.log;
  let output = '';
  console.log = function(...args) {
    output += args.join(' ') + '\n';
  };

  printHelp();
  console.log = originalLog;

  assert.strictEqual(output.includes('add'), true);
});

test('printHelp() outputs text containing "list"', () => {
  const originalLog = console.log;
  let output = '';
  console.log = function(...args) {
    output += args.join(' ') + '\n';
  };

  printHelp();
  console.log = originalLog;

  assert.strictEqual(output.includes('list'), true);
});

test('printHelp() outputs text containing "complete"', () => {
  const originalLog = console.log;
  let output = '';
  console.log = function(...args) {
    output += args.join(' ') + '\n';
  };

  printHelp();
  console.log = originalLog;

  assert.strictEqual(output.includes('complete'), true);
});

test('printHelp() outputs text containing "delete"', () => {
  const originalLog = console.log;
  let output = '';
  console.log = function(...args) {
    output += args.join(' ') + '\n';
  };

  printHelp();
  console.log = originalLog;

  assert.strictEqual(output.includes('delete'), true);
});

test('printHelp() outputs text containing "--help"', () => {
  const originalLog = console.log;
  let output = '';
  console.log = function(...args) {
    output += args.join(' ') + '\n';
  };

  printHelp();
  console.log = originalLog;

  assert.strictEqual(output.includes('--help'), true);
});

test('printHelp() exits without error when called', () => {
  // Capture console output to avoid polluting test output
  const originalLog = console.log;
  console.log = function() {};

  try {
    printHelp();
    console.log = originalLog;
    // If we get here, no error was thrown
    assert.ok(true);
  } catch (error) {
    console.log = originalLog;
    throw error;
  }
});

test('printHelp() outputs multiline help text', () => {
  const originalLog = console.log;
  let callCount = 0;
  console.log = function() {
    callCount++;
  };

  printHelp();
  console.log = originalLog;

  // Should call console.log multiple times for multiline output
  assert.ok(callCount > 3);
});

test('printHelp() contains all required commands in help text', () => {
  const originalLog = console.log;
  let output = '';
  console.log = function(...args) {
    output += args.join(' ') + '\n';
  };

  printHelp();
  console.log = originalLog;

  // Check that all required commands are present
  assert.strictEqual(output.includes('add'), true);
  assert.strictEqual(output.includes('list'), true);
  assert.strictEqual(output.includes('complete'), true);
  assert.strictEqual(output.includes('delete'), true);
  assert.strictEqual(output.includes('--help'), true);
});

// ============================================================================
// Summary
// ============================================================================

console.log('\n' + '='.repeat(70));
console.log(`Test Results: ${testResults.passed} passed, ${testResults.failed} failed`);
console.log('='.repeat(70));

if (testResults.failed > 0) {
  console.log('\nFailed tests:');
  testResults.errors.forEach(({ description, error }) => {
    console.log(`  - ${description}`);
    console.log(`    ${error.message}`);
  });
  process.exit(1);
} else {
  console.log('\n✓ All tests passed!');
  process.exit(0);
}
