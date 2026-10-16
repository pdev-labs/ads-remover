# ads-remover

Remove SDK ads from any APK. Runs on **Linux, Windows, macOS and Termux** with only Python 3.8+ installed — Java and helper files are auto-downloaded into `./tools` on first run (no admin rights needed).

## Use

```bash
python remove_ads.py            # step-by-step wizard (easiest: just double-click on Windows)
python remove_ads.py app.apk    # output: app-noads.apk
python remove_ads.py --setup    # download helpers / show install hints
python remove_ads.py --check-deps
```

Options: `-o out.apk`, `--dry-run` (analyse only), `--aggressive`
(also stubs ad display calls — better blocking, small splash-hang risk),
`--grant-rewards` (records intent only, never auto-grants),
`-y/--yes` (never prompt — for scripts), `--keep-workdir`, `--verbose`.

## What it does

1. Unpacks with apktool (native if installed, else auto-downloaded jar).
2. `AndroidManifest.xml`: removes ad activities/services/receivers/providers/meta-data (AdMob, Unity, AppLovin, Facebook Audience, ironSource, Vungle, StartApp, InMobi, Chartboost, MoPub, Tapjoy, AnyThink/TopOn, Pangle, GDT, KsAd, Mintegral/Sigmob, …) and ad-only permissions (`AD_ID`, `ACCESS_ADSERVICES_*`). Never touches `INTERNET`.
3. `smali*/`: inside known ad-SDK folders only — readiness checks (`isLoaded`, `canShow`, …) return false; `init`/`load*` return immediately. Your app's own code is never touched.
4. `res/layout*/`: layouts containing ad views become an empty hidden container (original kept as `.orig-ads`).
5. Rebuilds + aligns + signs. Uses system `zipalign`/`apksigner` when present, otherwise a bundled signer (one download, Java only). Installable on stock devices, no root.
6. Writes `<output>.report.json` + prints next steps.

## Per-OS notes

| OS | Java | Notes |
|---|---|---|
| Linux | `sudo apt install -y openjdk-17-jre` (or dnf/pacman equivalent) | `--setup` downloads the rest |
| Windows | `winget install -e --id EclipseAdoptium.Temurin.17.JRE`, reopen terminal | double-click `remove_ads.py` for the wizard; paths with spaces and drag-and-drop work |
| macOS | `brew install openjdk@17` | `--setup` downloads the rest |
| Termux | `pkg install -y openjdk-17` | run from a writable folder; allow storage access to reach Downloads |

## Limits (honest)

- Server-driven ads / sponsored content (no SDK to kill): flagged in the report, needs a per-app patch.
- Server-validated rewards: the gate can be opened, the entitlement can't be forged — never auto-granted.
- Splash hangs after patching = the stubbed `show()` swallowed the completion callback; re-run with `--aggressive` toggled, or patch that one method to fire the callback.

Verify every build: uninstall original first (signature change), open each screen that had ads, clean-install before the final test.

## Files / tests

- `remove_ads.py` — the tool (stdlib only)
- `test_remove_ads.py` — patch-core tests (11 checks)
- `test_cross_platform.py` — OS/UX tests (16 checks, no tools needed)
- `setup.sh` / `setup.bat` — thin wrappers around `remove_ads.py --setup`

```bash
python test_remove_ads.py && python test_cross_platform.py
```

## License

GPLv3 (or later) — see [LICENSE](LICENSE). SPDX: `GPL-3.0-or-later`.
