package com.shifter.guacamole;

import java.util.UUID;
import java.net.URI;
import java.net.HttpURLConnection;
import java.nio.charset.StandardCharsets;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.core.JsonProcessingException;
import java.io.IOException;

final class GatewayControl implements GatewayRenewals.Control, AutoCloseable {
    interface Identity extends AutoCloseable {
        String assertion() throws IOException;
        default void close() {}
    }
    private static final String ALLOWED = "allowed";
    private final URI endpoint;
    private final Identity identity;
    private final ObjectMapper json = new ObjectMapper()
        .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS)
        .enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION);

    GatewayControl(String endpoint, String audience) {
        this(URI.create(endpoint), new GoogleGatewayIdentity(audience));
    }

    GatewayControl(URI endpoint, Identity identity) {
        String host = endpoint.getHost();
        if (host == null || endpoint.getUserInfo() != null || endpoint.getFragment() != null
            || endpoint.getQuery() != null || !("https".equals(endpoint.getScheme())
                || ("http".equals(endpoint.getScheme()) && "127.0.0.1".equals(host))))
            throw new IllegalArgumentException("Invalid gateway control origin");
        this.endpoint = endpoint;
        this.identity = identity;
    }

    public boolean authorize(UUID bootstrap) throws IOException {
        String assertion = identity.assertion();
        if (!assertion.startsWith("Bearer ") || assertion.length() > 16_384
            || assertion.indexOf('\r') >= 0 || assertion.indexOf('\n') >= 0)
            throw new IllegalStateException("Gateway identity unavailable");
        HttpURLConnection request = (HttpURLConnection) endpoint.toURL().openConnection();
        request.setInstanceFollowRedirects(false);
        request.setConnectTimeout(2000);
        request.setReadTimeout(3000);
        request.setRequestMethod("POST");
        request.setRequestProperty("Authorization", assertion);
        request.setRequestProperty("Content-Type", "application/json");
        byte[] body = ("{\"bootstrap\":\"" + bootstrap + "\"}").getBytes(StandardCharsets.UTF_8);
        request.setDoOutput(true);
        request.setFixedLengthStreamingMode(body.length);
        try {
            try (java.io.OutputStream output = request.getOutputStream()) { output.write(body); }
            if (request.getResponseCode() == 401 || request.getResponseCode() == 403) return false;
            if (request.getResponseCode() != 200) throw new IllegalStateException("Gateway control unavailable");
            byte[] response;
            try (java.io.InputStream input = request.getInputStream()) { response = input.readNBytes(1025); }
            if (response.length > 1024) return false;
            JsonNode grant;
            try { grant = json.readTree(response); }
            catch (JsonProcessingException malformed) { return false; }
            return grant != null && grant.isObject() && grant.size() == 1
                && grant.has(ALLOWED) && grant.get(ALLOWED).isBoolean()
                && grant.get(ALLOWED).booleanValue();
        }
        finally { request.disconnect(); }
    }

    @Override public void close() { identity.close(); }
}
