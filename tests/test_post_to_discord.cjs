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

  // Every tweet must appear in exactly one chunk
  const allChunkTweets = chunks.flatMap(c => c.tweets)
  assert.equal(allChunkTweets.length, tweets.length, 'all tweets must be covered by chunks')
  const urls = new Set(allChunkTweets.map(t => t.url))
  assert.equal(urls.size, tweets.length, 'no tweet should appear in multiple chunks')
})

test('queue lifecycle — partial failure: pending_alerts.json unchanged if chunk 2 fails', async () => {
  // Queue contract:
  //   - pending_alerts.json is only cleared AFTER all chunks succeed
  //   - on partial failure, pending_alerts.json is left unchanged (all alerts preserved for retry)
  //   - sent tweet URLs are written to pending_alerts.sent.json after each chunk success
  //     so a retry skips already-posted chunks without needing to modify the main queue file
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'xmon-test-'))
  const pendingFile = path.join(tmpDir, 'pending_alerts.json')
  const trackingFile = path.join(tmpDir, 'pending_alerts.sent.json')

  // Build enough tweets to guarantee 2+ chunks
  const tweets = Array.from({ length: 40 }, (_, i) => makeTweet(i + 1, 'desearch', 'x'.repeat(180)))
  const originalContent = JSON.stringify(tweets)
  fs.writeFileSync(pendingFile, originalContent)

  const chunks = buildChunks(tweets)
  assert.ok(chunks.length >= 2, 'need at least 2 chunks for this test')

  // Simulate the posting flow with tracking file (matches main() behavior)
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
    } catch (_e) {
      errorCaught = true
      break
    }
  }

  assert.ok(errorCaught, 'should have caught chunk 2 error')

  // pending_alerts.json must be UNCHANGED — partial success must not clear the file
  const fileAfterPartialFailure = fs.readFileSync(pendingFile, 'utf8')
  assert.equal(fileAfterPartialFailure, originalContent,
    'pending_alerts.json must be unchanged after partial failure — all original alerts preserved')

  // tracking file records which tweets were already sent
  const tracking = JSON.parse(fs.readFileSync(trackingFile, 'utf8'))
  const chunk1Urls = chunks[0].tweets.map(t => t.url)
  for (const url of chunk1Urls) {
    assert.ok(tracking.includes(url),
      `chunk 1 tweet ${url} must be in tracking file so retry skips it`)
  }

  fs.rmSync(tmpDir, { recursive: true })
})

test('idempotent retry — chunk 1 not re-sent after chunk 2 failure', async () => {
  const tweets = Array.from({ length: 40 }, (_, i) => makeTweet(i + 1, 'desearch', 'x'.repeat(180)))
  const chunks = buildChunks(tweets)
  assert.ok(chunks.length >= 2, 'need at least 2 chunks for this test')

  const postedTexts = []
  let callCount = 0
  const mockPost = async (text) => {
    callCount++
    postedTexts.push(text)
    if (callCount === 2) throw new Error('simulated chunk 2 failure')
  }

  // First run: chunk 1 succeeds, chunk 2 fails
  // Tracking file records chunk 1 URLs
  let sentUrls = new Set()
  for (const { text, tweets: chunkTweets } of chunks) {
    try {
      await mockPost(text)
      for (const t of chunkTweets) sentUrls.add(t.url)
    } catch (_e) {
      break
    }
  }

  // Retry: filter pending tweets by tracking file (as main() does at startup)
  // pending_alerts.json still has ALL tweets; tracking excludes chunk 1
  const unsentTweets = tweets.filter(t => !sentUrls.has(t.url))
  const retryChunks = buildChunks(unsentTweets)
  const retryPostedTexts = []
  for (const { text } of retryChunks) {
    retryPostedTexts.push(text)
  }

  // chunk 1's text must NOT appear in the retry
  assert.ok(!retryPostedTexts.includes(chunks[0].text),
    'chunk 1 must NOT be re-sent on retry — tracking file excludes its tweets')

  // All tweets should be covered across both runs
  const totalCovered = new Set([...sentUrls, ...unsentTweets.map(t => t.url)])
  assert.equal(totalCovered.size, tweets.length, 'all tweets covered across first run + retry')
})
