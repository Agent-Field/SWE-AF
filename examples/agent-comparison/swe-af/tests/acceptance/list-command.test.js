const assert = require('assert');
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

console.log('\n========================================');
console.log('Acceptance Test: List Command');
console.log('========================================\n');

let testsPassed = 0;
let testsFailed = 0;

// Create temp directory for this test
const tempDir = fs.mkdtempSync(path.join(process.env.TMPDIR || '/tmp', 'cli-test-'));
const cliPath = path.join(process.cwd(), 'cli.js');

function cleanup() {
  try {
    fs.rmSync(tempDir, { recursive: true, force: true });
  } catch (e) {
    // ignore cleanup errors
  }
}

// Setup: Add test todos
spawnSync('node', [cliPath, 'add', 'Buy milk'], { cwd: tempDir, encoding: 'utf8' });
spawnSync('node', [cliPath, 'add', 'Walk dog'], { cwd: tempDir, encoding: 'utf8' });

// AC 4: List command format
console.log('Test 1: node cli.js list (format)');
try {
  const result = spawnSync('node', [cliPath, 'list'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 0, 'exit code should be 0');

  // Verify format: "ID | Title | [status]"
  const lines = result.stdout.trim().split('\n');
  assert(lines.length >= 2, 'should have at least 2 todos');

  // Check first line format
  assert(lines[0].includes('|'), 'line should contain pipe separator');
  assert(lines[0].match(/\d+\s*\|\s*.*\s*\|\s*\[.\]/), 'should match format "ID | Title | [status]"');

  console.log('✓ Test passed: list format correct\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 5: List status display
console.log('Test 2: List status - incomplete vs completed');
try {
  // Mark first todo as complete
  spawnSync('node', [cliPath, 'complete', '1'], { cwd: tempDir, encoding: 'utf8' });

  const result = spawnSync('node', [cliPath, 'list'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  const output = result.stdout;

  // First todo (completed) should show [x]
  const line1 = output.split('\n')[0];
  assert(line1.includes('[x]'), 'completed todo should show [x]');
  assert(line1.includes('Buy milk'), 'line should contain title');

  // Second todo (incomplete) should show [ ]
  const line2 = output.split('\n')[1];
  assert(line2.includes('[ ]'), 'incomplete todo should show [ ]');
  assert(line2.includes('Walk dog'), 'line should contain title');

  console.log('✓ Test passed: status display correct ([x] vs [ ])\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// Empty list test
console.log('Test 3: List when empty');
try {
  // Clean temp directory
  fs.rmSync(tempDir, { recursive: true, force: true });
  fs.mkdirSync(tempDir);

  const result = spawnSync('node', [cliPath, 'list'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 0, 'exit code should be 0');
  assert(result.stdout.includes('No todos found'), 'should display "No todos found" when empty');

  console.log('✓ Test passed: empty list displays "No todos found"\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// Cleanup
cleanup();

console.log('========================================');
console.log(`Tests passed: ${testsPassed}`);
console.log(`Tests failed: ${testsFailed}`);
console.log('========================================\n');

if (testsFailed > 0) {
  process.exit(1);
}
