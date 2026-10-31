package com.shifter.guacamole;

import java.io.IOException;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.concurrent.*;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;
import software.amazon.awssdk.http.urlconnection.UrlConnectionHttpClient;
import software.amazon.awssdk.services.sts.StsClient;
import software.amazon.awssdk.services.sts.model.GetWebIdentityTokenRequest;
import software.amazon.awssdk.services.sts.model.GetWebIdentityTokenResponse;

/** AWS STS outbound web identity for the attached IRSA role; no keys or application tokens. */
final class AwsGatewayIdentity implements GatewayControl.Identity, AutoCloseable {
    /** Obtains one native token for an audience. */
    interface Minter extends AutoCloseable {
        Minted mint(String audience) throws IOException, InterruptedException;
        @Override default void close() {}
    }

    /** One native token and its provider-reported expiration. */
    static final class Minted {
        final String token;
        final Instant expiration;

        Minted(String token, Instant expiration) {
            this.token = token;
            this.expiration = expiration;
        }
    }

    private static final Duration RENEW_BEFORE = Duration.ofSeconds(60);
    private final String audience;
    private final Minter minter;
    private final Clock clock;
    private final ThreadPoolExecutor worker;
    private Minted cached;

    AwsGatewayIdentity(String audience) {
        this(audience, new StsMinter(), Clock.systemUTC());
    }

    AwsGatewayIdentity(String audience, Minter minter, Clock clock) {
        if (audience == null || audience.isEmpty()) throw new IllegalArgumentException("Missing gateway audience");
        this.audience = audience;
        this.minter = minter;
        this.clock = clock;
        worker = new ThreadPoolExecutor(1, 1, 0, TimeUnit.SECONDS, new ArrayBlockingQueue<>(1), task -> {
            Thread thread = new Thread(task, "shifter-guacamole-workload-identity");
            thread.setDaemon(true);
            return thread;
        }, new ThreadPoolExecutor.AbortPolicy());
    }

    private synchronized String current() throws IOException, InterruptedException {
        Instant now = clock.instant();
        if (cached == null || !now.isBefore(cached.expiration.minus(RENEW_BEFORE))) {
            Minted minted = minter.mint(audience);
            if (minted == null || minted.token == null || minted.token.isEmpty() || minted.expiration == null
                || !now.isBefore(minted.expiration) || minted.token.chars().anyMatch(c -> c <= 32 || c >= 127))
                throw new IOException("Gateway identity unavailable");
            cached = minted;
        }
        return "Bearer " + cached.token;
    }

    public String assertion() throws IOException {
        Future<String> result;
        try { result = worker.submit(this::current); }
        catch (RejectedExecutionException busy) { throw new IOException("Gateway identity unavailable"); }
        try { return result.get(3, TimeUnit.SECONDS); }
        catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            throw new IOException("Gateway identity unavailable");
        }
        catch (ExecutionException | TimeoutException unavailable) {
            // Provider text can carry request detail; never chain or log it.
            throw new IOException("Gateway identity unavailable");
        }
        finally { result.cancel(true); worker.purge(); }
    }

    @Override public void close() {
        worker.shutdownNow();
        minter.close();
    }

    /** Native SDK minter; the default chain renews IRSA web-identity credentials. */
    static final class StsMinter implements Minter {
        private StsClient client;

        private synchronized StsClient client() {
            if (client == null)
                client = StsClient.builder()
                    .credentialsProvider(DefaultCredentialsProvider.builder().build())
                    .httpClient(UrlConnectionHttpClient.builder()
                        .connectionTimeout(Duration.ofSeconds(2)).socketTimeout(Duration.ofSeconds(2)).build())
                    .build();
            return client;
        }

        @Override public Minted mint(String audience) {
            GetWebIdentityTokenResponse response = client().getWebIdentityToken(GetWebIdentityTokenRequest.builder()
                .audience(audience).signingAlgorithm("ES384").durationSeconds(900).build());
            return new Minted(response.webIdentityToken(), response.expiration());
        }

        @Override public synchronized void close() {
            if (client != null) client.close();
        }
    }
}
