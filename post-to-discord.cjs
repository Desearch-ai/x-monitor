#!/usr/bin/env node
/**
 * X Monitor — Direct Discord poster (no LLM overhead)
 * Runs monitor.py, parses output, posts new tweets to Discord #x-alerts
 * Runtime: ~10-30s vs 180s+ with LLM agent
 */

const { execSync } = require('child_process')
const https = require('https')
const path = require('path')
const fs = require('fs')

const DISCORD_CHANNEL = '1477727527618347340'
const MONITOR_DIR = path.dirname(require.resolve('./monitor.py') || __filename)

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

// Category emoji map
const CATEGORY_EMOJI = {
  desearch: '🔍', brand: '🔍',
  bittensor: '🦾',
  competitor: '🏆',
  influencer: '🤝',
  system: '⚙️',
  builder: '🏗️',
  community: '👥',
  ai: '🤖',
  subnet: '#️⃣',
  keyword: '#️⃣',
  default: '📌'
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

function sleep(ms) { return new Promise(r => setTimeout(r, ms)) }

async function main() {
  const token = getDiscordToken()
  if (!token) { console.error('No Discord token found'); process.exit(1) }

  // Run monitor.py
  console.log('Running monitor.py...')
  let monitorOutput
  try {
    monitorOutput = execSync(
      `cd ${MONITOR_DIR} && python3 monitor.py 2>/dev/null`,
      { timeout: 120000, encoding: 'utf8' }
    )
  } catch (e) {
    console.error('monitor.py failed:', e.message)
    await postToDiscord(token, DISCORD_CHANNEL, `⚠️ X Monitor error: ${e.message.slice(0, 200)}`)
    process.exit(1)
  }

  let data
  try { data = JSON.parse(monitorOutput) }
  catch { console.error('monitor.py output not JSON'); process.exit(1) }

  const tweets = data.new_tweets || []
  console.log(`New tweets: ${tweets.length}`)

  if (tweets.length === 0) {
    console.log('No new tweets — silent exit')
    process.exit(0)
  }

  // Batch tweets into groups of 5 per message
  const BATCH_SIZE = 5
  for (let i = 0; i < tweets.length; i += BATCH_SIZE) {
    const batch = tweets.slice(i, i + BATCH_SIZE)
    const lines = batch.map(t => {
      const cat = (t._monitor_category || 'default').toLowerCase()
      const emoji = CATEGORY_EMOJI[cat] || CATEGORY_EMOJI.default
      const username = t.user?.username || t.user?.name || 'unknown'
      const text = (t.text || '').slice(0, 200).replace(/\n/g, ' ')
      const likes = t.like_count || 0
      const rts = t.retweet_count || 0
      const url = t.url || ''
      const context = t._monitor_context || ''

      let msg = `🔔 **X Monitor** | ${emoji} ${cat}\n@${username} · ❤️${likes} 🔄${rts}\n"${text}"\n🔗 <${url}>`
      if (context) msg += `\n_${context}_`
      return msg
    })

    const message = lines.join('\n\n')
    try {
      await postToDiscord(token, DISCORD_CHANNEL, message)
      console.log(`Posted batch ${Math.floor(i/BATCH_SIZE)+1}`)
      if (i + BATCH_SIZE < tweets.length) await sleep(1000) // rate limit
    } catch (e) {
      console.error('Discord post failed:', e.message)
    }
  }

  console.log(`Done: ${tweets.length} tweets posted`)
}

main().catch(e => { console.error(e); process.exit(1) })
