#!/usr/bin/env node
/**
 * X Monitor — Daily Stats direct poster (no LLM overhead)
 * Runtime: ~3s vs 600s+ with LLM agent
 */
const { execSync } = require('child_process')
const https = require('https')
const fs = require('fs')
const path = require('path')

const HOURS = process.argv[2] || '24'
const SCRIPT_DIR = path.dirname(__filename)
const CONFIG_FILE = path.join(SCRIPT_DIR, 'config.json')

function getDiscordToken() {
  if (process.env.DISCORD_BOT_TOKEN) return process.env.DISCORD_BOT_TOKEN
  try {
    const home = process.env.HOME || process.env.USERPROFILE || ''
    const cfg = JSON.parse(fs.readFileSync(path.join(home, '.openclaw/openclaw.json'), 'utf8'))
    return cfg.channels?.discord?.token
  } catch { return null }
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
        if (res.statusCode >= 400) {
          let hint = ''
          if (res.statusCode === 401) hint = ' (bad token — check DISCORD_BOT_TOKEN or openclaw.json)'
          else if (res.statusCode === 403) hint = ' (forbidden — bot lacks Send Messages permission)'
          else if (res.statusCode === 404) hint = ' (channel not found — check discord.alerts_channel in config.json)'
          reject(new Error(`Discord HTTP ${res.statusCode}${hint}: ${data}`))
        } else resolve()
      })
    })
    req.on('error', reject)
    req.write(body)
    req.end()
  })
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

  await postToDiscord(token, channelId, output)
  console.log('Posted daily stats')
}

main().catch(e => { console.error(e); process.exit(1) })
