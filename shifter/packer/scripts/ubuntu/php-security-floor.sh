#!/bin/bash
# Fail the image build when the installed PHP is below the security floor.
#
# Policy (#2556): Ubuntu 22.04 (jammy) ships PHP from the php8.1 source
# package. USN-8948-1 fixes CVE-2026-109618 and CVE-2026-109619 (php2shell) in
# 8.1.2-1ubuntu2.28. The check reads the installed package state with
# dpkg-query and compares Debian versions with dpkg --compare-versions.
#
# The check fails when:
#   - the OS is not the policy release (a new release needs its own policy);
#   - libapache2-mod-php8.1 is not installed and configured;
#   - a present package from a PHP source package is not from php8.1;
#   - a present php8.1 package is held, selected for removal, not fully
#     configured, or below the floor;
#   - Apache does not load the PHP module.
#
# The image bake runs this script. The exact-candidate validators run the same
# script on the booted image: scripts/bake/base-image-verify.sh (AWS) and
# gcp/scripts/validate/gather-evidence.sh (GCP).
#
# Update the three policy values below for a new security fix.
set -euo pipefail

PHP_POLICY_CODENAME="jammy"
PHP_POLICY_SOURCE="php8.1"
PHP_POLICY_FLOOR="8.1.2-1ubuntu2.28"
PHP_POLICY_PACKAGE="libapache2-mod-${PHP_POLICY_SOURCE}"

fail() {
  echo "php-security-floor: FAIL $*" >&2
  exit 1
}

codename="$(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release | tr -d '"')"
[[ "${codename}" == "${PHP_POLICY_CODENAME}" ]] \
  || fail "OS release is '${codename:-unknown}'; the policy covers ${PHP_POLICY_CODENAME} only"

status="$(dpkg-query -W -f='${db:Status-Abbrev}' "${PHP_POLICY_PACKAGE}" 2>/dev/null || true)"
[[ "${status}" == "ii " ]] || fail "${PHP_POLICY_PACKAGE} is not installed and configured"

# Capture the inventory first, so a failed query fails the check. A process
# substitution would hide the dpkg-query exit status.
inventory="$(dpkg-query -W -f='${db:Status-Abbrev}\t${Package}\t${source:Package}\t${Version}\n')" \
  || fail "dpkg-query could not list the installed packages"

checked=0
# The || clause also reads a last row that has no trailing newline.
while IFS=$'\t' read -r state package source version || [[ -n "${state:-}" ]]; do
  # The second status character is the current state. Skip only packages that
  # are not present: not installed (n) or removed with config files left (c).
  # Every other state counts as present, so held (h*) and removal-selected
  # (r*, p*) packages are checked too.
  case "${state:1:1}" in
    n | c) continue ;;
  esac
  case "${source}" in
    php[0-9]*) ;;
    *) continue ;;
  esac
  [[ "${source}" == "${PHP_POLICY_SOURCE}" ]] \
    || fail "${package} comes from ${source}; only ${PHP_POLICY_SOURCE} is covered by the policy"
  [[ "${state}" == "ii " ]] \
    || fail "${package} must be installed, configured, and not held or selected for removal (state '${state}')"
  dpkg --compare-versions "${version}" ge "${PHP_POLICY_FLOOR}" \
    || fail "${package} ${version} is below ${PHP_POLICY_FLOOR}"
  echo "php-security-floor: ${package} ${version} ok"
  checked=$((checked + 1))
done <<< "${inventory}"

[[ "${checked}" -gt 0 ]] || fail "no installed ${PHP_POLICY_SOURCE} packages found"

apache2ctl -M 2>/dev/null | grep -q 'php_module' || fail "Apache does not load the PHP module"
echo "php-security-floor: OK (${checked} packages at or above ${PHP_POLICY_FLOOR})"
