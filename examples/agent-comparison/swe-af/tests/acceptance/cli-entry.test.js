const assert = require('assert');
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

console.log('\n========================================');
console.log('Acceptance Test: CLI Entry Point');
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

// AC 1 & 15: Help via --help
console.log('Test 1: node cli.js --help');
try {
  const result = spawnSync('node', [cliPath, '--help'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 0, 'exit code should be 0');
  assert(result.stdout.includes('Usage:'), '--help should display usage');
  assert(result.stdout.includes('Commands:'), '--help should display commands');
  assert(result.stdout.includes('add'), '--help should include add command');
  assert(result.stdout.includes('list'), '--help should include list command');
  assert(result.stdout.includes('complete'), '--help should include complete command');
  assert(result.stdout.includes('delete'), '--help should include delete command');

  console.log('✓ Test passed: --help exits 0 and shows usage\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 1 & 15: Help via no arguments
console.log('Test 2: node cli.js (no arguments)');
try {
  const result = spawnSync('node', [cliPath], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 0, 'exit code should be 0');
  assert(result.stdout.includes('Usage:'), 'no args should display usage');
  assert(result.stdout.includes('Commands:'), 'no args should display commands');
  assert(result.stdout.includes('add'), 'no args should include add command');
  assert(result.stdout.includes('list'), 'no args should include list command');

  console.log('✓ Test passed: no args exits 0 and shows usage\n');
  testsPassed++;
} catch (error) {
  console.error('✗ Test failed');
  console.error(`  ${error.message}\n`);
  testsFailed++;
}

// AC 1: Help via -h flag
console.log('Test 3: node cli.js -h');
try {
  const result = spawnSync('node', [cliPath, '-h'], {
    cwd: tempDir,
    encoding: 'utf8'
  });

  assert.strictEqual(result.status, 0, 'exit code should be 0');
  assert(result.stdout.includes('Usage:'), '-h should display usage');

  console.log('✓ Test passed: -h exits 0 and shows usage\n');
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
