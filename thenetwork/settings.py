from pydantic import computed_field, model_validator
from pydantic_ai.settings import ThinkingLevel
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Database - kept as separate parts rather than a single DATABASE_URL.
    # Postgres itself gets POSTGRES_PASSWORD as a literal string (no
    # decoding); a hand-built connection URI would need the same value
    # percent-encoded, and those two representations silently desync for any
    # password containing a URI-reserved character (@, :, /, %, ...). Letting
    # SQLAlchemy's URL builder do the encoding removes that failure mode.
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "network_db"
    postgres_user: str = "network"
    postgres_password: str = "network"
    # A dedicated read-only role (granted pg_monitor, never the application
    # POSTGRES_USER) used only by the OTel collector's postgresql receiver to
    # scrape server-internal stats. Provisioned by alembic/versions/018_*.
    postgres_monitor_user: str = "network_monitor"
    postgres_monitor_password: str = "network_monitor"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        ).render_as_string(hide_password=False)

    # LLM - provider selected by config string (no vendor lock-in, no LiteLLM).
    # Deliberately no defaults: model selection must be explicit per
    # deployment, so a missing env var fails at startup instead of silently
    # running against a fallback vendor/model.
    agent_model: str
    # Optional provider-agnostic reasoning effort for the main agent. Set to
    # None to leave the model's default thinking behavior unchanged.
    agent_thinking_level: ThinkingLevel | None = "medium"
    # Cheaper/smaller-model tier for subtasks that don't need the main agent
    # model (currently: the primary-intake abuse judge, see worker/abuse_judge.py).
    small_agent_model: str
    agent_request_limit: int = 12
    agent_total_tokens_limit: int = 100_000
    # Embeddings are OpenAI-only and must produce 1536 dimensions to match the
    # pgvector schema. Validation runs at worker/producer startup.
    embed_model: str

    # Workload-specific API keys. Each model receives only its own credential,
    # regardless of which provider its model string selects.
    agent_api_key: str = ""
    small_agent_api_key: str = ""
    embed_api_key: str = ""

    # Test-only: the pydantic-evals LLMJudge model used by
    # tests/scenarios/test_live_archetypes.py and
    # sim/scoring/scoring.py's build_transcript_judge. Kept as its own
    # workload/credential pair, separate from agent_model, because an
    # unconfigured LLMJudge silently defaults to calling openai:gpt-5.2 -
    # this repo never wants a third-party API called by an implicit default,
    # so those call sites require this to be set explicitly and skip/fail
    # rather than falling back to that default.
    test_llm_judge_model: str | None = None
    test_llm_judge_api_key: str = ""

    # Default per-request timeout for every model API call (agent, sanitizer,
    # sim personas, LLM judge), applied via model_config.model_with_api_key.
    # The openai SDK's own default is 600s connect-included, which lets one
    # slow provider round-trip stall a whole agent run; this bounds each call
    # so a stalled upstream fails fast into the caller's own retry/error path
    # instead.
    model_request_timeout_seconds: float = 90.0

    # Email - IMAP (inbound polling) and SMTP (outbound send) are distinct
    # accounts/credentials, potentially on different providers entirely.
    imap_account: str = ""
    imap_password: str = ""
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    # Optional separate mailbox for relay-domain catch-all delivery. It uses
    # the same IMAP host/port as the primary mailbox; when unset, relay mail is
    # expected to arrive in the primary mailbox for backward compatibility.
    relay_imap_account: str = ""
    relay_imap_password: str = ""
    smtp_account: str = ""
    smtp_password: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    # Address used in the outbound From: header (distinct from smtp_account,
    # which is only the SMTP login credential).
    email_from: str = ""
    # Address printed in the standard user-facing email signature. Set this to
    # null to derive it from email_from instead.
    email_signature_address: str | None = "join@thenetwork.fyi"
    # Domain whose Dovecot catch-all feeds the IMAP inbox. Introduction reply
    # aliases are generated beneath this domain; no inbound HTTP service is
    # involved. Deliberately no default: every deployment must explicitly own
    # the relay namespace used by these server-authorized addresses.
    relay_domain: str
    # Folder outbound replies are appended to after a successful SMTP send, so
    # sent mail shows up in the account like it would from a normal mail
    # client rather than relying on provider-side auto-save.
    imap_sent_folder: str = "Sent"

    # Optional content scanner
    content_scan_enabled: bool = False

    # Local span classifier backing every sanitized projection (docs/security.md
    # THE SEAL layer 4). Weights are local and ungated, so this costs no model
    # call and no credential; it is mandatory, with no disable switch, because
    # without it a cross-user gist would carry raw names.
    sanitize_model: str = "openai/privacy-filter"

    # Procrastinate worker concurrency (global LLM-spend ceiling)
    worker_concurrency: int = 4

    # Outbound-only worker state metrics. The application opens no listener;
    # the Compose default reaches the Collector over its internal network.
    worker_metrics_otlp_endpoint: str = "http://otel-collector:4318/v1/metrics"
    worker_metrics_export_interval_seconds: float = 30.0
    worker_metrics_export_timeout_seconds: float = 5.0
    worker_metrics_collection_timeout_seconds: float = 2.0

    # Rate limiting: max inbound emails per hour
    rate_limit_per_hour: int = 20
    unauthenticated_rate_limit_per_hour: int = 6
    global_email_rate_limit_per_hour: int = 200

    # PII-safe audit correlation. Used to derive stable sender pseudonyms with
    # HMAC-SHA256; an unkeyed email hash is not safe because candidate-address
    # dictionary lookup can reverse it.
    sender_identifier_secret: str = ""

    # Optional primary-intake burst monitor. Its durable observations use only
    # keyed fingerprints, so enabling it without the HMAC secret is invalid.
    primary_intake_burst_monitoring_enabled: bool = False

    # Optional HMAC key for stable pseudonyms in redacted model-response audit
    # records. Leaving it unset preserves redaction while disabling correlation.
    response_log_redaction_secret: str = ""

    # Outbound send caps. Enforced inside the email capabilities, not by prompt.
    # The dispatch_* names predate the reply_to_sender/send_outreach tool split
    # and stay unchanged because they are deployment-facing env var names.
    dispatch_max_sends_per_run: int = 12
    dispatch_recipient_daily_cap: int = 12
    dispatch_sender_reply_daily_cap: int = 12
    registration_limit_per_day: int = 100
    consent_decline_cooldown_days: int = 90

    # Rolling-24h ceiling on tokens billed to the AGENT_MODEL/SMALL_AGENT_MODEL
    # endpoint (email_agent + memory_sanitizer + abuse_judge; embedding bills
    # a different provider and is tracked separately - see
    # thenetwork/llm_observability.py). A value <= 0 disables the cap. The
    # default is a deliberately conservative starting point, not a derived
    # constant: see .env.example for the formula to re-derive it per
    # AGENT_MODEL and the worst-case/blended cost it implies for the model
    # configured there today.
    daily_agent_token_cap: int = 15_000_000

    # Tool abuse bounds. Set a value to 0 or lower to disable that specific
    # guard in controlled development environments.
    remember_text_max_chars: int = 8_000
    search_query_max_chars: int = 1_000
    person_memory_limit: int = 500
    # Recent sender-owned gist projections injected into each registered
    # sender's agent run. Both limits apply before model invocation; search
    # remains available for older or semantically targeted recall.
    recent_memory_context_max_count: int = 20
    recent_memory_context_max_chars: int = 4_000

    # Introduction consent pacing. These limits are enforced at the server-side
    # proposal boundary, never by agent prompt wording. A proposal sends one
    # fixed consent request to each participant.
    introduction_max_proposals_per_run: int = 6
    introduction_max_outstanding_requests_per_person: int = 6
    introduction_max_requests_per_person_in_window: int = 6
    introduction_request_window_seconds: int = 86_400
    # Admin channel: allowlisted senders + PGP/MIME-signed request (see admin/auth.py)
    admin_emails: list[str] = []
    admin_gpg_public_key: str = ""  # armored public key of the trusted admin signer
    admin_replay_window_seconds: int = 300

    # Sender authentication: the From: header alone is spoofable (no envelope
    # check happens over IMAP), so trust it for Person resolution / self-
    # registration only when the third-party IMAP provider's first
    # Authentication-Results header reports dkim=pass, spf=pass, or auth=pass.
    # Disable only for dev/test environments where inbound mail carries no such
    # header. Only the header closest to the top of the message is evaluated;
    # deployments must confirm that the provider adds its verdict there.
    require_sender_auth: bool = True

    # Growth: footer appended to outbound user-facing replies (mailer-level,
    # not agent-composed, so prompt injection can't alter or suppress it)
    growth_footer_enabled: bool = True

    # Proactive semantic rematch (worker/proactive.py:scan_for_matches). Every
    # run revisits sanitized standing notes for people without an active consent
    # pair. This is unsolicited outreach, so the similarity floor is deliberately
    # conservative - a false positive costs a real email. The floor sits above
    # the ~0.55 band where thin keyword overlap lands while keeping specific
    # shared-ground matches (e.g. two ML-in-manufacturing operators, ~0.7+).
    proactive_match_threshold: float = 0.6
    proactive_rematch_top_k: int = 5
    proactive_surface_cooldown_seconds: int = 86_400

    # Event recommendation discovery is independent of introduction matching.
    # The scan compares active event embeddings with sanitized person-memory
    # embeddings, then applies both per-person and whole-scan fan-out bounds
    # before recording consideration and enqueueing agent jobs.
    event_match_threshold: float = 0.6
    event_match_top_k: int = 20
    event_scan_active_event_limit: int = 100
    event_scan_max_candidates: int = 50
    event_scan_max_per_person: int = 1

    @model_validator(mode="after")
    def _validate_primary_intake_monitor_secret(self) -> "Settings":
        if (
            self.primary_intake_burst_monitoring_enabled
            and not self.sender_identifier_secret
        ):
            raise ValueError(
                "SENDER_IDENTIFIER_SECRET is required when primary intake burst "
                "monitoring is enabled"
            )
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
