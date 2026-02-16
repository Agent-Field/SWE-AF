#!/usr/bin/env node
const { run } = require('../src/cli');

process.exitCode = run(process.argv.slice(2));
