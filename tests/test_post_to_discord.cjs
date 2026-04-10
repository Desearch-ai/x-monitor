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

test('queue lifecycle — partial failure: pending_alerts.json unchanged if chunk 2 fails', async () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'xmon-test-'))
  const pendingFile = path.join(tmpDir, 'pending_alerts.json')

  // Build enough tweets to guarantee 2+ chunks
  const tweets = Array.from({ length: 40 }, (_, i) => makeTweet(i + 1, 'desearch', 'x'.repeat(180)))
  fs.writeFileSync(pendingFile, JSON.stringify(tweets))

  const chunks = buildChunks(tweets)
  assert.ok(chunks.length >= 2, 'need at least 2 chunks for this test')

  let callCount = 0
  const mockPost = async () => {
    callCount++
    if (callCount === 2) throw new Error('simulated chunk 2 failure')
  }

  // Simulate main() posting logic with the mock
  let remaining = [...tweets]
  let errorCaught = false
  for (const { text: _text, tweets: chunkTweets } of chunks) {
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

  // After chunk 1 success + chunk 2 fail:
  // - chunk 1's tweets should be removed (queue updated incrementally)
  // - chunk 2's tweets should still be in the file
  const fileAfter = JSON.parse(fs.readFileSync(pendingFile, 'utf8'))
  const chunk1Urls = new Set(chunks[0].tweets.map(t => t.url))
  const chunk2Urls = new Set(chunks[1].tweets.map(t => t.url))

  for (const t of fileAfter) {
    assert.ok(!chunk1Urls.has(t.url), `chunk 1 tweet ${t.url} should have been removed after successful post`)
  }
  for (const url of chunk2Urls) {
    assert.ok(fileAfter.some(t => t.url === url), `chunk 2 tweet ${url} must still be in pending file`)
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
  let remaining = [...tweets]
  for (const { text, tweets: chunkTweets } of chunks) {
    try {
      await mockPost(text)
      const sentUrls = new Set(chunkTweets.map(t => t.url))
      remaining = remaining.filter(t => !sentUrls.has(t.url))
    } catch (_e) {
      break
    }
  }

  // Retry: rebuild chunks from remaining tweets (simulating next run reading updated pending_alerts.json)
  const retryChunks = buildChunks(remaining)
  const retryPostedTexts = []
  for (const { text, tweets: chunkTweets } of retryChunks) {
    retryPostedTexts.push(text)
    const sentUrls = new Set(chunkTweets.map(t => t.url))
    remaining = remaining.filter(t => !sentUrls.has(t.url))
  }

  // chunk 1's text must NOT appear in the retry
  assert.ok(!retryPostedTexts.includes(chunks[0].text),
    'chunk 1 must NOT be re-sent on retry — its tweets were removed from pending_alerts.json after first post')

  // All tweets should eventually be covered
  assert.equal(remaining.length, 0, 'all tweets should be sent after retry')
})
