# RB004: Portal unavailable

Applicability: GKE primary; EKS optional. Operator: platform incident
commander. Verified: 2026-10-23.

## Symptom

The public portal or `/health/` is unavailable, returns errors, or fails ingress
health checks.

## Investigation

1. Check DNS, certificate state, reserved ingress address, load-balancer/backend
   health, and the public `/health/` response. Keep the response and request ID;
   do not expose detailed dependency errors publicly.
2. Check rollout and pod state:

   Read-only command:

   ```bash
   kubectl -n "$NAMESPACE" get deploy portal-web
   kubectl -n "$NAMESPACE" rollout status deploy/portal-web --timeout=2m
   kubectl -n "$NAMESPACE" get pods \
     -l app.kubernetes.io/component=portal
   kubectl -n "$NAMESPACE" get events --sort-by=.lastTimestamp
   ```

3. Read bounded portal logs and separate startup/configuration, database,
   Redis, OpenFGA, identity, object-storage, and downstream provider failures.
4. Compare the running image digest and runtime-config checksum with the latest
   successful protected deployment. `/health/` is coarse readiness, not proof
   that every dependency or range operation works.

## Resolution

Restore a failed dependency through its owning runbook. For an image or config
regression, use the protected deployment workflow to redeploy or roll back an
exact previously verified revision and digest. Do not apply local Helm values,
patch Secrets, open the private control-plane endpoint, or bypass certificate
verification. Run the existing post-deploy smoke and confirm workload rollout,
ingress health, and a bounded application path.

## Prevention

Alert independently on public readiness, backend health, pod availability,
worker heartbeats, dependency health, certificate expiry, and deployment
readback. Retain exact image and workflow identities with each release.

Escalate when the private cluster cannot be reached through the approved access
path, the running digest is unknown, or restoration requires an unreviewed
configuration change.
