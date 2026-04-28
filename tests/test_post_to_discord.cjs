const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('fs')
const path = require('path')
const os = require('os')
const https = require('https')
const { EventEmitter } = require('events')
const { buildChunks, postPendingAlerts, postToDiscord, acquireLock } = require('../post-to-discord.cjs')

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

test('postToDiscord retries 429 using retry_after before acknowledging success', async () => {
  const originalRequest = https.request
  const sleeps = []
  const statuses = [
    { statusCode: 429, headers: {}, body: JSON.stringify({ retry_after: 0.05 }) },
    { statusCode: 200, headers: {}, body: JSON.stringify({ id: 'message-1' }) }
  ]

  https.request = (_opts, callback) => {
    const next = statuses.shift()
    const req = new EventEmitter()
    req.write = () => {}
    req.end = () => {
      const res = new EventEmitter()
      res.statusCode = next.statusCode
      res.headers = next.headers
      callback(res)
      res.emit('data', next.body)
      res.emit('end')
    }
    return req
  }

  try {
    const result = await postToDiscord('token', 'channel', 'hello', {
      maxAttempts: 3,
      sleep: async ms => sleeps.push(ms)
    })

    assert.equal(result.id, 'message-1')
    assert.deepEqual(sleeps, [50])
    assert.equal(statuses.length, 0)
  } finally {
    https.request = originalRequest
  }
})

test('postToDiscord bounds 429 retries and prefers Retry-After header seconds', async () => {
  const originalRequest = https.request
  const sleeps = []
  let attempts = 0

  https.request = (_opts, callback) => {
    attempts += 1
    const req = new EventEmitter()
    req.write = () => {}
    req.end = () => {
      const res = new EventEmitter()
      res.statusCode = 429
      res.headers = { 'retry-after': '0.02' }
      callback(res)
      res.emit('data', JSON.stringify({ retry_after: 99 }))
      res.emit('end')
    }
    return req
  }

  try {
    await assert.rejects(
      postToDiscord('token', 'channel', 'hello', {
        maxAttempts: 2,
        sleep: async ms => sleeps.push(ms)
      }),
      /Discord HTTP 429/
    )

    assert.equal(attempts, 2, '429 retry should stop at the configured attempt bound')
    assert.deepEqual(sleeps, [20], 'Retry-After header should drive the wait before the final attempt')
  } finally {
    https.request = originalRequest
  }
})

test('acquireLock respects active locks and safely recovers stale dead locks', () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'xmon-lock-'))
  const activeLock = path.join(tmpDir, 'active.lock')
  const staleLock = path.join(tmpDir, 'stale.lock')

  fs.mkdirSync(activeLock)
  fs.writeFileSync(path.join(activeLock, 'lock.json'), JSON.stringify({ pid: process.pid, createdAt: new Date(0).toISOString() }), 'utf8')
  assert.throws(() => acquireLock(activeLock, { staleMs: 1 }), /already running/)
  assert.ok(fs.existsSync(activeLock), 'active lock must not be removed')

  fs.mkdirSync(staleLock)
  fs.writeFileSync(path.join(staleLock, 'lock.json'), JSON.stringify({ pid: 999999, createdAt: new Date(0).toISOString() }), 'utf8')
  const release = acquireLock(staleLock, { staleMs: 1, now: () => Date.now() })
  assert.ok(fs.existsSync(path.join(staleLock, 'lock.json')), 'recovered lock should be replaced with fresh metadata')
  release()
  assert.ok(!fs.existsSync(staleLock), 'release removes the lock acquired by this process')

  fs.rmSync(tmpDir, { recursive: true, force: true })
})
