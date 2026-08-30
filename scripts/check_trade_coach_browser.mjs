import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import net from 'node:net'

const DEFAULT_URL = 'http://127.0.0.1:5173/'

function argument(name, fallback) {
  const index = process.argv.indexOf(name)
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback
}

function browserExecutable() {
  const configured = process.env.TRADE_COACH_BROWSER
  if (configured) return configured
  const candidates = [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  ]
  return candidates.find((path) => existsSync(path))
}

async function freePort() {
  const server = net.createServer()
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  const port = server.address().port
  await new Promise((resolve) => server.close(resolve))
  return port
}

async function waitForJson(url, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs
  let lastError = null
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url)
      if (response.ok) return await response.json()
    } catch (error) {
      lastError = error
    }
    await sleep(100)
  }
  throw new Error(`DevTools endpoint did not become ready: ${lastError?.message || url}`)
}

class DevToolsSession {
  constructor(url) {
    this.url = url
    this.nextId = 1
    this.pending = new Map()
    this.events = []
    this.socket = null
  }

  async connect() {
    this.socket = new WebSocket(this.url)
    await new Promise((resolve, reject) => {
      this.socket.addEventListener('open', resolve, { once: true })
      this.socket.addEventListener('error', reject, { once: true })
    })
    this.socket.addEventListener('message', (event) => {
      const message = JSON.parse(String(event.data))
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id)
        this.pending.delete(message.id)
        if (message.error) reject(new Error(message.error.message || 'DevTools command failed'))
        else resolve(message.result)
      } else {
        this.events.push(message)
      }
    })
  }

  command(method, params = {}) {
    const id = this.nextId++
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.socket.send(JSON.stringify({ id, method, params }))
    })
  }

  close() {
    if (this.socket && this.socket.readyState < 2) this.socket.close()
  }
}

function readRuntimeErrors(events) {
  const errors = []
  for (const event of events) {
    if (event.method === 'Runtime.exceptionThrown') {
      errors.push(event.params.exceptionDetails?.text || event.params.exceptionDetails?.exception?.description || 'runtime exception')
    }
    if (event.method === 'Runtime.consoleAPICalled' && event.params.type === 'error') {
      errors.push(event.params.args?.map((arg) => arg.value ?? arg.description ?? arg.type).join(' ') || 'console.error')
    }
  }
  return errors
}

async function main() {
  const targetUrl = argument('--url', DEFAULT_URL)
  const executable = browserExecutable()
  if (!executable) throw new Error('No Edge or Chrome executable found; set TRADE_COACH_BROWSER explicitly.')
  if (typeof WebSocket !== 'function') throw new Error('This Node runtime does not provide WebSocket for DevTools.')

  const debugPort = await freePort()
  const profile = mkdtempSync(join(tmpdir(), 'trade-coach-browser-'))
  const browser = spawn(executable, [
    '--headless=new',
    '--disable-gpu',
    '--no-sandbox',
    '--no-first-run',
    '--no-default-browser-check',
    `--user-data-dir=${profile}`,
    `--remote-debugging-port=${debugPort}`,
    '--remote-allow-origins=*',
    'about:blank',
  ], { stdio: 'ignore', windowsHide: true })
  let session = null
  try {
    const targets = await waitForJson(`http://127.0.0.1:${debugPort}/json/list`)
    const page = targets.find((item) => item.type === 'page' && item.webSocketDebuggerUrl)
    if (!page) throw new Error('No DevTools page target was created.')
    session = new DevToolsSession(page.webSocketDebuggerUrl)
    await session.connect()
    await session.command('Runtime.enable')
    await session.command('Page.enable')
    await session.command('Page.navigate', { url: targetUrl })

    const deadline = Date.now() + 20000
    let rendered = null
    while (Date.now() < deadline) {
      const result = await session.command('Runtime.evaluate', {
        expression: `(() => {
          const root = document.querySelector('#root')
          const text = root?.innerText || ''
          return {
            ready: document.readyState === 'complete',
            rootTextLength: text.trim().length,
            keyTitle: /个人交易导师|PERSONAL TRADE COACH|总览|先看事实/.test(text),
            title: document.title,
          }
        })()`,
        returnByValue: true,
        awaitPromise: true,
      })
      rendered = result.result?.value || null
      if (rendered?.ready && rendered.rootTextLength > 0 && rendered.keyTitle) break
      await sleep(200)
    }
    if (!rendered?.ready || rendered.rootTextLength <= 0) throw new Error('Rendered #root is empty.')
    if (!rendered.keyTitle) throw new Error('Rendered page has no expected Chinese/Product title.')

    const api = await session.command('Runtime.evaluate', {
      expression: `fetch('/api/trade-coach/summary').then((response) => response.status)`,
      returnByValue: true,
      awaitPromise: true,
    })
    const apiStatus = api.result?.value
    if (apiStatus !== 200) throw new Error(`Same-origin API proxy returned HTTP ${apiStatus}.`)

    const errors = readRuntimeErrors(session.events)
    if (errors.length) throw new Error(`Browser console/runtime errors: ${errors.join(' | ')}`)
    console.log(JSON.stringify({
      url: targetUrl,
      browser: executable,
      page_title: rendered.title,
      root_text_length: rendered.rootTextLength,
      expected_title_present: rendered.keyTitle,
      api_proxy_status: apiStatus,
      console_errors: 0,
    }))
  } finally {
    session?.close()
    if (process.platform === 'win32' && browser.pid) {
      spawnSync('taskkill.exe', ['/PID', String(browser.pid), '/T', '/F'], { stdio: 'ignore' })
    }
    browser.kill()
    // Chromium may release profile locks slightly after the process exits.
    // The profile is disposable test state, so cleanup retries must never
    // turn a successful DOM/console assertion into a false failure.
    await sleep(500)
    try {
      rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 })
    } catch {
      // A later OS cleanup can remove a lock-held disposable profile.
    }
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error))
  process.exitCode = 1
})
