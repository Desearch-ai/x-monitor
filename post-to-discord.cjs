#!/usr/bin/env node
const https = require('https')
const path = require('path')
const fs = require('fs')
const crypto = require('crypto')

const MONITOR_DIR = path.dirname(__filename)
const CONFIG_FILE = path.join(MONITOR_DIR, 'config.json')
const PENDING_ALERTS_FILE = path.join(MONITOR_DIR, 'pending_alerts.json')
const PENDING_ALERTS_LOCK_DIR = path.join(MONITOR_DIR, '.pending-alerts.lock')
const DISCORD_MAX_LEN = 2000
const MAX_TWEET_TEXT = 100
const DEFAULT_LOCK_STALE_MS = 60 * 60 * 1000
const DEFAULT_DISCORD_MAX_ATTEMPTS = 4
const DEFAULT_DISCORD_MAX_RETRY_MS = 60 * 1000

function atomicWriteJson(filePath, data) {
  const tmpPath = path.join(path.dirname(filePath), `.${path.basename(filePath)}.${process.pid}.tmp`)
  fs.writeFileSync(tmpPath, JSON.stringify(data, null, 2))
  fs.renameSync(tmpPath, filePath)
}

function pidIsRunning(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return error && error.code === 'EPERM'
  }
}

function readLockMetadata(lockDir) {
  try {
    return JSON.parse(fs.readFileSync(path.join(lockDir, 'lock.json'), 'utf8'))
  } catch {
    try {
      const pid = Number(fs.readFileSync(path.join(lockDir, 'pid'), 'utf8'))
      const stat = fs.statSync(lockDir)
      return { pid, createdAt: stat.mtime.toISOString(), legacy: true }
    } catch {
      return null
    }
  }
}

function isStaleLock(lockDir, { staleMs = DEFAULT_LOCK_STALE_MS, now = () => Date.now() } = {}) {
  const metadata = readLockMetadata(lockDir)
  const pid = Number(metadata?.pid)
  if (pidIsRunning(pid)) return false

  let createdAtMs = Date.parse(metadata?.createdAt || metadata?.updatedAt || '')
  if (!Number.isFinite(createdAtMs)) {
    try {
      createdAtMs = fs.statSync(lockDir).mtimeMs
    } catch {
      createdAtMs = now()
    }
  }

  return now() - createdAtMs > staleMs
}

function writeLockMetadata(lockDir, token, now = () => Date.now()) {
  fs.writeFileSync(path.join(lockDir, 'lock.json'), JSON.stringify({
    pid: process.pid,
    token,
    createdAt: new Date(now()).toISOString()
  }, null, 2), 'utf8')
  fs.writeFileSync(path.join(lockDir, 'pid'), String(process.pid), 'utf8')
}

function recoverStaleLock(lockDir, token) {
  const stalePath = `${lockDir}.stale-${process.pid}-${token}`
  try {
    fs.renameSync(lockDir, stalePath)
    fs.rmSync(stalePath, { recursive: true, force: true })
    return true
  } catch (error) {
    if (error && error.code === 'ENOENT') return false
    throw error
  }
}

function acquireLock(lockDir = PENDING_ALERTS_LOCK_DIR, options = {}) {
  const token = crypto.randomBytes(16).toString('hex')
  const now = options.now || (() => Date.now())

  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      fs.mkdirSync(lockDir)
      writeLockMetadata(lockDir, token, now)
      return () => {
        const metadata = readLockMetadata(lockDir)
        if (metadata?.pid === process.pid && metadata?.token === token) {
          fs.rmSync(lockDir, { recursive: true, force: true })
        }
      }
    } catch (error) {
      if (!error || error.code !== 'EEXIST') throw error
      if (!isStaleLock(lockDir, options)) {
        const metadata = readLockMetadata(lockDir)
        const pidHint = metadata?.pid ? ` (pid ${metadata.pid})` : ''
        throw new Error(`post-to-discord already running, lock held at ${lockDir}${pidHint}`)
      }
      if (!recoverStaleLock(lockDir, token)) continue
    }
  }

  throw new Error(`failed to acquire pending-alerts lock at ${lockDir}`)
}

function getDiscordToken() {
  if (process.env.DISCORD_BOT_TOKEN) return process.env.DISCORD_BOT_TOKEN
  try {
    const home = process.env.HOME || process.env.USERPROFILE || ''
    const cfg = JSON.parse(fs.readFileSync(path.join(home, '.openclaw/openclaw.json'), 'utf8'))
    return cfg.channels?.discord?.token
  } catch {
    return null
  }
}

