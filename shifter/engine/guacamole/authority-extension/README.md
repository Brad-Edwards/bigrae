# Guacamole session authority

This add-on targets Apache Guacamole 1.6.0's supported authentication-provider
decoration API. JSON authentication remains responsible for authenticating the
signed payload. The portal remains responsible for application policy. The
extension maintains a local monotonic authority deadline and exposes it through
`UserContext.isValid()`, which Apache's native session eviction checks even while
tunnels are active.

An authenticated JSON identity `access-<bootstrap UUID>` binds all its native
contexts, including PostgreSQL's supplementary context, to the original portal
bootstrap. Privileged views and redecoration retain that lease. Denial and expiry
are irreversible for the native session.

Configuration uses the existing common `OPENFGA_ENABLED` activation. When it is
enabled, unbound or non-JSON authenticated identities are refused. Explicitly
bound identities require renewal configuration even before common activation.

- `GUACAMOLE_CONTROL_URL`: fixed administrator-configured HTTPS authority endpoint.
- `GUACAMOLE_CONTROL_AUDIENCE`: audience of the metadata-issued Google ID token.

The portal independently verifies the configured audience, gateway email and
immutable numeric subject. Google's SDK requests full-format metadata identity
and caches its token. No application bearer credential is sent or stored here.
The control body contains only the bootstrap UUID. Redirects, URL credentials,
unknown response fields, duplicate fields and trailing JSON values are refused.
Loopback HTTP exists solely for local transport tests.

The lease lasts 90 seconds; renewal runs every 15 seconds. At most 512 decorated
contexts are registered, with four concurrent renewal workers and one bounded
queue slot per admitted context. A stalled or saturated control path cannot
extend the local deadline. Apache's one-minute eviction interval and guest
shutdown time must be included in native qualification.

The parent Dockerfile compiles against the provided Guacamole API and packages
Google's SDK into the extension, with its namespace relocated to avoid Guacamole
library conflicts. Third-party licenses and notices remain in the jar. Apache's
entrypoint hook installs the jar after runtime-home generation.

CI compiles with Java warnings treated as errors, runs JUnit and native local
SSH/SFTP/RDP qualification, checks bytecode with SpotBugs/Find Security Bugs, and
feeds JaCoCo coverage to the existing Sonar gate. The narrow SSRF scanner
disposition documents the immutable administrator-configured origin; it does
not exempt caller-controlled URLs or weaken redirect refusal.

See [continuing-access qualification](../../../../docs/technical/shifter_platform/content-range-authority.md)
for measured fixture outcomes and the remaining production/load qualification.
