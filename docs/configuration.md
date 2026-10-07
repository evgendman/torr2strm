# Configuration

Production configuration: `/etc/torr2strm/config.toml`.

Repository copy: `etc/config.toml.example`.

Main sections are `[torrserver]`, `[outputs.jellyfin]`, `[outputs.kodi]`, `[sync]`, `[quality]`, `[jacred]` and `[logging]`.

JacRed is optional. The public `https://jac.red` instance is the development/test backend; an empty URL disables enrichment.

Never commit a production config containing API keys, passwords or other secrets.
