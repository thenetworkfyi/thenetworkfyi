from pydantic import computed_field
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
    # Cheaper/smaller-model tier for subtasks that don't need the main agent
    # model (currently: the sanitize_memory_llm gist pass, see memory/sanitize.py).
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

    # Email - IMAP (inbound polling) and SMTP (outbound send) are distinct
    # accounts/credentials, potentially on different providers entirely.
    imap_account: str = ""
    imap_password: str = ""
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_account: str = ""
    smtp_password: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    # Address used in the outbound From: header (distinct from smtp_account,
    # which is only the SMTP login credential).
    email_from: str = ""
    # Folder outbound replies are appended to after a successful SMTP send, so
    # sent mail shows up in the account like it would from a normal mail
    # client rather than relying on provider-side auto-save.
    imap_sent_folder: str = "Sent"

    # Optional content scanner
    content_scan_enabled: bool = False

    # Optional higher-fidelity gist tier: run the LLM sanitizer (fixed prompt,
    # no tools - see docs/security.md THE SEAL layer 4) in addition to the
    # deterministic Presidio pass before a person-referencing
    # memory becomes eligible for cross-user search. Off by default (costs an
    # LLM call and adds latency on every such write); when off,
    # sanitize_memory_high_fidelity uses the deterministic Presidio pass only.
    sanitize_llm_tier_enabled: bool = False

    # Procrastinate worker concurrency (global LLM-spend ceiling)
    worker_concurrency: int = 4

    # Rate limiting: max inbound emails per hour
    rate_limit_per_hour: int = 10
    unauthenticated_rate_limit_per_hour: int = 3
    global_email_rate_limit_per_hour: int = 100

    # PII-safe audit correlation. Used to derive stable sender pseudonyms with
    # HMAC-SHA256; an unkeyed email hash is not safe because candidate-address
    # dictionary lookup can reverse it.
    sender_identifier_secret: str = ""

    # Outbound send caps. Enforced inside the email capabilities, not by prompt.
    # The dispatch_* names predate the reply_to_sender/send_outreach tool split
    # and stay unchanged because they are deployment-facing env var names.
    dispatch_max_sends_per_run: int = 6
    dispatch_recipient_daily_cap: int = 6
    dispatch_sender_reply_daily_cap: int = 6
    registration_limit_per_day: int = 50
    consent_decline_cooldown_days: int = 90

    # Tool abuse bounds. Set a value to 0 or lower to disable that specific
    # guard in controlled development environments.
    remember_text_max_chars: int = 8_000
    search_query_max_chars: int = 1_000
    person_memory_limit: int = 500

    # Introduction consent pacing. These limits are enforced at the server-side
    # proposal boundary, never by agent prompt wording. A proposal sends one
    # fixed consent request to each participant.
    introduction_max_proposals_per_run: int = 3
    introduction_max_outstanding_requests_per_person: int = 3
    introduction_max_requests_per_person_in_window: int = 3
    introduction_request_window_seconds: int = 86_400

    # Admin channel: allowlisted senders + PGP/MIME-signed request (see admin/auth.py)
    admin_emails: list[str] = []
    admin_gpg_public_key: str = ""  # armored public key of the trusted admin signer
    admin_replay_window_seconds: int = 300

    # Sender authentication: the From: header alone is spoofable (no envelope
    # check happens over IMAP), so trust it for Person resolution / self-
    # registration only when the receiving mail server's own
    # Authentication-Results header reports dkim=pass, spf=pass, or auth=pass.
    # Disable only for dev/test environments where inbound mail carries no such
    # header. If trusted_authserv_id is set, only an Authentication-Results
    # header whose authserv-id matches is considered (defense against an
    # untrusted intermediate relay forging the header); left blank, the header
    # closest to the top of the message (added last, i.e. by your own receiving
    # server) is trusted.
    require_sender_auth: bool = True
    trusted_authserv_id: str = ""

    # Growth: footer appended to outbound user-facing replies (mailer-level,
    # not agent-composed, so prompt injection can't alter or suppress it)
    growth_footer_enabled: bool = True

    # Proactive semantic rematch (worker/proactive.py:scan_for_matches). When a
    # newly-arrived memory closely matches an OLDER standing note about a
    # *different* person, surface the pair to the agent, which decides whether
    # to introduce. This is unsolicited outreach, so the similarity floor is
    # deliberately conservative - a false positive costs a real email (unlike
    # interactive search, where the agent can just ignore a weak hit). Lookback
    # bounds the scan to memories created since roughly the last hourly run, so
    # a match only fires once, when the counterpart first arrives. The floor
    # sits above the ~0.55 band where thin keyword overlap lands (two people
    # who merely both mention factories) while keeping specific shared-ground
    # matches (e.g. two ML-in-manufacturing operators, ~0.7+).
    proactive_match_threshold: float = 0.6
    proactive_rematch_lookback_minutes: int = 65
    proactive_rematch_top_k: int = 5
    proactive_surface_cooldown_seconds: int = 86_400


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
