export type Evidence = {
  evidence_id: string | null
  source_family: string | null
  source_url: string | null
  rule_identifier: string | null
  text: string
  language?: string | null
  metadata?: Record<string, unknown>
}

export type Finding = {
  finding_id: string
  title: string
  issue_type: string
  explanation: string
  recommended_fix: string
  affected_file: string | null
  line_start: number | null
  line_end: number | null
  corrected_code: string | null
  evidence_ids: string[]
  verification_status: string
  uncertain: boolean
  abstained: boolean
  risk_priority: string | null
  origin?: 'static' | 'llm'
  rule_id?: string | null
  severity?: string | null
  also_detected_by?: string[]
}

export type Review = {
  valid: boolean
  summary: string
  findings: Finding[]
  evidence: Evidence[]
  processing_errors: string[]
  static_analysis_status?: 'passed' | 'failed' | 'errored' | 'not_run'
  static_analysis_error?: { error_type?: string | null; message?: string | null; line?: number | null; column?: number | null } | null
}

export type ReviewCreated = { review_id: string; review: Review; provider_identity?: string }

export type PRFinding = Finding & {
  path: string
  source_line: number | null
  is_in_diff: boolean | null
  is_new_in_pr: boolean | null
  inline_supported: boolean
  diff_line: number | null
  change_relation: string
}

export type PRFileReview = {
  path: string
  status: string
  additions: number
  deletions: number
  review_status: 'reviewed' | 'partial' | 'failed' | 'skipped'
  findings: PRFinding[]
  evidence: Evidence[]
  previous_filename: string | null
  skip_reason: string | null
  error_code: string | null
  diff_status: 'mapped' | 'missing' | 'invalid'
  diff_error: string | null
  review_summary: string
  static_analysis_status?: 'passed' | 'failed' | 'errored' | 'not_run'
  static_analysis_error?: { error_type?: string | null; message?: string | null; line?: number | null; column?: number | null } | null
}

export type GitHubPRReview = {
  provider_identity?: string
  repository: string
  owner: string
  repository_name: string
  number: number
  title: string
  state: string
  base_sha: string
  head_sha: string
  html_url: string
  total_changed_files: number
  files_received: number
  files_reviewed: number
  files_skipped: number
  files_failed: number
  completion_status: 'complete' | 'partial' | 'failed' | 'no_supported_python' | 'empty_pr' | 'stale'
  pagination_truncated: boolean
  pagination_error: string | null
  findings_by_severity: Record<string, number>
  files: PRFileReview[]
  repository_context?: {
    status: string
    revision?: string
    files?: Array<{ path: string; revision?: string; bytes?: number; truncated?: boolean }>
    warnings?: string[]
    total_bytes?: number
  }
  head_repository?: string | null
  head_branch?: string | null
  stale?: boolean
  stale_reason?: string | null
  action_policy?: {
    manual_pr_review: 'review_only'
    webhook_comment_publication: 'enabled' | 'disabled'
    autonomous_fix_preparation: 'enabled by administrator policy' | 'disabled'
    autonomous_fixing: 'enabled' | 'disabled'
    candidate_staging_enabled: 'true' | 'false'
  commit_execution_enabled: 'true' | 'false'
    repository_source_sharing?: 'allowed' | 'blocked'
    source_sharing_authorization?: 'authorized' | 'not_authorized' | 'local_provider_no_external_sharing'
  }
  analyzed_at: number
}

export type GitHubActionRecord = {
  action_id: string
  finding_id: string
  action_type: 'review_comment' | 'autonomous_fix' | 'policy_decision'
  publication_status: string
  policy_decision: {
    action: string
    eligible: boolean
    status: string
    denial_reasons: string[]
    required_checks: string[]
    comment_kind?: 'inline' | 'general'
  }
  github_comment_id: number | null
  commit_sha: string | null
  lifecycle_state?: string
  verification_status?: string
  failure_code?: string | null
  head_sha?: string
  base_sha?: string
  retry_available?: boolean
  retry_after?: number | null
  execution_evidence?: {
    branch?: string
    candidate?: { candidate_branch?: string; candidate_sha?: string }
    ci?: { status?: string; candidate_sha?: string; workflow_identity?: string }
    behavioral?: { status?: string; candidate_sha256?: string; test_identity?: string; attestation_verified?: boolean }
    confidence?: { calibrated_confidence?: number; calibrated?: boolean; manifest_verified?: boolean }
  }
}

export type GitHubWebhookActionStatus = {
  delivery_id: string
  status: string
  repository: string
  pr_number: number
  base_sha?: string
  head_sha?: string
  failure_code?: string | null
  actions: GitHubActionRecord[]
}

export type FixCandidate = {
  candidate_id: string
  finding_ref: string
  original_code: string
  corrected_code: string | null
  explanation: string
  assumptions: string[]
  evidence_ids: string[]
  generation_status: string
  generation_errors: string[]
  verification_status: string
  approval_status: 'pending' | 'accepted' | 'rejected'
  approval_history?: Array<{ action: string; outcome: string; message: string }>
  verification_report?: VerificationReport | null
  provider_identity?: string | null
}

