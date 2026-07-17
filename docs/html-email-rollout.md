# HTML email rollout validation

This playbook validates the HTML-email presentation change without running a
simulation as part of implementation. It uses only synthetic multipart fixtures
and ordinary delivery telemetry. Do not publish or attach rendered simulation
HTML, raw mboxes, transcripts, audit logs, or message content.

## Fixture gate

Before a message type is enabled, run the focused fixture tests. The fixtures
must prove all of the following without importing a production template:

- the message is `multipart/alternative`, with complete `text/plain` first and
  `text/html` last;
- the simulator, personas, transcripts, and deterministic scorers use the
  canonical plain part and never receive markup as prompt input;
- normalized visible HTML has the same words as plain text, including the
  standard signature and any consent, event, or referral operational token;
- fixtures reject remote resources, scripts, forms, hidden or reordered content,
  event handlers, navigation attributes, and raw interpolation of hostile text;
- public mbox export replaces both alternatives with redacted placeholders while
  retaining safe multipart order. Private mbox retention does not change.

The fixture set is also the client-review input. Open only synthetic, non-user
content in Gmail web/mobile, current Apple Mail on macOS and iOS, Outlook web and
current Windows desktop, and Thunderbird with remote content blocked. Check
narrow widths, text scaling, dark mode, long tokens, Unicode, quotes/forwards,
and the plain-text view. Record the client/version, fixture identifier, result,
and screenshot location in the release ticket; do not store real message bodies
or HTML there.

## Baseline and phased enablement

Keep the feature flag plain-only during a baseline window of at least 14 days or
100 delivered user-facing messages per message type, whichever is later. For
each type, capture delivery attempts, SMTP successes, bounces, replies within
seven days, median time to first reply, consent completion, event opt-out or
suppression, complaints, and HTML-related support reports. Do not add open or
click tracking.

After fixture and client review pass, enable one user-facing message type for a
small cohort. Keep all other types plain-only. Expand only after the same metric
window shows no material degradation against that type's baseline. The flag must
force the complete canonical plain message, with the same subject, threading,
rate-limit charge, and domain behavior.

## Rollback criteria

Immediately return the affected type to plain-only and open an incident when:

- a delivered message lacks its plain part, has alternatives in the wrong order,
  has a visible-text/signature/token mismatch, or violates the fixture safety
  checks;
- a supported client has a reproducible unreadable rendering, or a recipient
  reports inaccessible or misleading content;
- renderer fallback exceeds 1 percent of attempted deliveries in 30 minutes, or
  five deliveries in an hour; or
- bounces rise by at least five percentage points and double the comparable
  baseline after at least 100 deliveries, or two credible HTML-related complaint
  or support reports arrive in a day.

Rollback is a feature-flag operation, not a retry or a resend. Preserve the
minimal structural delivery evidence needed to investigate, follow existing
redaction and retention rules, and do not put raw message content in public
artifacts. Re-enable only after a new synthetic fixture pass, client reproduction
check, and a documented owner approval.
