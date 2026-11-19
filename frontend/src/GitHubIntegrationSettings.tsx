import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api, ApiError } from './api'
import type { AccessibleRepository, AdminWebhookDelivery, GitHubAccountCheck, GitHubCredentialStatus, RepositoryPolicyResponse, WebhookAdminStatus } from './types'

type CredentialKind = 'github_token' | 'github_actions_token' | 'github_candidate_staging_token' | 'github_autonomous_commit_token' | 'webhook_secret'

function errorMessage(error: unknown, fallback: string) {
  if (error instanceof ApiError && error.status === 401) {
    window.dispatchEvent(new Event('admin-session-expired'))
  }
  return error instanceof ApiError ? error.message : fallback
}

export function GitHubIntegrationSettings() {
  const [credentials, setCredentials] = useState<GitHubCredentialStatus | null>(null)
  const [webhook, setWebhook] = useState<WebhookAdminStatus | null>(null)
  const [adminDeliveries, setAdminDeliveries] = useState<AdminWebhookDelivery[]>([])
  const [token, setToken] = useState('')
  const [actionToken, setActionToken] = useState('')
  const [stagingToken, setStagingToken] = useState('')
  const [commitToken, setCommitToken] = useState('')
  const [webhookSecret, setWebhookSecret] = useState('')
  const [persist, setPersist] = useState(true)
  const [connection, setConnection] = useState<GitHubAccountCheck | null>(null)
  const [repositories, setRepositories] = useState<AccessibleRepository[]>([])
  const [selectedRepo, setSelectedRepo] = useState('')
  const [policy, setPolicy] = useState<RepositoryPolicyResponse | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState('')
  const policyRequest = useRef(0)

  async function refreshLocalStatus() {
    setBusy('refresh'); setError('')
    const [credentialStatus, webhookStatus, deliveryStatus] = await Promise.allSettled([api.githubCredentialStatus(), api.githubWebhookStatus(), api.adminWebhookDeliveries()])
    if (credentialStatus.status === 'fulfilled') {
      setCredentials(credentialStatus.value)
      setPersist(credentialStatus.value.persistent_storage_available)
    }
    if (webhookStatus.status === 'fulfilled') setWebhook(webhookStatus.value)
    if (deliveryStatus.status === 'fulfilled') setAdminDeliveries(deliveryStatus.value.deliveries)
    if ([credentialStatus, webhookStatus, deliveryStatus].some((result) => result.status === 'rejected')) {
      const failure = [credentialStatus, webhookStatus, deliveryStatus].find((result) => result.status === 'rejected')
      setError(errorMessage(failure?.status === 'rejected' ? failure.reason : null, 'Some GitHub status could not be loaded.'))
    }
    setBusy('')
  }

  useEffect(() => {
    let live = true
    Promise.allSettled([api.githubCredentialStatus(), api.githubWebhookStatus(), api.adminWebhookDeliveries()]).then(([credentialStatus, webhookStatus, deliveryStatus]) => {
      if (!live) return
      if (credentialStatus.status === 'fulfilled') {
        setCredentials(credentialStatus.value)
        setPersist(credentialStatus.value.persistent_storage_available)
      }
      if (webhookStatus.status === 'fulfilled') setWebhook(webhookStatus.value)
      if (deliveryStatus.status === 'fulfilled') setAdminDeliveries(deliveryStatus.value.deliveries)
      const failure = [credentialStatus, webhookStatus, deliveryStatus].find((result) => result.status === 'rejected')
      if (failure?.status === 'rejected') setError(errorMessage(failure.reason, 'Some GitHub settings could not be loaded.'))
    })
    return () => { live = false }
  }, [])

  async function retryDelivery(delivery: AdminWebhookDelivery) {
    setBusy(`delivery-${delivery.delivery_id}`); setError(''); setMessage('')
    try {
      const result = await api.retryWebhookDelivery(delivery.delivery_id)
      setMessage(`Delivery retry scheduled${result.next_attempt_at ? ` for ${new Date(result.next_attempt_at * 1000).toLocaleString()}` : ''}.`)
      setAdminDeliveries((await api.adminWebhookDeliveries()).deliveries)
    } catch (e) { setError(errorMessage(e, 'Delivery retry could not be scheduled.')) }
    finally { setBusy('') }
  }

  async function retryAction(deliveryId: string, actionId: string) {
    setBusy(`action-${actionId}`); setError(''); setMessage('')
    try {
      const result = await api.retryWebhookAction(deliveryId, actionId)
      setMessage(result.status === 'published' ? 'The comment was already published and has been reconciled.' : 'Comment retry scheduled after remote state and current policy checks.')
      setAdminDeliveries((await api.adminWebhookDeliveries()).deliveries)
    } catch (e) { setError(errorMessage(e, 'Comment retry could not be scheduled.')) }
    finally { setBusy('') }
  }

  async function updateCredential(kind: CredentialKind, value: string, clear: (value: string) => void) {
    setBusy(kind); setError(''); setMessage(''); clear('')
    try {
      const result = await api.setGitHubCredential(kind, value, persist)
      setCredentials(result)
      setMessage(`${kind.replaceAll('_', ' ')} saved using ${result.storage.replaceAll('_', ' ')} storage.`)
      if (kind === 'webhook_secret') setWebhook(await api.githubWebhookStatus())
    } catch (e) { setError(errorMessage(e, 'Credential could not be saved.')) }
    finally { setBusy('') }
  }

  async function removeCredential(kind: CredentialKind) {
    setBusy(`remove-${kind}`); setError(''); setMessage('')
    if (kind === 'github_token') setToken('')
    if (kind === 'github_actions_token') setActionToken('')
    if (kind === 'github_candidate_staging_token') setStagingToken('')
    if (kind === 'github_autonomous_commit_token') setCommitToken('')
    if (kind === 'webhook_secret') setWebhookSecret('')
    try {
      setCredentials(await api.removeGitHubCredential(kind))
      if (kind === 'webhook_secret') setWebhook(await api.githubWebhookStatus())
      setMessage(`${kind.replaceAll('_', ' ')} removed.`)
    } catch (e) { setError(errorMessage(e, 'Credential could not be removed.')) }
    finally { setBusy('') }
  }

  async function testConnection() {
    setBusy('test'); setError(''); setMessage(''); setConnection(null)
    try { setConnection(await api.testGitHubConnection()) }
    catch (e) { setError(errorMessage(e, 'GitHub connection could not be tested.')) }
    finally { setBusy('') }
  }

  async function discoverRepositories() {
    setBusy('repositories'); setError(''); setMessage(''); setRepositories([])
    try {
      const result = await api.listGitHubRepositories()
      setRepositories(result.repositories)
      setMessage(`${result.repositories.length} accessible repositories discovered.`)
    } catch (e) { setError(errorMessage(e, 'Repository discovery failed. Test GitHub credentials first.')) }
    finally { setBusy('') }
  }

  async function selectRepository(identity: string) {
    const requestId = ++policyRequest.current
    setSelectedRepo(identity); setPolicy(null); setError('')
    if (!identity) return
    setBusy('policy')
    try {
      const result = await api.getRepositoryPolicy(identity)
      if (requestId === policyRequest.current) setPolicy(result)
    } catch (e) { if (requestId === policyRequest.current) setError(errorMessage(e, 'Repository policy could not be loaded.')) }
    finally { setBusy('') }
  }

  function setPolicyValue<K extends keyof NonNullable<typeof policy>['policy']>(key: K, value: NonNullable<typeof policy>['policy'][K]) {
    if (policy) setPolicy({ ...policy, policy: { ...policy.policy, [key]: value } })
  }

  async function savePolicy(event: FormEvent) {
    event.preventDefault()
    if (!policy || !selectedRepo) return
    setBusy('save-policy'); setError(''); setMessage('')
    try {
      const updated = await api.updateRepositoryPolicy(selectedRepo, policy)
      setPolicy(updated); setMessage('Repository policy saved. Commit execution remains disabled by the backend hard gate.')
      await discoverRepositories()
    } catch (e) { setError(errorMessage(e, 'Policy could not be saved.')) }
    finally { setBusy('') }
  }

  async function removePolicy() {
    if (!policy || !selectedRepo) return
    setBusy('remove-policy'); setError(''); setMessage('')
    try {
      await api.deleteRepositoryPolicy(selectedRepo, policy.revision)
      setPolicy(await api.getRepositoryPolicy(selectedRepo))
      setMessage('Repository-specific policy removed. Fail-closed defaults now apply.')
      await discoverRepositories()
    } catch (e) { setError(errorMessage(e, 'Policy could not be removed.')) }
    finally { setBusy('') }
  }

  const requiredChecks = ['syntax', 'static', 'behavioral', 'ci'] as const
  const policyConfigured = repositories.find((repo) => repo.full_name === selectedRepo)?.policy_configured
  const selectedRepository = repositories.find((repo) => repo.full_name === selectedRepo)

  return <div className="github-admin-layout">
    <section className="panel github-admin-panel">
      <div className="panel-heading"><div><div className="panel-kicker">AUTHENTICATED GITHUB REST</div><h2>GitHub connection</h2></div></div>
      <p className="github-help">Use a fine-grained personal access token. Read-only PR review needs repository Metadata read and Pull requests read; Contents read is used to retrieve source. Optional review comments require a separate token with Pull requests write. Avoid broad classic tokens.</p>
      <label className="persist-choice"><input type="checkbox" checked={persist} disabled={!credentials?.persistent_storage_available} onChange={(e) => setPersist(e.target.checked)} /> Store entered credentials in macOS Keychain</label>
      <small>{credentials?.persistent_storage_available ? 'Keychain persistence is available. Unchecked credentials remain in backend process memory only.' : 'Secure persistent credential storage is unavailable. You can explicitly use process-memory-only credentials; they clear on restart.'}</small>
      <div className="github-credential-row"><label>GitHub Personal Access Token <span>{credentials?.github_token_configured === true ? 'Configured · hidden' : credentials?.github_token_configured === false ? 'Not configured' : 'Status unavailable'}</span><input type="password" autoComplete="new-password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="Enter token to save or replace" /></label>
        <button className="primary-button" type="button" disabled={!token || !!busy} onClick={() => void updateCredential('github_token', token, setToken)}>{busy === 'github_token' ? 'Saving…' : 'Save token'}</button>
        <button className="secondary-button" type="button" disabled={!credentials?.github_token_configured || !!busy} onClick={() => void removeCredential('github_token')}>Remove</button></div>
      <div className="github-credential-row"><label>Optional review-comment token <span>{credentials?.github_actions_token_configured === true ? 'Configured · hidden' : credentials?.github_actions_token_configured === false ? 'Not configured' : 'Status unavailable'}</span><input type="password" autoComplete="new-password" value={actionToken} onChange={(e) => setActionToken(e.target.value)} placeholder="Separate token with Pull requests write" /></label>
        <button className="primary-button" type="button" disabled={!actionToken || !!busy} onClick={() => void updateCredential('github_actions_token', actionToken, setActionToken)}>{busy === 'github_actions_token' ? 'Saving…' : 'Save comment token'}</button>
        <button className="secondary-button" type="button" disabled={!credentials?.github_actions_token_configured || !!busy} onClick={() => void removeCredential('github_actions_token')}>Remove</button></div>
      <div className="github-credential-row"><label>Candidate-staging token <span>{credentials?.github_candidate_staging_token_configured === true ? 'Configured · hidden' : 'Not configured'}</span><input type="password" autoComplete="new-password" value={stagingToken} onChange={(e) => setStagingToken(e.target.value)} placeholder="Separate Contents write token for temporary candidates" /></label>
        <button className="primary-button" type="button" disabled={!stagingToken || !!busy} onClick={() => void updateCredential('github_candidate_staging_token', stagingToken, setStagingToken)}>{busy === 'github_candidate_staging_token' ? 'Saving…' : 'Save staging token'}</button>
        <button className="secondary-button" type="button" disabled={!credentials?.github_candidate_staging_token_configured || !!busy} onClick={() => void removeCredential('github_candidate_staging_token')}>Remove</button></div>
      <div className="github-credential-row"><label>Final PR-branch token <span>{credentials?.github_autonomous_commit_token_configured === true ? 'Configured · hidden' : 'Not configured'}</span><input type="password" autoComplete="new-password" value={commitToken} onChange={(e) => setCommitToken(e.target.value)} placeholder="Separate Contents write token for final branch update" /></label>
        <button className="primary-button" type="button" disabled={!commitToken || !!busy} onClick={() => void updateCredential('github_autonomous_commit_token', commitToken, setCommitToken)}>{busy === 'github_autonomous_commit_token' ? 'Saving…' : 'Save commit token'}</button>
        <button className="secondary-button" type="button" disabled={!credentials?.github_autonomous_commit_token_configured || !!busy} onClick={() => void removeCredential('github_autonomous_commit_token')}>Remove</button></div>
      <div className="settings-actions"><button className="secondary-button" type="button" disabled={!!busy} onClick={() => void testConnection()}>{busy === 'test' ? 'Testing…' : 'Test GitHub connection'}</button><button className="secondary-button" type="button" disabled={!!busy} onClick={() => void discoverRepositories()}>{busy === 'repositories' ? 'Discovering…' : 'Discover repositories'}</button></div>
      {connection && <div className="connectivity-results" role="status"><b>GitHub API · {connection.status}</b>{connection.account && <span>Signed in as @{connection.account.login} · account {connection.account.id} · scopes: {connection.account.scopes || 'fine-grained permissions'} · accepted permissions: {connection.account.accepted_permissions || 'not reported'}</span>}{connection.message && <span>{connection.message}</span>}</div>}
    </section>

    <section className="panel github-admin-panel">
      <div className="panel-heading"><div><div className="panel-kicker">REPOSITORY POLICY</div><h2>Repositories and permissions</h2></div></div>
      <p className="github-help">Repository discovery contacts GitHub only when requested. Policies are validated and stored in the existing canonical policy file using atomic, locked updates. Invalid policies fail closed.</p>
      {repositories.length > 0 && <label>Select a discovered repository<select disabled={!!busy} value={selectedRepo} onChange={(e) => void selectRepository(e.target.value)}><option value="">Choose a repository…</option>{repositories.map((repo) => <option key={repo.id || repo.full_name} value={repo.full_name}>{repo.full_name}{repo.private ? ' · private' : ' · public'}</option>)}</select></label>}
      {selectedRepo && selectedRepository && <div className="repo-identity-note">Repository: <strong>{selectedRepo}</strong> · owner @{selectedRepository.owner} · {selectedRepository.private ? 'private' : 'public'} · GitHub permissions: pull {selectedRepository.permissions.pull ? 'yes' : 'no'}, push {selectedRepository.permissions.push ? 'yes' : 'no'}, admin {selectedRepository.permissions.admin ? 'yes' : 'no'} · policy {policyConfigured ? 'configured' : 'using defaults'}. Permissions come from authenticated GitHub discovery, never from browser policy values.</div>}
      {policy && <form className="settings-form" onSubmit={savePolicy}><fieldset disabled={!!busy} className="policy-fields">
        <div className="github-permission-grid">
          <label className="consent-row"><input type="checkbox" checked={policy.policy.allow_source_sharing} onChange={(e) => setPolicyValue('allow_source_sharing', e.target.checked)} /> Allow external AI processing of this repository source</label>
          <label className="consent-row"><input type="checkbox" checked={policy.policy.publish_review_comments} onChange={(e) => setPolicyValue('publish_review_comments', e.target.checked)} /> Publish AI review comments automatically (separate opt-in)</label>
          <label className="consent-row"><input type="checkbox" checked={policy.policy.enable_autonomous_fixes} onChange={(e) => setPolicyValue('enable_autonomous_fixes', e.target.checked)} /> Opt in to gated low-risk automatic fixes</label>
        </div>
        {policy.policy.allow_source_sharing && <div className="processing-errors">With Groq selected, source from {selectedRepo} can be transmitted to an external provider.</div>}
        {policy.policy.publish_review_comments && credentials?.github_actions_token_configured !== true && <div className="processing-errors">Comment publishing stays disabled until a separate GitHub write token is configured.</div>}
        <label>Allowed low-risk categories<input value={policy.policy.allowed_low_risk_categories.join(', ')} onChange={(e) => setPolicyValue('allowed_low_risk_categories', e.target.value.split(',').map((x) => x.trim()).filter(Boolean))} placeholder="style, naming, formatting" /></label>
        <div className="settings-grid"><label>Maximum changed lines<input type="number" min="1" max="3" value={policy.policy.max_changed_lines} onChange={(e) => setPolicyValue('max_changed_lines', Number(e.target.value))} /></label>
          <label>Confidence threshold<input type="number" min="0.8" max="1" step="0.01" value={policy.policy.confidence_threshold} onChange={(e) => setPolicyValue('confidence_threshold', Number(e.target.value))} /></label></div>
        <label>Permitted branches (comma-separated)<input value={policy.policy.permitted_branches.join(', ')} onChange={(e) => setPolicyValue('permitted_branches', e.target.value.split(',').map((x) => x.trim()).filter(Boolean))} /></label>
        <label>Protected paths (comma-separated)<input value={policy.policy.protected_paths.join(', ')} onChange={(e) => setPolicyValue('protected_paths', e.target.value.split(',').map((x) => x.trim()).filter(Boolean))} /></label>
        <label>Required GitHub CI checks (comma-separated)<input value={policy.policy.required_github_checks.join(', ')} onChange={(e) => setPolicyValue('required_github_checks', e.target.value.split(',').map((x) => x.trim()).filter(Boolean))} /></label>
        <div className="required-check-list"><strong>Required verification checks (mandatory)</strong>{requiredChecks.map((check) => <label key={check}><input type="checkbox" checked={policy.policy.required_checks.includes(check)} disabled /> {check}</label>)}</div>
        <div className="github-hard-gate"><strong>Automatic fixes: runtime-gated and off by default</strong><span>Requires separate staging and final-write tokens, explicit server-side write switches, signed category-specific calibration, an attested isolated executor, required checks on the candidate SHA from the configured workflow, permitted branch policy, and a fresh PR head. Missing any gate routes the finding to manual review.</span></div>
        <div className="settings-actions"><button className="primary-button" type="submit" disabled={!!busy}>{busy === 'save-policy' ? 'Saving…' : 'Save repository policy'}</button>{policyConfigured && <button className="secondary-button" type="button" disabled={!!busy} onClick={() => void removePolicy()}>Remove policy</button>}</div>
      </fieldset></form>}
      {repositories.length === 0 && <p className="empty-note">Discover repositories after testing the GitHub connection.</p>}
    </section>

    <section className="panel github-admin-panel">
      <div className="panel-heading"><div><div className="panel-kicker">WEBHOOK MONITORING</div><h2>Webhook configuration</h2></div><button className="secondary-button" type="button" disabled={!!busy} onClick={() => void refreshLocalStatus()}>Refresh status</button></div>
      <label>GitHub webhook secret <span>{webhook?.secret_configured === true ? 'Configured · hidden' : webhook?.secret_configured === false ? 'Not configured' : 'Status unavailable'}</span><input type="password" autoComplete="new-password" value={webhookSecret} onChange={(e) => setWebhookSecret(e.target.value)} placeholder="Enter a secret to set or rotate" /></label>
      <div className="settings-actions"><button className="primary-button" type="button" disabled={!webhookSecret || !!busy} onClick={() => void updateCredential('webhook_secret', webhookSecret, setWebhookSecret)}>{busy === 'webhook_secret' ? 'Saving…' : 'Save or rotate secret'}</button><button className="secondary-button" type="button" disabled={webhook?.secret_configured !== true || !!busy} onClick={() => void removeCredential('webhook_secret')}>Remove secret</button></div>
      {webhook && <div className="webhook-admin-status"><div><b>Endpoint:</b> <code>{webhook.endpoint_url}</code></div><div><b>Required events:</b> {webhook.required_events.join(', ')}</div><div><b>Reachability:</b> {webhook.public_reachability.replaceAll('_', ' ')}</div><div><b>Signature:</b> {webhook.delivery_authentication}</div>
        {webhook.last_authenticated_delivery && <div><b>Last signed delivery:</b> {String(webhook.last_authenticated_delivery.delivery_id)} · {String(webhook.last_authenticated_delivery.status)}</div>}
        {webhook.last_successful_delivery && <div><b>Last successfully processed:</b> {String(webhook.last_successful_delivery.delivery_id)} · {String(webhook.last_successful_delivery.status)}</div>}
        {!webhook.last_authenticated_delivery && <div>No signed webhook delivery has been received.</div>}
        <div className="webhook-delivery-list">{webhook.recent_deliveries.map((delivery) => <div key={String(delivery.delivery_id)}><b>{String(delivery.repository)}#{String(delivery.pr_number)}</b><span>{String(delivery.status)} · {String(delivery.delivery_id)}</span>{typeof delivery.failure_code === 'string' && <span>Failure: {delivery.failure_code}</span>}</div>)}</div>
      </div>}
      <div className="webhook-delivery-list" aria-label="Retained webhook operations">
        <h3>Recent delivery operations</h3>
        {adminDeliveries.length === 0 && <span>No retained deliveries.</span>}
        {adminDeliveries.map((delivery) => {
          const canRetryDelivery = delivery.retry_available === true
          return <div key={delivery.delivery_id}>
            <b>{delivery.repository || 'Unknown repository'}{delivery.pr_number ? `#${delivery.pr_number}` : ''}</b>
            <span>{delivery.status} · attempt {delivery.attempts} · {delivery.delivery_id}</span>
            {delivery.failure_code && <span>Failure: {delivery.failure_code}</span>}
            {delivery.next_attempt_at && <span>Next attempt: {new Date(delivery.next_attempt_at * 1000).toLocaleString()}</span>}
            {canRetryDelivery && <button className="secondary-button" type="button" disabled={!!busy} onClick={() => void retryDelivery(delivery)}>{busy === `delivery-${delivery.delivery_id}` ? 'Scheduling…' : 'Schedule delivery retry'}</button>}
            {delivery.actions.map((action) => {
              const canRetryAction = action.retry_available === true
              return <div key={action.action_id}>
                <span>Comment {action.action_id}: {action.publication_status}{action.failure_code ? ` · ${action.failure_code}` : ''}</span>
                {action.retry_after && action.publication_status === 'pending' && <span>Scheduled retry window: {new Date(action.retry_after * 1000).toLocaleString()}</span>}
                {canRetryAction && <button className="secondary-button" type="button" disabled={!!busy} onClick={() => void retryAction(delivery.delivery_id, action.action_id)}>{busy === `action-${action.action_id}` ? 'Retrying…' : action.publication_status === 'pending' ? 'Retry comment now' : 'Verify remote state and retry'}</button>}
                {['unknown', 'requires_reconciliation'].includes(action.publication_status) && <span>Reconcile remote state before any retry.</span>}
              </div>
            })}
          </div>
        })}
      </div>
      <p className="github-help">GitHub cannot deliver to a localhost-only server. For remote delivery, configure a reviewed HTTPS tunnel or deployed backend yourself; this application does not expose ports, create tunnels, or alter firewall settings. Required event: pull_request. A configured secret does not mean an endpoint is reachable; only an authenticated signed delivery proves receipt.</p>
      <p className="github-help">For read-only PR review, grant repository Metadata read, Pull requests read, and Contents read. For optional review comments, use a separate least-privilege token with Pull requests write. Never place tokens in URLs or screenshots.</p>
    </section>
    {(error || message) && <div className={error ? 'settings-error' : 'settings-message'} role={error ? 'alert' : 'status'}>{error || message}</div>}
  </div>
}
