const assert = require('assert');
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

let testCount = 0;
let passCount = 0;
let failCount = 0;
let testDir = null;

// Test helper
function test(name, fn) {
  testCount++;
  try {
    fn();
    console.log(`✓ ${name}`);
    passCount++;
  } catch (error) {
    console.log(`✗ ${name}`);
    console.log(`  ${error.message}`);
    failCount++;
  }
}

function describe(suiteName, fn) {
  console.log(`\n${suiteName}`);
  fn();
}

// Helper to run CLI command in a temporary directory
function runCli(args, options = {}) {
  const testWorkDir = options.workDir || testDir;
  // Use absolute path to cli.js from the project root
  const cliPath = path.resolve(__dirname, '../../cli.js');
  const result = spawnSync('node', [cliPath, ...args], {
    cwd: testWorkDir,
    stdio: ['pipe', 'pipe', 'pipe'],
    encoding: 'utf8'
  });

  return {
    exitCode: result.status,
    stdout: result.stdout,
    stderr: result.stderr
  };
}

// Setup and teardown
function beforeEach() {
  // Create temporary directory for each test
  testDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cli-test-'));
}

function afterEach() {
  // Clean up temporary directory
  if (testDir && fs.existsSync(testDir)) {
    const files = fs.readdirSync(testDir);
    files.forEach(file => {
      fs.unlinkSync(path.join(testDir, file));
    });
    fs.rmdirSync(testDir);
  }
  testDir = null;
}

// Test suites
describe('CLI Router - Help and No Arguments', () => {
  test('node cli.js with no arguments exits code 0 and outputs usage', () => {
    beforeEach();
    const result = runCli([]);
    afterEach();

    assert.strictEqual(result.exitCode, 0);
    assert(result.stdout.includes('Usage: node cli.js <command>'));
    assert(result.stdout.includes('Commands:'));
  });

  test('node cli.js --help exits code 0 and outputs usage', () => {
    beforeEach();
    const result = runCli(['--help']);
    afterEach();

    assert.strictEqual(result.exitCode, 0);
    assert(result.stdout.includes('Usage: node cli.js <command>'));
  });

  test('node cli.js -h exits code 0 and outputs usage', () => {
    beforeEach();
    const result = runCli(['-h']);
    afterEach();

    assert.strictEqual(result.exitCode, 0);
    assert(result.stdout.includes('Usage: node cli.js <command>'));
  });
});

describe('CLI Router - Add Command', () => {
  test("node cli.js add 'Buy milk' exits code 0 and outputs success message", () => {
    beforeEach();
    const result = runCli(['add', 'Buy milk']);
    afterEach();

    assert.strictEqual(result.exitCode, 0);
    assert(result.stdout.includes('Created todo ID:'));
  });

  test('node cli.js ADD "Buy milk" (uppercase) exits code 0 and works identically to lowercase', () => {
    beforeEach();
    const result = runCli(['ADD', 'Buy milk']);
    afterEach();

    assert.strictEqual(result.exitCode, 0);
    assert(result.stdout.includes('Created todo ID: 1'));
  });

  test('node cli.js add exits code 1 (missing required argument) and outputs error', () => {
    beforeEach();
    const result = runCli(['add']);
    afterEach();

    assert.strictEqual(result.exitCode, 1);
    assert(result.stdout.includes('Error:'));
    assert(result.stdout.includes('Missing required argument'));
  });
});

describe('CLI Router - List Command', () => {
  test('node cli.js list exits code 0 and outputs "No todos found" when empty', () => {
    beforeEach();
    const result = runCli(['list']);
    afterEach();

    assert.strictEqual(result.exitCode, 0);
    assert(result.stdout.includes('No todos found'));
  });

  test('node cli.js list exits code 0 and outputs all todos when populated', () => {
    beforeEach();

    // Add a todo first
    runCli(['add', 'Buy milk']);

    // Then list
    const result = runCli(['list']);
    afterEach();

    assert.strictEqual(result.exitCode, 0);
    assert(result.stdout.includes('Buy milk'));
    assert(result.stdout.includes('1 |'));
  });

  test('node cli.js LIST works identically to list', () => {
    beforeEach();

    // Add a todo first
    runCli(['add', 'Buy milk']);

    // Then list with uppercase
    const result = runCli(['LIST']);
    afterEach();

    assert.strictEqual(result.exitCode, 0);
    assert(result.stdout.includes('Buy milk'));
  });
});

