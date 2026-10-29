# Guacamole Client
#
# Uses the official guacamole/guacamole image with a custom entrypoint that
# handles idempotent database schema initialization.
#
# The base image handles:
# - Extension loading (via JSON_ENABLED=true, OPENID_ENABLED=false)
#
# Our entrypoint adds:
# - Automatic schema creation on first startup (idempotent)
#
# Required environment variables:
# - JSON_SECRET_KEY: 128-bit hex key for signing/encrypting JSON auth payloads
# - POSTGRESQL_HOSTNAME, POSTGRESQL_DATABASE, POSTGRESQL_USER, POSTGRESQL_PASSWORD
#
# Reference: https://guacamole.apache.org/doc/gug/guacamole-docker.html

# Base image pinned by digest for reproducibility.
#
# This digest is the guacamole/guacamole:latest rebuild re-pointed 2026-10-03:
# the Guacamole 1.6.0 web application on a patched Apache Tomcat 9.0.x and
# Ubuntu 24.04. It is pinned by digest (not by the mutable :latest tag) so
# builds stay reproducible.
#
# Why :latest-by-digest and not the versioned :1.6.0 tag: the released :1.6.0
# tag bundles an older Tomcat/Ubuntu that still carry fixable HIGH/CRITICAL
# Tomcat + OS CVEs. The latest rebuild carries the patched Tomcat and Ubuntu
# base. This 2026-10-03 re-point clears the Ubuntu openssl finding
# CVE-2026-84782 (libssl3t64/openssl -> 3.0.13-0ubuntu3.16). Re-point this
# digest when a newer rebuild ships further OS/Tomcat fixes.
ARG GUACAMOLE_VERSION=1.6.0
# Maven 3.9.11 / Temurin 17; build tooling never enters the runtime image.
FROM maven@sha256:e4a7ace3dc0d645ed97f8d9ad0b0d3f0b14fa8d150138f27f116d7105a639b82 AS authority-build
WORKDIR /authority
COPY authority-extension/pom.xml ./
COPY authority-extension/spotbugs-exclude.xml ./
COPY authority-extension/src ./src
RUN mvn --batch-mode --no-transfer-progress \
    -Dtest=AuthorityLeaseTest,AuthorityUserContextTest,GatewayRenewalsTest,AuthorityProviderTest,GatewayControlTest,GoogleGatewayIdentityTest verify

# base image: guacamole 1.6.0 webapp on Tomcat 9.0.x / Ubuntu 24.04 (keep in sync with the digest below)
FROM guacamole/guacamole@sha256:1880e60e0b768294537a7071bc023fffbc8f4917b4f7caf0b72d8938ee4f238f AS guacamole-base

# ---------------------------------------------------------------------------
# Fixed bundled dependencies (#2610). Guacamole 1.6.0 bundles guava 32.1.3,
# jackson 2.19.0, and PostgreSQL JDBC 42.7.2, which carry fixable HIGH findings,
# and no Guacamole release ships the fixed versions. Each replacement jar is
# checked against the SHA-256 of its Maven Central artifact. patch_bundled_jars.py
# swaps the nested jars in the web application and the in-use extensions, and it
# fails when the upstream layout no longer has a match.
# ---------------------------------------------------------------------------
# base image: python:3.12-slim-trixie
FROM python@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS bundled-jars
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG MAVEN_CENTRAL=https://repo1.maven.org/maven2
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
WORKDIR /patch
RUN fetch() { curl --proto '=https' --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 -fsSL -o "$(basename "$1")" "${MAVEN_CENTRAL}/$1" && echo "$2  $(basename "$1")" | sha256sum -c -; } && \
    fetch com/google/guava/guava/33.7.2-jre/guava-33.7.2-jre.jar b530942257fb935f8b2cfaa5f8eb5bd59c555fd8e8d01b8ce98912e077ea606c && \
    fetch com/google/guava/failureaccess/1.0.3/failureaccess-1.0.3.jar cbfc3906b19b8f55dd7cfd6dfe0aa4532e834250d7f080bd8d211a3e246b59cb && \
    fetch com/fasterxml/jackson/core/jackson-core/2.21.8/jackson-core-2.21.8.jar 604ef7765013daf5cde192e40bc4c8412c4ca93f4aaf7bf8e9a62fad1e1026bb && \
    fetch com/fasterxml/jackson/core/jackson-databind/2.21.8/jackson-databind-2.21.8.jar ad77f2285717e2f34c319d988b7c85783a47d0b33f33a501d1fcbf196e61bbaa && \
    fetch com/fasterxml/jackson/core/jackson-annotations/2.21/jackson-annotations-2.21.jar 53ca085f4a150f703f49e1aabd935bd03b43e1ea3d55d135438292af22cef56b && \
    fetch com/fasterxml/jackson/module/jackson-module-jaxb-annotations/2.21.8/jackson-module-jaxb-annotations-2.21.8.jar 32a3b1ac114a3547c3ceb6e5a48091372de097e5fb6f87d660b18a2665405c36 && \
    fetch org/postgresql/postgresql/42.7.14/postgresql-42.7.14.jar 73914527305a40cce504b0d3d90b23caf565136912d607ae8ae7c5895512332c
