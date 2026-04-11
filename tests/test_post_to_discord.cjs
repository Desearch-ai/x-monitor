const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('fs')
const path = require('path')
const os = require('os')
const { buildChunks, postPendingAlerts } = require('../post-to-discord.cjs')

function makeTweet(id, category = 'desearch', text = 'x'.repeat(160)) {
  return {
    id: String(id),
    _monitor_category: category,
    text,
    like_count: 5,
    url: `https://x.com/u/status/${id}`,
    user: { username: `user${id}` }
  }
}

test('buildChunks returns Discord-safe {text, tweets} pairs', () => {
  const tweets = Array.from({ length: 40 }, (_, index) => makeTweet(index + 1, index < 20 ? 'desearch' : 'bittensor'))
  const chunks = buildChunks(tweets)

  assert.ok(chunks.length > 1)
  for (const chunk of chunks) {
    assert.ok(chunk.text.length <= 2000)
    assert.ok(Array.isArray(chunk.tweets))
    assert.ok(chunk.tweets.length > 0)
  }

  const covered = chunks.flatMap(chunk => chunk.tweets.map(tweet => tweet.url))
  assert.equal(new Set(covered).size, tweets.length)
})

test('queue lifecycle, partial failure keeps the original queue until the first successful chunk is acknowledged', async () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'xmon-'))
  const pendingFile = path.join(tmpDir, 'pending_alerts.json')
  const tweets = Array.from({ length: 40 }, (_, index) => makeTweet(index + 1, 'desearch', 'y'.repeat(180)))
  fs.writeFileSync(pendingFile, JSON.stringify(tweets), 'utf8')

  const fileSnapshots = []
  let calls = 0
  await assert.rejects(
    postPendingAlerts({
      token: 'token',
      channelId: 'channel',
      pendingFile,
      post: async () => {
        fileSnapshots.push(JSON.parse(fs.readFileSync(pendingFile, 'utf8')))
        calls += 1
        if (calls === 2) throw new Error('chunk 2 failed')
      }
    }),
    /chunk 2 failed/
  )

  assert.equal(fileSnapshots.length, 2)
  assert.deepEqual(fileSnapshots[0], tweets, 'before chunk 1 succeeds, the full original queue is still present')

  const afterFailure = JSON.parse(fs.readFileSync(pendingFile, 'utf8'))
  const chunks = buildChunks(tweets)
  const chunk1Urls = new Set(chunks[0].tweets.map(tweet => tweet.url))
  const chunk2Urls = new Set(chunks[1].tweets.map(tweet => tweet.url))

  assert.ok(afterFailure.length < tweets.length, 'successful chunk 1 should be removed from the queue')
  assert.ok(afterFailure.every(tweet => !chunk1Urls.has(tweet.url)), 'chunk 1 tweets must not remain queued after they were posted')
  assert.ok([...chunk2Urls].every(url => afterFailure.some(tweet => tweet.url === url)), 'chunk 2 tweets must stay queued after the failure')

  fs.rmSync(tmpDir, { recursive: true, force: true })
})

test('idempotent retry only posts unsent chunks after a mid-run failure', async () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'xmon-'))
  const pendingFile = path.join(tmpDir, 'pending_alerts.json')
  const tweets = Array.from({ length: 40 }, (_, index) => makeTweet(index + 1, 'desearch', 'z'.repeat(180)))
  fs.writeFileSync(pendingFile, JSON.stringify(tweets), 'utf8')

  const firstRunPosts = []
  let calls = 0
  await assert.rejects(
    postPendingAlerts({
      token: 'token',
      channelId: 'channel',
      pendingFile,
      post: async (_token, _channelId, text) => {
        firstRunPosts.push(text)
        calls += 1
        if (calls === 2) throw new Error('chunk 2 failed')
      }
    }),
    /chunk 2 failed/
  )

  const retryPosts = []
  await postPendingAlerts({
    token: 'token',
    channelId: 'channel',
    pendingFile,
    post: async (_token, _channelId, text) => {
      retryPosts.push(text)
    }
  })

  assert.ok(firstRunPosts.length >= 2)
  assert.ok(!retryPosts.includes(firstRunPosts[0]), 'retry must not re-send the already posted first chunk')
  assert.equal(JSON.parse(fs.readFileSync(pendingFile, 'utf8')).length, 0)

  fs.rmSync(tmpDir, { recursive: true, force: true })
})
