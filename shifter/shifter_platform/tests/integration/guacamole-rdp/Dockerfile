# Disposable synthetic RDP server for native Guacamole transport qualification.
FROM ubuntu@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    xrdp tigervnc-standalone-server xterm xfonts-base procps iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash synthetic \
    && echo 'synthetic:synthetic-test-only' | chpasswd \
    && printf '#!/bin/sh\nexec xterm\n' > /etc/xrdp/startwm.sh \
    && chmod +x /etc/xrdp/startwm.sh \
    && sed -i 's/^autorun=.*/autorun=Xvnc/' /etc/xrdp/xrdp.ini \
    && mkdir -p /run/xrdp
CMD ["sh", "-c", "/usr/sbin/xrdp-sesman && exec /usr/sbin/xrdp --nodaemon"]
