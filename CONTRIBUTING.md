# Contributing

Thanks for helping! This tool is deliberately **stdlib-only Python 3.8+** —
please keep it that way (no third-party runtime dependencies). Tests use only
the standard library; `test_workflow.py` additionally needs PyYAML.

## Run the tests

```bash
python test_remove_ads.py
python test_cross_platform.py
python test_features.py
pip install pyyaml && python test_workflow.py   # optional
```

All four must pass. CI runs the first three on Linux/Windows/macOS ×
Python 3.9/3.12 and `test_workflow.py` on Linux.

## Adding an ad SDK

Most networks only need two table entries in `remove_ads.py`:

```python
AD_SDK_SMALI_PREFIXES = {
    "com/example/ads": "ExampleAds",   # smali package path, '/'-separated
}
AD_MANIFEST_MARKERS = [
    ("com.example.ads", "ExampleAds"),  # manifest component name, '.'-separated
]
```

Then add a test to `test_features.py` (see the Smaato/Criteo cases) and bump
`known_sdk_labels()` expectations if you assert on the count.

Prefer **specific** package prefixes. A too-broad prefix risks touching app
code; matching is scoped to the ad SDK's own smali directory, but precision
still matters.

For a network you can't share upstream, the runtime `.ads-remover.json`
config file exists so users can add it themselves (see the README).

## Style

- Keep public helpers small and testable; never raise from `detect_platform`,
  `verify_*`, or download helpers — return a value/report instead.
- Every user-visible safety decision (installing packages, downloading files)
  must be decline-able and must never fire in an unattended run without `-y`.
- Match the surrounding code: 4-space indent, `snake_case`, short comments.

## License

By contributing you agree your work is released under GPL-3.0-or-later.
