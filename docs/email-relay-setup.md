# Hidden-address email relay setup

This runbook configures the address-only relay used after two people consent to an
introduction. Each pair receives one stable address:

```text
hidden-<introduction-reply-token>@relay.example.com
```

The existing mail host accepts every address at `relay.example.com` into either the
agent's normal IMAP mailbox or a separate relay mailbox on the same IMAP host. The
worker polls each configured mailbox, authorizes the sender and pair in server code, and
sends one message to the other participant through SES SMTP. SES is outbound-only.
There is no SES receiving rule, webhook, public application port, or separate inbound
service.

This feature hides participant email addresses from each other. The fixed introduction
also omits their names and prints the relay address in its body. Messages participants
later send through the relay do not have their subject or body rewritten.

## Deployment values

Choose these values before changing either system:

| Name | Example | Purpose |
|---|---|---|
| Primary mailbox | `agent@example.com` | Existing mailbox polled over IMAP |
| Relay mailbox | `relay-inbox@relay.example.com` | Optional separate catch-all destination |
| Relay domain | `relay.example.com` | Catch-all domain used for pair aliases |
| SES Region | `us-west-2` | Region containing the verified identity and SMTP credentials |
| SES SMTP endpoint | `email-smtp.us-west-2.amazonaws.com` | Regional STARTTLS endpoint |

Use a dedicated subdomain for the relay when possible. It keeps its catch-all and MX
policy separate from ordinary addresses while allowing SES to verify and DKIM-sign the
whole subdomain, including dynamically generated `hidden-*` addresses.

## 1. Configure the existing email host

### DNS and delivery

1. Publish an MX record for `RELAY_DOMAIN` pointing to the existing email host. If the
   relay domain is already hosted there, retain the existing MX record.
2. Configure one domain catch-all/default mailbox mapping:

   ```text
   *@relay.example.com -> relay-inbox@relay.example.com
   ```

3. Do not create one mailbox or alias per `hidden-*` address. The application creates
   pair addresses from existing database tokens; the mail host only needs the wildcard
   delivery rule.
4. Deliver catch-all messages directly into the INBOX read by
   `RELAY_IMAP_ACCOUNT`. If separate relay credentials are not configured, deliver them
   into `IMAP_ACCOUNT` instead. Do not forward between mailboxes if forwarding would
   replace authentication results or lose the original recipient.

The exact catch-all control belongs to the existing mail-host configuration. For a
hosted Dovecot service, use its default-address/catch-all control. A common self-managed
Postfix-to-Dovecot installation expresses the equivalent rule in its existing virtual
alias backend, for example:

```text
@relay.example.com    relay-inbox@relay.example.com
```

After changing a file-backed Postfix map, rebuild that map and reload Postfix using the
normal procedure for that host. Do not copy this example over an existing SQL/LDAP
virtual-user configuration; add the wildcard through the backend it already uses.
Dovecot remains the IMAP mailbox and local delivery target. This application does not
add another SMTP receiver.

### Preserve the original recipient

The IMAP message must retain the `hidden-*` destination. Intake checks these headers in
order and parses all addresses within them:

1. `X-Original-To`
2. `Delivered-To`
3. `Envelope-To`
4. `To`

For ordinary replies, `To` normally contains the proxy because the introduction sets
`Reply-To` to that address. For robust catch-all delivery, configure the existing SMTP
delivery path to retain the SMTP envelope recipient in `X-Original-To`, `Delivered-To`,
or `Envelope-To`. If aliases are rewritten before Dovecot delivery, pass the original
recipient through the existing Dovecot LDA/LMTP integration rather than only the final
catch-all mailbox address.

Inspect the raw source of a delivered probe message. At least one recognized header
must contain the original address:

```text
hidden-not-a-token@relay.example.com
```

It is acceptable for `Delivered-To` to also contain `agent@example.com`. Intake
prioritizes a bounded `hidden-*` candidate on the configured relay domain over that
catch-all mailbox address. Invalid hidden aliases are intentionally preserved long
enough for the worker to reject them before any agent execution.

### Check third-party sender-authentication results

Relay authorization does not trust the user-controlled `From` header by itself. The
application cannot configure or change the third-party IMAP provider's mail-processing
stack. Compatibility depends on the `Authentication-Results` header that provider
already writes into delivered messages.

Send a probe message from an external participant, then inspect its raw source in the
IMAP mailbox. Its first result should look similar to:

```text
Authentication-Results: mx1.example.com; dkim=pass ...; spf=pass ...
```

The application considers only the first `Authentication-Results` header and accepts a
passing `dkim`, `spf`, or `auth` result from it. Any lower, sender-supplied result is
ignored. The authserv-id before the first semicolon (`mx1.example.com` above) is
informational and is not a trust boundary.

