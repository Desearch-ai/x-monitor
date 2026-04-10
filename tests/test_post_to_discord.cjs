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

test('partial failure: chunk 1 tweets removed from queue, chunk 2 tweets preserved for retry', async () => {
  // Queue lifecycle contract (incremental model):
  //   - After chunk 1 succeeds, pending_alerts.json is rewritten with only chunk 2 tweets
  //   - After chunk 2 fails, pending_alerts.json is unchanged (still has chunk 2 tweets)
  //   - On retry, reading the updated file gives only the unsent tweets — chunk 1 NOT re-posted
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'xmon-test-'))
  const pendingFile = path.join(tmpDir, 'pending_alerts.json')

  // Build enough tweets to guarantee 2+ chunks
  const tweets = Array.from({ length: 40 }, (_, i) => makeTweet(i + 1, 'desearch', 'x'.repeat(180)))
  fs.writeFileSync(pendingFile, JSON.stringify(tweets))

  const chunks = buildChunks(tweets)
  assert.ok(chunks.length >= 2, 'need at least 2 chunks for this test')

  // Simulate the posting loop with incremental file updates (mirrors main() behavior)
  let callCount = 0
  const mockPost = async () => {
    callCount++
    if (callCount === 2) throw new Error('simulated chunk 2 failure')
  }

  let remaining = [...tweets]
  let errorCaught = false
  for (const { tweets: chunkTweets } of chunks) {
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

  // pending_alerts.json must contain ONLY chunk 2 tweets — chunk 1 tweets were removed on success
  const fileAfter = JSON.parse(fs.readFileSync(pendingFile, 'utf8'))
  const chunk1Urls = new Set(chunks[0].tweets.map(t => t.url))
  const chunk2Urls = new Set(chunks[1].tweets.map(t => t.url))

  for (const t of fileAfter) {
    assert.ok(!chunk1Urls.has(t.url),
      `chunk 1 tweet ${t.url} must have been removed from pending_alerts.json after successful post`)
  }
  for (const url of chunk2Urls) {
    assert.ok(fileAfter.some(t => t.url === url),
      `chunk 2 tweet ${url} must still be in pending_alerts.json for retry`)
  }
  const expectedRemaining = tweets.length - chunks[0].tweets.length
  assert.equal(fileAfter.length, expectedRemaining,
    `pending_alerts.json must contain exactly ${expectedRemaining} tweets after chunk 1 removed`)

  fs.rmSync(tmpDir, { recursive: true })
})

test('idempotent retry — on retry, only unsent tweets are posted; chunk 1 not re-sent', async () => {
  const tweets = Array.from({ length: 40 }, (_, i) => makeTweet(i + 1, 'desearch', 'x'.repeat(180)))
  const chunks = buildChunks(tweets)
  assert.ok(chunks.length >= 2, 'need at least 2 chunks for this test')

  // First run: chunk 1 succeeds, chunk 2 fails
  // After chunk 1 success, pending_alerts.json is updated to contain only chunk 2 tweets
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

  // `remaining` now contains only chunk 2 tweets (as would be read from updated pending_alerts.json)
  // Retry: build chunks from the updated file contents — only chunk 2 tweets are present
  const retryChunks = buildChunks(remaining)

  // Retry chunks must not contain any chunk 1 tweets
  const chunk1Urls = new Set(chunks[0].tweets.map(t => t.url))
  const retryUrls = new Set(retryChunks.flatMap(c => c.tweets).map(t => t.url))
  for (const url of chunk1Urls) {
    assert.ok(!retryUrls.has(url),
      `chunk 1 tweet ${url} must NOT appear in retry — already removed from pending_alerts.json`)
  }

  // All chunk 2 tweets must be in the retry
  const chunk2Urls = new Set(chunks[1].tweets.map(t => t.url))
  for (const url of chunk2Urls) {
    assert.ok(retryUrls.has(url),
      `chunk 2 tweet ${url} must appear in retry chunks`)
  }

  // Combined coverage: chunk 1 (sent in first run) + retry covers all original tweets
  const totalCovered = new Set([...chunks[0].tweets.map(t => t.url), ...retryUrls])
  assert.equal(totalCovered.size, tweets.length, 'all tweets covered across first run + retry')
})
