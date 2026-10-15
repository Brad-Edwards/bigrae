package com.shifter.guacamole;

import org.apache.guacamole.net.auth.DelegatingUserContext;
import org.apache.guacamole.net.auth.UserContext;
import java.util.concurrent.atomic.AtomicBoolean;

final class AuthorityUserContext extends DelegatingUserContext {
    private final AuthorityLease lease;
    private final Runnable release;
    private final String originalIdentity;
    private final AtomicBoolean invalidated = new AtomicBoolean();

    AuthorityUserContext(UserContext delegate, AuthorityLease lease, Runnable release) {
        super(delegate);
        this.lease = lease;
        this.release = release;
        this.originalIdentity = delegate.self() == null ? null : delegate.self().getIdentifier();
    }

    AuthorityLease lease() { return lease; }
    String originalIdentity() { return originalIdentity; }
    AuthorityUserContext redecorate(UserContext delegate) {
        return new AuthorityUserContext(delegate, lease, release);
    }

    @Override public boolean isValid() {
        return lease.isValid() && super.isValid();
    }

    @Override public UserContext getPrivileged() {
        // A privileged directory view must not escape session invalidation.
        return new AuthorityUserContext(super.getPrivileged(), lease, () -> {});
    }

    @Override public void invalidate() {
        lease.deny();
        if (invalidated.compareAndSet(false, true)) {
            try { super.invalidate(); }
            finally { release.run(); }
        }
    }
}
