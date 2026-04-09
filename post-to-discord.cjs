#!/usr/bin/env node
/**
 * X Monitor — Direct Discord poster (no LLM overhead)
 * Reads pending_alerts.json, splits into Discord-safe chunks, posts to #x-alerts.
 * Chunks preserve category grouping; pending_alerts.json is cleared only after all chunks succeed.
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
const CONFIG_FILE = path.join(MONITOR_DIR, 'config.json')
const MAX_TWEET_TEXT = 100
const DISCORD_MAX_LEN = 2000

// Read Discord token from DISCORD_BOT_TOKEN env var or openclaw.json (local fallback)
function getDiscordToken() {
  if (process.env.DISCORD_BOT_TOKEN) return process.env.DISCORD_BOT_TOKEN
  // Local OpenClaw fallback (not needed when using env var)
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
 * - pending_alerts.json is only cleared after all chunks are confirmed sent.
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

  // Append sep+text to buf if it fits; return true on success, false otherwise.
  function tryAppend(sep, text) {
    const candidate = buf === '' ? text : buf + sep + text
    if (candidate.length <= MAX) {
      buf = candidate
      return true
    }
    return false
  }

  function flush() {
    if (buf) chunks.push(buf)
    buf = ''
  }

  for (const [cat, catTweets] of groups) {
    let catHeaderInBuf = false

    for (const tweet of catTweets) {
      const line = tweetLine(tweet)

      if (!catHeaderInBuf) {
        // First time we encounter this category in the current chunk.
        // Try to add "catName\nline" as a block (joined to current content with \n\n).
        const catLine = `${cat}\n${line}`
        if (tryAppend('\n\n', catLine)) {
          catHeaderInBuf = true
        } else {
          // Doesn't fit — flush and open a new chunk with this category block.
          flush()
          buf = catLine
          catHeaderInBuf = true
        }
      } else {
        // Category header is already in the current chunk; add the line with \n.
        if (!tryAppend('\n', line)) {
          // Doesn't fit — flush and continue the category in a new chunk.
          flush()
          buf = `${cat}\n${line}`
          // catHeaderInBuf stays true: the category header is re-added above.
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

  // Read pending_alerts.json written by monitor.py
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

  const chunks = buildChunks(tweets)
  console.log(`Built ${chunks.length} Discord chunk(s) from ${tweets.length} tweet(s)`)

  for (let i = 0; i < chunks.length; i++) {
    const chunk = chunks[i]
    console.log(`Posting chunk ${i + 1}/${chunks.length} (${chunk.length} chars)`)
    try {
      await postToDiscord(token, channelId, chunk)
      console.log(`  chunk ${i + 1} OK`)
    } catch (e) {
      console.error(`Discord post failed on chunk ${i + 1}/${chunks.length}: ${e.message}`)
      console.error('pending_alerts.json preserved (not cleared) — fix the error and retry')
      process.exit(1)
    }
  }

  // All chunks confirmed sent — safe to clear
  fs.writeFileSync(PENDING_ALERTS_FILE, '[]', 'utf8')
  console.log(`Done: ${chunks.length} message(s) posted, pending_alerts.json cleared`)
}

module.exports = {
  buildChunks,
  tweetLine,
  getChannelId,
}

if (require.main === module) {
  main().catch(e => { console.error(e); process.exit(1) })
}
