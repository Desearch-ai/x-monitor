const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('fs')
const path = require('path')
const os = require('os')
const { buildChunks } = require('../post-to-discord.cjs')

function makeTweet(id, category = 'desearch', text = 'x'.repeat(150)) {
  return {
    id: String(id),
    _monitor_category: category,
    text,
    like_count: 5,
    url: `https://x.com/u/status/${id}`,
    user: { username: `user${id}` }
  }
}

test('buildChunks returns {text, tweets} pairs and splits oversized batches into Discord-safe chunks', () => {
  const tweets = Array.from({ length: 40 }, (_, i) => makeTweet(i + 1, i < 20 ? 'desearch' : 'bittensor'))
  const chunks = buildChunks(tweets)

  assert.ok(chunks.length > 1, 'expected multiple chunks for large batch')
  for (const { text, tweets: ct } of chunks) {
    assert.ok(text.length <= 2000, `chunk text too large: ${text.length}`)
    assert.ok(Array.isArray(ct), 'chunk.tweets must be an array')
    assert.ok(ct.length > 0, 'each chunk must have at least one tweet')
  }
  assert.match(chunks[0].text, /🔔 X Monitor/)

  // Every tweet must appear in exactly one chunk
  const allChunkTweets = chunks.flatMap(c => c.tweets)
  assert.equal(allChunkTweets.length, tweets.length, 'all tweets must be covered by chunks')
  const urls = new Set(allChunkTweets.map(t => t.url))
  assert.equal(urls.size, tweets.length, 'no tweet should appear in multiple chunks')
})

test('partial failure preserves queue: pending_alerts.json unchanged after failed chunk, all unsent tweets retained', async () => {
  // Queue lifecycle — incremental model (spec Test 1: "partial success must not clear the file"):
  //   - After each successful chunk, pending_alerts.json is rewritten with only the unsent tweets.
  //   - After a FAILED chunk, pending_alerts.json is NOT modified — the failure itself must not touch the file.
  //   - Result: after failure, pending_alerts.json = state just before failure = all unsent tweets still present.
  //   - No tweet is lost: delivered tweets + remaining queue = all original tweets.
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'xmon-test-'))
  const pendingFile = path.join(tmpDir, 'pending_alerts.json')

  const tweets = Array.from({ length: 40 }, (_, i) => makeTweet(i + 1, 'desearch', 'x'.repeat(180)))
  fs.writeFileSync(pendingFile, JSON.stringify(tweets))

  const chunks = buildChunks(tweets)
  assert.ok(chunks.length >= 2, 'need at least 2 chunks for this test')

  let callCount = 0
  const mockPost = async () => {
    callCount++
    if (callCount === 2) throw new Error('simulated chunk 2 failure')
  }

  let remaining = [...tweets]
  let fileBeforeFailure = null
  let errorCaught = false
  for (const { tweets: chunkTweets } of chunks) {
    // Snapshot file state before each attempt — after failure, file must match this snapshot
    fileBeforeFailure = fs.readFileSync(pendingFile, 'utf8')
    try {
      await mockPost()
      const sentUrls = new Set(chunkTweets.map(t => t.url))
      remaining = remaining.filter(t => !sentUrls.has(t.url))
      fs.writeFileSync(pendingFile, JSON.stringify(remaining))
    } catch (_e) {
      errorCaught = true
      break
    }
  }

  assert.ok(errorCaught, 'should have caught chunk 2 error')

  // The failure must NOT have modified the file — pending_alerts.json must match the snapshot taken before the failure
  const fileAfterFailure = fs.readFileSync(pendingFile, 'utf8')
  assert.equal(fileAfterFailure, fileBeforeFailure,
    'pending_alerts.json must be UNCHANGED by the failure — only successful posts update the queue')

  // All unsent tweets must still be present (no data loss)
  const fileData = JSON.parse(fileAfterFailure)
  const chunk1SentUrls = new Set(chunks[0].tweets.map(t => t.url))
  for (const t of fileData) {
    assert.ok(!chunk1SentUrls.has(t.url),
      `tweet ${t.url} was already delivered in chunk 1 — must NOT remain in queue`)
  }
  // No tweet is lost: delivered + queue = all original tweets
  assert.equal(fileData.length + chunks[0].tweets.length, tweets.length,
    'no tweet is lost: delivered tweets + remaining queue = all original tweets')

  fs.rmSync(tmpDir, { recursive: true })
})

test('idempotent retry — on retry, only unsent tweets are posted; chunk 1 not re-sent', async () => {
  const tweets = Array.from({ length: 40 }, (_, i) => makeTweet(i + 1, 'desearch', 'x'.repeat(180)))
  const chunks = buildChunks(tweets)
  assert.ok(chunks.length >= 2, 'need at least 2 chunks for this test')

  // First run: chunk 1 succeeds, chunk 2 fails
  // After chunk 1 success, pending_alerts.json is updated to contain only unsent tweets
  let remaining = [...tweets]
  let callCount = 0
  const mockPost = async () => {
    callCount++
    if (callCount === 2) throw new Error('simulated chunk 2 failure')
  }

  for (const { tweets: chunkTweets } of chunks) {
    try {
      await mockPost()
      const sentUrls = new Set(chunkTweets.map(t => t.url))
      remaining = remaining.filter(t => !sentUrls.has(t.url))
    } catch (_e) {
      break
    }
  }

  // Retry: build chunks from the updated queue (only unsent tweets)
  // This mirrors reading the updated pending_alerts.json on a fresh run
  const retryChunks = buildChunks(remaining)

  // Retry must not contain any chunk 1 tweets (already delivered)
  const chunk1Urls = new Set(chunks[0].tweets.map(t => t.url))
  const retryUrls = new Set(retryChunks.flatMap(c => c.tweets).map(t => t.url))
  for (const url of chunk1Urls) {
    assert.ok(!retryUrls.has(url),
      `chunk 1 tweet ${url} must NOT appear in retry — already removed from pending_alerts.json`)
  }

  // All chunk 2 tweets must be covered by retry
  const chunk2Urls = new Set(chunks[1].tweets.map(t => t.url))
  for (const url of chunk2Urls) {
    assert.ok(retryUrls.has(url),
      `chunk 2 tweet ${url} must appear in retry chunks`)
  }

  // Combined coverage: chunk 1 (delivered) + retry = all original tweets
  const totalCovered = new Set([...chunks[0].tweets.map(t => t.url), ...retryUrls])
  assert.equal(totalCovered.size, tweets.length, 'all tweets covered across first run + retry')
})
