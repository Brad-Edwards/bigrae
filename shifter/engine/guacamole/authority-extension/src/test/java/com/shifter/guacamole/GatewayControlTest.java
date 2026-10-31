package com.shifter.guacamole;

import com.sun.net.httpserver.HttpServer;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import static org.junit.jupiter.api.Assertions.*;

class GatewayControlTest {
    @Test void workloadAssertionAndOpaqueBindingAreTheOnlyRequestAuthority() throws Exception {
        UUID bootstrap = UUID.randomUUID();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/authority", exchange -> {
            String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            boolean valid = "POST".equals(exchange.getRequestMethod())
                && "Bearer synthetic-workload".equals(exchange.getRequestHeaders().getFirst("Authorization"))
                && body.equals("{\"bootstrap\":\"" + bootstrap + "\"}");
            byte[] response = ("{\"allowed\":" + valid + "}").getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, response.length);
            exchange.getResponseBody().write(response);
            exchange.close();
        });
        server.start();
        try {
            GatewayControl control = new GatewayControl(URI.create("http://127.0.0.1:" + server.getAddress().getPort() + "/authority"), () -> "Bearer synthetic-workload");
            assertTrue(control.authorize(bootstrap));
            assertFalse(control.authorize(UUID.randomUUID()));
        }
        finally { server.stop(0); }
    }

    @ParameterizedTest
    @ValueSource(strings = {"{\"allowed\":true,\"additional\":true}", "{\"allowed\":\"true\"}",
        "{\"allowed\":false,\"allowed\":true}", "{\"allowed\":true}{\"other\":true}"})
    void malformedControlRepliesCannotExtendAuthority(String reply) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/authority", exchange -> {
            byte[] response = reply.getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, response.length);
            exchange.getResponseBody().write(response);
            exchange.close();
        });
        server.start();
        try {
            GatewayControl control = new GatewayControl(URI.create("http://127.0.0.1:" + server.getAddress().getPort() + "/authority"), () -> "Bearer synthetic-workload");
            assertFalse(control.authorize(UUID.randomUUID()));
        }
        finally { server.stop(0); }
    }

    @Test void workloadTokensCannotBeSentToCleartextRemoteOriginsOrRedirectTargets() {
        assertThrows(IllegalArgumentException.class, () -> new GatewayControl(URI.create("http://remote.example.test/authority"), () -> "Bearer synthetic"));
        assertThrows(IllegalArgumentException.class, () -> new GatewayControl(URI.create("https://user:password@remote.example.test/authority"), () -> "Bearer synthetic"));
    }

    @Test void refusedOriginIsRejectedBeforeAnIdentityExists() {
        // An unknown kind fails only at identity selection, so this message proves the origin check ran first.
        IllegalArgumentException refused = assertThrows(IllegalArgumentException.class,
            () -> new GatewayControl("http://remote.example.test/authority", "synthetic-control", "azure"));
        assertEquals("Invalid gateway control origin", refused.getMessage());
    }
}
