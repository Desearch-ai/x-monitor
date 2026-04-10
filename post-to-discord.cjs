#!/usr/bin/env node
/**
 * X Monitor — Direct Discord poster (no LLM overhead)
 * Reads pending_alerts.json written by monitor.py, splits into Discord-safe chunks,
 * and posts each chunk. Queue is updated incrementally: after each chunk posts
 * successfully those tweets are removed from pending_alerts.json immediately.
 * If a later chunk fails, earlier tweets are already durably removed so retries
 * never re-send them.
 *
 * Format per chunk:
 *   🔔 X Monitor
 *
 *   brand
 *   • @user1 ❤️6 — "tweet text..." [→](<url>)
 *
 *   bittensor
 *   • @user3 ❤️5 — "tweet text..." [→](<url>)
 */

'use strict'

const https = require('https')
const path = require('path')
const fs = require('fs')

const MONITOR_DIR = path.dirname(__filename)
const PENDING_ALERTS_FILE = path.join(MONITOR_DIR, 'pending_alerts.json')
const CONFIG_FILE = path.join(MONITOR_DIR, 'config.json')
const MAX_TWEET_TEXT = 100
const DISCORD_SAFE_LIMIT = 1900  // conservative — Discord hard limit is 2000

// Read channel ID from config.discord.alerts_channel
function getDiscordChannel () {
  try {
    const config = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'))
    return (config.discord && config.discord.alerts_channel) || null
  } catch { return null }
}

// Read Discord token from DISCORD_BOT_TOKEN env var or openclaw.json (local fallback)
function getDiscordToken () {
  if (process.env.DISCORD_BOT_TOKEN) return process.env.DISCORD_BOT_TOKEN
  try {
    const home = process.env.HOME || process.env.USERPROFILE || ''
    const cfg = JSON.parse(fs.readFileSync(path.join(home, '.openclaw/openclaw.json'), 'utf8'))
    return (cfg.channels && cfg.channels.discord && cfg.channels.discord.token) || null
  } catch { return null }
}

/**
 * Post a single message to Discord. Rejects with a descriptive error on failure,
 * including status code, issue classification, channel id, content length, and
 * the parsed Discord error body.
 */
function postToDiscord (token, channelId, message) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ content: message })
    const contentLength = Buffer.byteLength(body)
    const opts = {
      hostname: 'discord.com',
      path: `/api/v10/channels/${channelId}/messages`,
      method: 'POST',
      headers: {
        'Authorization': `Bot ${token}`,
        'Content-Type': 'application/json',
        'Content-Length': contentLength
      }
    }
    const req = https.request(opts, res => {
      let data = ''
      res.on('data', d => { data += d })
      res.on('end', () => {
        if (res.statusCode >= 400) {
          let discordBody = data
          try { discordBody = JSON.stringify(JSON.parse(data)) } catch {}
          let issue = 'discord-api-error'
          if (res.statusCode === 400 && contentLength > 2000) issue = 'payload-too-large'
          else if (res.statusCode === 401) issue = 'invalid-token'
          else if (res.statusCode === 403) issue = 'missing-channel-permission'
          reject(new Error(
            `Discord ${res.statusCode} [${issue}] channel=${channelId} contentLength=${contentLength}: ${discordBody}`
          ))
        } else {
          try { resolve(JSON.parse(data)) } catch { resolve({}) }
        }
      })
    })
    req.on('error', reject)
    req.write(body)
    req.end()
  })
}

/**
 * Get a stable string identity for a tweet (id > url > text fingerprint).
 * Used to match tweets when removing them from pending_alerts.json.
 */
function tweetId (t) {
  return String(
    t.id || t.id_str || t.url ||
    JSON.stringify({ u: (t.user && t.user.username) || '', s: (t.text || '').slice(0, 50) })
  )
}

/**
 * Read pending_alerts.json. Returns [] on missing or invalid file.
 */
function readPendingAlerts (pendingFile) {
  try {
    const raw = fs.readFileSync(pendingFile, 'utf8')
    const data = JSON.parse(raw)
    return Array.isArray(data) ? data : []
  } catch { return [] }
}

/**
 * Build Discord-safe message chunks from a tweet array.
 *
 * Tweets are iterated in order; a new category header is inserted whenever
 * _monitor_category changes. When adding the next tweet would push the chunk
 * over `limit` (default DISCORD_SAFE_LIMIT) chars, the current chunk is
 * finalised and a new one begins.
 *
 * Returns an array of {text, tweets} pairs:
 *   text  — the formatted Discord message string (≤ limit chars)
 *   tweets — the subset of input tweets included in that message
 *
 * @param {object[]} tweets
 * @param {number}   [limit]
 * @returns {{text: string, tweets: object[]}[]}
 */
