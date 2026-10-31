package com.shifter.guacamole;

import java.io.IOException;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class AwsGatewayIdentityTest {
    private static final Instant NOW = Instant.parse("2026-10-29T12:00:00Z");

    private static final class MutableClock extends Clock {
        private final AtomicReference<Instant> now = new AtomicReference<>(NOW);
        @Override public ZoneOffset getZone() { return ZoneOffset.UTC; }
        @Override public Clock withZone(java.time.ZoneId zone) { return this; }
        @Override public Instant instant() { return now.get(); }
        void advanceSeconds(long seconds) { now.set(now.get().plusSeconds(seconds)); }
    }

    @Test void nativeTokenIsCachedForTheExactAudienceAndRenewedBeforeExpiry() throws Exception {
        MutableClock clock = new MutableClock();
        AtomicInteger minted = new AtomicInteger();
        AwsGatewayIdentity.Minter minter = audience -> {
            assertEquals("https://portal.example.test", audience);
            int count = minted.incrementAndGet();
            return new AwsGatewayIdentity.Minted("synthetic-token-" + count, clock.instant().plusSeconds(900));
        };
        try (AwsGatewayIdentity identity = new AwsGatewayIdentity("https://portal.example.test", minter, clock)) {
            assertEquals("Bearer synthetic-token-1", identity.assertion());
            clock.advanceSeconds(830);
            assertEquals("Bearer synthetic-token-1", identity.assertion());
            assertEquals(1, minted.get());
            clock.advanceSeconds(20);
            assertEquals("Bearer synthetic-token-2", identity.assertion());
            assertEquals(2, minted.get());
        }
    }

    @Test void providerFailuresNeverExposeTokenOrProviderText() {
        AwsGatewayIdentity.Minter minter = audience -> { throw new IllegalStateException("SYNTHETIC_SENSITIVE_PROVIDER_RESPONSE"); };
        try (AwsGatewayIdentity identity = new AwsGatewayIdentity("https://portal.example.test", minter, Clock.systemUTC())) {
            IOException error = assertThrows(IOException.class, identity::assertion);
            assertEquals("Gateway identity unavailable", error.getMessage());
            assertNull(error.getCause());
        }
    }

    @Test void expiredOrMalformedNativeResultsAreRefused() {
        Clock clock = Clock.fixed(NOW, ZoneOffset.UTC);
        AwsGatewayIdentity.Minter expired = audience -> new AwsGatewayIdentity.Minted("synthetic", NOW.minusSeconds(1));
        AwsGatewayIdentity.Minter blank = audience -> new AwsGatewayIdentity.Minted("", NOW.plusSeconds(900));
        AwsGatewayIdentity.Minter injected = audience -> new AwsGatewayIdentity.Minted("a\r\nX: y", NOW.plusSeconds(900));
        for (AwsGatewayIdentity.Minter minter : java.util.List.of(expired, blank, injected)) {
            try (AwsGatewayIdentity identity = new AwsGatewayIdentity("https://portal.example.test", minter, clock)) {
                assertThrows(IOException.class, identity::assertion);
            }
        }
    }

    @Test void aBlockedProviderCannotHoldAnAdmissionBeyondTheIdentityTimeout() {
        CountDownLatch unblock = new CountDownLatch(1);
        AwsGatewayIdentity.Minter minter = audience -> {
            unblock.await(10, TimeUnit.SECONDS);
            return new AwsGatewayIdentity.Minted("synthetic", NOW.plusSeconds(900));
        };
        try (AwsGatewayIdentity identity = new AwsGatewayIdentity("https://portal.example.test", minter, Clock.systemUTC())) {
            long started = System.nanoTime();
            assertThrows(IOException.class, identity::assertion);
            assertTrue(TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started) < 4000);
        }
        finally { unblock.countDown(); }
    }

    @Test void missingAudienceIsRefusedBeforeAnyProviderRequest() {
        assertThrows(IllegalArgumentException.class,
            () -> new AwsGatewayIdentity("", audience -> { throw new AssertionError(); }, Clock.systemUTC()));
    }

    @Test void identitySelectionDefaultsToGoogleAndFailsClosedOnUnknownKinds() {
        assertInstanceOf(GoogleGatewayIdentity.class, GatewayControl.identity(null, "synthetic-control"));
        assertInstanceOf(GoogleGatewayIdentity.class, GatewayControl.identity("google", "synthetic-control"));
        assertInstanceOf(AwsGatewayIdentity.class, GatewayControl.identity("aws", "https://portal.example.test"));
        assertThrows(IllegalArgumentException.class, () -> GatewayControl.identity("azure", "synthetic-control"));
        assertThrows(IllegalArgumentException.class, () -> GatewayControl.identity("AWS ", "synthetic-control"));
    }
}