describe('CLI Router - Complete Command', () => {
  test('node cli.js complete 1 exits code 0 and outputs success message', () => {
    beforeEach();

    // Add a todo first
    runCli(['add', 'Buy milk']);

    // Then complete it
    const result = runCli(['complete', '1']);
    afterEach();

    assert.strictEqual(result.exitCode, 0);
    assert(result.stdout.includes('Marked todo 1 as complete') || result.stdout.includes('complete'));
  });

  test('node cli.js COMPLETE 1 works identically to complete', () => {
    beforeEach();

    // Add a todo first
    runCli(['add', 'Buy milk']);

    // Then complete with uppercase
    const result = runCli(['COMPLETE', '1']);
    afterEach();

    assert.strictEqual(result.exitCode, 0);
    assert(result.stdout.includes('complete'));
  });

  test('node cli.js complete 999 exits code 1 and outputs error message', () => {
    beforeEach();
    const result = runCli(['complete', '999']);
    afterEach();

    assert.strictEqual(result.exitCode, 1);
    assert(result.stdout.includes('Error:'));
    assert(result.stdout.includes('not found'));
  });

  test('node cli.js complete exits code 1 (missing required argument) and outputs error', () => {
    beforeEach();
    const result = runCli(['complete']);
    afterEach();

    assert.strictEqual(result.exitCode, 1);
    assert(result.stdout.includes('Error:'));
    assert(result.stdout.includes('Missing required argument'));
  });
});

describe('CLI Router - Delete Command', () => {
  test('node cli.js delete 1 exits code 0 and outputs success message', () => {
    beforeEach();

    // Add a todo first
    runCli(['add', 'Buy milk']);

    // Then delete it
    const result = runCli(['delete', '1']);
    afterEach();

    assert.strictEqual(result.exitCode, 0);
    assert(result.stdout.includes('Deleted todo 1'));
  });

  test('node cli.js DELETE 1 works identically to delete', () => {
    beforeEach();

    // Add a todo first
    runCli(['add', 'Buy milk']);

    // Then delete with uppercase
    const result = runCli(['DELETE', '1']);
    afterEach();

    assert.strictEqual(result.exitCode, 0);
    assert(result.stdout.includes('Deleted todo 1'));
  });

  test('node cli.js delete 999 exits code 1 and outputs error message', () => {
    beforeEach();
    const result = runCli(['delete', '999']);
    afterEach();

    assert.strictEqual(result.exitCode, 1);
    assert(result.stdout.includes('Error:'));
    assert(result.stdout.includes('not found'));
  });

  test('node cli.js delete exits code 1 (missing required argument) and outputs error', () => {
    beforeEach();
    const result = runCli(['delete']);
    afterEach();

    assert.strictEqual(result.exitCode, 1);
    assert(result.stdout.includes('Error:'));
    assert(result.stdout.includes('Missing required argument'));
  });
});

describe('CLI Router - Error Handling', () => {
  test("node cli.js invalid-command exits code 1 and outputs 'Error: Unknown command 'invalid-command''", () => {
    beforeEach();
    const result = runCli(['invalid-command']);
    afterEach();

    assert.strictEqual(result.exitCode, 1);
    assert(result.stdout.includes("Error: Unknown command 'invalid-command'"));
  });

  test('Error messages are prefixed with "Error: "', () => {
    beforeEach();
    const result = runCli(['complete', '999']);
    afterEach();

    assert.strictEqual(result.exitCode, 1);
    assert(result.stdout.startsWith('Error:'));
  });
});

describe('CLI Router - Success Messages', () => {
  test('Success messages output correctly (e.g., "Created todo ID: 1")', () => {
    beforeEach();
    const result = runCli(['add', 'Test todo']);
    afterEach();

    assert.strictEqual(result.exitCode, 0);
    assert.match(result.stdout, /Created todo ID: \d+/);
  });
});

// Run the tests
console.log('='.repeat(50));
console.log('CLI Router Integration Tests');
console.log('='.repeat(50));

// Note: All tests run the beforeEach/afterEach themselves
// Final summary
console.log(`\n${'='.repeat(50)}`);
console.log(`Tests run: ${testCount}, Passed: ${passCount}, Failed: ${failCount}`);
console.log('='.repeat(50));

process.exit(failCount > 0 ? 1 : 0);