function buildChunks (tweets, limit) {
  if (!tweets || tweets.length === 0) return []
  const safeLimit = limit || DISCORD_SAFE_LIMIT

  const HEADER = '🔔 X Monitor\n\n'

  const chunks = []
  let chunkTweets = []
  let chunkText = HEADER
  let lastCat = null

  for (const tweet of tweets) {
    const username = (tweet.user && (tweet.user.username || tweet.user.name)) || 'unknown'
    let text = (tweet.text || '').replace(/\n/g, ' ').trim()
    if (text.length > MAX_TWEET_TEXT) text = text.slice(0, MAX_TWEET_TEXT) + '...'
    const likes = tweet.like_count || 0
    const url = tweet.url || ''
    const cat = ((tweet._monitor_category || 'default')).toLowerCase()

    // Category separator: blank line before a new section (except the very first)
    const catHeader = cat !== lastCat
      ? (lastCat !== null ? '\n' : '') + `${cat}\n`
      : ''
    const tweetLine = `• @${username} ❤️${likes} — "${text}" [→](<${url}>)\n`
    const addition = catHeader + tweetLine

    if (chunkText.length + addition.length > safeLimit && chunkTweets.length > 0) {
      // Finalise current chunk and start a new one
      chunks.push({ text: chunkText.trimEnd(), tweets: chunkTweets })
      chunkTweets = [tweet]
      chunkText = HEADER + `${cat}\n` + tweetLine
      lastCat = cat
    } else {
      chunkText += addition
      chunkTweets.push(tweet)
      lastCat = cat
    }
  }

  if (chunkTweets.length > 0) {
    chunks.push({ text: chunkText.trimEnd(), tweets: chunkTweets })
  }

  return chunks
}

/**
 * Process the pending alert queue: build chunks, post each one, and
 * incrementally remove sent tweets from pendingFile after each success.
 *
 * If a chunk fails, posting stops immediately. Tweets from chunks that
 * already posted have been durably removed; the rest remain in the file
 * so the next run (retry) picks them up without duplication.
 *
 * @param {string}   pendingFile  - absolute path to pending_alerts.json
 * @param {string}   token        - Discord bot token
 * @param {string}   channelId    - Discord channel ID
 * @param {Function} postFn       - async (token, channelId, message) => result
 * @param {object}   [opts]
 * @param {number}   [opts.limit] - override DISCORD_SAFE_LIMIT (for tests)
 * @returns {Promise<{posted: number, remaining: number, failed: boolean}>}
 */
async function processQueue (pendingFile, token, channelId, postFn, opts) {
  const limit = (opts && opts.limit) || DISCORD_SAFE_LIMIT

  const tweets = readPendingAlerts(pendingFile)
  if (tweets.length === 0) {
    console.log('No pending alerts — nothing to post')
    return { posted: 0, remaining: 0, failed: false }
  }
  console.log(`Pending alerts: ${tweets.length}`)

  const chunks = buildChunks(tweets, limit)
  if (chunks.length === 0) {
    console.log('Nothing to post')
    return { posted: 0, remaining: 0, failed: false }
  }
  console.log(`Built ${chunks.length} chunk(s) for Discord`)

  let totalPosted = 0
  let failed = false

  for (let i = 0; i < chunks.length; i++) {
    const chunk = chunks[i]
    try {
      await postFn(token, channelId, chunk.text)
      console.log(`Chunk ${i + 1}/${chunks.length}: posted ${chunk.tweets.length} tweet(s)`)
      totalPosted += chunk.tweets.length

      // Incrementally remove only the tweets in this chunk from the file.
      // Re-read the file so we work with current state (not a stale snapshot).
      const sentIds = new Set(chunk.tweets.map(tweetId))
      const current = readPendingAlerts(pendingFile)
      const updated = current.filter(t => !sentIds.has(tweetId(t)))
      fs.writeFileSync(pendingFile, JSON.stringify(updated, null, 2), 'utf8')
      console.log(
        `  Durably removed ${chunk.tweets.length} tweet(s) — ${updated.length} remaining in queue`
      )
    } catch (e) {
      console.error(`Chunk ${i + 1}/${chunks.length} failed: ${e.message}`)
      failed = true
      break  // preserve remaining tweets for retry
    }
  }

  const remaining = readPendingAlerts(pendingFile).length
  return { posted: totalPosted, remaining, failed }
}

async function main () {
  const token = getDiscordToken()
  if (!token) {
    console.error('No Discord token found (set DISCORD_BOT_TOKEN or configure ~/.openclaw/openclaw.json)')
    process.exit(1)
  }

  const channelId = getDiscordChannel()
  if (!channelId) {
    console.error('No Discord channel ID — set config.discord.alerts_channel in config.json')
    process.exit(1)
  }

  if (!fs.existsSync(PENDING_ALERTS_FILE)) {
    console.log('pending_alerts.json not found — nothing to post')
    process.exit(0)
  }

  // Validate file is parseable before starting
  try {
    const raw = fs.readFileSync(PENDING_ALERTS_FILE, 'utf8')
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) throw new Error('expected JSON array')
  } catch (e) {
    console.error('Failed to parse pending_alerts.json:', e.message)
    process.exit(1)
  }

  const result = await processQueue(PENDING_ALERTS_FILE, token, channelId, postToDiscord)

  if (result.failed) {
    console.error(
      `Partial failure: ${result.posted} tweet(s) posted, ${result.remaining} still pending`
    )
    process.exit(1)
  }

  console.log(`Done: posted ${result.posted} tweet(s) across all chunks`)
}

if (require.main === module) {
  main().catch(e => { console.error(e); process.exit(1) })
}

module.exports = {
  buildChunks,
  processQueue,
  postToDiscord,
  tweetId,
  readPendingAlerts,
  DISCORD_SAFE_LIMIT
}
