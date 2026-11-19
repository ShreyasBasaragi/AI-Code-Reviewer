import type {
  ApprovalResponse,
  ExportResponse,
  FixCandidate,
  GitHubPRReview,
  ReviewCreated,
  Review,
  VerificationReport,
  GitHubWebhookActionStatus,
  ProviderSettings,
  ProviderOptions,
  ProviderConnectivity,
  GitHubCredentialStatus,
  GitHubAccountCheck,
  AccessibleRepository,
  RepositoryPolicyResponse,
  WebhookAdminStatus,
  AdminWebhookDelivery,
} from './types'

const baseUrl = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '')

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${baseUrl}${path}`, {
      ...init,
      credentials: init?.credentials ?? 'same-origin',
      headers: { 'Content-Type': 'application/json', ...init?.headers },
    })
  } catch {
    throw new ApiError(0, 'Could not reach the review API. Check that the backend is running.')
  }

  if (!response.ok) {
    let detail = `Request failed (${response.status}).`
    try {
      const body = await response.json() as { detail?: unknown }
      if (typeof body.detail === 'string') detail = friendlyError(response.status, body.detail, path)
    } catch {
      // Keep a safe, generic message if the server did not return JSON.
    }
    if (response.status === 401 && (
      path.startsWith('/api/admin/') || path.startsWith('/api/fixes/') ||
      /^\/api\/reviews\/[^/]+(?:\/findings\/|$)/.test(path)
    )) window.dispatchEvent(new Event('admin-session-expired'))
    throw new ApiError(response.status, detail)
  }
  return response.json() as Promise<T>
}

function friendlyError(status: number, detail: string, path: string): string {
  if (path === '/api/admin/session' && status === 401) return 'Administrator credentials were not accepted.'
  if (path === '/api/admin/setup' && status === 401) return 'The one-time setup credential was not accepted.'
  if (path === '/api/admin/password-reset' && status === 401) return 'The one-time recovery credential was not accepted.'
  if (path.startsWith('/api/admin/webhook/')) {
    if (status === 401) return 'Your administrator session has expired. Sign in again.'
    if (status === 404) return 'No retained webhook record matches that ID.'
    if (status === 409) return detail.replace(/_/g, ' ')
    if (status >= 500) return 'The webhook operation could not be completed.'
  }
  if (path.startsWith('/api/github/')) {
    if (path.includes('/deliveries/')) {
      if (status === 401) return 'The webhook status token was not accepted.'
      if (status === 404) return 'No retained webhook delivery matches that ID.'
      if (status === 503) return 'Webhook status is not configured on this backend.'
    }
    if (status === 401) return 'GitHub rejected the configured token. Check its credential in GitHub Integration settings.'
    if (status === 403 && detail === 'source_sharing_not_authorized') return 'External source processing is not authorized. Review provider consent and repository policy in settings.'
    if (status === 403) return 'The GitHub token cannot read this repository. Private repositories need pull request and contents read access.'
    if (status === 404) return 'The pull request was not found or is not accessible with this credential.'
    if (status === 413) return 'The PR or a source file exceeds the configured review size limit.'
    if (status === 422) return 'Enter a canonical GitHub pull request URL.'
    if (status === 429) return detail === 'github_pr_review_busy' ? 'Another PR review is running. Try again shortly.' : 'GitHub rate limited this request. Try again later.'
    if (status === 504) return 'GitHub did not respond before the request timeout.'
    if (status >= 500) return 'The GitHub PR review could not be completed.'
  }
  if (path.startsWith('/api/admin/') && status === 401) return 'Your administrator session has expired. Sign in again.'
  if (status === 401) return 'Administrator sign-in is required for persisted review and fix actions. Sign in through AI Settings.'
  if (status === 403) {
    return detail === 'source_sharing_not_authorized'
      ? 'External source processing is not authorized. Review the source-sharing consent in AI Settings.'
      : 'This operation is not authorized for the current account.'
  }
  if (status === 404) {
    if (detail.includes('review')) return 'This review was not found or has expired. Check the review ID or submit the source again.'
    if (detail.includes('candidate') || path.includes('/fixes/')) return 'This candidate was not found or has expired. Check the candidate ID.'
    return 'The requested item was not found.'
  }
  if (status === 413) return 'The code is larger than the 512 KiB limit.'
  if (status === 422) return 'The request could not be validated. Check the Python input and selected finding.'
  if (status === 429) return 'The service is busy or rate limited. Wait briefly before trying again.'
  if (status === 409) return 'This action is not available for the candidate’s current verification or approval state.'
  if (status >= 500) return 'The configured review service could not complete this request. Check provider availability and try again later.'
  return 'The request could not be completed.'
}

export const api = {
  health: () => request<{ status: string; provider: string; provider_initialized: boolean; state: string }>('/api/health'),
  submitReview: (code: string) => request<ReviewCreated>('/api/reviews', {
    method: 'POST', body: JSON.stringify({ code, language: 'python' }),
  }),
  analyzeGitHubPR: (url: string) => request<GitHubPRReview>('/api/github/pr-reviews', {
    method: 'POST', body: JSON.stringify({ url }),
  }),
  getWebhookActions: (deliveryId: string, statusToken: string) => request<GitHubWebhookActionStatus>(
    `/api/github/webhook/deliveries/${encodeURIComponent(deliveryId)}/actions`,
    { headers: { Authorization: `Bearer ${statusToken}` } },
  ),
  reconcileWebhookAction: (deliveryId: string, actionId: string, statusToken: string) => request<{ action_id: string; status: string; github_comment_id?: number | null; retry_safe?: boolean }>(
    `/api/github/webhook/deliveries/${encodeURIComponent(deliveryId)}/actions/${encodeURIComponent(actionId)}/reconcile`,
    { method: 'POST', headers: { Authorization: `Bearer ${statusToken}` } },
  ),
  getReview: (reviewId: string) => request<{ review_id: string; review: Review }>(`/api/reviews/${encodeURIComponent(reviewId)}`, { credentials: 'include' }),
  generateFix: (reviewId: string, findingId: string) => request<FixCandidate>(
    `/api/reviews/${encodeURIComponent(reviewId)}/findings/${encodeURIComponent(findingId)}/fixes`,
    { method: 'POST', body: JSON.stringify({ language: 'python' }), credentials: 'include' },
  ),
  getFix: (candidateId: string) => request<FixCandidate>(`/api/fixes/${encodeURIComponent(candidateId)}`, { credentials: 'include' }),
  verifyFix: (candidateId: string) => request<VerificationReport>(`/api/fixes/${encodeURIComponent(candidateId)}/verify`, { method: 'POST', credentials: 'include' }),
  acceptFix: (candidateId: string) => request<ApprovalResponse>(`/api/fixes/${encodeURIComponent(candidateId)}/accept`, { method: 'POST', credentials: 'include' }),
  rejectFix: (candidateId: string) => request<ApprovalResponse>(`/api/fixes/${encodeURIComponent(candidateId)}/reject`, { method: 'POST', credentials: 'include' }),
  exportFix: (candidateId: string) => request<ExportResponse>(`/api/fixes/${encodeURIComponent(candidateId)}/export`, { credentials: 'include' }),
  adminSession: () => request<{ authenticated: boolean; expires_in_seconds: number; setup_required: boolean; legacy_token_available: boolean }>('/api/admin/session', { credentials: 'include' }),
  loginAdmin: (admin_token: string) => request<{ authenticated: boolean; expires_in_seconds: number }>('/api/admin/session', { method: 'POST', credentials: 'include', body: JSON.stringify({ admin_token }) }),
  loginAdminPassword: (password: string) => request<{ authenticated: boolean; expires_in_seconds: number }>('/api/admin/session', { method: 'POST', credentials: 'include', body: JSON.stringify({ password }) }),
  setupAdmin: (bootstrap_credential: string, password: string, password_confirmation: string) => request<{ authenticated: boolean; expires_in_seconds: number }>('/api/admin/setup', { method: 'POST', credentials: 'include', body: JSON.stringify({ bootstrap_credential, password, password_confirmation }) }),
  resetAdminPassword: (recovery_credential: string, password: string, password_confirmation: string) => request<{ authenticated: boolean; expires_in_seconds: number }>('/api/admin/password-reset', { method: 'POST', credentials: 'include', body: JSON.stringify({ recovery_credential, password, password_confirmation }) }),
  logoutAdmin: () => request<{ authenticated: boolean }>('/api/admin/session', { method: 'DELETE', credentials: 'include' }),
  getProviderSettings: () => request<ProviderSettings>('/api/admin/provider-settings', { credentials: 'include' }),
  getProviderOptions: () => request<ProviderOptions>('/api/admin/provider-options', { credentials: 'include' }),
  updateProviderSettings: (settings: Partial<ProviderSettings> & { groq_api_key?: string }) => request<ProviderSettings>('/api/admin/provider-settings', { method: 'PUT', credentials: 'include', body: JSON.stringify(settings) }),
  checkProviderConnectivity: () => request<ProviderConnectivity>('/api/admin/provider-connectivity', { method: 'POST', credentials: 'include', body: '{}' }),
  githubCredentialStatus: () => request<GitHubCredentialStatus>('/api/admin/github/status', { credentials: 'include' }),
  setGitHubCredential: (credential_type: 'github_token' | 'github_actions_token' | 'github_candidate_staging_token' | 'github_autonomous_commit_token' | 'webhook_secret', value: string, persistent: boolean) => request<GitHubCredentialStatus & { updated: boolean; storage: string; credential_type: string }>('/api/admin/github/credentials', { method: 'PUT', credentials: 'include', body: JSON.stringify({ credential_type, value, persistent }) }),
  removeGitHubCredential: (credential: 'github_token' | 'github_actions_token' | 'github_candidate_staging_token' | 'github_autonomous_commit_token' | 'webhook_secret') => request<GitHubCredentialStatus & { removed: boolean }>(`/api/admin/github/credentials/${credential}`, { method: 'DELETE', credentials: 'include' }),
  testGitHubConnection: () => request<GitHubAccountCheck>('/api/admin/github/test', { method: 'POST', credentials: 'include', body: '{}' }),
  listGitHubRepositories: () => request<{ repositories: AccessibleRepository[] }>('/api/admin/github/repositories', { credentials: 'include' }),
  getRepositoryPolicy: (identity: string) => { const [owner, repository] = identity.split('/'); return request<RepositoryPolicyResponse>(`/api/admin/github/repositories/${encodeURIComponent(owner)}/${encodeURIComponent(repository)}/policy`, { credentials: 'include' }) },
  updateRepositoryPolicy: (identity: string, policy: RepositoryPolicyResponse) => { const [owner, repository] = identity.split('/'); return request<RepositoryPolicyResponse>(`/api/admin/github/repositories/${encodeURIComponent(owner)}/${encodeURIComponent(repository)}/policy`, { method: 'PUT', credentials: 'include', body: JSON.stringify({ revision: policy.revision, ...policy.policy }) }) },
  deleteRepositoryPolicy: (identity: string, revision: string) => { const [owner, repository] = identity.split('/'); return request<{ removed: boolean }>(`/api/admin/github/repositories/${encodeURIComponent(owner)}/${encodeURIComponent(repository)}/policy?revision=${encodeURIComponent(revision)}`, { method: 'DELETE', credentials: 'include' }) },
  githubWebhookStatus: () => request<WebhookAdminStatus>('/api/admin/github/webhook', { credentials: 'include' }),
  adminWebhookDeliveries: () => request<{ deliveries: AdminWebhookDelivery[] }>('/api/admin/webhook/deliveries?limit=25', { credentials: 'include' }),
  retryWebhookDelivery: (deliveryId: string) => request<{ delivery_id: string; status: string; next_attempt_at: number | null; reason: string }>(`/api/admin/webhook/deliveries/${encodeURIComponent(deliveryId)}/retry`, { method: 'POST', credentials: 'include' }),
  retryWebhookAction: (deliveryId: string, actionId: string) => request<{ action_id: string; status: string; next_attempt_at?: number; reason?: string }>(`/api/admin/webhook/deliveries/${encodeURIComponent(deliveryId)}/actions/${encodeURIComponent(actionId)}/retry`, { method: 'POST', credentials: 'include' }),
}

export function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).length
}
