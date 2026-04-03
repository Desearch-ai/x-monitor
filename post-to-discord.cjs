#!/usr/bin/env node
/**
 * X Monitor — Direct Discord poster (no LLM overhead)
 * Runs monitor.py, parses output, posts ONE grouped message to Discord #x-alerts
 * Format:
 *   🔔 X Monitor
 *
 *   brand
 *   • @user1 ❤️6 — "tweet text..." [→](url)
 *
 *   bittensor
 *   • @user3 ❤️5 — "tweet text..." [→](url)
 */

const https = require('https')
const path = require('path')
const fs = require('fs')

const DISCORD_CHANNEL = '1477727527618347340'
const MONITOR_DIR = path.dirname(__filename)
const PENDING_ALERTS_FILE = path.join(MONITOR_DIR, 'pending_alerts.json')
const MAX_TWEET_TEXT = 100

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
        if (res.statusCode >= 400) reject(new Error(`Discord ${res.statusCode}: ${data}`))
        else resolve(JSON.parse(data))
      })
    })
    req.on('error', reject)
    req.write(body)
    req.end()
  })
}

/**
 * Build a single Discord message for all tweets, grouped by category.
 * Returns null if there are no tweets.
 */
function buildMessage(tweets) {
  if (!tweets || tweets.length === 0) return null

  // Group by _monitor_category (preserve insertion order)
  const groups = new Map()
  for (const t of tweets) {
    const cat = (t._monitor_category || 'default').toLowerCase()
    if (!groups.has(cat)) groups.set(cat, [])
    groups.get(cat).push(t)
  }

  const sections = []
  for (const [cat, catTweets] of groups) {
    const lines = catTweets.map(t => {
      const username = t.user?.username || t.user?.name || 'unknown'
      let text = (t.text || '').replace(/\n/g, ' ').trim()
      if (text.length > MAX_TWEET_TEXT) text = text.slice(0, MAX_TWEET_TEXT) + '...'
      const likes = t.like_count || 0
      const url = t.url || ''
      return `• @${username} ❤️${likes} — "${text}" [→](${url})`
    })
    sections.push(`${cat}\n${lines.join('\n')}`)
  }

  return `🔔 X Monitor\n\n${sections.join('\n\n')}`
}

async function main() {
  const token = getDiscordToken()
  if (!token) { console.error('No Discord token found'); process.exit(1) }

  // Read pending_alerts.json written by monitor.py
  let tweets = []
  if (!fs.existsSync(PENDING_ALERTS_FILE)) {
    console.log('pending_alerts.json not found — nothing to post')
    process.exit(0)
  }

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

  const message = buildMessage(tweets)
  if (!message) {
    console.log('Nothing to post')
    process.exit(0)
  }

  try {
    await postToDiscord(token, DISCORD_CHANNEL, message)
    console.log(`Done: posted 1 message covering ${tweets.length} tweet(s)`)
    // Clear pending alerts after successful post
    fs.writeFileSync(PENDING_ALERTS_FILE, '[]', 'utf8')
    console.log('Cleared pending_alerts.json')
  } catch (e) {
    console.error('Discord post failed:', e.message)
    process.exit(1)
  }
}

main().catch(e => { console.error(e); process.exit(1) })
