package com.shifter.guacamole;

import com.google.auth.oauth2.IdToken;
import com.google.auth.oauth2.IdTokenCredentials;
import com.google.auth.oauth2.IdTokenProvider;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class GoogleGatewayIdentityTest {
    private String token() {
        return Base64.getUrlEncoder().withoutPadding().encodeToString("{\"alg\":\"RS256\"}".getBytes(StandardCharsets.UTF_8))
            + "." + Base64.getUrlEncoder().withoutPadding().encodeToString(
                ("{\"exp\":" + (System.currentTimeMillis() / 1000 + 3600) + "}").getBytes(StandardCharsets.UTF_8))
            + ".synthetic-signature";
    }

    @Test void nativeSdkCachesTheGatewayAssertionForItsExactAudience() throws Exception {
        String token = token();
        AtomicInteger minted = new AtomicInteger();
        IdTokenCredentials credentials = GoogleGatewayIdentity.credentials("synthetic-control", (audience, options) -> {
                assertEquals("synthetic-control", audience);
                assertTrue(options != null && options.contains(IdTokenProvider.Option.FORMAT_FULL));
                minted.incrementAndGet();
                return IdToken.create(token);
            });
        try (GoogleGatewayIdentity identity = new GoogleGatewayIdentity(credentials)) {
            assertEquals("Bearer " + token, identity.assertion());
            assertEquals("Bearer " + token, identity.assertion());
            assertEquals(1, minted.get());
        }
    }

    @Test void providerFailuresNeverExposeTheIdentityAssertionInExceptions() {
        IdTokenCredentials credentials = IdTokenCredentials.newBuilder().setTargetAudience("synthetic-control")
            .setIdTokenProvider((audience, options) -> { throw new IOException("SYNTHETIC_SENSITIVE_PROVIDER_RESPONSE"); }).build();
        try (GoogleGatewayIdentity identity = new GoogleGatewayIdentity(credentials)) {
            IOException error = assertThrows(IOException.class, identity::assertion);
            assertEquals("Gateway identity unavailable", error.getMessage());
            assertNull(error.getCause());
        }
    }

    @Test void aBlockedMetadataProviderCannotHoldAnAdmissionBeyondTheIdentityTimeout() {
        CountDownLatch unblock = new CountDownLatch(1);
        IdTokenCredentials credentials = IdTokenCredentials.newBuilder().setTargetAudience("synthetic-control")
            .setIdTokenProvider((audience, options) -> {
                try { unblock.await(10, TimeUnit.SECONDS); }
                catch (InterruptedException interrupted) { throw new IOException("Synthetic interruption"); }
                return IdToken.create(token());
            }).build();
        try (GoogleGatewayIdentity identity = new GoogleGatewayIdentity(credentials)) {
            long started = System.nanoTime();
            assertThrows(IOException.class, identity::assertion);
            assertTrue(TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started) < 4000);
        }
        finally { unblock.countDown(); }
    }

    @Test void missingAudienceIsRefusedBeforeAnyMetadataRequest() {
        assertThrows(IllegalArgumentException.class, () -> new GoogleGatewayIdentity(""));
    }
}