COPY patch_bundled_jars.py /patch/patch_bundled_jars.py
COPY --from=guacamole-base /opt/guacamole/webapp/guacamole.war /archives/webapp/guacamole.war
COPY --from=guacamole-base /opt/guacamole/extensions/guacamole-auth-json/guacamole-auth-json.jar /archives/json/guacamole-auth-json.jar
COPY --from=guacamole-base /opt/guacamole/extensions/guacamole-auth-jdbc/postgresql/guacamole-auth-jdbc-postgresql.jar /archives/postgresql/guacamole-auth-jdbc-postgresql.jar
RUN python3 -I /patch/patch_bundled_jars.py \
      --replace 'guava-[0-9.]+-jre\.jar=/patch/guava-33.7.2-jre.jar' \
      --replace 'failureaccess-[0-9.]+\.jar=/patch/failureaccess-1.0.3.jar' \
      --replace 'jackson-core-[0-9.]+\.jar=/patch/jackson-core-2.21.8.jar' \
      --replace 'jackson-databind-[0-9.]+\.jar=/patch/jackson-databind-2.21.8.jar' \
      --replace 'jackson-annotations-[0-9.]+\.jar=/patch/jackson-annotations-2.21.jar' \
      --replace 'jackson-module-jaxb-annotations-[0-9.]+\.jar=/patch/jackson-module-jaxb-annotations-2.21.8.jar' \
      /archives/webapp/guacamole.war /archives/json/guacamole-auth-json.jar \
      /archives/postgresql/guacamole-auth-jdbc-postgresql.jar

FROM guacamole-base

# Install PostgreSQL client for schema initialization
USER root
# Shifter authenticates with JSON auth and stores sessions in PostgreSQL. The
# other upstream extensions and database drivers are unused, and they bundle
# vulnerable dependencies: Bouncy Castle in the SSO and vault extensions, Apache
# MINA in LDAP, jackson in duo and LDAP, guava in each extension, and the SQL
# Server and MySQL drivers. Remove them and their activation links instead of
# shipping dormant vulnerable code (#2237, #2610).
RUN rm -rf /opt/guacamole/extensions/guacamole-auth-sso \
    /opt/guacamole/extensions/guacamole-vault \
    /opt/guacamole/extensions/guacamole-auth-duo \
    /opt/guacamole/extensions/guacamole-auth-ldap \
    /opt/guacamole/extensions/guacamole-auth-header \
    /opt/guacamole/extensions/guacamole-auth-jdbc/mysql \
    /opt/guacamole/extensions/guacamole-auth-jdbc/sqlserver \
    /opt/guacamole/extensions/guacamole-auth-quickconnect \
    /opt/guacamole/extensions/guacamole-auth-restrict \
    /opt/guacamole/extensions/guacamole-auth-totp \
    /opt/guacamole/drivers/mysql-jdbc.jar \
    /opt/guacamole/drivers/mssql-jdbc.jar \
    /opt/guacamole/environment/CAS_ \
    /opt/guacamole/environment/SSL_AUTH_ \
    /opt/guacamole/environment/KSM_ \
    /opt/guacamole/environment/DUO_ \
    /opt/guacamole/environment/LDAP_ \
    /opt/guacamole/environment/HTTP_AUTH_ \
    /opt/guacamole/environment/MYSQL_ \
    /opt/guacamole/environment/SQLSERVER_ \
    /opt/guacamole/environment/OPENID_ \
    /opt/guacamole/environment/SAML_ \
    /opt/guacamole/environment/QUICKCONNECT_ \
    /opt/guacamole/environment/RESTRICT_ \
    /opt/guacamole/environment/TOTP_

# The web application, the in-use extensions, and the PostgreSQL driver with the
# fixed bundled dependencies (#2610).
COPY --from=bundled-jars /archives/webapp/guacamole.war /opt/guacamole/webapp/guacamole.war
COPY --from=bundled-jars /archives/json/guacamole-auth-json.jar /opt/guacamole/extensions/guacamole-auth-json/guacamole-auth-json.jar
COPY --from=bundled-jars /archives/postgresql/guacamole-auth-jdbc-postgresql.jar /opt/guacamole/extensions/guacamole-auth-jdbc/postgresql/guacamole-auth-jdbc-postgresql.jar
COPY --from=bundled-jars /patch/postgresql-42.7.14.jar /opt/guacamole/drivers/postgresql-jdbc.jar

# The base image is pinned by digest. The client package version floats so a
# rebuild gets the current Ubuntu security fixes.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Add custom entrypoint for idempotent schema initialization
COPY entrypoint.sh /opt/guacamole/bin/custom-entrypoint.sh
COPY --from=authority-build /authority/target/guacamole-authority-1.0.0.jar /opt/guacamole/shifter-authority.jar
COPY authority-entrypoint.sh /opt/guacamole/entrypoint.d/600-shifter-authority.sh
RUN chmod +x /opt/guacamole/bin/custom-entrypoint.sh

USER guacamole

# Same check as the Kubernetes probes: the web application answers on 8080.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD curl -fsS -o /dev/null http://127.0.0.1:8080/guacamole/ || exit 1

# custom-entrypoint.sh runs idempotent schema init then `exec "$@"` to hand off
# to the base image's start command. Guacamole 1.6.0's image starts via
# /opt/guacamole/bin/entrypoint.sh (the base image's own CMD); older images used
# start.sh, which the pinned 1.6.0 rebuild no longer ships. Keep this in sync
# with the base image's CMD (`docker inspect ... .Config.Cmd`).
ENTRYPOINT ["/opt/guacamole/bin/custom-entrypoint.sh"]
CMD ["/opt/guacamole/bin/entrypoint.sh"]
