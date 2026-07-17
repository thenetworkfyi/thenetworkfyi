# HTML email plan and presentation contract

Status: presentation contract approved for implementation
Date: 2026-07-17
Scope: user-facing application mail and the simulation harness

## Executive decision

The Network will send lightweight `multipart/alternative` email: a complete
`text/plain` part followed by a server-rendered `text/html` part. Plain text is
canonical. The agent supplies only subject and plain text; the service owns
template choice, markup, signature, links, and MIME assembly. Plain-only output
remains a feature-flag fallback and a permanent recipient alternative.

The presentation is a carefully formatted personal email, not a newsletter:
one column, system fonts, restrained spacing, no decorative media, and a fixed
service identity.

## Research basis

This decision rests on normal client behavior and broad recipient usability,
not on vocal online complaints or marketing conversion data.

- The [DMA Consumer Email Tracker 2023](https://deployteq.com/wp-content/uploads/2023/04/dma-email-consumer-tracker-2023.pdf)
  surveyed 2,004 nationally representative UK adults. It found that design,
  fonts, headings, and uncluttered presentation help recipients understand
  email. It is not a direct HTML-versus-text preference survey, but it is useful
  evidence for visual hierarchy.
- [Outlook documents HTML as its default compose format](https://support.microsoft.com/en-US/Outlook/change-the-message-format-to-html-rich-text-format-or-plain-text-in-outlook),
  and [Gmail exposes formatting in its normal composer](https://support.google.com/mail/answer/8260).
  HTML is normal email infrastructure; plain-text composition is an opt-in mode.
- [Litmus client telemetry](https://www.litmus.com/email-client-market-share)
  is useful to prioritize clients, not to establish format preference because it
  is measured from tracked HTML opens. Gmail, Apple Mail, and Outlook are the
  primary compatibility targets.
- An older [HubSpot survey](https://blog.hubspot.com/marketing/plain-text-vs-html-emails-data)
  of more than 1,000 professionals is historical corroboration only, not the
  primary basis for this product decision.

The supported conclusion is narrow: HTML is familiar modern email infrastructure,
and deliberate visual structure can improve comprehension. A complete plain part
preserves accessibility, interoperability, and recipient choice.

## Security and transport contract

1. Model-facing reply and outreach tools accept `subject` and `body_text` only.
   They cannot provide HTML, CSS, template names, signature variants, URLs, or
   asset references.
2. The full plain-text message is created first. HTML derives from the same
   trusted rendering input.
3. Fixed capability messages use an application-selected named template with a
   typed, server-owned context. A model or inbound message cannot select it.
4. Model and inbound strings are escaped in their final HTML context. There is
   no raw-HTML or `safe` path for untrusted content.
5. A rendering failure sends the complete plain-text message only. It must not
   create a partial HTML message or a second delivery job.

The initial renderer may emit only static, server-authored `html`, `head`,
`meta`, `body`, `div`, `p`, `br`, `hr`, `strong`, and `blockquote` elements.
CSS is static and server-owned. It must never emit or accept remote or embedded
images, web fonts, stylesheets, tracking pixels, scripts, event handlers, forms,
inputs, video, hidden/preheader content, CSS-hidden/reordered semantic text,
arbitrary Markdown, or raw HTML.

Initial link policy: no HTML anchors in user-facing mail. No URL auto-linking,
click rewriting, agent-selected destinations, or inbound-selected destinations.
The literal referral account address is not a `mailto:` link. A future link needs
a named server-owned template field, visible descriptive text, and separate review.

Use stdlib `EmailMessage`: set the complete plain part, then add the HTML
alternative last. This follows [RFC 2046 section 5.1.4](https://datatracker.ietf.org/doc/html/rfc2046#section-5.1.4).
Both parts share one `Message-ID`, threading headers, rate-limit charge, SMTP
result, Sent append, and audit trace. Append signature and quoted trail in the
same semantic order to both. Internal/admin messages stay plain-only unless a
later user-facing need is approved.

## Renderer and template decision

Use Jinja package templates. Configure one process-wide environment with
`PackageLoader`, `StrictUndefined`, and `select_autoescape` for `.html` files.
Do not use dynamic include paths, database templates, or filters that bless
untrusted strings as markup.

Jinja keeps a small Python-native template set auditable without a Node runtime.
Defer MJML and React Email: their toolchains and generated markup are excessive
for a one-column conversational email. Defer Markdown-to-HTML because raw HTML,
model-selected links/images, and text/HTML drift widen the security surface.

The rendering boundary is equivalent to:

```text
RenderedEmail(text: str, html: str | None)
render_conversational_email(body_text, signature_variant, quoted_message)
render_fixed_email(template_name, typed_context, signature_variant)
```

For the first release, conversational text supports paragraphs at blank lines
and explicit line breaks only. Each string is autoescaped. Lists, emphasis, and
links require a future typed content-block design with parity tests.

## Approved visual contract

- One fluid column, at most approximately 600 px wide.
- 16 px body text, approximately 1.5 line height, system font stack,
  left-aligned copy, readable paragraph spacing, and high-contrast neutral colors.
- Semantic paragraphs and quotes only; no decorative headings, layout images,
  banners, avatars, social icons, forced light background, or dark-mode hacks.
- If Outlook needs layout tables, they are presentational and retain logical
  reading order. Check each final CSS feature with [Can I Email](https://www.caniemail.com/).

The supported-client release matrix is Gmail web/mobile; Apple Mail on current
iOS and macOS in light/dark mode; Outlook web/current Windows desktop;
Thunderbird with remote content blocked; and a plain-text-only view. Before
default enablement, inspect synthetic fixtures in each target for narrow widths,
long tokens, Unicode names, quoted/forwarded mail, dark mode, disabled images,
and text scaling.

## Approved signature and referral contract

Every user-facing message has exactly one server-owned signature after its body
and before a quoted inbound trail. The standard visible text is:

```text
--
The Network
An automated connection service
Reply anytime.
```

This is a truthful fixed service identity and automation disclosure. It does
not invent a human author or imply human review. HTML uses a subtle divider, a
bold live-text service name, and the same words; it has no image or link.

| Variant | Use | Content |
|---|---|---|
| `standard` | Default user-facing mail | The standard signature above. |
| `standard_with_referral` | Explicitly designated growth-enabled user-facing mail | `standard` plus the fixed referral line below. |
| `none` | Internal admin and operational alerts | No signature. |

The referral line is fixed server copy in both MIME parts:

```text
Know someone who should be on this? Forward this along — they can join by emailing <configured inbound account> directly.
```

`standard_with_referral` is disabled by default for the initial HTML release.
It can be enabled only by existing server configuration for message types product
has designated. The agent, inbound content, and template arguments cannot add it.
Consent tokens, event opt-out language, and other capability instructions remain
in their bodies, not the signature.

This plan is not legal advice. Before enabling the referral variant in an
operating jurisdiction, the accountable legal/compliance owner must record
approval of the exact wording, affected message types, sender identity, and
any required footer or suppression handling. Until then, ship `standard` only.
Any future destination link requires a separate approved exception to the
no-links policy.

## Test, simulation, and rollout contract

- Test escaping of tags, quotes, ampersands, malformed input, and Unicode;
  paragraph/line-break behavior; strict missing context; variants; exactly-once
  signature placement; and normalized visible-text parity.
- Test `multipart/alternative` ordering and charsets, shared headers and one
  `Message-ID`, plain-only fallback, and audit fields that contain only format
  presence, template id, counts, outcome, and duration.
- Assert no model-facing schema exposes `body_html` or an equivalent HTML,
  template, signature, or link escape hatch. Keep `tests/security/` green.
- Migrate fixed messages in reviewable groups and verify identity disclosure
  remains behind the existing consent gate in both parts.
- The simulator must preserve production MIME shape, while personas, transcript
  extraction, and scoring consume the plain canonical part. Public mbox
  redaction removes both bodies while retaining safe MIME shape.

Do not run simulations as part of this work without explicit user approval.
Fixture-level MIME checks and unit tests are sufficient until that approval is
given. Do not publish rendered HTML previews containing real simulation content.

Enable message types gradually behind a plain-only rollback flag after fixture
validation in the supported-client matrix. Track reply rate, time to reply,
consent completion, event suppression, bounces, complaints, and support reports.
Do not add open or click tracking.

## Decision status

| Presentation input | Resolution |
|---|---|
| Service identity and disclosure | `The Network` / `An automated connection service`. |
| Signature variants | `standard`, `standard_with_referral`, `none`. |
| Referral behavior | Fixed text, no link, disabled by default; compliance approval gate. |
| Initial client support | Gmail, Apple Mail, Outlook, Thunderbird remote-blocked, and plain-text view as specified above. |
| Template system | Strict, autoescaped Jinja package templates. |
| Agent HTML authority | None. |
| Assets and links | No remote assets, tracking, scripts, forms, hidden content, arbitrary links, or auto-links. |
| Operational fallback | Stable feature flag forces plain-only output. |

Renderer work has no unresolved presentation inputs. Remaining decisions are
operational: rollout cohort/duration based on actual volume, and whether later
measurement justifies a typed content-block schema.

## Non-goals

Newsletter layouts; open/click tracking; remote logos or web fonts; agent-authored
HTML/CSS/template selection; arbitrary Markdown; interactive email; per-user
themes; and replacing `EmailMessage`, SMTP, or the capability-based recipient
model are out of scope.
