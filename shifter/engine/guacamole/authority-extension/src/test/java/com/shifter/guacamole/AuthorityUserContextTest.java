package com.shifter.guacamole;

import java.time.Duration;
import java.util.concurrent.atomic.AtomicInteger;
import org.apache.guacamole.net.auth.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class AuthorityUserContextTest {
    private UserContext nativeContext() {
        return new AbstractUserContext() {
            public User self() { return null; }
            public AuthenticationProvider getAuthenticationProvider() { return null; }
            public boolean isValid() { return true; }
        };
    }

    @Test void nativeEvictionObservesPolicyDenial() {
        AuthorityLease lease = new AuthorityLease(() -> 0, Duration.ofSeconds(90));
        UserContext context = new AuthorityUserContext(nativeContext(), lease, () -> {});
        assertTrue(context.isValid());
        lease.deny();
        assertFalse(context.isValid());
    }

    @Test void privilegedViewRetainsTheSameAuthorityLease() {
        AuthorityLease lease = new AuthorityLease(() -> 0, Duration.ofSeconds(90));
        UserContext context = new AuthorityUserContext(nativeContext(), lease, () -> {});
        UserContext privileged = context.getPrivileged();
        lease.deny();
        assertFalse(privileged.isValid());
    }

    @Test void invalidationReleasesCapacityExactlyOnceAndLatchesTheLease() {
        AtomicInteger released = new AtomicInteger();
        AuthorityLease lease = new AuthorityLease(() -> 0, Duration.ofSeconds(90));
        UserContext context = new AuthorityUserContext(nativeContext(), lease, released::incrementAndGet);
        context.invalidate();
        context.invalidate();
        assertEquals(1, released.get());
        assertFalse(lease.renew());
    }
}
