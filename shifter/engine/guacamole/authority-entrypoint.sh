#!/bin/bash
# Sourced by Apache's entrypoint after it creates the runtime GUACAMOLE_HOME.
# Keep the extension outside the template: deployment mounts may replace it.
ln -s /opt/guacamole/shifter-authority.jar "$GUACAMOLE_HOME/extensions/shifter-authority.jar"