function getChannelId() {
  try {
    const cfg = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'))
    return String(cfg.discord?.alerts_channel || '')
  } catch (e) {
    console.error('Failed to read config.json:', e.message)
    return ''
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function parseJsonMaybe(data) {
  try {
    return JSON.parse(data)
  } catch {
    return null
  }
}

function retryAfterMs(res, data, attempt, { maxRetryMs = DEFAULT_DISCORD_MAX_RETRY_MS } = {}) {
  const parsed = parseJsonMaybe(data)
  const retryAfter = res.headers?.['retry-after'] ?? res.headers?.['Retry-After'] ?? parsed?.retry_after
  const retryAfterNumber = Number(retryAfter)
  const backoffMs = Number.isFinite(retryAfterNumber) && retryAfterNumber >= 0
    ? retryAfterNumber * 1000
    : Math.min(1000 * 2 ** (attempt - 1), maxRetryMs)
  return Math.min(Math.ceil(backoffMs), maxRetryMs)
}

function discordHttpError(res, data) {
  let hint = ''
  if (res.statusCode === 400 && data.includes('BASE_TYPE_MAX_LENGTH')) hint = ' (payload too large, split messages below 2000 chars)'
  else if (res.statusCode === 401) hint = ' (bad token, check DISCORD_BOT_TOKEN or openclaw.json)'
  else if (res.statusCode === 403) hint = ' (bot lacks Send Messages permission in this channel)'
  else if (res.statusCode === 404) hint = ' (channel not found, check discord.alerts_channel in config.json)'
  else if (res.statusCode === 413) hint = ' (payload too large)'
  return new Error(`Discord HTTP ${res.statusCode}${hint}: ${data}`)
}

function postToDiscordOnce(token, channelId, message) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ content: message })
    const opts = {
      hostname: 'discord.com',
      path: `/api/v10/channels/${channelId}/messages`,
      method: 'POST',
      headers: {
        Authorization: `Bot ${token}`,
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body)
      }
    }

    const req = https.request(opts, res => {
      let data = ''
      res.on('data', d => { data += d })
      res.on('end', () => {
        if (res.statusCode >= 400) {
          const error = discordHttpError(res, data)
          error.statusCode = res.statusCode
          error.responseBody = data
          error.responseHeaders = res.headers || {}
          reject(error)
          return
        }
        resolve(parseJsonMaybe(data) || {})
      })
    })

    req.on('error', reject)
    req.write(body)
    req.end()
  })
}

async function postToDiscord(token, channelId, message, options = {}) {
  const maxAttempts = options.maxAttempts || DEFAULT_DISCORD_MAX_ATTEMPTS
  const wait = options.sleep || sleep
  const log = options.log || (() => {})

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await postToDiscordOnce(token, channelId, message)
    } catch (error) {
      if (error.statusCode !== 429 || attempt >= maxAttempts) throw error
      const res = { headers: error.responseHeaders || {}, statusCode: error.statusCode }
      const waitMs = retryAfterMs(res, error.responseBody || '', attempt, options)
      log(`Discord 429 rate limited; retrying attempt ${attempt + 1}/${maxAttempts} after ${waitMs}ms`)
      await wait(waitMs)
    }
  }
}

function tweetLine(tweet) {
  const username = tweet.user?.username || tweet.user?.name || 'unknown'
  let text = (tweet.text || '').replace(/\n/g, ' ').trim()
  if (text.length > MAX_TWEET_TEXT) text = `${text.slice(0, MAX_TWEET_TEXT)}...`
  const likes = tweet.like_count || 0
  const url = tweet.url || ''
  return `• @${username} ❤️${likes} — "${text}" [→](<${url}>)`
}

function buildMessage(tweets) {
  const chunks = buildChunks(tweets)
  return chunks[0]?.text || null
}

