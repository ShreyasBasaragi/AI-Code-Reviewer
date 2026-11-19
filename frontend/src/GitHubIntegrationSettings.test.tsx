import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { GitHubIntegrationSettings } from './GitHubIntegrationSettings'

;(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const roots: Root[] = []

function response(body: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response
}

function mount() {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  roots.push(root)
  act(() => root.render(<GitHubIntegrationSettings />))
  return container
}

async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
}

afterEach(() => {
  for (const root of roots.splice(0)) act(() => root.unmount())
  vi.unstubAllGlobals()
})

describe('GitHub integration operations', () => {
  it('offers retries only when the backend marks them available and sends admin credentials', async () => {
    const requests: Array<[string, RequestInit | undefined]> = []
    const deliveries = [
      { delivery_id: 'eligible-delivery', repository: 'acme/repo', pr_number: 2, status: 'failed', failure_code: 'github_timeout', attempts: 1, next_attempt_at: null, retry_available: true, actions: [] },
      { delivery_id: 'blocked-delivery', repository: 'acme/repo', pr_number: 3, status: 'dead_letter', failure_code: 'github_rate_limited', attempts: 3, next_attempt_at: null, retry_available: false, actions: [] },
      { delivery_id: 'unknown-delivery', repository: 'acme/repo', pr_number: 4, status: 'completed', failure_code: null, attempts: 1, next_attempt_at: null, retry_available: false, actions: [{
        action_id: 'unknown-action', action_type: 'review_comment', publication_status: 'unknown', attempts: 1,
        failure_code: 'github_timeout', retry_available: false,
      }] },
    ]
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      requests.push([url, init])
      if (url.endsWith('/api/admin/github/status')) return response({ github_token_configured: false, github_actions_token_configured: false, webhook_secret_configured: false, persistent_storage_available: false, storage_mode: 'memory' })
      if (url.endsWith('/api/admin/github/webhook')) return response({ endpoint_url: 'http://127.0.0.1:8000/api/github/webhook', secret_configured: false, required_events: ['pull_request'], public_reachability: 'localhost_only', last_authenticated_delivery: null, last_successful_delivery: null, recent_deliveries: [], delivery_authentication: 'HMAC SHA-256 signature required' })
      if (url.endsWith('/api/admin/webhook/deliveries?limit=25')) return response({ deliveries })
      if (url.endsWith('/api/admin/webhook/deliveries/eligible-delivery/retry')) return response({ delivery_id: 'eligible-delivery', status: 'pending', next_attempt_at: 100, reason: 'retry_scheduled' }, 202)
      throw new Error(`Unexpected request ${url}`)
    }))
    const container = mount()
    await flush()

    expect(container.textContent).toContain('dead_letter')
    expect(container.textContent).toContain('unknown')
    expect(container.textContent).toContain('Reconcile remote state before any retry.')
    const retryButtons = [...container.querySelectorAll('button')].filter((item) => item.textContent?.includes('Schedule delivery retry'))
    expect(retryButtons).toHaveLength(1)

    await act(async () => {
      retryButtons[0].dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve(); await Promise.resolve()
    })
    const retryRequest = requests.find(([url]) => url.endsWith('/api/admin/webhook/deliveries/eligible-delivery/retry'))
    expect(retryRequest?.[1]?.method).toBe('POST')
    expect(retryRequest?.[1]?.credentials).toBe('include')
  })
})
