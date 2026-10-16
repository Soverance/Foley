# Security

## `foley browse` is for a trusted network only

`foley browse` is a small web server for auditioning your library. It has **no
authentication and no encryption**: anyone who can reach its port can list, search and
play every indexed sound, download the files, read the scan logs, change the shortlist,
test set and scan schedule, and start or stop scans. It never writes to the library
itself.

- By default it listens on `127.0.0.1` only. Bind it to other interfaces (and list host
  names in `[browse] allowed_hosts`) only on a network where you trust every device,
  such as a studio LAN behind a firewall. Never expose it to the internet, directly or
  through port forwarding or a tunnel.
- Serving sounds to other people is redistribution. Many commercial and free sample
  libraries licence you to use the sounds in your productions, not to share the raw
  files, even inside your own organisation. Check the licences of the packs in your
  library before you serve it to anyone but yourself.

The MCP server (`foley-mcp`) speaks over stdio to the local client that starts it and
opens no network port.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately through GitHub's
**Report a vulnerability** button on the repository's Security tab, with a description and,
if you can, steps to reproduce. Foley is shared as-is (see `CONTRIBUTING.md`), so we
cannot promise a response time, but reports are read.
