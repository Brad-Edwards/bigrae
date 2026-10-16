package com.shifter.guacamole;

import java.time.Duration;
import java.util.concurrent.atomic.AtomicLong;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class AuthorityLeaseTest {
    @Test void controlOutageExpiresTheSessionAtTheDeadline() {
        AtomicLong clock = new AtomicLong(10);
        AuthorityLease lease = new AuthorityLease(clock::get, Duration.ofNanos(100));
        clock.set(109);
        assertTrue(lease.isValid());
        clock.set(110);
        assertFalse(lease.isValid());
        assertFalse(lease.renew());
    }

    @Test void LateRenewalCannotReviveAnExpiredSessionEvenBeforeEviction() {
        AtomicLong clock = new AtomicLong(0);
        AuthorityLease lease = new AuthorityLease(clock::get, Duration.ofNanos(100));
        clock.set(101);
        assertFalse(lease.renew());
        assertFalse(lease.isValid());
    }

    @Test void policyDenialLatchesDespiteLaterSuccessfulControlReplies() {
        AuthorityLease lease = new AuthorityLease(() -> 0L, Duration.ofSeconds(90));
        lease.deny();
        assertFalse(lease.renew());
        assertFalse(lease.isValid());
    }

    @Test void successfulRenewalExtendsOnlyAnUnexpiredLease() {
        AtomicLong clock = new AtomicLong(0);
        AuthorityLease lease = new AuthorityLease(clock::get, Duration.ofNanos(100));
        clock.set(99);
        assertTrue(lease.renew());
        clock.set(150);
        assertTrue(lease.isValid());
        clock.set(199);
        assertFalse(lease.isValid());
    }

    @Test void monotonicCounterWrapDoesNotExtendTheLease() {
        AtomicLong clock = new AtomicLong(Long.MAX_VALUE - 50);
        AuthorityLease lease = new AuthorityLease(clock::get, Duration.ofNanos(100));
        clock.addAndGet(99);
        assertTrue(lease.isValid());
        clock.incrementAndGet();
        assertFalse(lease.isValid());
    }
}
