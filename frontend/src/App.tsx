import { useEffect, useMemo, useState } from 'react'
import { api, ApiError, utf8ByteLength } from './api'
import { PythonEditor } from './PythonEditor'
import { AISettings } from './AISettings'
import type { ExportResponse, Finding, FixCandidate, GitHubPRReview, GitHubWebhookActionStatus, Review, VerificationReport } from './types'

const MAX_CODE_BYTES = 512 * 1024
const MAX_CODE_CHARACTERS = 200_000

type ReviewSession = { reviewId: string; review: Review; submittedCode: string; providerIdentity?: string }

function formatBytes(bytes: number) {
  return `${(bytes / (1024 * 1024)).toFixed(2)} MiB`
}

function sanitizeFilename(name: string, extension: '.py' | '.patch') {
  const leaf = name.split(/[\\/]/).pop() || 'candidate'
  const stem = leaf.replace(/\.[^.]*$/, '').replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 64) || 'candidate'
  return `${stem}${extension}`
}

function downloadText(contents: string, filename: string, mime: string) {
  const blob = new Blob([contents], { type: `${mime};charset=utf-8` })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

function Step({ number, title, active, done }: { number: string; title: string; active?: boolean; done?: boolean }) {
  return <div className={`workflow-step ${active ? 'active' : ''} ${done ? 'done' : ''}`}>
    <span className="step-number">{done ? '✓' : number}</span><span>{title}</span>
  </div>
}

export default function App() {
  const [code, setCode] = useState('')
  const [session, setSession] = useState<ReviewSession | null>(null)
  const [selectedFindingId, setSelectedFindingId] = useState('')
  const [candidate, setCandidate] = useState<FixCandidate | null>(null)
  const [verification, setVerification] = useState<VerificationReport | null>(null)
  const [exportResult, setExportResult] = useState<ExportResponse | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [backendStatus, setBackendStatus] = useState<'checking' | 'ready' | 'offline'>('checking')
  const [reviewMode, setReviewMode] = useState<'manual' | 'github'>('manual')
  const [prUrl, setPrUrl] = useState('')
  const [prResult, setPrResult] = useState<GitHubPRReview | null>(null)
  const [accessibleRepos, setAccessibleRepos] = useState<Array<{ full_name: string; owner: string; name: string }>>([])
  const [selectedRepo, setSelectedRepo] = useState('')
  const [selectedPrNumber, setSelectedPrNumber] = useState('')
  const [deliveryId, setDeliveryId] = useState('')
  const [statusToken, setStatusToken] = useState('')
  const [webhookActions, setWebhookActions] = useState<GitHubWebhookActionStatus | null>(null)
  const [webhookStatusError, setWebhookStatusError] = useState('')
  const [activePage, setActivePage] = useState<'review' | 'settings' | 'github'>(() =>
    window.location.pathname === '/settings' || window.location.pathname === '/setup' || window.location.pathname === '/password-reset'
      ? 'settings' : 'review')

  const byteCount = useMemo(() => utf8ByteLength(code), [code])
  const selectedFinding = session?.review.findings.find((finding) => finding.finding_id === selectedFindingId) || null
  const tooLarge = byteCount > MAX_CODE_BYTES || code.length > MAX_CODE_CHARACTERS

  useEffect(() => {
    let live = true
    api.health().then(() => { if (live) setBackendStatus('ready') })
      .catch(() => { if (live) setBackendStatus('offline') })
    return () => { live = false }
  }, [])

  function updateCode(value: string) {
    setCode(value)
    if (session && value !== session.submittedCode) {
      setSession(null)
      setSelectedFindingId('')
      setCandidate(null)
      setVerification(null)
      setExportResult(null)
      setNotice('Source changed. The previous review and candidate were cleared.')
    }
    setError('')
  }

  function resetWorkspace() {
    setCode('')
    setSession(null)
    setSelectedFindingId('')
    setCandidate(null)
    setVerification(null)
    setExportResult(null)
    setError('')
    setNotice('Workspace cleared.')
  }

  async function reviewCode() {
    setError('')
    setNotice('')
    if (!code.trim()) { setError('Add Python code before starting a review.'); return }
    if (tooLarge) {
      setError(code.length > MAX_CODE_CHARACTERS
        ? 'The API limit is 200,000 characters and 512 KiB when encoded as UTF-8.'
        : 'The code exceeds the 512 KiB UTF-8 size limit.')
      return
    }
    setBusy('review')
    try {
      const result = await api.submitReview(code)
      setSession({ reviewId: result.review_id, review: result.review, submittedCode: code, providerIdentity: result.provider_identity })
      const firstFinding = result.review.findings[0]
      setSelectedFindingId(firstFinding?.finding_id || '')
      setCandidate(null)
      setVerification(null)
      setExportResult(null)
      setNotice(result.review.findings.length
        ? `Review complete · ${result.review.findings.length} finding${result.review.findings.length === 1 ? '' : 's'}`
        : 'Review complete · no findings returned.')
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : 'The review request failed.')
    } finally {
      setBusy('')
    }
  }

  async function analyzePullRequest() {
    setError('')
    setNotice('')
    setPrResult(null)
    if (!/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/[1-9][0-9]*\/?$/.test(prUrl.trim())) {
      setError('Enter a canonical https://github.com/owner/repository/pull/number URL.')
      return
    }
    setBusy('github')
    try {
      const result = await api.analyzeGitHubPR(prUrl.trim())
      setPrResult(result)
      setNotice(result.completion_status === 'complete'
        ? 'Pull request review complete.'
        : result.completion_status === 'empty_pr'
          ? 'This pull request has no changed files to review.'
        : result.completion_status === 'no_supported_python'
          ? 'No supported Python files were found in this pull request.'
          : result.completion_status === 'failed'
            ? 'The pull request could not be reviewed. Check per-file results.'
            : 'Partial review complete. Check skipped files and mapping notes.')
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : 'The pull request review failed.')
    } finally {
      setBusy('')
    }
  }

  async function discoverPRRepositories() {
    setError(''); setNotice(''); setBusy('repo-discovery')
    try {
      const result = await api.listGitHubRepositories()
      setAccessibleRepos(result.repositories)
      setNotice(`${result.repositories.length} repositories are accessible to the configured GitHub token.`)
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : 'Repository discovery failed. Configure administrator GitHub credentials first.')
    } finally { setBusy('') }
  }

  function applySelectedRepository() {
    const number = Number(selectedPrNumber)
    const repository = accessibleRepos.find((item) => item.full_name === selectedRepo)
    if (!repository || !Number.isSafeInteger(number) || number < 1) {
      setError('Select a discovered repository and enter a valid pull request number.')
      return
    }
    setPrUrl(`https://github.com/${repository.full_name}/pull/${number}`)
    setError('')
  }

  async function inspectWebhookActions() {
    setWebhookStatusError('')
    setWebhookActions(null)
    if (!deliveryId.trim() || !statusToken.trim()) {
      setStatusToken('')
      setWebhookStatusError('Enter a delivery ID and the configured webhook status token.')
      return
    }
    const token = statusToken.trim()
    setStatusToken('')
    setBusy('webhook-status')
    try {
      setWebhookActions(await api.getWebhookActions(deliveryId.trim(), token))
    } catch (requestError) {
      setWebhookStatusError(requestError instanceof ApiError ? requestError.message : 'Webhook action status could not be loaded.')
    } finally {
      setBusy('')
    }
  }

  function chooseFinding(finding: Finding) {
    setSelectedFindingId(finding.finding_id)
    setCandidate(null)
    setVerification(null)
    setExportResult(null)
    setError('')
    setNotice('')
  }

  async function generateFix() {
    if (!session || !selectedFinding || selectedFinding.abstained) return
    setBusy('generate')
    setError('')
    setNotice('')
    try {
      const result = await api.generateFix(session.reviewId, selectedFinding.finding_id)
      setCandidate(result)
      setVerification(null)
      setExportResult(null)
      setNotice('Candidate generated. Review the complete code and diff before verification.')
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : 'Fix generation failed.')
    } finally {
      setBusy('')
    }
  }

  async function verifyFix() {
    if (!candidate) return
    setBusy('verify')
    setError('')
    setNotice('')
    try {
      const result = await api.verifyFix(candidate.candidate_id)
      setVerification(result)
      setCandidate({ ...candidate, verification_status: result.verification_status, verification_report: result })
      setNotice(result.verification_status === 'syntax_passed'
        ? 'Syntax and static verification passed. Behavioral tests were not run.'
        : result.verification_status === 'static_analysis_failed'
          ? 'Static verification found newly introduced issues.'
          : result.verification_status === 'verification_error'
            ? 'Verification could not complete. Review the reported errors.'
            : 'Python syntax check did not pass.')
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : 'Verification failed.')
    } finally {
      setBusy('')
    }
  }

  async function decide(action: 'accept' | 'reject') {
    if (!candidate) return
    setBusy(action)
    setError('')
    setNotice('')
    try {
      const result = action === 'accept'
        ? await api.acceptFix(candidate.candidate_id)
        : await api.rejectFix(candidate.candidate_id)
      setCandidate(result.candidate)
      setExportResult(null)
      setNotice(result.message)
      if (result.error || result.outcome === 'blocked') setError(result.message)
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : `Could not ${action} this candidate.`)
    } finally {
      setBusy('')
    }
  }

  async function exportFix() {
    if (!candidate) return
    setBusy('export')
    setError('')
    setNotice('')
    try {
      const result = await api.exportFix(candidate.candidate_id)
      setExportResult(result)
      const base = sanitizeFilename(result.filename || 'candidate.py', '.py').replace(/\.py$/, '')
      downloadText(result.corrected_code, `${base}.py`, 'text/x-python')
      downloadText(result.unified_diff, `${base}.patch`, 'text/x-diff')
      setNotice('Python source and backend-generated diff downloaded.')
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : 'Export failed.')
    } finally {
      setBusy('')
    }
  }

  const evidenceById = new Map((session?.review.evidence || []).map((item) => [item.evidence_id, item]))
  const canAccept = candidate?.approval_status === 'pending'
    && verification?.verification_status === 'syntax_passed'
    && verification.static_analysis_status === 'passed'
    && (verification.introduced_static_issues?.length || 0) === 0
  const currentStep = !session ? 1 : !candidate ? 2 : !verification ? 3 : candidate.approval_status === 'pending' ? 4 : 5

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand-lockup"><div className="brand-mark"><span>c</span><i /></div><div><strong>critique</strong><small>AI CODE REVIEWER</small></div></div>
      <div className="sidebar-section-label">WORKSPACE</div>
      <nav className="side-nav" aria-label="Main navigation">
        <button className={`nav-item ${activePage === 'review' ? 'selected' : ''}`} type="button" onClick={() => setActivePage('review')}><span className="nav-symbol">⌘</span>Code review</button>
        <button className={`nav-item ${activePage === 'settings' ? 'selected' : ''}`} type="button" onClick={() => setActivePage('settings')}><span className="nav-symbol">⚙</span>AI Settings</button>
        <button className={`nav-item ${activePage === 'github' ? 'selected' : ''}`} type="button" onClick={() => setActivePage('github')}><span className="nav-symbol">◈</span>GitHub Integration</button>
      </nav>
      <div className="sidebar-divider" />
      <div className="sidebar-section-label">CURRENT SESSION</div>
      <div className="session-card">
        <span className="session-dot" />
        <div><strong>Python review</strong><small>{session ? `Review ${session.reviewId.slice(0, 8)}` : 'Ready for your code'}</small></div>
      </div>
      <div className="sidebar-bottom">
        <div className={`connection-state ${backendStatus}`}><span />{backendStatus === 'ready' ? 'API connected' : backendStatus === 'offline' ? 'API unavailable' : 'Connecting to API'}</div>
        <div className="sidebar-footnote">Private workspace<br />Code stays in this session</div>
      </div>
    </aside>

    <main id="review" className="main-area">
      <header className="topbar">
        <div className="breadcrumbs"><span>Workspace</span><b>/</b><strong>{activePage === 'review' ? 'Code review' : activePage === 'github' ? 'GitHub Integration' : 'AI Settings'}</strong></div>
        <div className="topbar-right"><span className="python-pill"><span className="python-dot">●</span> Python only</span><button className="avatar" aria-label="User profile">S</button></div>
      </header>

      <div className="content-wrap">
        {activePage !== 'review' ? <AISettings initialSection={activePage === 'github' ? 'github' : 'ai'} /> : <>
        <section className="page-intro">
          <div className="eyebrow"><span className="eyebrow-dot" /> EVIDENCE-GROUNDED REVIEW</div>
          <h1>Ship code with <em>confidence.</em></h1>
          <p>Review Python with relevant evidence, inspect a proposed fix, and decide what happens next.</p>
        </section>

        <div className="review-mode-switch" role="tablist" aria-label="Review source">
          <button role="tab" aria-selected={reviewMode === 'manual'} className={reviewMode === 'manual' ? 'active' : ''} onClick={() => { setReviewMode('manual'); setError(''); setNotice('') }}>Manual code</button>
          <button role="tab" aria-selected={reviewMode === 'github'} className={reviewMode === 'github' ? 'active' : ''} onClick={() => { setReviewMode('github'); setError(''); setNotice('') }}>GitHub pull request</button>
        </div>

        {reviewMode === 'github' && <section className="panel pr-panel">
          <div className="panel-heading"><div><div className="panel-kicker">READ-ONLY GITHUB REVIEW</div><h2>Review a pull request</h2></div></div>
          <form className="pr-form" onSubmit={(event) => { event.preventDefault(); void analyzePullRequest() }}>
            <label htmlFor="github-pr-url">GitHub pull request URL</label>
            <div className="settings-actions"><button className="secondary-button" type="button" disabled={!!busy} onClick={() => void discoverPRRepositories()}>{busy === 'repo-discovery' ? 'Discovering…' : 'Discover accessible repositories'}</button></div>
            {accessibleRepos.length > 0 && <div className="pr-repository-select"><label htmlFor="pr-repository">Configured repository</label><select id="pr-repository" value={selectedRepo} onChange={(event) => setSelectedRepo(event.target.value)}><option value="">Choose repository…</option>{accessibleRepos.map((repo) => <option key={repo.full_name} value={repo.full_name}>{repo.full_name}</option>)}</select><label htmlFor="pr-number">PR number</label><input id="pr-number" type="number" min="1" step="1" value={selectedPrNumber} onChange={(event) => setSelectedPrNumber(event.target.value)} /><button className="secondary-button" type="button" onClick={applySelectedRepository}>Use selected repository</button></div>}
            <div className="pr-input-row"><input id="github-pr-url" type="url" autoComplete="url" placeholder="https://github.com/owner/repository/pull/123" value={prUrl} onChange={(event) => { setPrUrl(event.target.value); setPrResult(null) }} disabled={busy === 'github'} />
              <button className="primary-button" type="submit" disabled={busy === 'github' || !prUrl.trim()}>{busy === 'github' ? <><span className="spinner" /> Analyzing…</> : <>Analyze PR <span aria-hidden="true">→</span></>}</button>
            </div>
            <small>Public repositories can be read without a token. Private repositories need a backend GITHUB_TOKEN with read access. Manual analysis is always read-only; webhook comment publication is separately configured and off by default. Autonomous fixing and commit execution are disabled.</small>
          </form>
          <div className="pr-form webhook-status-form">
            <div className="panel-kicker">WEBHOOK ACTION STATUS</div>
            <label htmlFor="webhook-delivery-id">Delivery ID</label>
            <div className="pr-input-row"><input id="webhook-delivery-id" value={deliveryId} onChange={(event) => { setDeliveryId(event.target.value); setWebhookActions(null) }} placeholder="GitHub delivery UUID" autoComplete="off" />
              <input id="webhook-status-token" type="password" value={statusToken} onChange={(event) => setStatusToken(event.target.value)} placeholder="Status token (not saved)" autoComplete="off" />
              <button className="secondary-button" type="button" disabled={busy === 'webhook-status'} onClick={() => void inspectWebhookActions()}>{busy === 'webhook-status' ? 'Loading…' : 'Check status'}</button>
            </div>
            <small>The status token is sent only with this request and cleared afterward. Webhook results stay behind the backend status-token check.</small>
            {webhookStatusError && <div className="processing-errors" role="alert">{webhookStatusError}</div>}
            {webhookActions && <div className="webhook-action-status" role="status">
              <strong>{webhookActions.repository}#{webhookActions.pr_number} · {webhookActions.status}</strong>
              {(webhookActions.head_sha || webhookActions.base_sha) && <small>Base {webhookActions.base_sha?.slice(0, 12) || 'unknown'} · head {webhookActions.head_sha?.slice(0, 12) || 'unknown'}</small>}
              {webhookActions.failure_code && <small>Delivery failure: {webhookActions.failure_code}</small>}
              <div>Review mode: {webhookActions.actions.length ? 'Policy-evaluated' : 'Review-only'} · Autonomous commits: Disabled</div>
              {webhookActions.actions.length === 0 && <small>No GitHub action was recorded for this delivery.</small>}
              {webhookActions.actions.map((action) => {
                const label = action.action_type === 'review_comment' && action.publication_status === 'published' ? 'Published comment'
                  : action.action_type === 'review_comment' && ['failed', 'unknown'].includes(action.publication_status) ? 'Failed or unconfirmed GitHub action'
                    : action.action_type === 'review_comment' && action.publication_status === 'denied' ? 'Comment blocked by policy'
                    : action.action_type === 'autonomous_fix' && action.policy_decision.action === 'potential_autonomous_fix' ? 'Fix pending required verification'
                      : action.action_type === 'autonomous_fix' ? 'Fix blocked by policy'
                        : action.policy_decision.action === 'no_external_action' ? 'No external action · finding validation incomplete'
                          : action.action_type === 'policy_decision' ? 'Review-only · action denied by policy'
                        : action.publication_status === 'skipped' || action.publication_status === 'stale' ? 'Comment skipped or stale'
                          : 'Comment action pending'
                return <div className="webhook-action-row" key={action.action_id}>
                  <b>{label}</b><small>Action {action.action_id} · {action.finding_id} · {action.lifecycle_state || action.publication_status.replaceAll('_', ' ')}</small>
                  <small>Verification: {action.verification_status || 'not reported'} · GitHub comment: {action.github_comment_id ?? 'none'} · Commit: disabled</small>
                  {action.failure_code && <small>Failure: {action.failure_code}</small>}
                  {action.policy_decision.denial_reasons.length > 0 && <small>{action.policy_decision.denial_reasons.join(', ')}</small>}
                </div>
              })}
            </div>}
          </div>
          {(error || notice) && <div className={`toast ${error ? 'error' : 'success'}`} role={error ? 'alert' : 'status'}>
            <span>{error ? '!' : '✓'}</span><p>{error || notice}</p><button aria-label="Dismiss message" onClick={() => { setError(''); setNotice('') }}>×</button>
          </div>}
          {prResult && <div className="pr-results">
            <div className="pr-result-heading"><div><span className={`pr-completion ${prResult.completion_status}`}>{prResult.completion_status.replaceAll('_', ' ')}</span><h3><a href={prResult.html_url} target="_blank" rel="noreferrer">{prResult.repository}#{prResult.number} · {prResult.title}</a></h3><small>PR state: {prResult.state} · head {prResult.head_sha.slice(0, 12)}</small></div>
              <div className="pr-stats"><span>{prResult.files_reviewed}<small>Reviewed</small></span><span>{prResult.files_skipped}<small>Skipped</small></span><span>{prResult.files_failed}<small>Failed</small></span></div>
            </div>
            <div className="pr-status-note">{prResult.total_changed_files} changed files · {prResult.files_received} retrieved · {Object.values(prResult.findings_by_severity).reduce((sum, count) => sum + count, 0)} findings · Provider/model: {prResult.provider_identity || 'not reported'}</div>
            <div className="pr-status-note" role="status">
              Manual PR review: Review-only · Source sharing: {prResult.action_policy?.source_sharing_authorization || 'not reported'} · Webhook comments: {prResult.action_policy?.webhook_comment_publication === 'enabled' ? 'Enabled by administrator policy' : 'Disabled'} · Fix preparation: {prResult.action_policy?.autonomous_fix_preparation || 'Disabled'} · Automatic commits: Disabled
            </div>
            {(prResult.pagination_truncated || prResult.pagination_error) && <div className="processing-errors"><strong>Changed-file list was incomplete</strong><div>{prResult.pagination_error || 'Configured pagination or file limit reached.'}</div></div>}
            {prResult.files.map((file) => {
              const evidenceById = new Map(file.evidence.map((item) => [item.evidence_id, item]))
              return <article className="pr-file" key={file.path}>
                <div className="pr-file-heading"><div><strong>{file.path}</strong>{file.previous_filename && <small>Renamed from {file.previous_filename}</small>}</div><span className={`pr-file-status ${file.review_status}`}>{file.review_status}</span></div>
                <div className="pr-file-meta">{file.status} · +{file.additions} / −{file.deletions} · diff {file.diff_status}</div>
                {file.static_analysis_status && <div className="pr-file-meta">Static analysis · {file.static_analysis_status}{file.static_analysis_error?.line ? ` · parse error at line ${file.static_analysis_error.line}` : ''}</div>}
                {file.skip_reason && <div className="pr-file-note">{file.review_status === 'skipped' ? `Skipped: ${file.skip_reason}` : `Review note: ${file.skip_reason}`}</div>}
                {file.error_code && <div className="pr-file-note">File review failed: {file.error_code}</div>}
                {file.review_summary && <p className="pr-file-summary">{file.review_summary}</p>}
                {file.findings.map((finding) => <div className="pr-finding" key={finding.finding_id}>
                  <div className="pr-finding-heading"><strong>{finding.title}</strong><span>{finding.severity || finding.risk_priority || finding.issue_type}</span></div>
                  <small>{finding.origin === 'static' ? 'Static Analysis' : finding.also_detected_by?.includes('static') ? 'AI/RAG + Static Analysis' : 'AI/RAG'}{finding.rule_id ? ` · ${finding.rule_id}` : ''}{finding.source_line ? ` · line ${finding.source_line}` : ''}</small>
                  <p>{finding.explanation}</p>
                  <div className="pr-line-status">{finding.is_in_diff === true ? finding.inline_supported ? 'Changed line · valid new-side location' : 'Changed line · not eligible for inline comment' : finding.is_in_diff === false ? 'Outside changed lines · contextual finding only' : 'Diff location unavailable · not eligible for inline comment'}{finding.is_new_in_pr === null ? ' · newness unknown' : finding.is_new_in_pr === false ? ' · pre-existing/contextual' : ''}</div>
                  {!!finding.evidence_ids.length && <div className="pr-evidence">{finding.evidence_ids.map((id) => { const evidence = evidenceById.get(id); return <div key={id}><b>{evidence?.source_family || 'Evidence'} · {id}</b><p>{evidence?.text || 'Evidence text unavailable.'}</p></div> })}</div>}
                </div>)}
              </article>
            })}
          </div>}
        </section>}

        {reviewMode === 'manual' && <>
        <section className="workflow-strip" aria-label="Review workflow">
          <Step number="01" title="Review" active={currentStep === 1} done={!!session} />
          <span className="step-connector" />
          <Step number="02" title="Select finding" active={currentStep === 2} done={!!candidate} />
          <span className="step-connector" />
          <Step number="03" title="Verify fix" active={currentStep === 3} done={verification?.verification_status === 'syntax_passed'} />
          <span className="step-connector" />
          <Step number="04" title="Decide & export" active={currentStep >= 4} done={candidate?.approval_status === 'accepted'} />
        </section>

        {(error || notice) && <div className={`toast ${error ? 'error' : 'success'}`} role={error ? 'alert' : 'status'}>
          <span>{error ? '!' : '✓'}</span><p>{error || notice}</p><button aria-label="Dismiss message" onClick={() => { setError(''); setNotice('') }}>×</button>
        </div>}

        <div className="workspace-grid">
          <section className="panel editor-panel">
            <div className="panel-heading">
              <div><div className="panel-kicker">SOURCE INPUT</div><h2>Code editor</h2></div>
              <div className="editor-heading-actions"><span className="file-chip"><span>PY</span> untitled.py</span><button className="icon-button" title="Clear code" aria-label="Clear code" onClick={resetWorkspace}>⌫</button></div>
            </div>
            <div className="editor-toolbar"><span><i className="live-dot" /> Python 3</span><span>UTF-8</span><span>Spaces: 4</span></div>
            <PythonEditor value={code} onChange={updateCode} placeholder={'# Paste Python code here\n\n'} />
            <div className="editor-footer"><span>{code ? `${code.split('\n').length} lines` : 'Start with an empty file'}</span><span className={tooLarge ? 'size-over' : ''}>{formatBytes(byteCount)} / 0.50 MiB <i>·</i> {code.length.toLocaleString()} / 200,000 chars</span></div>
            {tooLarge && <div className="inline-validation">Input exceeds the API limit. Reduce the source before reviewing.</div>}
            <div className="editor-actions"><div className="privacy-note"><span>▣</span> Review only · no files changed</div><button className="primary-button" onClick={reviewCode} disabled={!!busy || !code.trim() || tooLarge}>
              {busy === 'review' ? <><span className="spinner" /> Reviewing…</> : <>Review code <span aria-hidden="true">→</span></>}
            </button></div>
          </section>

          <section className="panel findings-panel" id="evidence">
            <div className="panel-heading findings-heading"><div><div className="panel-kicker">ANALYSIS</div><h2>Review findings <span className="count-badge">{session?.review.findings.length ?? '—'}</span></h2></div>
              {session && <span className="review-id">ID {session.reviewId.slice(0, 8)}</span>}
            </div>
            {!session ? <div className="empty-analysis"><div className="empty-icon"><span>⌕</span></div><h3>Your review will appear here</h3><p>Add Python code and start a review. Findings include evidence references when relevant sources are available.</p><div className="empty-hint"><span>↳</span> No code is sent until you choose Review code</div></div> : <div className="analysis-content">
              <div className="review-summary"><div className="summary-icon">✳</div><div><span>REVIEW SUMMARY</span><p>{session.review.summary || 'The review returned without a summary.'}</p><small>Provider/model: {session.providerIdentity || 'not reported'}</small></div></div>
              {session.review.static_analysis_status && <div className="processing-errors"><strong>Static analysis · {session.review.static_analysis_status}</strong>
                {session.review.static_analysis_error && <div>{session.review.static_analysis_error.error_type || 'Parse error'}{session.review.static_analysis_error.message ? ' · ' + session.review.static_analysis_error.message : ''}{session.review.static_analysis_error.line ? ' · line ' + session.review.static_analysis_error.line : ''}</div>}
              </div>}
              {!!session.review.processing_errors.length && <div className="processing-errors"><strong>Processing notes</strong>{session.review.processing_errors.map((item, i) => <div key={i}>{item}</div>)}</div>}
              {session.review.findings.length ? <>
                <div className="findings-list-label">FINDINGS <span>{session.review.findings.length}</span></div>
                <div className="finding-list">{session.review.findings.map((finding, index) => <button key={finding.finding_id} className={`finding-row ${selectedFindingId === finding.finding_id ? 'selected' : ''}`} onClick={() => chooseFinding(finding)}>
                  <span className={`finding-index ${finding.issue_type}`}>{String(index + 1).padStart(2, '0')}</span><span className="finding-main"><strong>{finding.title}</strong><small>{finding.issue_type} {finding.line_start ? `· line ${finding.line_start}` : ''}</small><small>{finding.origin === 'static' ? 'Static Analysis' : finding.also_detected_by?.includes('static') ? 'AI/RAG + Static Analysis' : 'AI/RAG'}{finding.rule_id ? ` · ${finding.rule_id}` : ''}{finding.severity ? ` · ${finding.severity}` : ''}</small></span><span className="finding-chevron">›</span>
                </button>)}</div>
              </> : <div className="no-findings"><span>✓</span><div><strong>No findings returned</strong><small>The review found no actionable issues in this submission.</small></div></div>}
              {selectedFinding && <div className="selected-finding">
                <div className="detail-topline"><span>SELECTED FINDING</span>{selectedFinding.uncertain && <span className="uncertain-tag">Uncertain</span>}</div>
                <h3>{selectedFinding.title}</h3><p className="finding-explanation">{selectedFinding.explanation}</p>
                <div className="recommendation"><span>RECOMMENDED FIX</span><p>{selectedFinding.recommended_fix}</p></div>
                {selectedFinding.abstained && <div className="abstained-note">The reviewer abstained from proposing a fix for this finding.</div>}
                {!!selectedFinding.evidence_ids.length && <div className="evidence-block"><span className="evidence-heading">EVIDENCE <i>{selectedFinding.evidence_ids.length}</i></span>
                  {selectedFinding.evidence_ids.map((id) => { const evidence = evidenceById.get(id); return <div className="evidence-card" key={id}><div className="evidence-card-top"><b>{evidence?.source_family || 'Source evidence'}</b><code>{id}</code></div>
                    {(evidence?.rule_identifier || evidence?.source_url) && <div className="evidence-meta">{evidence.rule_identifier}{evidence.rule_identifier && evidence.source_url ? ' · ' : ''}{evidence.source_url}</div>}
                    {evidence?.text && <p>{evidence.text}</p>}</div> })}
                </div>}
                <button className="secondary-button generate-button" onClick={generateFix} disabled={!!busy || selectedFinding.abstained}>
                  {busy === 'generate' ? <><span className="spinner dark" /> Generating candidate…</> : <>✦ Generate fix candidate <span>→</span></>}
                </button>
              </div>}
            </div>}
          </section>
        </div>

        {candidate && <section className="panel fix-panel">
          <div className="panel-heading fix-heading"><div><div className="panel-kicker">FIX CANDIDATE</div><h2>Review the proposed change</h2><p className="candidate-id">{candidate.candidate_id} · Provider/model: {candidate.provider_identity || 'not reported'}</p></div>
            <div className={`approval-pill ${candidate.approval_status}`}><span />{candidate.approval_status}</div>
          </div>
          <div className="fix-explanation"><div><span>WHY THIS CHANGED</span><p>{candidate.explanation}</p></div>{candidate.assumptions.length > 0 && <div><span>ASSUMPTIONS & TRADE-OFFS</span><ul>{candidate.assumptions.map((assumption, i) => <li key={i}>{assumption}</li>)}</ul></div>}</div>
          <div className="diff-toolbar"><div><strong>Change preview</strong><span>Backend-generated unified diff</span></div><div className="diff-legend"><span className="add-dot" /> Added <span className="remove-dot" /> Removed</div></div>
          <div className="diff-view" aria-label="Unified diff">{verification?.unified_diff ? verification.unified_diff.split('\n').map((line, index) => {
            const cls = line.startsWith('+++') || line.startsWith('---') ? 'diff-file' : line.startsWith('+') ? 'diff-add' : line.startsWith('-') ? 'diff-remove' : line.startsWith('@@') ? 'diff-hunk' : ''
            return <div className={`diff-line ${cls}`} key={index}><span className="diff-line-number">{line.startsWith('+') ? '+' : line.startsWith('-') ? '−' : ' '}</span><code>{line || ' '}</code></div>
          }) : <div className="diff-empty">Generate a verification report to view the backend diff.</div>}</div>
          <div className="code-compare">
            <div className="code-view"><div className="code-view-heading"><span><i className="remove-dot" /> Original source</span><small>UNCHANGED</small></div><PythonEditor label="Original source code" value={candidate.original_code} onChange={() => undefined} readOnly /></div>
            <div className="code-view"><div className="code-view-heading"><span><i className="add-dot" /> Corrected candidate</span><small>NOT APPLIED</small></div><PythonEditor label="Corrected candidate code" value={candidate.corrected_code || ''} onChange={() => undefined} readOnly /></div>
          </div>
          <div className="verify-row"><div className={`syntax-status ${verification?.syntax_check.status || 'not-run'}`}><span>{verification?.syntax_check.status === 'passed' ? '✓' : verification?.syntax_check.status === 'failed' || verification?.syntax_check.status === 'errored' ? '!' : '○'}</span><div><strong>{verification?.syntax_check.status === 'passed' ? 'Python syntax passed' : verification?.verification_status === 'syntax_failed' ? 'Python syntax failed' : verification?.syntax_check.status === 'errored' ? 'Syntax check errored' : 'Syntax not checked'}</strong>
            {verification?.syntax_check.status === 'failed' && <small>{verification.syntax_check.error_type}: {verification.syntax_check.message}{verification.syntax_check.line ? ` · line ${verification.syntax_check.line}${verification.syntax_check.column ? `, column ${verification.syntax_check.column}` : ''}` : ''}</small>}
            {verification?.verification_status === 'syntax_passed' && <small>AST parsing passed. Behavioral tests were not run.</small>}
          </div></div><button className="secondary-button verify-button" onClick={verifyFix} disabled={!!busy}>{busy === 'verify' ? <><span className="spinner dark" /> Checking…</> : 'Verify syntax & static analysis'}</button></div>
          <div className="not-run-row"><span>Static verification <b>{verification?.static_analysis_status || 'Not Run'}</b></span><span>Behavioral testing <b>Not Run</b></span><small>Candidate code is never executed by this workflow.</small></div>
          {!!verification?.introduced_static_issues?.length && <div className="processing-errors"><strong>New static-analysis issues</strong>{verification.introduced_static_issues.map((issue) => <div key={issue.finding_id}>{issue.rule_id} · line {issue.line_start}: {issue.title}</div>)}</div>}
          {!!verification?.pre_existing_static_issues?.length && <div className="processing-errors"><strong>Pre-existing static-analysis issues</strong>{verification.pre_existing_static_issues.map((issue) => <div key={issue.finding_id}>{issue.rule_id} · line {issue.line_start}: {issue.title}</div>)}</div>}
          {!!verification?.static_analysis_errors?.length && <div className="processing-errors"><strong>Static-analysis errors</strong>{verification.static_analysis_errors.map((issue, i) => <div key={i}>{issue.source || 'Source'}: {issue.error_type || 'Error'}{issue.message ? ` · ${issue.message}` : ''}</div>)}</div>}
          <div className="approval-actions"><div><strong>Approval is your decision</strong><small>Accepting does not apply this code or guarantee it behaves correctly.</small></div><div className="approval-buttons">
            <button className="reject-button" onClick={() => decide('reject')} disabled={!!busy || candidate.approval_status !== 'pending'}>{busy === 'reject' ? 'Rejecting…' : 'Reject'}</button>
            <button className="accept-button" onClick={() => decide('accept')} disabled={!!busy || !canAccept}>{busy === 'accept' ? 'Accepting…' : 'Accept candidate'}</button>
          </div></div>
          {candidate.approval_status === 'accepted' && <div className="export-row"><div><span className="export-check">✓</span><div><strong>Candidate accepted</strong><small>Download both the corrected Python source and the backend diff.</small></div></div><button className="primary-button export-button" onClick={exportFix} disabled={!!busy}>{busy === 'export' ? <><span className="spinner" /> Preparing…</> : <>↓ Export files</>}</button></div>}
          {candidate.approval_status === 'rejected' && <div className="rejected-note">Candidate rejected. Original source and review remain unchanged.</div>}
          {exportResult && <div className="export-confirmation" role="status">Export prepared for <code>{sanitizeFilename(exportResult.filename, '.py')}</code> and its `.patch` diff.</div>}
        </section>}
        </>}
        </>}
        <footer className="page-footer"><span>CRITIQUE <i>·</i> EVIDENCE-GROUNDED CODE REVIEW</span><span>Syntax checks do not replace tests or human review.</span></footer>
      </div>
    </main>
  </div>
}
