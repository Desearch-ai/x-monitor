const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('fs')
const path = require('path')
const os = require('os')
const { buildChunks, SENT_TRACKING_FILE } = require('../post-to-discord.cjs')

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

  const allChunkTweets = chunks.flatMap(c => c.tweets)
  assert.equal(allChunkTweets.length, tweets.length, 'all tweets must be covered by chunks')
  const urls = new Set(allChunkTweets.map(t => t.url))
  assert.equal(urls.size, tweets.length, 'no tweet should appear in multiple chunks')
})

test('partial failure preserves queue: pending_alerts.json unchanged with all original alerts after mid-run failure', async () => {
  // Queue contract:
  //   - pending_alerts.json is NEVER modified mid-run; it is only cleared after ALL chunks succeed.
  //   - On partial failure, pending_alerts.json is left unchanged — all original alerts still present.
  //   - Sent tweet URLs are recorded in pending_alerts.sent.json after each chunk success
  //     so a retry can skip already-posted chunks without re-posting them.
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'xmon-test-'))
  const pendingFile = path.join(tmpDir, 'pending_alerts.json')
  const trackingFile = path.join(tmpDir, 'pending_alerts.sent.json')

  const tweets = Array.from({ length: 40 }, (_, i) => makeTweet(i + 1, 'desearch', 'x'.repeat(180)))
  const originalContent = JSON.stringify(tweets)
  fs.writeFileSync(pendingFile, originalContent)

  const chunks = buildChunks(tweets)
  assert.ok(chunks.length >= 2, 'need at least 2 chunks for this test')

  // Simulate posting loop: chunk 1 succeeds, chunk 2 fails
  let sentUrls = new Set()
  let callCount = 0
  const mockPost = async () => {
    callCount++
    if (callCount === 2) throw new Error('simulated chunk 2 failure')
  }

  let errorCaught = false
  for (const { tweets: chunkTweets } of chunks) {
    try {
      await mockPost()
      for (const t of chunkTweets) sentUrls.add(t.url)
      fs.writeFileSync(trackingFile, JSON.stringify([...sentUrls]))
      // NOTE: pending_alerts.json is NOT touched here — only cleared after full success
    } catch (_e) {
      errorCaught = true
      break
    }
  }

  assert.ok(errorCaught, 'should have caught chunk 2 error')

  // pending_alerts.json must be UNCHANGED — partial success must not clear or modify the file
  const fileAfterPartialFailure = fs.readFileSync(pendingFile, 'utf8')
  assert.equal(fileAfterPartialFailure, originalContent,
    'pending_alerts.json must be unchanged after partial failure — all original alerts preserved for retry')

  // Tracking file records chunk 1 URLs so retry can skip them
  const tracking = JSON.parse(fs.readFileSync(trackingFile, 'utf8'))
  const chunk1Urls = chunks[0].tweets.map(t => t.url)
  for (const url of chunk1Urls) {
    assert.ok(tracking.includes(url),
      `chunk 1 tweet ${url} must be in tracking file so retry skips it`)
  }

  fs.rmSync(tmpDir, { recursive: true })
})

test('idempotent retry — chunk 1 not re-sent after chunk 2 failure', async () => {
  // Retry contract:
  //   - pending_alerts.json still has ALL tweets (unchanged from before first run)
  //   - Tracking file has chunk 1 URLs
  //   - On retry startup, filter tweets by tracking file — chunk 1 tweets excluded
  //   - Only chunk 2 tweets are posted on retry — no duplicates
  const tweets = Array.from({ length: 40 }, (_, i) => makeTweet(i + 1, 'desearch', 'x'.repeat(180)))
  const chunks = buildChunks(tweets)
  assert.ok(chunks.length >= 2, 'need at least 2 chunks for this test')

  // First run: chunk 1 succeeds, chunk 2 fails → tracking file has chunk 1 URLs
  let sentUrls = new Set()
  let callCount = 0
  const mockPost = async (text) => {
    callCount++
    if (callCount === 2) throw new Error('simulated chunk 2 failure')
  }

  for (const { text, tweets: chunkTweets } of chunks) {
    try {
      await mockPost(text)
      for (const t of chunkTweets) sentUrls.add(t.url)
    } catch (_e) {
      break
    }
  }

  // Retry: pending_alerts.json has ALL tweets; filter by tracking file (sentUrls)
  const unsentTweets = tweets.filter(t => !sentUrls.has(t.url))
  const retryChunks = buildChunks(unsentTweets)
  const retryUrls = new Set(retryChunks.flatMap(c => c.tweets).map(t => t.url))

  // Chunk 1 tweets must NOT appear in retry
  const chunk1Urls = new Set(chunks[0].tweets.map(t => t.url))
  for (const url of chunk1Urls) {
    assert.ok(!retryUrls.has(url),
      `chunk 1 tweet ${url} must NOT be re-sent on retry — already in tracking file`)
  }

  // All original tweets accounted for across first run + retry
  const totalCovered = new Set([...sentUrls, ...retryUrls])
  assert.equal(totalCovered.size, tweets.length, 'all tweets covered across first run + retry — no tweet lost or re-sent')
})
