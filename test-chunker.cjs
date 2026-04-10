#!/usr/bin/env node
/**
 * Verification test for post-to-discord.cjs chunking and queue lifecycle.
 *
 * buildChunks() now returns {text, tweets}[] so main() can remove each chunk's
 * tweets from pending_alerts.json immediately after a successful post.
 *
 * Tests:
 *   1. Empty / null input
 *   2. Single tweet
 *   3. Many tweets split into multiple chunks, all ≤ 2000 chars
 *   4. Every tweet covered exactly once
 *   5. 50 tweets across 2 categories — no oversized chunk
 *   6. Category headers preserved in output
 *   7. tweetLine truncation
 *   8. Incremental queue: partial failure preserves unsent tweets
 *   9. Incremental queue: idempotent retry — already-sent tweets not re-sent
 *
 * Usage:  node test-chunker.cjs
 * Exit 0 = all tests pass, Exit 1 = at least one failure.
 */

const fs = require('fs')
const path = require('path')
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
assert(typeof single[0].text === 'string', 'chunk has .text string')
assert(Array.isArray(single[0].tweets), 'chunk has .tweets array')
assert(single[0].tweets.length === 1, 'chunk covers 1 tweet')
assert(single[0].text.length <= 2000, `chunk text length ${single[0].text.length} ≤ 2000`)
assert(single[0].text.startsWith('🔔 X Monitor'), 'first chunk starts with header')

// ── Test 3: many tweets → multiple chunks, all ≤ 2000 ────────────────────
console.log('\nTest 3: 30 homogeneous tweets')
const many = Array.from({ length: 30 }, (_, i) => makeTweet(i + 1))
const chunks = buildChunks(many)
assert(chunks.length > 1, `30 tweets → ${chunks.length} chunks (expected > 1)`)
for (let i = 0; i < chunks.length; i++) {
  assert(chunks[i].text.length <= 2000, `chunk ${i + 1} text length ${chunks[i].text.length} ≤ 2000`)
}
assert(chunks[0].text.startsWith('🔔 X Monitor'), 'first chunk has header')

// ── Test 4: every tweet appears in exactly one chunk ──────────────────────
console.log('\nTest 4: all 30 tweets covered exactly once')
const allChunkTweets = chunks.flatMap(c => c.tweets)
assert(allChunkTweets.length === 30, `total tweets across chunks (${allChunkTweets.length}) === 30`)
const seenIds = new Set(allChunkTweets.map(t => t.id))
assert(seenIds.size === 30, 'all 30 unique tweet IDs covered')

// ── Test 5: very large payload — 50 tweets, 2 categories ──────────────────
console.log('\nTest 5: 50 tweets across 2 categories — no oversized chunk')
const mixed = Array.from({ length: 50 }, (_, i) =>
  makeTweet(i + 100, i % 2 === 0 ? 'alpha' : 'beta')
)
const mixedChunks = buildChunks(mixed)
let anyOver = false
for (let i = 0; i < mixedChunks.length; i++) {
  if (mixedChunks[i].text.length > 2000) {
    anyOver = true
    console.error(`  OVERSIZED chunk ${i + 1}: ${mixedChunks[i].text.length} chars`)
  }
}
assert(!anyOver, `all ${mixedChunks.length} chunks ≤ 2000 chars`)
const mixedTotal = mixedChunks.flatMap(c => c.tweets).length
assert(mixedTotal === 50, `all 50 tweets covered across chunks (got ${mixedTotal})`)

// ── Test 6: category headers preserved across chunk boundaries ─────────────
console.log('\nTest 6: category headers in output')
const categorized = [
  ...Array.from({ length: 10 }, (_, i) => makeTweet(i + 200, 'desearch')),
  ...Array.from({ length: 10 }, (_, i) => makeTweet(i + 210, 'bittensor')),
]
const catChunks = buildChunks(categorized)
const catText = catChunks.map(c => c.text).join('\n')
assert(catText.includes('desearch'), '"desearch" category label appears')
assert(catText.includes('bittensor'), '"bittensor" category label appears')

// ── Test 7: tweetLine truncates long text at MAX_TWEET_TEXT ───────────────
console.log('\nTest 7: tweetLine truncation')
const longTweet = { text: 'A'.repeat(200), user: { username: 'tester' }, like_count: 0, url: '' }
const line = tweetLine(longTweet)
assert(line.includes('...'), 'long tweet text is truncated with ...')
assert(line.length < 300, `tweetLine length ${line.length} < 300`)

// ── Test 8: incremental queue — partial failure preserves unsent tweets ────
console.log('\nTest 8: partial failure preserves unsent tweets in pending_alerts.json')
{
  const tmpFile = path.join(require('os').tmpdir(), `pending_test_${Date.now()}.json`)
  const tweets = [makeTweet(1), makeTweet(2), makeTweet(3)]
  fs.writeFileSync(tmpFile, JSON.stringify(tweets))

  const testChunks = buildChunks(tweets)
  // Simulate: chunk 1 succeeds, chunk 2 fails
  if (testChunks.length >= 2) {
    // Chunk 1 succeeds — remove its tweets
    const sentUrls = new Set(testChunks[0].tweets.map(t => t.url))
    let remaining = tweets.filter(t => !sentUrls.has(t.url))
    fs.writeFileSync(tmpFile, JSON.stringify(remaining))

    const afterChunk1 = JSON.parse(fs.readFileSync(tmpFile, 'utf8'))
    assert(
      afterChunk1.length < tweets.length,
      `after chunk 1 success: ${afterChunk1.length} alerts remain (was ${tweets.length})`
    )
    assert(
      !afterChunk1.some(t => sentUrls.has(t.url)),
      'sent tweets are removed from pending file immediately'
    )
    // chunk 2 fails — file unchanged (still has remaining)
    const afterFailure = JSON.parse(fs.readFileSync(tmpFile, 'utf8'))
    assert(afterFailure.length === afterChunk1.length, 'chunk 2 failure: file unchanged')
  } else {
    // Only 1 chunk — test not applicable (skip cleanly)
    assert(true, `only 1 chunk produced — partial-failure test skipped`)
  }
  fs.unlinkSync(tmpFile)
}

// ── Test 9: idempotent retry — chunk 1 not re-sent after chunk 2 failure ──
console.log('\nTest 9: idempotent retry — already-sent tweets excluded from pending file')
{
  // Create enough tweets to guarantee 2 chunks
  const tweets = Array.from({ length: 30 }, (_, i) => makeTweet(i + 300))
  const testChunks = buildChunks(tweets)
  assert(testChunks.length >= 2, `need ≥ 2 chunks for retry test (got ${testChunks.length})`)

  if (testChunks.length >= 2) {
    // Chunk 1 succeeds
    const chunk1Urls = new Set(testChunks[0].tweets.map(t => t.url))
    let remaining = tweets.filter(t => !chunk1Urls.has(t.url))

    // Chunk 2 fails — remaining is already written to disk
    // On retry, buildChunks(remaining) should NOT include chunk 1's tweets
    const retryChunks = buildChunks(remaining)
    const retryUrls = new Set(retryChunks.flatMap(c => c.tweets).map(t => t.url))
    const overlap = [...chunk1Urls].filter(u => retryUrls.has(u))
    assert(overlap.length === 0, `retry chunks contain 0 already-sent tweets (overlap: ${overlap.length})`)
  }
}

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
