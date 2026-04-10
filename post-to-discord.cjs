#!/usr/bin/env node
/**
 * X Monitor — Direct Discord poster (no LLM overhead)
 * Reads pending_alerts.json, splits into Discord-safe chunks, posts to #x-alerts.
 * Chunks preserve category grouping. pending_alerts.json is only cleared after ALL
 * chunks succeed. Sent tweet URLs are tracked in pending_alerts.sent.json so retries
 * never re-post already-sent chunks without needing to modify the main queue file.
 * Format (first chunk):
 *   🔔 X Monitor
 *
 *   bittensor
 *   • @user1 ❤️6 — "tweet text..." [→](url)
 *
 *   desearch
 *   • @user2 ❤️3 — "tweet text..." [→](url)
 */

const https = require('https')
const path = require('path')
const fs = require('fs')

const MONITOR_DIR = path.dirname(__filename)
const PENDING_ALERTS_FILE = path.join(MONITOR_DIR, 'pending_alerts.json')
const SENT_TRACKING_FILE = path.join(MONITOR_DIR, 'pending_alerts.sent.json')
const CONFIG_FILE = path.join(MONITOR_DIR, 'config.json')
const MAX_TWEET_TEXT = 100
const DISCORD_MAX_LEN = 2000

// Read Discord token from DISCORD_BOT_TOKEN env var or openclaw.json (local fallback)
function getDiscordToken() {
  if (process.env.DISCORD_BOT_TOKEN) return process.env.DISCORD_BOT_TOKEN
  try {
    const home = process.env.HOME || process.env.USERPROFILE || ''
    const cfg = JSON.parse(fs.readFileSync(path.join(home, '.openclaw/openclaw.json'), 'utf8'))
    return cfg.channels?.discord?.token
  } catch { return null }
}

// Read Discord channel ID from config.json discord.alerts_channel
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
        'Authorization': `Bot ${token}`,
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body)
      }
    }
    const req = https.request(opts, res => {
      let data = ''
      res.on('data', d => data += d)
      res.on('end', () => {
        if (res.statusCode >= 400) {
          let hint = ''
          if (res.statusCode === 413) hint = ' (payload too large — message exceeds Discord 2000-char limit)'
          else if (res.statusCode === 401) hint = ' (bad token — check DISCORD_BOT_TOKEN or openclaw.json)'
          else if (res.statusCode === 403) hint = ' (forbidden — bot lacks Send Messages permission in channel)'
          else if (res.statusCode === 404) hint = ' (channel not found — check discord.alerts_channel in config.json)'
          reject(new Error(`Discord HTTP ${res.statusCode}${hint}: ${data}`))
        } else {
          resolve(JSON.parse(data))
        }
      })
    })
    req.on('error', reject)
    req.write(body)
    req.end()
  })
}

/**
 * Format a single tweet as a Discord bullet line.
 */
function tweetLine(t) {
  const username = t.user?.username || t.user?.name || 'unknown'
  let text = (t.text || '').replace(/\n/g, ' ').trim()
  if (text.length > MAX_TWEET_TEXT) text = text.slice(0, MAX_TWEET_TEXT) + '...'
  const likes = t.like_count || 0
  const url = t.url || ''
  return `• @${username} ❤️${likes} — "${text}" [→](<${url}>)`
}

/**
 * Split grouped tweets into Discord-safe message chunks (each <= DISCORD_MAX_LEN chars).
 * - First chunk is prefixed with "🔔 X Monitor".
 * - Category grouping is preserved; large categories split across chunks with the
 *   category header repeated at the start of the continuation chunk.
 * - Returns {text, tweets}[] so main() can track which tweets belong to each chunk.
 */
