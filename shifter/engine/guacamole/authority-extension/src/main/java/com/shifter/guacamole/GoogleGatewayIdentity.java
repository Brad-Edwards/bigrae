package com.shifter.guacamole;

import com.google.auth.oauth2.ComputeEngineCredentials;
import com.google.auth.oauth2.IdTokenCredentials;
import com.google.auth.oauth2.AccessToken;
import com.google.auth.oauth2.IdTokenProvider;
import java.io.IOException;
import java.util.concurrent.*;

/** Google SDK metadata identity; no service account keys or application tokens. */
final class GoogleGatewayIdentity implements GatewayControl.Identity, AutoCloseable {
    private final IdTokenCredentials credentials;
    private final ThreadPoolExecutor worker;

    GoogleGatewayIdentity(String audience) {
        this(credentials(audience));
    }

    private static IdTokenCredentials credentials(String audience) {
        return credentials(audience, ComputeEngineCredentials.create());
    }

    static IdTokenCredentials credentials(String audience, IdTokenProvider provider) {
        if (audience == null || audience.isEmpty()) throw new IllegalArgumentException("Missing gateway audience");
        return IdTokenCredentials.newBuilder()
            .setIdTokenProvider(provider).setTargetAudience(audience)
            .setOptions(java.util.Collections.singletonList(IdTokenProvider.Option.FORMAT_FULL)).build();
    }

    GoogleGatewayIdentity(IdTokenCredentials credentials) {
        this.credentials = credentials;
        worker = new ThreadPoolExecutor(1, 1, 0, TimeUnit.SECONDS, new ArrayBlockingQueue<>(1), task -> {
            Thread thread = new Thread(task, "shifter-guacamole-workload-identity");
            thread.setDaemon(true);
            return thread;
        }, new ThreadPoolExecutor.AbortPolicy());
    }

    public String assertion() throws IOException {
        Future<String> result = worker.submit(() -> {
            credentials.refreshIfExpired();
            AccessToken token = credentials.getAccessToken();
            if (token == null) throw new IOException("Gateway identity unavailable");
            return "Bearer " + token.getTokenValue();
        });
        try { return result.get(3, TimeUnit.SECONDS); }
        catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            throw new IOException("Gateway identity unavailable");
        }
        catch (ExecutionException | TimeoutException unavailable) {
            throw new IOException("Gateway identity unavailable");
        }
        finally { result.cancel(true); worker.purge(); }
    }

    @Override public void close() { worker.shutdownNow(); }
}
