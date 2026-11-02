# Security Policy

## Supported versions

This is a self-hosted project; only the latest `main` (and the most recent
tagged release) receives security fixes. Please run a current version.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub's **"Report a vulnerability"** button on the
repository's **Security** tab (Security → Advisories → Report a vulnerability).
This opens a private advisory visible only to the maintainers.

If you cannot use that channel, contact the maintainer at the address listed in
[`COMMERCIAL-LICENSE.md`](./COMMERCIAL-LICENSE.md).

Please include:

- affected version / commit,
- a description of the issue and its impact,
- reproduction steps or a proof of concept.

You can expect an initial acknowledgement within a few days. Once a fix is
available it will be released on `main` and noted in
[`CHANGELOG.md`](./CHANGELOG.md).

## Hardening notes

This dashboard controls physical printers and stores fleet credentials. Run it
on a trusted network:

- keep the app bound to `127.0.0.1` behind a TLS reverse proxy, or expose it
  only on your LAN/VPN — never directly on the public internet;
- keep `COOKIE_SECURE=1` when served over HTTPS;
- change the first-run admin password immediately;
- put printers and the host on an isolated subnet/VLAN where practical.
