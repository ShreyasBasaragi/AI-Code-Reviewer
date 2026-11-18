import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'

;(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const roots: Root[] = []
const reviewId = 'review-local-123'
const candidateId = 'fix-local-456'
const source = 'def calculate():\n    return 1\n'
const corrected = 'def calculate():\n    return 2\n'

const findings = [
  {
    finding_id: 'finding-1', title: 'Unexpected return value', issue_type: 'bug',
    explanation: 'The function returns a value that does not match its stated result.',
    recommended_fix: 'Return the expected value.', affected_file: null, line_start: 2, line_end: 2,
    corrected_code: null, evidence_ids: ['evidence-1'], verification_status: 'not_checked',
    uncertain: false, abstained: false, risk_priority: null,
  },
  {
    finding_id: 'finding-2', title: 'Possible missing guard', issue_type: 'style',
    explanation: 'A guard could make the intent clearer.', recommended_fix: 'Add a guard.',
    affected_file: null, line_start: null, line_end: null, corrected_code: null,
    evidence_ids: [], verification_status: 'not_checked', uncertain: true,
    abstained: false, risk_priority: null,
  },
]

const review = {
  valid: true, summary: 'One correctness issue is supported by available evidence.',
  findings, evidence: [{ evidence_id: 'evidence-1', source_family: 'reference_rules',
    source_url: null, rule_identifier: 'PY-01', text: 'Keep function outputs consistent.' }],
  processing_errors: [],
}

const candidate = {
  candidate_id: candidateId, finding_ref: 'finding-1', original_code: source,
  corrected_code: corrected, explanation: 'Return the expected value.',
  assumptions: ['The function contract expects 2.'], evidence_ids: ['evidence-1'],
  generation_status: 'generated', generation_errors: [], verification_status: 'not_checked',
  approval_status: 'pending', approval_history: [], verification_report: null, provider_identity: 'qwen_local:Qwen/Qwen2.5-Coder-3B-Instruct',
}

const verification = {
  candidate_id: candidateId,
  unified_diff: '--- a/candidate.py\n+++ b/candidate.py\n@@ -1,2 +1,2 @@\n def calculate():\n-    return 1\n+    return 2\n',
  syntax_check: { check_name: 'ast.parse', status: 'passed', error_type: null, message: null, line: null, column: null },
  verification_status: 'syntax_passed', behavioral_tests_status: 'not_run',
  static_analysis_status: 'passed', pre_existing_static_issues: [],
  introduced_static_issues: [], static_analysis_errors: [], verification_errors: [],
}

const exportPayload = {
  candidate_id: candidateId, filename: 'candidate.py', corrected_code: corrected,
  unified_diff: verification.unified_diff, approval_status: 'accepted',
  syntax_verification_status: 'syntax_passed',
  warning: 'Behavioral tests were not run.',
}

function jsonResponse(body: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body }
}

function installApiMock(handler: (url: string, init?: RequestInit) => unknown | Promise<unknown>) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const body = await handler(url, init)
    return body as Response
  }))
}

function mount() {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  roots.push(root)
  act(() => root.render(<App />))
  return container
}

function button(container: HTMLElement, name: string | RegExp) {
  const result = [...container.querySelectorAll('button')].find((item) =>
    typeof name === 'string' ? item.textContent?.trim() === name : name.test(item.textContent || ''),
  )
  if (!result) throw new Error(`Button not found: ${name}`)
  return result as HTMLButtonElement
}

async function click(element: Element) {
  await act(async () => {
    element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await Promise.resolve()
  })
}

async function submitSource(container: HTMLElement) {
  const editor = container.querySelector('[aria-label="Python source"]') as HTMLTextAreaElement
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set
    setter?.call(editor, source)
    editor.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await click(button(container, /Review code/))
}

async function openCandidate(container: HTMLElement) {
  await submitSource(container)
  await click(button(container, /Generate fix candidate/))
}

afterEach(() => {
  for (const root of roots.splice(0)) act(() => root.unmount())
  vi.unstubAllGlobals()
})

