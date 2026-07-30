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

## Baseline and release monitoring

Before release, record the existing delivery baseline for at least 14 days or
100 delivered user-facing messages per message type, whichever is later. For
each type, capture delivery attempts, SMTP successes, bounces, replies within
seven days, median time to first reply, consent completion, event opt-out or
suppression, complaints, and HTML-related support reports. Do not add open or
click tracking.

After fixture and client review pass, release all applicable user-facing message
types with their complete plain alternative. Compare the same metrics against
the baseline and investigate material degradation without adding tracking.

## Rollback criteria

Open an incident and pause further rollout work when:

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

Preserve the minimal structural delivery evidence needed to investigate, follow
existing redaction and retention rules, and do not put raw message content in
public artifacts. Any corrective deployment requires a new synthetic fixture
pass, client reproduction check, and a documented owner approval.