With `REQUIRE_SENDER_AUTH=true`, relay mail is rejected when the provider supplies no
usable first verdict. Treat the raw-message check as a provider compatibility check
before enabling relay delivery; there is no application setting that can add a missing
provider verdict.

### Email-host acceptance checks

Before changing the application, verify all of the following:

- `dig +short MX relay.example.com` resolves to the intended existing mail host.
- Mail to a previously nonexistent address at the relay domain arrives in the
  configured relay inbox (`RELAY_IMAP_ACCOUNT`, or `IMAP_ACCOUNT` when no separate
  relay mailbox is configured).
- Raw message source preserves the original recipient in a recognized header.
- A raw message confirms that the third-party provider supplies a first
  `Authentication-Results` header with a passing `dkim`, `spf`, or `auth` result for a
  legitimate external sender.
- IMAP TLS login works with the exact host, port, account, and password that the worker
  will use.

## 2. Configure Amazon SES for outbound relay mail

All SES resources in this section are regional. Use the same Region for the verified
identity, SMTP credentials, production-access request, and `SMTP_HOST` endpoint.

1. In SES, create a domain identity for `relay.example.com`, not an identity for one
   generated `hidden-*` address.
2. Enable Easy DKIM and publish the CNAME records SES provides. Wait until the identity
   and DKIM status are verified.
3. If the account is in the SES sandbox, request production access. Sandbox accounts
   cannot relay to arbitrary participant addresses.
4. Create SES SMTP credentials in that Region. SES SMTP credentials are not the same as
   ordinary AWS access keys.
5. Allow the credential's principal to send from the relay-domain identity. SES SMTP
   authorization ultimately requires `ses:SendRawEmail` access.
6. Use the regional SMTP endpoint with STARTTLS on port 587. The application calls
   `EHLO`, upgrades with `STARTTLS`, authenticates, and then sends the message.

The application sets both visible `From` and `Reply-To` to the generated proxy address.
Verifying the domain identity authorizes those dynamic addresses; no per-address SES
identity is needed.

### DKIM, SPF, DMARC, and MAIL FROM

Enable SES DKIM for the relay-domain identity and confirm delivered mail passes DKIM with
alignment to the visible proxy domain. Publish a DMARC policy appropriate for the
deployment after validating delivery. Start with a monitoring policy if this is a new
domain and tighten it based on reports.

SES's default `amazonses.com` MAIL FROM domain already passes SPF for SES. A custom MAIL
FROM domain is optional. If one is used, choose a separate subdomain such as
`bounce.relay.example.com` and publish the MX and SPF records SES supplies for that
subdomain.

Do not configure the catch-all relay domain itself as the SES custom MAIL FROM domain.
The relay domain's MX must continue pointing to the existing Dovecot mail host for human
replies, while SES requires a custom MAIL FROM domain's MX to point to its feedback
endpoint. Separate subdomains avoid that conflict.

Useful references:

