#!/usr/bin/env node
const https = require('https')
const path = require('path')
const fs = require('fs')

const MONITOR_DIR = path.dirname(__filename)
const CONFIG_FILE = path.join(MONITOR_DIR, 'config.json')
const PENDING_ALERTS_FILE = path.join(MONITOR_DIR, 'pending_alerts.json')
const PENDING_ALERTS_LOCK_DIR = path.join(MONITOR_DIR, '.pending-alerts.lock')
const DISCORD_MAX_LEN = 2000
const MAX_TWEET_TEXT = 100

function atomicWriteJson(filePath, data) {
  const tmpPath = path.join(path.dirname(filePath), `.${path.basename(filePath)}.${process.pid}.tmp`)
  fs.writeFileSync(tmpPath, JSON.stringify(data, null, 2))
  fs.renameSync(tmpPath, filePath)
}

function acquireLock(lockDir = PENDING_ALERTS_LOCK_DIR) {
  try {
    fs.mkdirSync(lockDir)
    fs.writeFileSync(path.join(lockDir, 'pid'), String(process.pid), 'utf8')
    return () => fs.rmSync(lockDir, { recursive: true, force: true })
  } catch (error) {
    if (error && error.code === 'EEXIST') {
      throw new Error(`post-to-discord already running, lock held at ${lockDir}`)
    }
    throw error
  }
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

function postToDiscord(token, channelId, message) {
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
          let hint = ''
          if (res.statusCode === 400 && data.includes('BASE_TYPE_MAX_LENGTH')) hint = ' (payload too large, split messages below 2000 chars)'
          else if (res.statusCode === 401) hint = ' (bad token, check DISCORD_BOT_TOKEN or openclaw.json)'
          else if (res.statusCode === 403) hint = ' (bot lacks Send Messages permission in this channel)'
          else if (res.statusCode === 404) hint = ' (channel not found, check discord.alerts_channel in config.json)'
          else if (res.statusCode === 413) hint = ' (payload too large)'
          reject(new Error(`Discord HTTP ${res.statusCode}${hint}: ${data}`))
          return
        }
        resolve(JSON.parse(data))
      })
    })

    req.on('error', reject)
    req.write(body)
    req.end()
  })
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

async function postPendingAlerts({ token = getDiscordToken(), channelId = getChannelId(), pendingFile = PENDING_ALERTS_FILE, post = postToDiscord, acquire = acquireLock } = {}) {
  if (!token) throw new Error('No Discord token found')
  if (!channelId) throw new Error('No Discord channel configured — set discord.alerts_channel in config.json')

  const release = acquire()
  try {
    if (!fs.existsSync(pendingFile)) return { chunksPosted: 0, remaining: [], reason: 'missing-pending-file' }

    let tweets = JSON.parse(fs.readFileSync(pendingFile, 'utf8'))
    if (!Array.isArray(tweets)) tweets = []
    if (tweets.length === 0) return { chunksPosted: 0, remaining: [], reason: 'empty-pending-file' }

    const chunks = buildChunks(tweets)
    let remaining = [...tweets]

    for (let index = 0; index < chunks.length; index++) {
      const { text, tweets: chunkTweets } = chunks[index]
      await post(token, channelId, text)
      const sentUrls = new Set(chunkTweets.map(tweet => tweet.url))
      remaining = remaining.filter(tweet => !sentUrls.has(tweet.url))
      atomicWriteJson(pendingFile, remaining)
    }

    return { chunksPosted: chunks.length, remaining }
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

  const release = acquireLock()
  try {
    if (!fs.existsSync(PENDING_ALERTS_FILE)) {
      console.log('pending_alerts.json not found — nothing to post')
      process.exit(0)
    }

    let tweets
    try {
      tweets = JSON.parse(fs.readFileSync(PENDING_ALERTS_FILE, 'utf8'))
      if (!Array.isArray(tweets)) tweets = []
    } catch (e) {
      console.error('Failed to parse pending_alerts.json:', e.message)
      process.exit(1)
    }

    console.log(`Pending alerts: ${tweets.length}`)
    if (tweets.length === 0) {
      console.log('No pending alerts — silent exit')
      process.exit(0)
    }

    const chunks = buildChunks(tweets)
    console.log(`Built ${chunks.length} Discord chunk(s) from ${tweets.length} tweet(s)`)

    let remaining = [...tweets]
    for (let index = 0; index < chunks.length; index++) {
      const { text, tweets: chunkTweets } = chunks[index]
      console.log(`Posting chunk ${index + 1}/${chunks.length} (${text.length} chars, ${chunkTweets.length} tweet(s))`)
      try {
        await postToDiscord(token, channelId, text)
        const sentUrls = new Set(chunkTweets.map(tweet => tweet.url))
        remaining = remaining.filter(tweet => !sentUrls.has(tweet.url))
        atomicWriteJson(PENDING_ALERTS_FILE, remaining)
        console.log(`  chunk ${index + 1} OK — ${remaining.length} alert(s) still pending`)
      } catch (e) {
        console.error(`Discord post failed on chunk ${index + 1}/${chunks.length}: ${e.message}`)
        console.error(`pending_alerts.json preserved with ${remaining.length} tweet(s) — fix the error and retry`)
        process.exit(1)
      }
    }

    console.log(`Done: ${chunks.length} message(s) posted, pending_alerts.json cleared`)
  } finally {
    release()
  }
}

module.exports = { buildMessage, buildChunks, tweetLine, getChannelId, postToDiscord, postPendingAlerts, acquireLock, atomicWriteJson }

if (require.main === module) {
  main().catch(error => {
    console.error(error)
    process.exit(1)
  })
}