function buildChunks(tweets) {
  if (!tweets || tweets.length === 0) return []

  const HEADER = '🔔 X Monitor'
  const MAX = DISCORD_MAX_LEN

  // Group by _monitor_category (preserve insertion order)
  const groups = new Map()
  for (const t of tweets) {
    const cat = (t._monitor_category || 'default').toLowerCase()
    if (!groups.has(cat)) groups.set(cat, [])
    groups.get(cat).push(t)
  }

  const chunks = []
  let buf = HEADER  // first chunk always starts with the header
  let chunkTweets = []

  function tryAppend(sep, text, tweet) {
    const candidate = buf === '' ? text : buf + sep + text
    if (candidate.length <= MAX) {
      buf = candidate
      if (tweet) chunkTweets.push(tweet)
      return true
    }
    return false
  }

  function flush() {
    if (buf) chunks.push({ text: buf, tweets: chunkTweets })
    buf = ''
    chunkTweets = []
  }

  for (const [cat, catTweets] of groups) {
    let catHeaderInBuf = false

    for (const tweet of catTweets) {
      const line = tweetLine(tweet)

      if (!catHeaderInBuf) {
        const catLine = `${cat}\n${line}`
        if (tryAppend('\n\n', catLine, tweet)) {
          catHeaderInBuf = true
        } else {
          flush()
          buf = catLine
          chunkTweets = [tweet]
          catHeaderInBuf = true
        }
      } else {
        if (!tryAppend('\n', line, tweet)) {
          flush()
          buf = `${cat}\n${line}`
          chunkTweets = [tweet]
        }
      }
    }
  }

  flush()
  return chunks
}

async function main() {
  const token = getDiscordToken()
  if (!token) {
    console.error('ERROR: No Discord token — set DISCORD_BOT_TOKEN env var or configure ~/.openclaw/openclaw.json')
    process.exit(1)
  }

  const channelId = getChannelId()
  if (!channelId) {
    console.error('ERROR: No Discord channel configured — set discord.alerts_channel in config.json')
    process.exit(1)
  }
  console.log(`Discord channel: ${channelId}`)

  if (!fs.existsSync(PENDING_ALERTS_FILE)) {
    console.log('pending_alerts.json not found — nothing to post')
    process.exit(0)
  }

  let tweets = []
  try {
    const raw = fs.readFileSync(PENDING_ALERTS_FILE, 'utf8')
    tweets = JSON.parse(raw)
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

  // Load sent-tweet tracking file from any previous partial run.
  // pending_alerts.json is never modified mid-run; only the tracking file is updated.
  // On retry, chunk 1 tweets are filtered out via the tracking file — no re-posts.
  let sentUrls = new Set()
  if (fs.existsSync(SENT_TRACKING_FILE)) {
    try {
      const raw = JSON.parse(fs.readFileSync(SENT_TRACKING_FILE, 'utf8'))
      if (Array.isArray(raw)) sentUrls = new Set(raw)
    } catch { /* ignore corrupt tracking file */ }
  }

  const unsentTweets = tweets.filter(t => !sentUrls.has(t.url))
  if (unsentTweets.length < tweets.length) {
    console.log(`Resuming after partial failure — ${tweets.length - unsentTweets.length} tweet(s) already sent, ${unsentTweets.length} remaining`)
  }

  const chunks = buildChunks(unsentTweets)
  console.log(`Built ${chunks.length} Discord chunk(s) from ${unsentTweets.length} tweet(s)`)

  for (let i = 0; i < chunks.length; i++) {
    const { text, tweets: chunkTweets } = chunks[i]
    console.log(`Posting chunk ${i + 1}/${chunks.length} (${text.length} chars, ${chunkTweets.length} tweet(s))`)
    try {
      await postToDiscord(token, channelId, text)
      for (const t of chunkTweets) sentUrls.add(t.url)
      fs.writeFileSync(SENT_TRACKING_FILE, JSON.stringify([...sentUrls]), 'utf8')
      console.log(`  chunk ${i + 1} OK — ${tweets.length - sentUrls.size} alert(s) still pending`)
    } catch (e) {
      console.error(`Discord post failed on chunk ${i + 1}/${chunks.length}: ${e.message}`)
      console.error(`pending_alerts.json preserved — ${tweets.length - sentUrls.size} alert(s) still unsent, retry to continue`)
      process.exit(1)
    }
  }

  // All chunks confirmed sent — clear queue and tracking file
  fs.writeFileSync(PENDING_ALERTS_FILE, '[]', 'utf8')
  if (fs.existsSync(SENT_TRACKING_FILE)) fs.unlinkSync(SENT_TRACKING_FILE)
  console.log(`Done: ${chunks.length} message(s) posted, pending_alerts.json cleared`)
}

module.exports = {
  buildChunks,
  tweetLine,
  getChannelId,
  SENT_TRACKING_FILE,
}

if (require.main === module) {
  main().catch(e => { console.error(e); process.exit(1) })
}
