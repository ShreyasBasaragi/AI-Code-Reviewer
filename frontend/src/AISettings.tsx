import { useEffect, useState, type FormEvent } from 'react'
import { api, ApiError } from './api'
import type { ProviderConnectivity, ProviderOptions, ProviderSettings } from './types'
import { GitHubIntegrationSettings } from './GitHubIntegrationSettings'

export function AISettings({ initialSection = 'ai' }: { initialSection?: 'ai' | 'github' }) {
  const [section, setSection] = useState<'ai' | 'github'>(initialSection)
  const [authenticated, setAuthenticated] = useState(false)
  const [legacyTokenAvailable, setLegacyTokenAvailable] = useState(false)
  const [authScreen, setAuthScreen] = useState<'loading' | 'setup' | 'login' | 'recovery'>('loading')
  const [adminToken, setAdminToken] = useState('')
  const [legacyLogin, setLegacyLogin] = useState(false)
  const [bootstrap, setBootstrap] = useState('')
  const [recovery, setRecovery] = useState('')
  const [password, setPassword] = useState('')
  const [passwordConfirmation, setPasswordConfirmation] = useState('')
  const [settings, setSettings] = useState<ProviderSettings | null>(null)
  const [options, setOptions] = useState<ProviderOptions | null>(null)
  const [groqKey, setGroqKey] = useState('')
  const [clearGroqKey, setClearGroqKey] = useState(false)
  const [connectivity, setConnectivity] = useState<ProviderConnectivity | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState('')

  async function loadSettings() {
    const [current, available] = await Promise.all([api.getProviderSettings(), api.getProviderOptions()])
    setSettings(current)
    setOptions(available)
    setAuthenticated(true)
    window.dispatchEvent(new Event('admin-session-updated'))
  }

  useEffect(() => {
    let live = true
    const sessionExpired = () => {
      setAuthenticated(false); setAuthScreen('login'); setSettings(null); setOptions(null)
      setError('Administrator session expired. Sign in again to continue.')
    }
    window.addEventListener('admin-session-expired', sessionExpired)
    api.adminSession().then(async (session) => {
      if (!live) return
      setLegacyTokenAvailable(session.legacy_token_available)
      if (session.authenticated) await loadSettings()
      else setAuthScreen(session.setup_required ? 'setup' : 'login')
    }).catch(() => { if (live) { setAuthScreen('login'); setError('Admin session status could not be loaded.') } })
    return () => { live = false; window.removeEventListener('admin-session-expired', sessionExpired) }
  }, [])

  async function login(event: FormEvent) {
    event.preventDefault()
    setError(''); setMessage(''); setBusy('login')
    const secret = legacyLogin ? adminToken : password
    setAdminToken(''); setPassword('')
    try {
      if (legacyLogin) await api.loginAdmin(secret)
      else await api.loginAdminPassword(secret)
      await loadSettings(); setMessage('Authenticated for this browser session.')
    } catch (e) { setError(e instanceof ApiError ? e.message : 'Admin sign-in failed.') }
    finally { setBusy(''); setPasswordConfirmation('') }
  }

  async function completeSetup(event: FormEvent) {
    event.preventDefault(); setBusy('setup'); setError(''); setMessage('')
    const credential = bootstrap; const chosen = password; const confirmation = passwordConfirmation
    setBootstrap(''); setPassword(''); setPasswordConfirmation('')
    try { await api.setupAdmin(credential, chosen, confirmation); await loadSettings(); setMessage('Administrator configured. Store your password safely.') }
    catch (e) { setError(e instanceof ApiError ? e.message : 'Administrator setup failed.') }
    finally { setBusy('') }
  }

  async function recoverPassword(event: FormEvent) {
    event.preventDefault(); setBusy('recovery'); setError(''); setMessage('')
    const credential = recovery; const chosen = password; const confirmation = passwordConfirmation
    setRecovery(''); setPassword(''); setPasswordConfirmation('')
    try { await api.resetAdminPassword(credential, chosen, confirmation); await loadSettings(); setMessage('Password reset. Other administrator sessions were revoked.') }
    catch (e) { setError(e instanceof ApiError ? e.message : 'Password recovery failed.') }
    finally { setBusy('') }
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!settings) return
    setBusy('save'); setError(''); setMessage(''); setConnectivity(null)
    const update: Partial<ProviderSettings> & { groq_api_key?: string } = {
      review_provider: settings.review_provider, fix_provider: settings.fix_provider,
      groq_model: settings.groq_model, qwen_base_model: settings.qwen_base_model,
      qwen_adapter_path: settings.qwen_adapter_path, qwen_quantization: settings.qwen_quantization,
      review_temperature: settings.review_temperature, fix_temperature: settings.fix_temperature,
      max_output_tokens: settings.max_output_tokens, source_sharing_consent: settings.source_sharing_consent,
    }
    if (clearGroqKey) update.groq_api_key = ''
    else if (groqKey) update.groq_api_key = groqKey
    setGroqKey('')
    setClearGroqKey(false)
    try { setSettings(await api.updateProviderSettings(update)); setMessage('Provider settings saved in backend process memory.') }
    catch (e) { if (e instanceof ApiError && e.status === 401) { setAuthenticated(false); setAuthScreen('login') }; setError(e instanceof ApiError ? e.message : 'Settings could not be saved.') }
    finally { setBusy('') }
  }

  async function checkConnection() {
    setBusy('connect'); setError(''); setMessage(''); setConnectivity(null)
    try { setConnectivity(await api.checkProviderConnectivity()) }
    catch (e) { if (e instanceof ApiError && e.status === 401) { setAuthenticated(false); setAuthScreen('login') }; setError(e instanceof ApiError ? e.message : 'Provider connectivity check failed.') }
    finally { setBusy('') }
  }

  async function logout() {
    setBusy('logout')
    try { await api.logoutAdmin() } catch { /* A fresh login is required if server state expired. */ }
    setAuthenticated(false); setSettings(null); setOptions(null); setConnectivity(null); setGroqKey(''); setClearGroqKey(false); setAuthScreen('login'); setBusy('')
    window.dispatchEvent(new Event('admin-session-expired'))
  }

  return <section className="panel ai-settings" aria-labelledby="ai-settings-heading">
    <div className="panel-heading"><div><div className="panel-kicker">ADMINISTRATOR SETTINGS</div><h2 id="ai-settings-heading">{section === 'ai' ? 'AI settings' : 'GitHub Integration'}</h2></div>
      {authenticated && <button className="secondary-button" type="button" onClick={() => void logout()} disabled={!!busy}>Sign out</button>}</div>
    {authenticated && <div className="admin-section-tabs" role="tablist" aria-label="Administrator settings section">
      <button type="button" role="tab" aria-selected={section === 'ai'} className={section === 'ai' ? 'active' : ''} onClick={() => setSection('ai')}>AI Settings</button>
      <button type="button" role="tab" aria-selected={section === 'github'} className={section === 'github' ? 'active' : ''} onClick={() => setSection('github')}>GitHub Integration</button>
    </div>}
    {!authenticated ? authScreen === 'loading' ? <p role="status">Checking administrator session…</p>
      : authScreen === 'setup' ? <form className="settings-form" onSubmit={completeSetup}>
        <p>First-run setup is local-only. Enter the one-time bootstrap credential printed by the backend, then choose a password with at least 12 characters.</p>
        <label htmlFor="bootstrap-credential">One-time bootstrap credential</label><input id="bootstrap-credential" type="password" autoComplete="off" value={bootstrap} onChange={(e) => setBootstrap(e.target.value)} />
        <label htmlFor="new-admin-password">Administrator password</label><input id="new-admin-password" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        <label htmlFor="confirm-admin-password">Confirm password</label><input id="confirm-admin-password" type="password" autoComplete="new-password" value={passwordConfirmation} onChange={(e) => setPasswordConfirmation(e.target.value)} />
        <button className="primary-button" type="submit" disabled={!bootstrap || password.length < 12 || !passwordConfirmation || !!busy}>{busy === 'setup' ? 'Setting up…' : 'Complete administrator setup'}</button>
        {legacyTokenAvailable && <button className="secondary-button" type="button" onClick={() => { setLegacyLogin(true); setAuthScreen('login') }}>Use existing legacy administrator token</button>}
      </form> : authScreen === 'recovery' ? <form className="settings-form" onSubmit={recoverPassword}>
        <p>Recovery credentials are generated locally by <code>python -m mcp_server.admin_cli reset-password</code> and expire after 10 minutes.</p>
        <label htmlFor="recovery-credential">One-time recovery credential</label><input id="recovery-credential" type="password" autoComplete="off" value={recovery} onChange={(e) => setRecovery(e.target.value)} />
        <label htmlFor="recovery-new-password">New administrator password</label><input id="recovery-new-password" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        <label htmlFor="recovery-confirm-password">Confirm password</label><input id="recovery-confirm-password" type="password" autoComplete="new-password" value={passwordConfirmation} onChange={(e) => setPasswordConfirmation(e.target.value)} />
        <button className="primary-button" type="submit" disabled={!recovery || password.length < 12 || !passwordConfirmation || !!busy}>{busy === 'recovery' ? 'Resetting…' : 'Reset password'}</button>
        <button className="secondary-button" type="button" onClick={() => setAuthScreen('login')}>Back to sign in</button>
      </form> : <form className="settings-form" onSubmit={login}>
        <p>Sign in with your administrator password. The browser receives only a short-lived HttpOnly session cookie.</p>
        {legacyLogin ? <><label htmlFor="admin-token">Legacy administrator token</label><input id="admin-token" type="password" autoComplete="off" value={adminToken} onChange={(e) => setAdminToken(e.target.value)} /></>
          : <><label htmlFor="admin-password">Administrator password</label><input id="admin-password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></>}
        <button className="primary-button" type="submit" disabled={!(legacyLogin ? adminToken : password) || !!busy}>{busy === 'login' ? 'Signing in…' : 'Sign in'}</button>
        <button className="secondary-button" type="button" onClick={() => setAuthScreen('recovery')}>Forgot password</button>
        {legacyLogin ? <button className="secondary-button" type="button" onClick={() => { setLegacyLogin(false); setAdminToken('') }}>Use administrator password</button>
          : <button className="secondary-button" type="button" onClick={() => setLegacyLogin(true)}>Use legacy migration token</button>}
      </form> : settings && options ? section === 'github' ? <GitHubIntegrationSettings /> : <form className="settings-form" onSubmit={save}>
      <p>Secrets and settings are held in backend process memory. The Groq key is write-only here and is never returned to the browser.</p>
      <div className="settings-grid">
        <label>Review provider<select value={settings.review_provider} onChange={(e) => setSettings({ ...settings, review_provider: e.target.value as ProviderSettings['review_provider'] })}>{[...new Set([...options.review_providers, settings.review_provider])].map((provider) => <option key={provider} value={provider}>{provider === 'groq' ? 'Groq' : 'Local Qwen'}</option>)}</select></label>
        <label>Fix provider<select value={settings.fix_provider} onChange={(e) => setSettings({ ...settings, fix_provider: e.target.value as ProviderSettings['fix_provider'] })}>{[...new Set([...options.fix_providers, settings.fix_provider])].map((provider) => <option key={provider} value={provider}>{provider === 'groq' ? 'Groq' : 'Local Qwen'}</option>)}</select></label>
        <label>Approved Groq model<select value={settings.groq_model} onChange={(e) => setSettings({ ...settings, groq_model: e.target.value })}>{options.groq_models.map((model) => <option key={model} value={model}>{model}</option>)}</select><small>Server-approved strict structured-output models.</small></label>
        <label>Groq API key{settings.groq_api_key_configured && <small> A key is configured; leave blank to keep it.</small>}<input type="password" autoComplete="new-password" value={groqKey} onChange={(e) => { setGroqKey(e.target.value); setClearGroqKey(false) }} placeholder={settings.groq_api_key_configured ? 'Configured · hidden' : 'Enter key'} />{settings.groq_api_key_configured && <span className="consent-row"><input aria-label="Remove configured Groq key" type="checkbox" checked={clearGroqKey} onChange={(e) => { setClearGroqKey(e.target.checked); if (e.target.checked) setGroqKey('') }} /> Remove the configured key when saving</span>}</label>
        <label>Qwen base model<input value={settings.qwen_base_model} readOnly /></label>
        <label>Approved Qwen adapter path<input value={settings.qwen_adapter_path} onChange={(e) => setSettings({ ...settings, qwen_adapter_path: e.target.value })} /><small>Backend validates this path against configured adapter roots.</small></label>
        <label>Qwen quantization<select value={settings.qwen_quantization} disabled><option value="none">None</option></select></label>
        <label>Max output tokens<input type="number" min="1" max="4096" value={settings.max_output_tokens ?? ''} onChange={(e) => setSettings({ ...settings, max_output_tokens: e.target.value ? Number(e.target.value) : null })} /></label>
        <label>Review temperature<input type="number" min="0" max="1" step="0.05" value={settings.review_temperature} onChange={(e) => setSettings({ ...settings, review_temperature: Number(e.target.value) })} /></label>
        <label>Fix temperature<input type="number" min="0" max="1" step="0.05" value={settings.fix_temperature} onChange={(e) => setSettings({ ...settings, fix_temperature: Number(e.target.value) })} /></label>
      </div>
      <label className="consent-row"><input type="checkbox" checked={settings.source_sharing_consent} onChange={(e) => setSettings({ ...settings, source_sharing_consent: e.target.checked })} /> I explicitly consent to sending submitted source to Groq when Groq is selected.</label>
      <small>Review and fix providers are saved independently. A provider failure is reported; the backend does not silently switch providers.</small>
      <div className="settings-actions"><button className="primary-button" type="submit" disabled={!!busy}>{busy === 'save' ? 'Saving…' : 'Save settings'}</button><button className="secondary-button" type="button" onClick={() => void checkConnection()} disabled={!!busy}>{busy === 'connect' ? 'Checking…' : 'Check provider connection'}</button></div>
      <small>Connection checks run only when clicked. Groq check lists models; it does not run model inference. Qwen readiness inspects artifacts only and never loads weights.</small>
      {connectivity && <div className="connectivity-results" role="status">{(['groq', 'qwen'] as const).map((provider) => <div key={provider}><b>{provider === 'groq' ? 'Groq' : 'Qwen'} · {connectivity[provider].status.replace('_', ' ')}</b><span>{connectivity[provider].message}</span></div>)}</div>}
      <small>Current status: Groq {settings.groq_connectivity.replace('_', ' ')} · Qwen {settings.qwen_readiness.replaceAll('_', ' ')} · secret storage {settings.secret_storage.replaceAll('_', ' ')}</small>
    </form> : <p role="status">Loading provider settings…</p>}
    {(error || message) && <div className={error ? 'settings-error' : 'settings-message'} role={error ? 'alert' : 'status'}>{error || message}</div>}
  </section>
}
