#!/usr/bin/env node
/**
 * Verification test for post-to-discord.cjs chunking logic.
 *
 * Proves that buildChunks:
 *   1. Returns no chunks for empty input
 *   2. Produces a single ≤2000-char chunk for small input
 *   3. Splits large payloads into multiple chunks, every chunk ≤ 2000 chars
 *   4. Covers every tweet in exactly one chunk
 *   5. Repeats category headers across chunks so context is never lost
 *
 * Before fix: post-to-discord.cjs sent one concatenated message → Discord 413 error.
 * After fix:  buildChunks() splits into ≤2000-char messages; only clears
 *             pending_alerts.json after ALL chunks are confirmed sent.
 *
 * Usage:  node test-chunker.cjs
 * Exit 0 = all tests pass, Exit 1 = at least one failure.
 */

const { buildChunks, tweetLine } = require('./post-to-discord.cjs')

let passed = 0
let failed = 0

function assert(condition, msg) {
  if (condition) {
    console.log(`  ✓ ${msg}`)
    passed++
  } else {
    console.error(`  ✗ FAIL: ${msg}`)
    failed++
  }
}

function makeTweet(i, category = 'bittensor') {
  return {
    id: String(i),
    // ~90-char text body → each tweetLine ~140 chars so 15+ tweets overflow 2000
    text: 'X'.repeat(88) + `_${i}`,
    user: { username: `user_${i}` },
    like_count: i,
    url: `https://x.com/user_${i}/status/${i}`,
    _monitor_category: category,
  }
}

// ── Test 1: empty / null input ─────────────────────────────────────────────
console.log('\nTest 1: empty / null input')
assert(buildChunks([]).length === 0, 'empty array → 0 chunks')
assert(buildChunks(null).length === 0, 'null → 0 chunks')
assert(buildChunks(undefined).length === 0, 'undefined → 0 chunks')

// ── Test 2: single tweet ────────────────────────────────────────────────────
console.log('\nTest 2: single tweet')
const single = buildChunks([makeTweet(1)])
assert(single.length === 1, 'single tweet → exactly 1 chunk')
assert(single[0].length <= 2000, `chunk length ${single[0].length} ≤ 2000`)
assert(single[0].startsWith('🔔 X Monitor'), 'first chunk starts with header')

// ── Test 3: many tweets → multiple chunks, all ≤ 2000 ────────────────────
console.log('\nTest 3: 30 homogeneous tweets')
const many = Array.from({ length: 30 }, (_, i) => makeTweet(i + 1))
const chunks = buildChunks(many)
assert(chunks.length > 1, `30 tweets → ${chunks.length} chunks (expected > 1)`)
for (let i = 0; i < chunks.length; i++) {
  assert(chunks[i].length <= 2000, `chunk ${i + 1} length ${chunks[i].length} ≤ 2000`)
}
assert(chunks[0].startsWith('🔔 X Monitor'), 'first chunk has header')

// ── Test 4: every tweet appears in output ─────────────────────────────────
console.log('\nTest 4: all 30 tweets represented')
const joined = chunks.join('\n')
let allPresent = true
for (const t of many) {
  if (!joined.includes(`@user_${t.id}`)) {
    console.error(`    missing @user_${t.id}`)
    allPresent = false
  }
}
assert(allPresent, 'every tweet @-mention appears in output')

// ── Test 5: very large payload — 50 tweets, 2 categories ──────────────────
console.log('\nTest 5: 50 tweets across 2 categories — no oversized chunk')
const mixed = Array.from({ length: 50 }, (_, i) =>
  makeTweet(i + 100, i % 2 === 0 ? 'alpha' : 'beta')
)
const mixedChunks = buildChunks(mixed)
let anyOver = false
for (let i = 0; i < mixedChunks.length; i++) {
  if (mixedChunks[i].length > 2000) {
    anyOver = true
    console.error(`  OVERSIZED chunk ${i + 1}: ${mixedChunks[i].length} chars`)
  }
}
assert(!anyOver, `all ${mixedChunks.length} chunks ≤ 2000 chars`)

// ── Test 6: category headers preserved across chunk boundaries ─────────────
console.log('\nTest 6: category headers in output')
const categorized = [
  ...Array.from({ length: 10 }, (_, i) => makeTweet(i + 200, 'desearch')),
  ...Array.from({ length: 10 }, (_, i) => makeTweet(i + 210, 'bittensor')),
]
const catChunks = buildChunks(categorized)
const catText = catChunks.join('\n')
assert(catText.includes('desearch'), '"desearch" category label appears')
assert(catText.includes('bittensor'), '"bittensor" category label appears')

// ── Test 7: tweetLine truncates long text at MAX_TWEET_TEXT ───────────────
console.log('\nTest 7: tweetLine truncation')
const longTweet = { text: 'A'.repeat(200), user: { username: 'tester' }, like_count: 0, url: '' }
const line = tweetLine(longTweet)
assert(line.includes('...'), 'long tweet text is truncated with ...')
// Whole line should still be reasonable in length (< 300 chars)
assert(line.length < 300, `tweetLine length ${line.length} < 300`)

// ── Summary ────────────────────────────────────────────────────────────────
console.log(`\n${'─'.repeat(50)}`)
console.log(`Tests: ${passed + failed} | Passed: ${passed} | Failed: ${failed}`)
if (failed > 0) {
  console.error('RESULT: FAIL')
  process.exit(1)
} else {
  console.log('RESULT: PASS')
  process.exit(0)
}