describe('AI Code Reviewer workflow', () => {
  it('analyzes a canonical GitHub PR URL and renders grouped read-only results', async () => {
    const prResult = {
      repository: 'acme/repo', owner: 'acme', repository_name: 'repo', number: 7,
      title: 'Safer parser', state: 'open', base_sha: 'base-sha', head_sha: 'head-sha', provider_identity: 'groq:openai/gpt-oss-20b',
      html_url: 'https://github.com/acme/repo/pull/7', total_changed_files: 1,
      files_received: 1, files_reviewed: 1, files_skipped: 0, files_failed: 0,
      completion_status: 'complete', pagination_truncated: false, pagination_error: null,
      findings_by_severity: { high: 1 }, analyzed_at: 1,
      files: [{ path: 'src/main.py', status: 'modified', additions: 1, deletions: 1,
        review_status: 'reviewed', previous_filename: null, skip_reason: null, error_code: null,
        diff_status: 'mapped', diff_error: null, review_summary: 'Unsafe dynamic execution.',
        evidence: [], findings: [{ finding_id: 'static-1', title: 'Dynamic execution', issue_type: 'security',
          explanation: 'eval executes dynamic code.', recommended_fix: 'Avoid eval.', affected_file: 'src/main.py',
          line_start: 2, line_end: 2, corrected_code: null, evidence_ids: [], verification_status: 'not_checked',
          uncertain: false, abstained: false, risk_priority: null, origin: 'static', rule_id: 'PYSEC001', severity: 'high',
          also_detected_by: [], path: 'src/main.py', source_line: 2, is_in_diff: true, is_new_in_pr: true,
          inline_supported: true, diff_line: 2, change_relation: 'added_or_modified' }] }],
    }
    const requests: Array<[string, RequestInit | undefined]> = []
    installApiMock((url, init) => {
      requests.push([url, init])
      if (url.endsWith('/api/health')) return jsonResponse({ status: 'ready' })
      if (url.endsWith('/api/github/pr-reviews')) return jsonResponse(prResult)
      throw new Error(`Unexpected request ${url}`)
    })
    const container = mount()
    await click(button(container, 'GitHub pull request'))
    const input = container.querySelector('#github-pr-url') as HTMLInputElement
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
      setter?.call(input, 'https://github.com/acme/repo/pull/7')
      input.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await click(button(container, /Analyze PR/))
    expect(container.textContent).toContain('Safer parser')
    expect(container.textContent).toContain('src/main.py')
    expect(container.textContent).toContain('Static Analysis · PYSEC001')
    expect(container.textContent).toContain('Provider/model: groq:openai/gpt-oss-20b')
    expect(container.textContent).toContain('valid new-side location')
    const submission = requests.find(([url]) => url.endsWith('/api/github/pr-reviews'))
    expect(submission?.[1]?.method).toBe('POST')
    expect(submission?.[1]?.body).toBe(JSON.stringify({ url: 'https://github.com/acme/repo/pull/7' }))
  })

  it('rejects noncanonical GitHub URLs before making an API request', async () => {
    const prCalls: string[] = []
    installApiMock((url) => {
      if (url.endsWith('/api/health')) return jsonResponse({ status: 'ready' })
      prCalls.push(url)
      throw new Error('PR endpoint should not be called')
    })
    const container = mount()
    await click(button(container, 'GitHub pull request'))
    const input = container.querySelector('#github-pr-url') as HTMLInputElement
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
      setter?.call(input, 'https://github.com.evil.test/acme/repo/pull/7')
      input.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await click(button(container, /Analyze PR/))
    expect(container.textContent).toContain('canonical https://github.com/owner/repository/pull/number')
    expect(prCalls).toEqual([])
  })

  it('opens AI settings without a provider request or persistent browser storage', async () => {
    const calls: string[] = []
    installApiMock((url) => {
      calls.push(url)
      if (url.endsWith('/api/health')) return jsonResponse({ status: 'ready' })
      if (url.endsWith('/api/admin/session')) return jsonResponse({ authenticated: false, expires_in_seconds: 0, setup_required: false, legacy_token_available: false })
      throw new Error(`Unexpected request ${url}`)
    })
    const container = mount()
    await click(button(container, /AI Settings/))
    await act(async () => { await Promise.resolve() })
    expect(container.textContent).toContain('Administrator password')
    expect(calls.some((url) => url.includes('provider-connectivity'))).toBe(false)
    expect(window.localStorage?.length ?? 0).toBe(0)
    expect(window.sessionStorage?.length ?? 0).toBe(0)
  })

  it('uses an authenticated settings session and checks connectivity only on explicit click', async () => {
    const calls: Array<[string, RequestInit | undefined]> = []
    const settings = { review_provider: 'groq', fix_provider: 'qwen_local', groq_model: 'openai/gpt-oss-20b',
      groq_api_key_configured: false, qwen_base_model: 'Qwen/Qwen2.5-Coder-3B-Instruct', qwen_adapter_path: '/approved/adapter',
      qwen_quantization: 'none', review_temperature: 0.1, fix_temperature: 0.1, max_output_tokens: 1024,
      source_sharing_consent: false, groq_connectivity: 'not_checked', qwen_readiness: 'adapter_artifacts_missing',
      generation: 0, secret_storage: 'process_memory_only' }
    installApiMock((url, init) => {
      calls.push([url, init])
      if (url.endsWith('/api/health')) return jsonResponse({ status: 'ready' })
      if (url.endsWith('/api/admin/session') && init?.method === 'POST') return jsonResponse({ authenticated: true, expires_in_seconds: 900 })
      if (url.endsWith('/api/admin/session')) return jsonResponse({ authenticated: false, expires_in_seconds: 0, setup_required: false, legacy_token_available: false })
      if (url.endsWith('/api/admin/provider-settings') && init?.method === 'PUT') return jsonResponse(settings)
      if (url.endsWith('/api/admin/provider-settings')) return jsonResponse(settings)
      if (url.endsWith('/api/admin/provider-options')) return jsonResponse({ groq_models: ['openai/gpt-oss-20b'],
        groq_model_source: 'server_approved_structured_output_models', qwen_base_model: settings.qwen_base_model,
        qwen_adapter_path: settings.qwen_adapter_path, qwen_quantization: ['none'],
        review_providers: ['groq', 'qwen_local'], fix_providers: ['groq', 'qwen_local'], default_groq_model: settings.groq_model })
      if (url.endsWith('/api/admin/provider-connectivity')) return jsonResponse({
        groq: { status: 'passed', message: 'Account reachable.' }, qwen: { status: 'failed', message: 'Adapter missing.' },
      })
      throw new Error(`Unexpected request ${url}`)
    })
    const container = mount()
    await click(button(container, /AI Settings/))
    const token = container.querySelector('#admin-password') as HTMLInputElement
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
      setter?.call(token, 'admin-password-never-persisted')
      token.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await click(button(container, 'Sign in'))
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(container.textContent).toContain('Review provider')
    expect(calls.some(([url]) => url.endsWith('/api/admin/provider-connectivity'))).toBe(false)
    expect(window.localStorage?.length ?? 0).toBe(0)
    expect(window.sessionStorage?.length ?? 0).toBe(0)
    await click(button(container, 'Check provider connection'))
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(container.textContent).toContain('Groq · passed')
    const loginCall = calls.find(([url, init]) => url.endsWith('/api/admin/session') && init?.method === 'POST')
    const connectivityCall = calls.find(([url]) => url.endsWith('/api/admin/provider-connectivity'))
    expect(connectivityCall?.[1]?.method).toBe('POST')
    expect(loginCall?.[1]?.credentials).toBe('include')
    expect(connectivityCall?.[1]?.credentials).toBe('include')
  })

  it('completes first-run setup and opens GitHub integration without automatic GitHub requests', async () => {
    const calls: Array<[string, RequestInit | undefined]> = []
    const providerSettings = { review_provider: 'qwen_local', fix_provider: 'groq', groq_model: 'openai/gpt-oss-20b',
      groq_api_key_configured: false, qwen_base_model: 'Qwen/Qwen2.5-Coder-3B-Instruct', qwen_adapter_path: '/approved/adapter',
      qwen_quantization: 'none', review_temperature: 0.1, fix_temperature: 0.1, max_output_tokens: null,
      source_sharing_consent: false, groq_connectivity: 'not_checked', qwen_readiness: 'adapter_artifacts_missing',
      generation: 0, secret_storage: 'process_memory_only' }
    installApiMock((url, init) => {
      calls.push([url, init])
      if (url.endsWith('/api/health')) return jsonResponse({ status: 'ready' })
      if (url.endsWith('/api/admin/session')) return jsonResponse({ authenticated: false, expires_in_seconds: 0, setup_required: true, legacy_token_available: false })
      if (url.endsWith('/api/admin/setup')) return jsonResponse({ authenticated: true, expires_in_seconds: 900 })
      if (url.endsWith('/api/admin/provider-settings')) return jsonResponse(providerSettings)
      if (url.endsWith('/api/admin/provider-options')) return jsonResponse({ groq_models: ['openai/gpt-oss-20b'],
        groq_model_source: 'server_approved_structured_output_models', qwen_base_model: providerSettings.qwen_base_model,
        qwen_adapter_path: providerSettings.qwen_adapter_path, qwen_quantization: ['none'],
        review_providers: ['groq', 'qwen_local'], fix_providers: ['groq', 'qwen_local'], default_groq_model: providerSettings.groq_model })
      if (url.endsWith('/api/admin/github/status')) return jsonResponse({ github_token_configured: false,
        github_actions_token_configured: false, webhook_secret_configured: false,
        persistent_storage_available: false, storage_mode: 'session_only_available' })
      if (url.endsWith('/api/admin/github/webhook')) return jsonResponse({ endpoint_url: 'http://127.0.0.1:8000/api/github/webhook',
        secret_configured: false, required_events: ['pull_request'], public_reachability: 'localhost_only',
        last_authenticated_delivery: null, last_successful_delivery: null, recent_deliveries: [],
        delivery_authentication: 'HMAC SHA-256 signature required' })
      if (url.endsWith('/api/admin/github/test')) return jsonResponse({ status: 'passed', account: { login: 'review-admin', id: 7, scopes: 'repo', accepted_permissions: '' } })
      throw new Error(`Unexpected request ${url}`)
    })
    const container = mount()
    await click(button(container, /AI Settings/))
    const bootstrap = container.querySelector('#bootstrap-credential') as HTMLInputElement
    const password = container.querySelector('#new-admin-password') as HTMLInputElement
    const confirmation = container.querySelector('#confirm-admin-password') as HTMLInputElement
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
      setter?.call(bootstrap, 'one-time-bootstrap-credential')
      bootstrap.dispatchEvent(new Event('input', { bubbles: true }))
      setter?.call(password, 'secure-admin-password-123')
      password.dispatchEvent(new Event('input', { bubbles: true }))
      setter?.call(confirmation, 'secure-admin-password-123')
      confirmation.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await click(button(container, 'Complete administrator setup'))
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    await click(container.querySelector('.admin-section-tabs button[role="tab"]:nth-child(2)')!)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(container.textContent).toContain('GitHub Personal Access Token')
    expect(container.textContent).toContain('localhost only')
    expect(calls.some(([url]) => url.endsWith('/api/admin/github/test'))).toBe(false)
    expect(calls.some(([url]) => url.endsWith('/api/admin/github/repositories'))).toBe(false)
    await click(button(container, 'Test GitHub connection'))
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(container.textContent).toContain('@review-admin')
    expect(calls.some(([url, init]) => url.endsWith('/api/admin/setup') && init?.credentials === 'include')).toBe(true)
  })

  it('submits source and renders the returned review summary and findings', async () => {
    installApiMock((url) => {
      if (url.endsWith('/api/health')) return jsonResponse({ status: 'ready', provider: 'mock', provider_initialized: false, state: 'in_memory_process_local' })
      if (url.endsWith('/api/reviews')) return jsonResponse({ review_id: reviewId, review, provider_identity: 'groq:openai/gpt-oss-20b' }, 201)
      throw new Error(`Unexpected request ${url}`)
    })
    const container = mount()
    await submitSource(container)
    expect(container.textContent).toContain('One correctness issue is supported by available evidence.')
    expect(container.textContent).toContain('Unexpected return value')
    expect(container.textContent).toContain('evidence-1')
    expect(container.textContent).toContain('Provider/model: groq:openai/gpt-oss-20b')
    const calls = vi.mocked(fetch).mock.calls
    expect(calls.some(([url]) => String(url).endsWith('/api/reviews'))).toBe(true)
  })

  it('allows selecting a different finding', async () => {
    installApiMock((url) => url.endsWith('/api/health')
      ? jsonResponse({ status: 'ready' })
      : jsonResponse({ review_id: reviewId, review }, 201))
    const container = mount()
    await submitSource(container)
    await click(button(container, /Possible missing guard/))
    const selected = [...container.querySelectorAll('.finding-row')].find((row) => row.textContent?.includes('Possible missing guard'))
    expect(selected?.className).toContain('selected')
  })

  it('generates a fix only after the finding action and shows the returned candidate', async () => {
    installApiMock((url) => {
      if (url.endsWith('/api/health')) return jsonResponse({ status: 'ready' })
      if (url.endsWith('/api/reviews')) return jsonResponse({ review_id: reviewId, review }, 201)
      if (url.includes('/findings/finding-1/fixes')) return jsonResponse(candidate, 201)
      throw new Error(`Unexpected request ${url}`)
    })
    const container = mount()
    await openCandidate(container)
    expect(container.textContent).toContain(candidateId)
    expect(container.textContent).toContain('Provider/model: qwen_local:Qwen/Qwen2.5-Coder-3B-Instruct')
    expect(container.textContent).toContain('The function contract expects 2.')
    const firstCall = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes('/findings/finding-1/fixes'))
    expect(firstCall).toHaveLength(1)
    expect(JSON.parse(String(firstCall[0][1]?.body))).toEqual({ language: 'python' })
    await click(button(container, /Generate fix candidate/))
    const fixCalls = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes('/findings/finding-1/fixes'))
    expect(fixCalls).toHaveLength(2)
    expect(fixCalls.every(([url]) => String(url).includes(`/findings/${findings[0].finding_id}/fixes`))).toBe(true)
    expect(fixCalls.every(([, init]) => JSON.parse(String(init?.body)).language === 'python')).toBe(true)
  })

  it('verifies via the API and keeps acceptance disabled until required checks pass', async () => {
    installApiMock((url) => {
      if (url.endsWith('/api/health')) return jsonResponse({ status: 'ready' })
      if (url.endsWith('/api/reviews')) return jsonResponse({ review_id: reviewId, review }, 201)
      if (url.includes('/findings/finding-1/fixes')) return jsonResponse(candidate, 201)
      if (url.endsWith('/verify')) return jsonResponse(verification)
      if (url.endsWith('/accept')) return jsonResponse({ candidate_id: candidateId, action: 'accepted', approval_status: 'accepted', outcome: 'accepted', message: 'Accepted.', verification_status: 'syntax_passed', candidate: { ...candidate, approval_status: 'accepted', verification_status: 'syntax_passed' } })
      throw new Error(`Unexpected request ${url}`)
    })
    const container = mount()
    await openCandidate(container)
    expect(button(container, 'Accept candidate').disabled).toBe(true)
    await click(button(container, 'Verify syntax & static analysis'))
    expect(container.textContent).toContain('Python syntax passed')
    expect(container.textContent).toContain('Behavioral tests')
    expect(container.textContent).toContain('Static verification')
    expect(button(container, 'Accept candidate').disabled).toBe(false)
  })

  it('keeps acceptance disabled when static verification finds a new issue', async () => {
    const failed = {
      ...verification,
      verification_status: 'static_analysis_failed',
      static_analysis_status: 'failed',
      introduced_static_issues: [{
        finding_id: 'static-1', rule_id: 'PYSEC001', title: 'Dynamic execution',
        severity: 'high', explanation: 'Unsafe.', affected_file: 'candidate.py',
        line_start: 1, line_end: 1, column: 0, code: 'eval(value)',
      }],
    }
    installApiMock((url) => {
      if (url.endsWith('/api/health')) return jsonResponse({ status: 'ready' })
      if (url.endsWith('/api/reviews')) return jsonResponse({ review_id: reviewId, review }, 201)
      if (url.includes('/findings/finding-1/fixes')) return jsonResponse(candidate, 201)
      if (url.endsWith('/verify')) return jsonResponse(failed)
      throw new Error('Unexpected request ' + url)
    })
    const container = mount()
    await openCandidate(container)
    await click(button(container, 'Verify syntax & static analysis'))
    expect(container.textContent).toContain('PYSEC001')
    expect(button(container, 'Accept candidate').disabled).toBe(true)
  })

  it('accepts after syntax validation and exports corrected code and the backend diff', async () => {
    const requests: string[] = []
    installApiMock((url) => {
      requests.push(url)
      if (url.endsWith('/api/health')) return jsonResponse({ status: 'ready' })
      if (url.endsWith('/api/reviews')) return jsonResponse({ review_id: reviewId, review }, 201)
      if (url.includes('/findings/finding-1/fixes')) return jsonResponse(candidate, 201)
      if (url.endsWith('/verify')) return jsonResponse(verification)
      if (url.endsWith('/accept')) return jsonResponse({ candidate_id: candidateId, action: 'accepted', approval_status: 'accepted', outcome: 'accepted', message: 'Accepted.', verification_status: 'syntax_passed', candidate: { ...candidate, approval_status: 'accepted', verification_status: 'syntax_passed' } })
      if (url.endsWith('/export')) return jsonResponse(exportPayload)
      throw new Error(`Unexpected request ${url}`)
    })
    const create = vi.fn(() => 'blob:download')
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: create })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    const downloads: string[] = []
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      downloads.push(this.download)
    })
    const container = mount()
    await openCandidate(container)
    await click(button(container, 'Verify syntax & static analysis'))
    await click(button(container, 'Accept candidate'))
    expect(container.textContent).toContain('Accepted.')
    await click(button(container, /Export files/))
    expect(requests.some((url) => url.endsWith('/export'))).toBe(true)
    expect(create).toHaveBeenCalledTimes(2)
    expect(clickSpy).toHaveBeenCalledTimes(2)
    expect(downloads).toEqual(['candidate.py', 'candidate.patch'])
  })

  it('rejects a candidate without changing the submitted source', async () => {
    installApiMock((url) => {
      if (url.endsWith('/api/health')) return jsonResponse({ status: 'ready' })
      if (url.endsWith('/api/reviews')) return jsonResponse({ review_id: reviewId, review }, 201)
      if (url.includes('/findings/finding-1/fixes')) return jsonResponse(candidate, 201)
      if (url.endsWith('/reject')) return jsonResponse({ candidate_id: candidateId, action: 'rejected', approval_status: 'rejected', outcome: 'rejected', message: 'Candidate explicitly rejected.', verification_status: 'not_checked', candidate: { ...candidate, approval_status: 'rejected' } })
      throw new Error(`Unexpected request ${url}`)
    })
    const container = mount()
    await openCandidate(container)
    await click(button(container, 'Reject'))
    expect(container.textContent).toContain('Candidate rejected. Original source and review remain unchanged.')
    expect((container.querySelector('[aria-label="Python source"]') as HTMLTextAreaElement).value).toBe(source)
  })

  it('shows API errors and handles expired review handles', async () => {
    installApiMock((url) => {
      if (url.endsWith('/api/health')) return jsonResponse({ status: 'ready' })
      if (url.endsWith('/api/reviews')) return jsonResponse({ review_id: reviewId, review }, 201)
      if (url.includes('/findings/')) return jsonResponse({ detail: 'review_not_found_or_expired' }, 404)
      throw new Error(`Unexpected request ${url}`)
    })
    const container = mount()
    await submitSource(container)
    await click(button(container, /Generate fix candidate/))
    expect(container.textContent).toContain('This review or fix has expired.')
  })

  it('clears a stale review and candidate when source text changes', async () => {
    installApiMock((url) => {
      if (url.endsWith('/api/health')) return jsonResponse({ status: 'ready' })
      if (url.endsWith('/api/reviews')) return jsonResponse({ review_id: reviewId, review }, 201)
      if (url.includes('/findings/finding-1/fixes')) return jsonResponse(candidate, 201)
      throw new Error(`Unexpected request ${url}`)
    })
    const container = mount()
    await openCandidate(container)
    expect(container.textContent).toContain(candidateId)
    const editor = container.querySelector('[aria-label="Python source"]') as HTMLTextAreaElement
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set
      setter?.call(editor, 'def calculate():\n    return 3\n')
      editor.dispatchEvent(new Event('input', { bubbles: true }))
    })
    expect(container.textContent).not.toContain(candidateId)
    expect(container.textContent).not.toContain('Review summary')
    expect(container.textContent).toContain('previous review and candidate were cleared')
  })
})