function buildChunks(tweets) {
  if (!Array.isArray(tweets) || tweets.length === 0) return []

  const header = '🔔 X Monitor'
  const groups = new Map()
  for (const tweet of tweets) {
    const category = (tweet._monitor_category || 'default').toLowerCase()
    if (!groups.has(category)) groups.set(category, [])
    groups.get(category).push(tweet)
  }

  const chunks = []
  let text = header
  let chunkTweets = []

  const flush = () => {
    if (text) chunks.push({ text, tweets: chunkTweets })
    text = ''
    chunkTweets = []
  }

  const append = (separator, value, tweet) => {
    const candidate = text ? `${text}${separator}${value}` : value
    if (candidate.length > DISCORD_MAX_LEN) return false
    text = candidate
    if (tweet) chunkTweets.push(tweet)
    return true
  }

  for (const [category, categoryTweets] of groups) {
    let categoryOpen = false
    for (const tweet of categoryTweets) {
      const line = tweetLine(tweet)
      if (!categoryOpen) {
        const firstLine = `${category}\n${line}`
        if (append('\n\n', firstLine, tweet)) {
          categoryOpen = true
          continue
        }
        flush()
        text = firstLine
        chunkTweets = [tweet]
        categoryOpen = true
        continue
      }

      if (append('\n', line, tweet)) continue
      flush()
      text = `${category}\n${line}`
      chunkTweets = [tweet]
      categoryOpen = true
    }
  }

  flush()
  return chunks
}

async function postPendingAlerts({ token = getDiscordToken(), channelId = getChannelId(), pendingFile = PENDING_ALERTS_FILE, post = postToDiscord, acquire = acquireLock, log = () => {}, errorLog = () => {} } = {}) {
  if (!token) throw new Error('No Discord token found')
  if (!channelId) throw new Error('No Discord channel configured — set discord.alerts_channel in config.json')

  const release = acquire()
  try {
    if (!fs.existsSync(pendingFile)) return { chunksPosted: 0, remaining: [], reason: 'missing-pending-file' }

    let tweets
    try {
      tweets = JSON.parse(fs.readFileSync(pendingFile, 'utf8'))
      if (!Array.isArray(tweets)) tweets = []
    } catch (e) {
      errorLog(`Failed to parse ${path.basename(pendingFile)}: ${e.message}`)
      throw e
    }
    log(`Pending alerts: ${tweets.length}`)
    if (tweets.length === 0) return { chunksPosted: 0, remaining: [], reason: 'empty-pending-file' }

    const chunks = buildChunks(tweets)
    log(`Built ${chunks.length} Discord chunk(s) from ${tweets.length} tweet(s)`)
    let remaining = [...tweets]
    let chunksPosted = 0

    const postChunk = post === postToDiscord
      ? (postToken, postChannelId, text) => post(postToken, postChannelId, text, { log })
      : post

    for (let index = 0; index < chunks.length; index++) {
      const { text, tweets: chunkTweets } = chunks[index]
      log(`Posting chunk ${index + 1}/${chunks.length} (${text.length} chars, ${chunkTweets.length} tweet(s))`)
      try {
        await postChunk(token, channelId, text)
      } catch (e) {
        errorLog(`Discord post failed on chunk ${index + 1}/${chunks.length}: ${e.message}`)
        errorLog(`pending_alerts.json preserved with ${remaining.length} tweet(s) — fix the error and retry`)
        throw e
      }
      const sentUrls = new Set(chunkTweets.map(tweet => tweet.url))
      remaining = remaining.filter(tweet => !sentUrls.has(tweet.url))
      atomicWriteJson(pendingFile, remaining)
      chunksPosted += 1
      log(`  chunk ${index + 1} OK — ${remaining.length} alert(s) still pending`)
    }

    return { chunksPosted, remaining }
  } finally {
    release()
  }
}

async function main() {
  const token = getDiscordToken()
  if (!token) {
    console.error('No Discord token found')
    process.exit(1)
  }

  const channelId = getChannelId()
  if (!channelId) {
    console.error('ERROR: No Discord channel configured — set discord.alerts_channel in config.json')
    process.exit(1)
  }
  console.log(`Discord channel: ${channelId}`)

  try {
    const result = await postPendingAlerts({ token, channelId, log: console.log, errorLog: console.error })
    if (result.reason === 'missing-pending-file') {
      console.log('pending_alerts.json not found — nothing to post')
      return
    }
    if (result.reason === 'empty-pending-file') {
      console.log('No pending alerts — silent exit')
      return
    }
    console.log(`Done: ${result.chunksPosted} message(s) posted, pending_alerts.json cleared`)
  } catch {
    process.exit(1)
  }
}

module.exports = { buildMessage, buildChunks, tweetLine, getChannelId, postToDiscord, postPendingAlerts, acquireLock, atomicWriteJson }

if (require.main === module) {
  main().catch(error => {
    console.error(error)
    process.exit(1)
  })
}
