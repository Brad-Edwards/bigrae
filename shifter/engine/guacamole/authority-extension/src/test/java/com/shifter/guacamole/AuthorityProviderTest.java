package com.shifter.guacamole;

import java.time.Duration;
import java.util.UUID;
import org.apache.guacamole.GuacamoleException;
import org.apache.guacamole.net.auth.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class AuthorityProviderTest {
    private AuthenticationProvider source(String identifier) {
        return new AbstractAuthenticationProvider() {
            public String getIdentifier() { return identifier; }
        };
    }
    private AuthenticatedUser user(String identity, String source) {
        AuthenticatedUser user = new AbstractAuthenticatedUser(false) {
            public AuthenticationProvider getAuthenticationProvider() { return source(source); }
            public Credentials getCredentials() { return null; }
        };
        user.setIdentifier(identity);
        return user;
    }
    private UserContext context(String identity) {
        return context(identity, "json");
    }

    private UserContext context(String identity, String provider) {
        User self = new AbstractUser(false) {};
        self.setIdentifier(identity);
        return new AbstractUserContext() {
            public User self() { return self; }
            public AuthenticationProvider getAuthenticationProvider() { return source(provider); }
            public boolean isValid() { return true; }
        };
    }
    private GatewayRenewals renewals() {
        return new GatewayRenewals(id -> true, () -> 0, Duration.ofSeconds(90), 2);
    }

    @Test void enabledGatewayRefusesUnboundAndNonJsonIdentities() {
        try (GatewayRenewals renewals = renewals()) {
            AuthorityProvider provider = new AuthorityProvider(true, renewals);
            String bound = "access-" + UUID.randomUUID();
            assertThrows(GuacamoleException.class, () -> provider.decorate(context("legacy"), user("legacy", "json"), null));
            assertThrows(GuacamoleException.class, () -> provider.decorate(context(bound), user(bound, "other"), null));
        }
    }

    @Test void supplementalDatabaseContextRetainsTheJsonAuthenticatedAuthority() throws Exception {
        try (GatewayRenewals renewals = renewals()) {
            AuthorityProvider provider = new AuthorityProvider(true, renewals);
            String bound = "access-" + UUID.randomUUID();
            UserContext decorated = provider.decorate(context(bound, "postgresql"), user(bound, "json"), null);
            assertInstanceOf(AuthorityUserContext.class, decorated);
            ((AuthorityUserContext) decorated).lease().deny();
            assertFalse(decorated.isValid());
        }
    }

    @Test void nativeContextValidityTracksOriginalLeaseAcrossRedecoration() throws Exception {
        try (GatewayRenewals renewals = renewals()) {
            AuthorityProvider provider = new AuthorityProvider(true, renewals);
            String bound = "access-" + UUID.randomUUID();
            UserContext original = provider.decorate(context(bound), user(bound, "json"), null);
            assertInstanceOf(AuthorityUserContext.class, original);
            ((AuthorityUserContext) original).lease().deny();
            UserContext updated = provider.redecorate(original, context(bound), user(bound, "json"), null);
            assertFalse(updated.isValid());
            assertThrows(GuacamoleException.class, () -> provider.redecorate(original, context("different"), user("different", "json"), null));
        }
    }

    @Test void legacyModeStillProtectsExplicitlyBoundPrincipalSessions() throws Exception {
        try (GatewayRenewals renewals = renewals()) {
            AuthorityProvider provider = new AuthorityProvider(false, renewals);
            String bound = "access-" + UUID.randomUUID();
            assertInstanceOf(AuthorityUserContext.class, provider.decorate(context(bound), user(bound, "json"), null));
            UserContext legacy = context("legacy");
            assertSame(legacy, provider.decorate(legacy, user("legacy", "json"), null));
        }
    }

    @Test void everyModeRefusesPasswordLoginsFromTheDatabaseProvider() {
        try (GatewayRenewals renewals = renewals()) {
            for (boolean enabled : new boolean[] {false, true}) {
                AuthorityProvider provider = new AuthorityProvider(enabled, renewals);
                assertThrows(GuacamoleException.class,
                    () -> provider.decorate(context("guacadmin", "postgresql"), user("guacadmin", "postgresql"), null));
                assertThrows(GuacamoleException.class,
                    () -> provider.redecorate(context("guacadmin", "postgresql"), context("guacadmin", "postgresql"),
                                              user("guacadmin", "postgresql"), null));
            }
        }
    }

    @Test void mutableDelegateCannotRebindTheRecordedSessionDuringRedecoration() throws Exception {
        try (GatewayRenewals renewals = renewals()) {
            AuthorityProvider provider = new AuthorityProvider(true, renewals);
            String originalBinding = "access-" + UUID.randomUUID();
            String otherBinding = "access-" + UUID.randomUUID();
            UserContext decorated = provider.decorate(context(originalBinding), user(originalBinding, "json"), null);
            decorated.self().setIdentifier(otherBinding);
            assertThrows(GuacamoleException.class, () -> provider.redecorate(decorated, context(otherBinding), user(otherBinding, "json"), null));
        }
    }

    @Test void unknownGatewayIdentityKindLeavesBoundSessionsRefused() {
        assertNull(AuthorityProvider.configuredRenewals("https://portal.example.test/control", "synthetic-control", "azure"));
        assertNull(AuthorityProvider.configuredRenewals(null, "synthetic-control", "aws"));
        AuthorityProvider provider = new AuthorityProvider(true, null);
        String bound = "access-" + UUID.randomUUID();
        assertThrows(GuacamoleException.class, () -> provider.decorate(context(bound), user(bound, "json"), null));
        provider.shutdown();
    }
}
