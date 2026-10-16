package com.shifter.guacamole;

import java.time.Duration;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class GatewayRenewalsTest {
    @Test void blockedControlWorkersCannotBlockLocalExpiryForTheAdmittedLoad() throws Exception {
        AtomicLong clock = new AtomicLong();
        AtomicBoolean unavailable = new AtomicBoolean();
        CountDownLatch blocked = new CountDownLatch(4);
        CountDownLatch unblock = new CountDownLatch(1);
        java.util.List<AuthorityLease> leases = new java.util.ArrayList<>();
        try (GatewayRenewals renewals = new GatewayRenewals(id -> {
            if (unavailable.get()) {
                blocked.countDown();
                try { unblock.await(5, TimeUnit.SECONDS); }
                catch (InterruptedException interrupted) { throw new java.io.IOException(); }
                throw new java.io.IOException("Synthetic outage");
            }
            return true;
        }, clock::get, Duration.ofNanos(100), 512)) {
            for (int session = 0; session < 512; session++) leases.add(renewals.admit(UUID.randomUUID()));
            unavailable.set(true);
            renewals.renew();
            assertTrue(blocked.await(2, TimeUnit.SECONDS));
            clock.set(100);
            for (AuthorityLease lease : leases) {
                assertFalse(lease.isValid());
                assertFalse(lease.renew());
            }
            unblock.countDown();
        }
    }
    @Test void everyAdmittedSessionGetsABoundedRenewalSlotUnderConcurrentLoad() throws Exception {
        AtomicBoolean renewing = new AtomicBoolean();
        CountDownLatch unblock = new CountDownLatch(1);
        CountDownLatch renewed = new CountDownLatch(512);
        try (GatewayRenewals renewals = new GatewayRenewals(id -> {
            if (renewing.get()) {
                try { unblock.await(5, TimeUnit.SECONDS); }
                catch (InterruptedException interrupted) { throw new java.io.IOException(); }
                renewed.countDown();
            }
            return true;
        }, () -> 0, Duration.ofSeconds(90), 512)) {
            for (int session = 0; session < 512; session++) renewals.admit(UUID.randomUUID());
            renewing.set(true);
            renewals.renew();
            unblock.countDown();
            assertTrue(renewed.await(5, TimeUnit.SECONDS), "Admitted sessions must not be skipped by queue saturation");
        }
    }
    @Test void deniedInitialAuthorityCannotCreateASession() throws Exception {
        try (GatewayRenewals renewals = new GatewayRenewals(id -> false, () -> 0, Duration.ofSeconds(90), 1)) {
            assertThrows(Exception.class, () -> renewals.admit(UUID.randomUUID()));
        }
    }

    @Test void admissionIsBoundedAndInvalidationReleasesCapacity() throws Exception {
        try (GatewayRenewals renewals = new GatewayRenewals(id -> true, () -> 0, Duration.ofSeconds(90), 1)) {
            AuthorityLease first = renewals.admit(UUID.randomUUID());
            assertThrows(Exception.class, () -> renewals.admit(UUID.randomUUID()));
            renewals.release(first);
            renewals.release(first);
            assertFalse(first.isValid());
            assertNotNull(renewals.admit(UUID.randomUUID()));
            assertThrows(Exception.class, () -> renewals.admit(UUID.randomUUID()));
        }
    }

    @Test void gatewayShutdownInvalidatesEveryRegisteredSession() throws Exception {
        GatewayRenewals renewals = new GatewayRenewals(id -> true, () -> 0, Duration.ofSeconds(90), 1);
        AuthorityLease lease = renewals.admit(UUID.randomUUID());
        renewals.close();
        assertFalse(lease.isValid());
        assertThrows(Exception.class, () -> renewals.admit(UUID.randomUUID()));
    }

    @Test void slowInitialControlCannotInstallAnAlreadyExpiredLease() throws Exception {
        AtomicLong clock = new AtomicLong();
        try (GatewayRenewals renewals = new GatewayRenewals(id -> { clock.set(100); return true; }, clock::get, Duration.ofNanos(100), 1)) {
            assertThrows(Exception.class, () -> renewals.admit(UUID.randomUUID()));
        }
    }
}
