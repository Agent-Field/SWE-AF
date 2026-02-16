const assert = require('assert');
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

console.log('\n========================================');
console.log('Acceptance Test: Complete Command');
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

// AC 6: Complete command success
console.log('Test 1: node cli.js complete 1 (success)');
try {
  const result = spawnSync('node', [cliPath, 'complete', '1'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 0, 'exit code should be 0');
  assert(result.stdout.includes('Marked todo 1 as complete'), 'should display success message');

  // Verify status changed in list
  const listResult = spawnSync('node', [cliPath, 'list'], {
    cwd: tempDir,
    encoding: 'utf8'
  });
  const firstLine = listResult.stdout.split('\n')[0];
  assert(firstLine.includes('[x]'), 'completed todo should show [x]');

  console.log('✓ Test passed: complete success with status update\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 7: Complete command - invalid ID error
console.log('Test 2: node cli.js complete 999 (non-existent ID)');
try {
  const result = spawnSync('node', [cliPath, 'complete', '999'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 1, 'exit code should be 1');
  assert(result.stdout.includes('Error:'), 'should display error message');
  assert(result.stdout.includes('not found') || result.stdout.includes('999'), 'should mention ID not found');

  console.log('✓ Test passed: complete error exits 1 for non-existent ID\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 17: Complete already-completed todo
console.log('Test 3: Complete already-completed todo');
try {
  const result = spawnSync('node', [cliPath, 'complete', '1'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  // Should exit 0 (success) but indicate it's already complete
  assert.strictEqual(result.status, 0, 'exit code should be 0');
  assert(result.stdout.includes('already complete'), 'should indicate todo is already complete');

  console.log('✓ Test passed: already-completed todo info message\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// Test: Complete with invalid ID format
console.log('Test 4: node cli.js complete abc (invalid ID format)');
try {
  const result = spawnSync('node', [cliPath, 'complete', 'abc'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 1, 'exit code should be 1');
  assert(result.stdout.includes('Error:'), 'should display error message');

  console.log('✓ Test passed: invalid ID format error exits 1\n');
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
