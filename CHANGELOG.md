# Changelog

All notable changes to this project. Format loosely follows Keep a Changelog;
this project uses Semantic Versioning.

## [2.0.0] - 2026-10-17

### Added
- **30+ more ad SDKs**: Amazon (APS/DeviceAds), Huawei Ads, Smaato, Criteo,
  PubNative, Ogury, Teads, LoopMe, MobFox, AdMost, Yandex Ads, Magnite/Rubicon,
  BidMachine, HyprMX, AerServ, Tremor, Verizon Media/Oath, SuperAwesome,
  Kidoz, Moloco, Liftoff, DigitalTurbine, Zucks, Zedo, Google IMA, MBridge,
  and more. `--list-sdks` now reports 55 networks.
- **Post-patch verification**: after patching, the manifest is re-parsed to
  prove no ad entries remain, and the rebuilt APK is checked to be a valid,
  signed archive. Results land in `report["verification"]`.
- **Supply-chain hardening**: downloaded helper jars are verified against
  pinned SHA-256 hashes and must be real zip archives. Opt out with
  `--no-verify-downloads`.
- **Batch mode**: pass several APK paths or a folder; `-o` becomes an output
  folder. A per-file summary and a combined exit code are printed.
- **Config file**: `.ads-remover.json` (or `--config`) adds extra SDK prefixes,
  manifest markers, permissions and permission substrings without code changes.
- **SDK selection**: `--sdks NAME[,NAME]` limits patching to chosen networks.
- **`--list-sdks`** and **`--version`**.
- **`--json`** now also prints the final report as JSON (not just `--check-deps`).
- `Dockerfile`, `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`,
  `.editorconfig`, and `test_features.py` (54 checks).

### Changed
- `--aggressive` now genuinely gates the display calls (`show*`): normal mode
  only neutralises loading/init and readiness checks; aggressive mode also
  stubs the display calls. Previously display calls were stubbed in both modes.
- Broader layout detection (native/ad container view names) and broader
  server-ad hint patterns.
- `is_ad_permission()` centralises the ad-permission decision.

## [1.0.0] - 2026-10-17

### Added
- Initial release: wizard + CLI, manifest/smali/layout patching for ~25 ad
  SDKs, rebuild + sign (system or bundled signer), JSON report, per-OS Java
  install, GitHub Actions pipeline, GPLv3 license.
