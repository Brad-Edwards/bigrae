package com.shifter.guacamole;

import java.time.Duration;
import java.util.UUID;
import java.util.function.LongSupplier;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import java.io.IOException;

final class GatewayRenewals implements AutoCloseable {
    interface Control extends AutoCloseable {
        boolean authorize(UUID bootstrap) throws IOException;
        default void close() {}
    }
    private final Control control;
    private final LongSupplier clock;
    private final Duration lifetime;
    private final Semaphore capacity;
    private final ConcurrentMap<AuthorityLease, Registration> registrations = new ConcurrentHashMap<>();
    private final ThreadPoolExecutor workers;
    private boolean closed;

    private static final class Registration {
        final UUID bootstrap;
        final AtomicBoolean pending = new AtomicBoolean();
        Registration(UUID bootstrap) { this.bootstrap = bootstrap; }
    }

    GatewayRenewals(Control control, LongSupplier clock, Duration lifetime, int capacity) {
        if (capacity < 1 || capacity > 512) throw new IllegalArgumentException("Invalid gateway capacity");
        this.control = control;
        this.clock = clock;
        this.lifetime = lifetime;
        this.capacity = new Semaphore(capacity);
        workers = new ThreadPoolExecutor(4, 4, 0, TimeUnit.SECONDS, new ArrayBlockingQueue<>(capacity), task -> {
            Thread thread = new Thread(task, "shifter-guacamole-renewal");
            thread.setDaemon(true);
            return thread;
        }, new ThreadPoolExecutor.AbortPolicy());
    }

    AuthorityLease admit(UUID bootstrap) throws IOException {
        if (!capacity.tryAcquire()) throw new IllegalStateException("Gateway authority capacity exhausted");
        AuthorityLease lease = new AuthorityLease(clock, lifetime);
        boolean installed = false;
        try {
            if (!control.authorize(bootstrap) || !lease.isValid())
                throw new IllegalStateException("Gateway authority refused");
            synchronized (this) {
                if (closed) throw new IllegalStateException("Gateway authority unavailable");
                registrations.put(lease, new Registration(bootstrap));
                installed = true;
            }
            return lease;
        }
        finally {
            if (!installed) { lease.deny(); capacity.release(); }
        }
    }

    void release(AuthorityLease lease) {
        lease.deny();
        if (registrations.remove(lease) != null) capacity.release();
    }

    void renew() {
        registrations.forEach((lease, registration) -> {
            if (!lease.isValid()) { release(lease); return; }
            if (!registration.pending.compareAndSet(false, true)) return;
            try {
                workers.execute(() -> renewLease(lease, registration));
            }
            catch (RejectedExecutionException saturated) {
                registration.pending.set(false);
                // A full bounded queue cannot extend a lease.
            }
        });
    }

    private void renewLease(AuthorityLease lease, Registration registration) {
        try {
            if (!lease.isValid()) return;
            if (control.authorize(registration.bootstrap)) lease.renew();
            else lease.deny();
        }
        catch (Exception unavailable) {
            // Failure retains the existing deadline without logging proof or response data.
        }
        finally { registration.pending.set(false); }
    }

    public synchronized void close() {
        closed = true;
        registrations.keySet().forEach(this::release);
        workers.shutdownNow();
        control.close();
    }
}
