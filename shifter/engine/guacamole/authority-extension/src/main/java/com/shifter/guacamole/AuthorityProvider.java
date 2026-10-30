package com.shifter.guacamole;

import java.time.Duration;
import java.util.UUID;
import java.util.concurrent.*;
import org.apache.guacamole.GuacamoleException;
import org.apache.guacamole.GuacamoleSecurityException;
import org.apache.guacamole.net.auth.*;

public final class AuthorityProvider extends AbstractAuthenticationProvider {
    private final boolean enabled;
    private final GatewayRenewals renewals;
    private final ScheduledExecutorService timer;

    public AuthorityProvider() {
        this(Boolean.parseBoolean(java.util.Objects.toString(System.getenv("OPENFGA_ENABLED"), "false").trim()),
             configuredRenewals(System.getenv("GUACAMOLE_CONTROL_URL"), System.getenv("GUACAMOLE_CONTROL_AUDIENCE"),
                                System.getenv("GUACAMOLE_CONTROL_IDENTITY")));
    }

    /** Build renewals from deployment settings; incomplete or unknown settings leave sessions refused. */
    static GatewayRenewals configuredRenewals(String endpoint, String audience, String kind) {
        if (endpoint == null || audience == null) return null;
        try {
            return new GatewayRenewals(new GatewayControl(endpoint, audience, kind), System::nanoTime,
                                       Duration.ofSeconds(90), 512);
        }
        catch (IllegalArgumentException unsupported) { return null; }
    }

    AuthorityProvider(boolean enabled, GatewayRenewals renewals) {
        this.enabled = enabled;
        this.renewals = renewals;
        timer = Executors.newSingleThreadScheduledExecutor(task -> {
            Thread thread = new Thread(task, "shifter-guacamole-authority-timer");
            thread.setDaemon(true);
            return thread;
        });
        if (renewals != null) timer.scheduleAtFixedRate(renewals::renew, 15, 15, TimeUnit.SECONDS);
    }

    @Override public String getIdentifier() { return "shifter-authority"; }

    private UUID binding(UserContext context, AuthenticatedUser user) throws GuacamoleException {
        // The portal issues every session through signed JSON auth. Refuse an
        // identity from any other provider in every mode, for example a password
        // login against the PostgreSQL extension on the public login page.
        AuthenticationProvider source = user.getAuthenticationProvider();
        if (source == null || !"json".equals(source.getIdentifier()))
            throw new GuacamoleSecurityException("Gateway authority refused");
        String identifier = user.getIdentifier();
        if (!enabled && (identifier == null || !identifier.startsWith("access-"))) return null;
        try {
            if (!"json".equals(user.getAuthenticationProvider().getIdentifier())
                || !identifier.equals(context.self().getIdentifier()) || !identifier.startsWith("access-"))
                throw new IllegalArgumentException();
            String value = identifier.substring(7);
            UUID binding = UUID.fromString(value);
            if (!value.equals(binding.toString())) throw new IllegalArgumentException();
            return binding;
        }
        catch (RuntimeException invalid) { throw new GuacamoleSecurityException("Gateway authority refused"); }
    }

    @Override public UserContext decorate(UserContext context, AuthenticatedUser user, Credentials credentials)
            throws GuacamoleException {
        UUID binding = binding(context, user);
        if (binding == null) return context;
        try {
            if (renewals == null) throw new IllegalStateException();
            AuthorityLease lease = renewals.admit(binding);
            return new AuthorityUserContext(context, lease, () -> renewals.release(lease));
        }
        catch (java.io.IOException | RuntimeException refused) {
            throw new GuacamoleSecurityException("Gateway authority unavailable");
        }
    }

    @Override public UserContext redecorate(UserContext decorated, UserContext context,
            AuthenticatedUser user, Credentials credentials) throws GuacamoleException {
        if (decorated instanceof AuthorityUserContext) {
            binding(context, user);
            if (!((AuthorityUserContext) decorated).originalIdentity().equals(user.getIdentifier()))
                throw new GuacamoleSecurityException("Gateway authority refused");
            return ((AuthorityUserContext) decorated).redecorate(context);
        }
        return decorate(context, user, credentials);
    }

    @Override public void shutdown() {
        timer.shutdownNow();
        if (renewals != null) renewals.close();
    }
}
