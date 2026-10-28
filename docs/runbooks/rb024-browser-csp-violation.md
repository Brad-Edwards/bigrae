# RB024: Browser feature blocked by Content Security Policy

Applicability: every deployed portal; GCP primary. Operator: platform owner with
the security lead. Verified: 2026-10-27.

## Symptom

A portal page loads, but a script, style, connection, or terminal feature does
not work. The browser console shows a `Content Security Policy` refusal. The
portal logs show `csp.violation` events with `disposition=enforce`.

## Investigation

1. Confirm the response header. Set `PORTAL_HOSTNAME` to the deployment's
   public hostname. An enforced portal sends `Content-Security-Policy`; a
   degraded portal sends `Content-Security-Policy-Report-Only`.

   Read-only command:

   ```bash
   curl -sS -o /dev/null -D - "https://$PORTAL_HOSTNAME/privacy/" | grep -i '^content-security-policy'
   ```

2. Find the blocked directive and the page. The collector logs only the
   directive, the disposition and fingerprints of the blocked and document URLs.

   Read-only command:

   ```bash
   kubectl -n "$NAMESPACE" logs deploy/portal-web --since="$SINCE" | grep 'csp.violation'
   ```

3. Reproduce the failure in a browser with the developer console open. Record
   the directive, the page route and the release revision. Do not record
   cookies, tokens, page content or full URLs.
4. Decide whether the policy blocks product code or blocks injected content. An
   unknown script or style source is a possible attack. Treat it as a security
   incident before any change.
5. For an organizer image that shows as a link, read the event's image list
   (`GET /api/v1/ctf/events/<event_id>/content-images/`). The `failure_code`
   names the reason, for example `redirect` or `too_large`. Images stay pending
   while the CTF scheduler or the `content-egress` proxy is down; check them with
   [RB019](rb019-check-system-health.md).

## Resolution

Fix product code in a reviewed change. Move inline script, handlers and styles to
static files. A library that creates `<style>` elements at run time receives the
per-response nonce through the SPA `csp-nonce` module. Do not add
`unsafe-inline`, `unsafe-eval`, wildcards or new origins to quiet the failure.

If a blocked feature cannot wait for a fix, the bounded rollback is a degraded
state. Use one of these two paths:

1. Deploy the exact previously verified revision and digest through
   [RB012](rb012-portal-deployment.md).
2. Merge a reviewed change that sets `BROWSER_CSP_MODE=report-only` and adds an
   ADR-036 entry with an owner and expiry to `docs/adr/exceptions.yaml`.

In report-only mode, the browser reports violations but does not block them.
The portal then has no browser-enforced script, style or connection limit.
Restore `enforce` before the exception expires, and confirm the enforced header
again with step 1 of the investigation.

## Prevention

The hermetic browser lanes fail on any violation. Run `make test-platform-e2e`
and `make test-platform-a11y` for frontend and template changes. Exercise the
deployed identity provider, MFA and terminal paths after a release that changes
them. Keep the policy code-owned in `config/_browser_security.py`.

Escalate when the blocked source is unknown, the violation comes from content a
participant or author controls, the rollback would last past one release, or the
identity provider changes its browser origins.
