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
  console.log('Summarizer done')
  console.log(output.slice(0, 200))
  process.exit(0)
} catch (e) {
  if (e.stdout?.includes('No tweets')) {
    console.log('No tweets in window — skipped')
    process.exit(0)
  }
  console.error('summarize.py failed:', e.message.slice(0, 200))
  process.exit(1)
}
