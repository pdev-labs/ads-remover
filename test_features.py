#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the v2 features: expanded SDKs, verification, config, batch,
download checksums, and the aggressive-mode gating. Stdlib only, no tools."""
import hashlib
import io
import json
import sys
import tempfile
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import remove_ads as ra

FAIL = 0

def check(name, cond, extra=""):
    global FAIL
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAIL += 1

# --- 1. version + SDK catalogue ---
check("version is a string", isinstance(ra.__version__, str) and ra.__version__)

buf = io.StringIO()
with redirect_stdout(buf):
    rc = ra.main(["--list-sdks"])
out = buf.getvalue()
check("--list-sdks exits 0", rc == 0, f"rc={rc}")
check("--list-sdks names AdMob", "AdMob" in out)
check("--list-sdks names v2 SDK", "Smaato" in out and "AmazonAds" in out)

for label in ("Smaato", "AmazonAds", "HuaweiAds", "Criteo", "BidMachine", "Moloco"):
    check(f"SDK table has {label}", label in ra.known_sdk_labels())

# --- 2. ad-permission classifier ---
check("AD_ID is ad permission", ra.is_ad_permission("com.google.android.gms.permission.AD_ID"))
check("ADSERVICES substring is ad permission",
      ra.is_ad_permission("android.permission.ACCESS_ADSERVICES_ATTRIBUTION"))
check("INTERNET is not ad permission", not ra.is_ad_permission("android.permission.INTERNET"))
check("ACCESS_NETWORK_STATE is not ad permission",
      not ra.is_ad_permission("android.permission.ACCESS_NETWORK_STATE"))
check("empty is not ad permission", not ra.is_ad_permission(""))

# --- 3. aggressive gating: normal keeps show(), aggressive stubs it ---
SMALI = '''.class public Lcom/google/android/gms/ads/InterstitialAd;
.super Ljava/lang/Object;
.method public isLoaded()Z
    .registers 2
    sget-boolean v0, Lcom/example/State;->loaded:Z
    return v0
.end method
.method public show()V
    .registers 2
    invoke-direct {p0}, Lcom/google/android/gms/ads/InterstitialAd;->realShow()V
    return-void
.end method
'''
with tempfile.TemporaryDirectory() as td:
    def make():
        d = Path(td) / "smali" / "com" / "google" / "android" / "gms" / "ads"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "InterstitialAd.smali"
        f.write_text(SMALI, encoding="utf-8")
        return f
    f = make()
    nf, nv = ra.patch_smali_file(f, False, False, {})
    norm = f.read_text()
    check("normal: readiness stubbed", "const/4 v0, 0x0" in norm and nf == 1)
    check("normal: show() left intact", "realShow" in norm and nv == 0)
    f = make()
    nf, nv = ra.patch_smali_file(f, True, False, {})
    aggr = f.read_text()
    check("aggressive: show() stubbed", "realShow" not in aggr and nv == 1)

check("NOOP_METHOD_RE stays as alias", ra.NOOP_METHOD_RE is ra.LOAD_METHOD_RE)

# --- 4. manifest: v2 SDK stripped + verification reports nothing left ---
with tempfile.TemporaryDirectory() as td:
    m = Path(td) / "AndroidManifest.xml"
    m.write_text('''<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.example.app">
    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="com.google.android.gms.permission.AD_ID" />
    <application android:label="Test">
        <activity android:name="com.smaato.soma.interstitial.InterstitialActivity" />
        <service android:name="com.criteo.publisher.CriteoService" />
        <activity android:name="com.example.app.MainActivity" />
    </application>
</manifest>''', encoding="utf-8")
    rep = {}
    removed, perms = ra.patch_manifest(m, rep)
    names = " ".join(r["name"] for r in removed)
    check("manifest strips Smaato activity", "smaato" in names)
    check("manifest strips Criteo service", "criteo" in names)
    check("manifest keeps MainActivity", "MainActivity" not in names)
    residual = ra.verify_manifest_clean(m)
    check("verification finds no residual", residual == [], json.dumps(residual))
    check("INTERNET kept", "INTERNET" in m.read_text())

# residual detection works on an unpatched manifest
with tempfile.TemporaryDirectory() as td:
    m = Path(td) / "AndroidManifest.xml"
    m.write_text('''<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <application><activity android:name="com.google.android.gms.ads.AdActivity"/></application>
</manifest>''', encoding="utf-8")
    check("verification detects leftover ad entry",
          any("AdActivity" in x for x in ra.verify_manifest_clean(m)))

# --- 5. output APK verification ---
with tempfile.TemporaryDirectory() as td:
    good = Path(td) / "ok.apk"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("AndroidManifest.xml", "<manifest/>")
        z.writestr("classes.dex", "x")
        z.writestr("META-INF/CERT.SF", "x")
    v = ra.verify_output_apk(good)
    check("output verify: valid zip", v["valid_zip"] and v["exists"])
    check("output verify: has manifest+dex", v["has_manifest"] and v["has_dex"])
    check("output verify: signed", v["signed"])
    bad = Path(td) / "bad.apk"
    bad.write_text("not a zip", encoding="utf-8")
    vb = ra.verify_output_apk(bad)
    check("output verify: rejects non-zip", vb["exists"] and not vb["valid_zip"])
    check("output verify: missing file", not ra.verify_output_apk(td + "/nope.apk")["exists"])

# --- 6. download checksum + magic verification ---
check("pinned checksums are sha256 hex",
      all(len(v) == 64 and all(ch in "0123456789abcdef" for ch in v)
          for v in ra.TOOL_CHECKSUMS.values()))
with tempfile.TemporaryDirectory() as td:
    jar = Path(td) / "x.jar"
    jar.write_bytes(b"PK\x03\x04" + b"payload")
    good_hash = hashlib.sha256(jar.read_bytes()).hexdigest()
    check("sha256 helper matches hashlib", ra._sha256_file(jar) == good_hash)
    ra._verify_download(jar, good_hash, True)  # must not raise
    check("checksum verify passes on match", True)
    raised = False
    try:
        ra._verify_download(jar, "0" * 64, True)
    except RuntimeError:
        raised = True
    check("checksum mismatch rejected", raised)
    html = Path(td) / "html.jar"
    html.write_bytes(b"<html>error</html>")
    raised = False
    try:
        ra._verify_download(html, None, True)
    except RuntimeError:
        raised = True
    check("non-zip download rejected", raised)

# --- 7. config file merges into live tables ---
saved_prefixes = dict(ra.AD_SDK_SMALI_PREFIXES)
saved_markers = list(ra.AD_MANIFEST_MARKERS)
saved_perms = set(ra.AD_ONLY_PERMISSIONS)
saved_subs = ra.AD_ONLY_PERMISSION_SUBSTR
try:
    with tempfile.TemporaryDirectory() as td:
        cfgp = Path(td) / "cfg.json"
        cfgp.write_text(json.dumps({
            "extra_sdk_prefixes": {"com/example/ads": "ExampleAds"},
            "extra_manifest_markers": [["com.example.ads", "ExampleAds"]],
            "extra_permissions": ["com.example.permission.AD_ID"],
            "extra_permission_substrings": ["EXAMPLE_AD"],
        }), encoding="utf-8")
        cfg = ra.load_config(str(cfgp))
        ra.apply_config(cfg)
        check("config adds sdk prefix", ra.AD_SDK_SMALI_PREFIXES.get("com/example/ads") == "ExampleAds")
        check("config adds manifest marker", ("com.example.ads", "ExampleAds") in ra.AD_MANIFEST_MARKERS)
        check("config adds permission", "com.example.permission.AD_ID" in ra.AD_ONLY_PERMISSIONS)
        check("config adds substring", "EXAMPLE_AD" in ra.AD_ONLY_PERMISSION_SUBSTR)
        check("config records its path", cfg.get("_path", "").endswith("cfg.json"))
    # bad config is a clean error, not a traceback
    with tempfile.TemporaryDirectory() as td:
        badp = Path(td) / "bad.json"
        badp.write_text("{not json", encoding="utf-8")
        raised = False
        try:
            ra.load_config(str(badp))
        except RuntimeError:
            raised = True
        check("bad config raises RuntimeError", raised)
    check("missing config -> empty dict", ra.load_config("/no/such/file.json") == {})
finally:
    ra.AD_SDK_SMALI_PREFIXES = dict(saved_prefixes)
    ra.AD_MANIFEST_MARKERS = list(saved_markers)
    ra.AD_ONLY_PERMISSIONS = set(saved_perms)
    ra.AD_ONLY_PERMISSION_SUBSTR = saved_subs

# --- 8. --sdks restriction ---
try:
    ra.restrict_to_sdks(["AdMob"])
    check("restrict keeps only AdMob (smali)",
          all(v.lower() == "admob" for v in ra.AD_SDK_SMALI_PREFIXES.values())
          and ra.AD_SDK_SMALI_PREFIXES)
    check("restrict keeps only AdMob (manifest)",
          all(v.lower() == "admob" for _, v in ra.AD_MANIFEST_MARKERS))
    before = dict(ra.AD_SDK_SMALI_PREFIXES)
    ra.restrict_to_sdks([])  # empty selection must not wipe the tables
    check("empty restriction is a no-op", ra.AD_SDK_SMALI_PREFIXES == before)
finally:
    ra.AD_SDK_SMALI_PREFIXES = dict(saved_prefixes)
    ra.AD_MANIFEST_MARKERS = list(saved_markers)

# --- 9. batch input expansion ---
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / "a.apk").write_bytes(b"PK\x03\x04")
    (root / "b.apk").write_bytes(b"PK\x03\x04")
    (root / "notes.txt").write_text("x", encoding="utf-8")
    files = ra.expand_inputs([str(root)])
    check("expand_inputs finds apks in a folder",
          sorted(p.name for p in files) == ["a.apk", "b.apk"])
    files2 = ra.expand_inputs([str(root / "a.apk"), str(root / "b.apk")])
    check("expand_inputs passes files through", len(files2) == 2)

# --- 10. new layout patterns are neutralised ---
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / "res" / "layout").mkdir(parents=True)
    (root / "res" / "layout" / "smaato.xml").write_text(
        '<FrameLayout><com.smaato.soma.banner.SomaBannerView android:id="@+id/a"/></FrameLayout>',
        encoding="utf-8")
    neut = ra.patch_resources(root, {})
    check("smaato layout neutralised", len(neut) == 1 and "smaato" in neut[0], json.dumps(neut))

# --- 11. parser exposes the v2 flags ---
ap = ra.build_parser()
opts = {s for a in ap._actions for s in a.option_strings}
for want in ("--version", "--list-sdks", "--sdks", "--config",
             "--no-verify-downloads", "--json"):
    check(f"parser has {want}", want in opts)

print("FAILURES:", FAIL)
sys.exit(1 if FAIL else 0)