export type ProviderSettings = {
  review_provider: 'groq' | 'qwen_local'
  fix_provider: 'groq' | 'qwen_local'
  groq_model: string
  groq_api_key_configured: boolean
  qwen_base_model: string
  qwen_adapter_path: string
  qwen_quantization: 'none'
  review_temperature: number
  fix_temperature: number
  max_output_tokens: number | null
  source_sharing_consent: boolean
  groq_connectivity: string
  qwen_readiness: string
  generation: number
  secret_storage: string
}

export type ProviderOptions = {
  groq_models: string[]
  groq_model_source: string
  qwen_base_model: string
  qwen_adapter_path: string
  qwen_quantization: string[]
  review_providers: Array<'groq' | 'qwen_local'>
  fix_providers: Array<'groq' | 'qwen_local'>
  default_groq_model: string
}

export type ProviderConnectivity = {
  groq: { status: 'passed' | 'failed' | 'not_checked'; message: string; available_approved_models?: string[] }
  qwen: { status: 'passed' | 'failed' | 'not_checked'; message: string }
}

export type GitHubCredentialStatus = {
  github_token_configured: boolean | null
  github_actions_token_configured: boolean | null
  github_candidate_staging_token_configured?: boolean | null
  github_autonomous_commit_token_configured?: boolean | null
  webhook_secret_configured: boolean | null
  persistent_storage_available: boolean
  storage_mode: string
}

export type GitHubAccountCheck = {
  status: 'passed' | 'failed'
  account?: { login: string; id: number; scopes: string; accepted_permissions: string }
  error_code?: string
  message?: string
}

export type AccessibleRepository = {
  id: number | null
  full_name: string
  owner: string
  name: string
  private: boolean
  permissions: Record<string, boolean>
  policy_configured?: boolean
  policy?: RepositoryPolicy
}

export type RepositoryPolicy = {
  publish_review_comments: boolean
  enable_autonomous_fixes: boolean
  allowed_low_risk_categories: string[]
  confidence_threshold: number
  max_changed_lines: number
  protected_paths: string[]
  permitted_branches: string[]
  required_checks: string[]
  allow_source_sharing: boolean
  required_github_checks: string[]
}

export type RepositoryPolicyResponse = {
  repository: string
  revision: string
  policy: RepositoryPolicy
  autonomous_commits?: string
}

export type WebhookAdminStatus = {
  endpoint_url: string
  secret_configured: boolean | null
  required_events: string[]
  public_reachability: 'localhost_only' | 'not_verified' | string
  last_authenticated_delivery: Record<string, unknown> | null
  last_successful_delivery: Record<string, unknown> | null
  recent_deliveries: Array<Record<string, unknown>>
  delivery_authentication: string
}

export type AdminWebhookAction = {
  action_id: string
  action_type: string
  publication_status: string
  attempts: number
  failure_code?: string | null
  github_comment_id?: number | null
  commit_sha?: string | null
  verification_status?: string
  lifecycle_state?: string
  execution_evidence?: {
    branch?: string
    candidate?: { candidate_branch?: string; candidate_sha?: string }
    ci?: { status?: string; candidate_sha?: string; workflow_identity?: string }
    behavioral?: { status?: string; candidate_sha256?: string; test_identity?: string; attestation_verified?: boolean }
    confidence?: { calibrated_confidence?: number; calibrated?: boolean; manifest_verified?: boolean }
  }
  retry_available?: boolean
  retry_after?: number | null
}

export type AdminWebhookDelivery = {
  delivery_id: string
  repository?: string | null
  pr_number?: number | null
  status: string
  failure_code?: string | null
  attempts: number
  next_attempt_at?: number | null
  actions: AdminWebhookAction[]
  retry_available?: boolean
}

export type SyntaxCheck = {
  check_name: string
  status: 'passed' | 'failed' | 'errored' | 'not_run'
  error_type?: string | null
  message?: string | null
  line?: number | null
  column?: number | null
}

export type VerificationReport = {
  candidate_id: string
  unified_diff: string
  syntax_check: SyntaxCheck
  verification_status: 'syntax_passed' | 'syntax_failed' | 'static_analysis_failed' | 'not_checked' | 'verification_error'
  behavioral_tests_status: 'not_run'
  static_analysis_status: 'passed' | 'failed' | 'errored' | 'not_run'
  pre_existing_static_issues?: StaticIssue[]
  introduced_static_issues?: StaticIssue[]
  static_analysis_errors?: Array<{ source?: string; error_type?: string | null; message?: string | null; line?: number | null }>
  original_sha256?: string | null
  corrected_sha256?: string | null
  verification_errors: string[]
}

export type StaticIssue = {
  finding_id: string
  rule_id: string
  title: string
  severity: string
  explanation: string
  affected_file: string
  line_start: number
  line_end: number
  column: number
  code: string
}

export type ApprovalResponse = {
  candidate_id: string
  action: 'accepted' | 'rejected'
  approval_status: 'pending' | 'accepted' | 'rejected'
  outcome: string
  message: string
  error?: string | null
  verification_status: string
  candidate: FixCandidate
}

export type ExportResponse = {
  candidate_id: string
  filename: string
  corrected_code: string
  unified_diff: string
  approval_status: string
  syntax_verification_status: string
  static_verification_status?: string
  warning: string
}