- [Creating and verifying identities in Amazon SES](https://docs.aws.amazon.com/ses/latest/dg/creating-identities.html)
- [Connecting to an SES SMTP endpoint](https://docs.aws.amazon.com/ses/latest/dg/smtp-connect.html)
- [Requesting production access](https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html)
- [DMARC with SES](https://docs.aws.amazon.com/ses/latest/dg/send-email-authentication-dmarc.html)
- [Using a custom MAIL FROM domain](https://docs.aws.amazon.com/ses/latest/dg/mail-from.html)
- [Dovecot LDA with Postfix](https://doc.dovecot.org/2.3/configuration_manual/howto/dovecot_lda_postfix/)

## 3. Configure the application

Add the relay and sender-authentication values to the production `.env` alongside the
existing mailbox configuration:

```dotenv
# Existing Dovecot mailbox used for ordinary inbound agent mail.
IMAP_ACCOUNT=agent@example.com
IMAP_PASSWORD=...
IMAP_HOST=imap.example.com
IMAP_PORT=993
IMAP_SENT_FOLDER=Sent

# Optional separate relay catch-all mailbox on the same IMAP host/port. Set both
# values together; leave both empty when relay mail is delivered to IMAP_ACCOUNT.
RELAY_IMAP_ACCOUNT=relay-inbox@relay.example.com
RELAY_IMAP_PASSWORD=...

# Amazon SES SMTP credentials and regional endpoint.
SMTP_ACCOUNT=...
SMTP_PASSWORD=...
SMTP_HOST=email-smtp.us-west-2.amazonaws.com
SMTP_PORT=587

# Existing address used for ordinary agent mail. Relay messages override From with the
# server-generated hidden address.
EMAIL_FROM=agent@example.com

# Bare domain only: no scheme, wildcard, address, path, or trailing mailbox name.
RELAY_DOMAIN=relay.example.com

# Required for production relay authorization.
REQUIRE_SENDER_AUTH=true
```

`RELAY_DOMAIN` is required configuration and must be supplied as the canonical bare
domain handled by the deployment. The application uses it verbatim when generating
addresses, accepts only canonical lowercase UUID tokens in valid aliases, and compares
incoming domains case-insensitively. It still recognizes malformed `hidden-*` addresses
as relay attempts so they fail closed instead of reaching the agent.

No relay-specific table or service is required. Pair aliases reuse
`IntroductionConsent.reply_token`; the worker polls each configured IMAP inbox and sends
through SMTP. On a production host (a git checkout, redeployed by pulling and
rebuilding - see `.github/workflows/ci.yml`'s `deploy` job):

```bash
git pull origin main
docker compose up -d --build --force-recreate
docker compose logs -f worker
```

For a local process outside Compose:

```bash
docker compose up -d db
uv run alembic upgrade head
uv run thenetwork-worker
```

The worker needs outbound access to Postgres, IMAP TLS, the SES SMTP endpoint, and the
configured model providers. It still needs no inbound application port.

## 4. Validate the deployed flow

Use test participants and mailboxes that you control.

### Catch-all and fail-closed probe

1. Send an external message to `hidden-not-a-token@relay.example.com`.
2. Confirm it arrives in the agent's IMAP INBOX with the original recipient and trusted
   authentication result intact.
3. Run one poll cycle or wait for the minute-periodic poll:

   ```bash
   uv run thenetwork-producer
   ```

4. Confirm the worker records `worker.message_rejected` with reason `relay_invalid`.
5. Confirm no SMTP message is sent and no agent run occurs. Audit output must not contain
   the hidden or participant address.

### Mutual-consent and two-way relay

1. Create an introduction between two controlled, authenticated participants and complete
   consent from both sides.
2. Confirm each participant receives a separate introduction. Each message must have:
   - one real recipient in `To`;
   - `From: The Network <hidden-<token>@relay.example.com>`;
   - the same hidden address in `Reply-To`; and
   - the same hidden address in the body, with no participant name or real address; and
   - a match recap containing only the proposal's sanitized gist snapshots.
3. Reply from participant A with a controlled multipart message and attachment. Confirm
   exactly one message reaches participant B with the same proxy `From`/`Reply-To` and
   subject; the participant-authored MIME body, including plain/HTML alternatives and the
   attachment, is preserved while every source routing header is absent.
4. Reply from participant B and confirm the same behavior in the other direction.
5. Confirm neither relay direction invokes the agent, consent parser, memory writes, or
   the agent-mail content scanner.
6. Revoke the introduction and send another reply to the proxy. Confirm no message is
   forwarded.

### Ordinary-mail regression

Send ordinary mail to the agent's normal address and confirm it still follows the normal
agent path. A non-hidden address at the relay domain is not a pair alias.

## 5. Operations and failure behavior

- The catch-all accepts arbitrary local parts, so expect invalid and spam traffic. Hidden
  candidates are rate-limited and fail closed unless the sender is authenticated, is an
  exact participant, and the pair remains introduced.
- INBOX messages are marked seen after durable enqueue but are not deleted or moved.
  Plan mailbox retention and storage accordingly.
- SMTP send failures propagate to the Procrastinate retry policy. IMAP Sent-folder append
  failures are logged but do not retry an already successful SMTP delivery.
- Invalid token, unknown pair, third-party sender, unauthenticated sender, declined pair,
  and revoked pair attempts send nothing and never fall through to the agent.
- Monitor SES bounces, complaints, suppression state, and sending quotas in the selected
  Region. The application does not replace SES reputation or suppression controls.
- Keep the IMAP and SES SMTP credentials in the deployment secret store and out of the
  repository. Rotate either credential independently and restart the worker.

## Completion checklist

- [ ] Relay-domain MX points to the existing mail host.
- [ ] Domain catch-all delivers to `RELAY_IMAP_ACCOUNT`, or to `IMAP_ACCOUNT` when no
      separate relay mailbox is configured.
- [ ] Original hidden recipient survives delivery in a recognized header.
- [ ] Raw source confirms that the third-party IMAP provider supplies a usable first
      `Authentication-Results` verdict.
- [ ] SES domain identity and DKIM are verified in the selected Region.
- [ ] SES production access and SMTP credentials are active in that Region.
- [ ] Optional custom MAIL FROM uses a separate bounce subdomain.
- [ ] `.env` contains the IMAP, SES SMTP, relay-domain, and sender-auth settings.
- [ ] Invalid-alias probe fails closed without an agent or SMTP send.
- [ ] Mutual consent sends two separate proxy-addressed introductions.
- [ ] A-to-B and B-to-A replies relay without exposing either real address.
- [ ] Revocation blocks subsequent relay delivery.
- [ ] Ordinary mail still follows the existing agent path.
