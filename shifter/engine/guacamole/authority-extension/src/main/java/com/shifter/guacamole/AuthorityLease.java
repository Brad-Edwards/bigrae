package com.shifter.guacamole;

import java.time.Duration;
import java.util.function.LongSupplier;

/** Local monotonic lease; an expired or denied session can never be revived. */
final class AuthorityLease {
    private final LongSupplier clock;
    private final long lifetimeNanos;
    private long deadline;
    private boolean denied;

    AuthorityLease(LongSupplier clock, Duration lifetime) {
        this.clock = clock;
        this.lifetimeNanos = lifetime.toNanos();
        if (lifetimeNanos <= 0 || lifetime.compareTo(Duration.ofSeconds(90)) > 0)
            throw new IllegalArgumentException("Invalid authority lease lifetime");
        deadline = clock.getAsLong() + lifetimeNanos;
    }

    synchronized boolean isValid() {
        // Subtraction remains valid when System.nanoTime() wraps.
        if (clock.getAsLong() - deadline >= 0) denied = true;
        return !denied;
    }

    synchronized boolean renew() {
        if (!isValid()) return false;
        deadline = clock.getAsLong() + lifetimeNanos;
        return true;
    }

    synchronized void deny() { denied = true; }
}
