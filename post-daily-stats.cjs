#!/usr/bin/env node
/**
 * X Monitor — Daily Stats direct poster (no LLM overhead)
 * Runtime: ~3s vs 600s+ with LLM agent
 */
const { execSync } = require('child_process')
const https = require('https')
const fs = require('fs')
const path = require('path')

const CONFIG_FILE = path.join(__dirname, 'config.json')

function getChannelId() {
  const cfg = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'))
  return String(cfg.discord?.alerts_channel || '')
}
const HOURS = process.argv[2] || '24'
const SCRIPT_DIR = path.dirname(__filename)

function getDiscordToken() {
  const cfg = JSON.parse(fs.readFileSync('/Users/giga/.openclaw/openclaw.json', 'utf8'))
  return cfg.channels?.discord?.token
}

function postToDiscord(token, channelId, message) {
  return new Promise((resolve, reject) => {
    // Discord max message length is 2000 chars
    const truncated = message.length > 1990 ? message.slice(0, 1987) + '...' : message
    const body = JSON.stringify({ content: truncated })
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
        else resolve()
      })
    })
    req.on('error', reject)
    req.write(body)
    req.end()
  })
}

async function main() {
  const token = getDiscordToken()
  if (!token) { console.error('No Discord token'); process.exit(1) }

  let output
  try {
    output = execSync(
      `cd ${SCRIPT_DIR} && python3 daily_stats.py --hours ${HOURS} 2>/dev/null`,
      { timeout: 30000, encoding: 'utf8' }
    ).trim()
  } catch (e) {
    console.error('daily_stats.py failed:', e.message)
    process.exit(1)
  }

  if (!output || output.includes('No tweets')) {
    console.log('No stats to post')
    process.exit(0)
  }

  const channelId = getChannelId()
  if (!channelId) { console.error('No Discord channel configured'); process.exit(1) }

  await postToDiscord(token, channelId, output)
  console.log('Posted daily stats')
}

main().catch(e => { console.error(e); process.exit(1) })
