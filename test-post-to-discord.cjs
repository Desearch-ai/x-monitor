#!/usr/bin/env node
/**
 * Tests for post-to-discord.cjs
 *
 * Run:  node test-post-to-discord.cjs
 *
 * Covers:
 *   - buildChunks unit tests (Discord-safe split, coverage, ordering)
 *   - Test 1: partial failure preserves queue
 *       chunk 1 succeeds → tweets durably removed; chunk 2 fails → tweets preserved
 *   - Test 2: idempotent retry
 *       retry only sends chunk 2; pending_alerts.json is inspected between attempts
 */

'use strict'

const assert = require('assert')
const fs = require('fs')
const os = require('os')
const path = require('path')

const {
  buildChunks,
  processQueue,
  tweetId,
  readPendingAlerts,
  DISCORD_SAFE_LIMIT
} = require('./post-to-discord.cjs')

// ── Test runner ──────────────────────────────────────────────────────────────

const suite = []
let passed = 0
let failed = 0

function test (name, fn) {
  suite.push({ name, fn, async: false })
}
function testAsync (name, fn) {
  suite.push({ name, fn, async: true })
}

async function runAll () {
  for (const t of suite) {
    try {
      if (t.async) await t.fn()
      else t.fn()
      console.log(`  ✓ ${t.name}`)
      passed++
    } catch (e) {
      console.error(`  ✗ ${t.name}`)
      console.error(`      ${e.message}`)
      failed++
    }
  }
  console.log(`\nResults: ${passed} passed, ${failed} failed`)
  if (failed > 0) process.exit(1)
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Create a minimal tweet object for testing.
 * id and url are set so tweetId() returns a stable string.
 */
function makeTweet (id, cat, text) {
  const sid = String(id)
  return {
    id: sid,
    url: `https://x.com/u${sid}/s/${sid}`,
    user: { username: `u${sid}` },
    text: text || `Tweet ${sid}`,
    like_count: 0,
    _monitor_category: cat || 'general'
  }
}

/** Write an array of tweets to a temp file, return the file path. */
function writeTmpAlerts (tweets) {
  const file = path.join(
    os.tmpdir(),
    `pending_test_${Date.now()}_${Math.random().toString(36).slice(2)}.json`
  )
  fs.writeFileSync(file, JSON.stringify(tweets, null, 2))
  return file
}

// Small limit that forces a split at 2 'brand' tweets + 2 'bittensor' tweets.
// With this limit the first two tweets fit but adding the third (different cat)
// exceeds it, so we get exactly chunk1=[t1,t2] and chunk2=[t3,t4].
const TEST_LIMIT = 200

// ── buildChunks unit tests ────────────────────────────────────────────────────

console.log('\nbuildChunks:')

test('empty input returns empty array', () => {
  assert.deepStrictEqual(buildChunks([]), [])
  assert.deepStrictEqual(buildChunks(null), [])
  assert.deepStrictEqual(buildChunks(undefined), [])
})

test('single tweet produces one chunk with header and category', () => {
  const chunks = buildChunks([makeTweet('a', 'bittensor')])
  assert.strictEqual(chunks.length, 1)
  assert.strictEqual(chunks[0].tweets.length, 1)
  assert.ok(chunks[0].text.includes('🔔 X Monitor'), 'header present')
  assert.ok(chunks[0].text.includes('bittensor'), 'category present')
  assert.ok(chunks[0].text.includes('@ua'), 'username present')
})

test('chunk text stays within Discord-safe limit', () => {
  const tweets = Array.from({ length: 30 }, (_, i) =>
    makeTweet(i + 1, 'general', 'A'.repeat(90))
  )
  const chunks = buildChunks(tweets)
  assert.ok(chunks.length > 0, 'should produce at least one chunk')
  for (const chunk of chunks) {
    assert.ok(
      chunk.text.length <= DISCORD_SAFE_LIMIT,
      `chunk length ${chunk.text.length} exceeds limit ${DISCORD_SAFE_LIMIT}`
    )
  }
})

test('all tweets appear exactly once across chunks', () => {
  const tweets = Array.from({ length: 20 }, (_, i) =>
    makeTweet(i + 1, i < 10 ? 'brand' : 'bittensor', 'T'.repeat(60))
  )
  const chunks = buildChunks(tweets, TEST_LIMIT)
  const allTweets = chunks.flatMap(c => c.tweets)
  assert.strictEqual(allTweets.length, tweets.length, 'tweet count must match')
  const ids = new Set(allTweets.map(t => t.id))
  assert.strictEqual(ids.size, tweets.length, 'no duplicate tweets in chunks')
})

test('tweet order is preserved across chunks', () => {
  const tweets = Array.from({ length: 10 }, (_, i) => makeTweet(i + 1, 'cat'))
  const chunks = buildChunks(tweets, TEST_LIMIT)
  const combined = chunks.flatMap(c => c.tweets).map(t => t.id)
  assert.deepStrictEqual(combined, tweets.map(t => t.id))
})

test('chunk splits at category boundary under small limit', () => {
  // With TEST_LIMIT=200: 2 brand tweets fit, adding bittensor (new cat + header) exceeds it
  const tweets = [
    makeTweet('c1a', 'brand'),
    makeTweet('c1b', 'brand'),
    makeTweet('c2a', 'bittensor'),
    makeTweet('c2b', 'bittensor')
  ]
  const chunks = buildChunks(tweets, TEST_LIMIT)
  assert.ok(chunks.length >= 2, `expected ≥2 chunks, got ${chunks.length}`)
  // chunk 1 must contain only brand tweets
  assert.ok(
    chunks[0].tweets.every(t => t._monitor_category === 'brand'),
    'chunk 1 should be brand only'
  )
  // chunk 2 must contain only bittensor tweets
  assert.ok(
    chunks[1].tweets.every(t => t._monitor_category === 'bittensor'),
    'chunk 2 should be bittensor only'
  )
})

test('tweetId uses id, then url, then fingerprint', () => {
  assert.strictEqual(tweetId({ id: '123' }), '123')
  assert.strictEqual(tweetId({ url: 'https://x.com/u/s/1' }), 'https://x.com/u/s/1')
  const fp = tweetId({ user: { username: 'alice' }, text: 'hello world' })
  assert.ok(fp.includes('alice'), 'fingerprint should include username')
})

// ── Queue lifecycle Test 1 ────────────────────────────────────────────────────

console.log('\nQueue lifecycle:')

testAsync('Test 1: partial failure — chunk 1 success, chunk 2 fails => only chunk-2 tweets remain', async () => {
  const brandTweets = [makeTweet('c1a', 'brand'), makeTweet('c1b', 'brand')]
  const bitTweets = [makeTweet('c2a', 'bittensor'), makeTweet('c2b', 'bittensor')]
  const all = [...brandTweets, ...bitTweets]

  const file = writeTmpAlerts(all)

  // Verify test setup forces exactly 2 chunks
  const preChunks = buildChunks(all, TEST_LIMIT)
  assert.ok(preChunks.length >= 2, `setup: expected ≥2 chunks with TEST_LIMIT, got ${preChunks.length}`)

  let callCount = 0
  const mockPost = async () => {
    callCount++
    if (callCount === 1) return { id: 'discord-msg-1' }  // chunk 1 ok
    throw new Error('Discord 500 [discord-api-error] channel=test: Internal Server Error')  // chunk 2 fails
  }

  const result = await processQueue(file, 'fake-token', 'fake-channel', mockPost, { limit: TEST_LIMIT })

  assert.strictEqual(result.failed, true, 'result.failed should be true')
  assert.strictEqual(callCount, 2, 'postFn should have been called twice')

  const remaining = readPendingAlerts(file)
  const remainingIds = new Set(remaining.map(t => t.id))

  // Chunk 1 tweets must be durably removed
  for (const t of preChunks[0].tweets) {
    assert.ok(!remainingIds.has(t.id), `chunk-1 tweet ${t.id} should have been removed`)
  }

  // Chunk 2 tweets must still be pending
  for (const t of preChunks[1].tweets) {
    assert.ok(remainingIds.has(t.id), `chunk-2 tweet ${t.id} should still be pending`)
  }

  fs.unlinkSync(file)
})

// ── Queue lifecycle Test 2 ────────────────────────────────────────────────────

testAsync('Test 2: idempotent retry — retry sends only chunk-2, not chunk-1 again', async () => {
  const brandTweets = [makeTweet('r1a', 'brand'), makeTweet('r1b', 'brand')]
  const bitTweets = [makeTweet('r2a', 'bittensor'), makeTweet('r2b', 'bittensor')]
  const all = [...brandTweets, ...bitTweets]

  const file = writeTmpAlerts(all)

  const preChunks = buildChunks(all, TEST_LIMIT)
  assert.ok(preChunks.length >= 2, `setup: expected ≥2 chunks with TEST_LIMIT, got ${preChunks.length}`)

  const chunk1Ids = new Set(preChunks[0].tweets.map(t => t.id))
  const chunk2Ids = new Set(preChunks[1].tweets.map(t => t.id))

  // ── Attempt 1: chunk 1 succeeds, chunk 2 fails ──────────────────────────
  let attempt1Calls = 0
  const mockPost1 = async () => {
    attempt1Calls++
    if (attempt1Calls === 1) return { id: 'discord-msg-1' }
    throw new Error('Network error on attempt 1')
  }

  const result1 = await processQueue(file, 'tok', 'chan', mockPost1, { limit: TEST_LIMIT })
  assert.strictEqual(result1.failed, true, 'attempt 1 should fail')

  // ── Inspect file between attempts ────────────────────────────────────────
  const midState = readPendingAlerts(file)
  const midIds = new Set(midState.map(t => t.id))

  for (const id of chunk1Ids) {
    assert.ok(!midIds.has(id), `between attempts: chunk-1 tweet ${id} should be gone`)
  }
  for (const id of chunk2Ids) {
    assert.ok(midIds.has(id), `between attempts: chunk-2 tweet ${id} should remain`)
  }

  // ── Attempt 2 (retry): should only post chunk-2 tweets ──────────────────
  const sentMessages = []
  const mockPost2 = async (tok, chan, message) => {
    sentMessages.push(message)
    return { id: 'discord-msg-retry' }
  }

  const result2 = await processQueue(file, 'tok', 'chan', mockPost2, { limit: TEST_LIMIT })
  assert.strictEqual(result2.failed, false, 'retry should succeed')

  // Only 1 message sent on retry (only chunk 2 remaining)
  assert.strictEqual(sentMessages.length, 1, `retry should send exactly 1 message, sent ${sentMessages.length}`)

  // The retry message must NOT contain chunk-1 usernames
  const retryMsg = sentMessages[0]
  for (const id of chunk1Ids) {
    assert.ok(!retryMsg.includes(`@u${id}`), `retry message must not include chunk-1 @u${id}`)
  }

  // The retry message MUST contain chunk-2 usernames
  for (const id of chunk2Ids) {
    assert.ok(retryMsg.includes(`@u${id}`), `retry message must include chunk-2 @u${id}`)
  }

  // Queue must be fully empty after successful retry
  const afterRetry = readPendingAlerts(file)
  assert.strictEqual(afterRetry.length, 0, 'queue should be empty after successful retry')

  fs.unlinkSync(file)
})

// ── Run ───────────────────────────────────────────────────────────────────────

runAll().catch(e => { console.error(e); process.exit(1) })
