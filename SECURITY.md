# Security

## Reporting a vulnerability

Open a private security advisory on GitHub, or a normal issue if the report is
not sensitive. Please include the version (`python remove_ads.py --version`),
OS, and a minimal reproduction.

## Threat model

`ads-remover` downloads two Java helper jars on first use (apktool and a
signer) unless you already have them. That download is the main supply-chain
surface, so:

- Downloads are verified against **pinned SHA-256 hashes** (`TOOL_CHECKSUMS`).
- A download must be a real zip/jar (magic-byte check), so an HTML error page
  or truncated file is rejected rather than executed.
- On mismatch the file is deleted and the run fails loudly.

`--no-verify-downloads` disables these checks and is **not recommended**; only
use it if you have independently verified the file.

## What the tool does to your system

- Reads an APK, writes a patched APK plus a `.report.json` next to it.
- Uses a **debug keystore** by default (`~/.ads-remover/debug.keystore`) — the
  output is signed with a throwaway key, never a production key.
- Installs nothing by itself. Java installation is offered by `--setup` and
  always requires an explicit `y`/`-y`; unattended runs decline.
- Makes **no network calls at runtime** other than the one-time jar downloads.

## Scope

The tool only removes **client-side** ad SDKs. It deliberately does not forge
server-validated rewards or touch server-driven ads, and it refuses to remove
`INTERNET`. Those are app-specific and out of scope by design.
