#!/usr/bin/env node
/**
 * X Monitor — Summarizer runner (no LLM overhead)
 * summarize.py handles Discord posting internally
 * Runtime: ~60s (OpenRouter LLM) vs 420s+ with double LLM
 */
const { execSync } = require('child_process')
const path = require('path')

const SCRIPT_DIR = path.dirname(__filename)
const HOURS = process.argv[2] || '12'

try {
  const output = execSync(
    `cd ${SCRIPT_DIR} && python3 summarize.py --hours ${HOURS} 2>&1`,
    { timeout: 180000, encoding: 'utf8' }
  )
  const lines = output.trim().split('\n')
  console.log('Summarizer done')
  // Print last 5 lines (status lines from stderr + first line of summary)
  console.log(lines.slice(-5).join('\n'))
  process.exit(0)
} catch (e) {
  const out = (e.stdout || '') + (e.stderr || '') + e.message
  if (out.includes('No tweets')) {
    console.log('No tweets in window — skipped')
    process.exit(0)
  }
  console.error('summarize.py failed:')
  console.error(out.slice(0, 500))
  process.exit(1)
}
