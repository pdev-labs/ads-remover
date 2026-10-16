#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Self-test for remove_ads.py patch logic (no apktool/java needed)."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import remove_ads as ra

FAIL = 0

def check(name, cond, extra=""):
    global FAIL
    print(("PASS " if cond else "FAIL ") + name + (f" ({extra})" if extra and not cond else ""))
    if not cond:
        FAIL += 1

# --- 1. manifest patching ---
with tempfile.TemporaryDirectory() as td:
    m = Path(td) / "AndroidManifest.xml"
    m.write_text('''<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.example.app">
    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="com.google.android.gms.permission.AD_ID" />
    <application android:label="Test">
        <activity android:name="com.example.app.MainActivity" />
        <activity android:name="com.google.android.gms.ads.AdActivity" />
        <service android:name="com.applovin.impl.sdk.AppLovinService" />
        <meta-data android:name="com.google.android.gms.ads.APPLICATION_ID" android:value="x" />
    </application>
</manifest>''', encoding="utf-8")
    rep = {}
    removed, perms = ra.patch_manifest(m, rep)
    check("manifest strips AdActivity", any("AdActivity" in r["name"] for r in removed))
    check("manifest strips AppLovin service", any("AppLovin" in r["name"] for r in removed))
    check("manifest strips AD_ID perm", "com.google.android.gms.permission.AD_ID" in perms)
    txt = m.read_text()
    check("manifest keeps MainActivity", "MainActivity" in txt)
    check("manifest keeps INTERNET", "android.permission.INTERNET" in txt)

# --- 2. smali patching ---
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    sm = root / "smali" / "com" / "google" / "android" / "gms" / "ads"
    sm.mkdir(parents=True)
    f = sm / "InterstitialAd.smali"
    f.write_text('''.class public Lcom/google/android/gms/ads/InterstitialAd;
.super Ljava/lang/Object;
.method public isLoaded()Z
    .registers 2
    sget-boolean v0, Lcom/example/State;->loaded:Z
    return v0
.end method
.method public loadAd(Lcom/google/android/gms/ads/AdRequest;)V
    .registers 3
    invoke-direct {p0}, Lcom/google/android/gms/ads/InterstitialAd;->realLoad()V
    return-void
.end method
.method public show()V
    .registers 2
    invoke-direct {p0}, Lcom/google/android/gms/ads/InterstitialAd;->realShow()V
    return-void
.end method
''', encoding="utf-8")
    rep = {}
    nf, nv = ra.patch_smali_file(f, False, False, rep)
    out = f.read_text()
    check("readiness -> false", "const/4 v0, 0x0" in out and "return v0" in out, out[:400])
    check("loadAd stubbed", nv >= 1, f"nf={nf} nv={nv}")
    check("per-sdk counted", "AdMob" in rep, json.dumps(rep))

# --- 3. non-ad smali untouched ---
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "smali" / "com" / "example" / "app"
    p.mkdir(parents=True)
    f = p / "Main.smali"
    orig = ".class public Lcom/example/app/Main;\n.super Ljava/lang/Object;\n.method public isLoaded()Z\n.registers 1\nconst/4 v0, 0x1\nreturn v0\n.end method\n"
    f.write_text(orig, encoding="utf-8")
    rep = {}
    ra.patch_smali_file(f, False, False, rep)
    check("app code untouched", f.read_text() == orig and rep == {})

# --- 4. layout neutralisation ---
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / "res" / "layout").mkdir(parents=True)
    (root / "res" / "layout" / "ad_banner.xml").write_text(
        '<LinearLayout><com.google.android.gms.ads.AdView android:id="@+id/ad"/></LinearLayout>',
        encoding="utf-8")
    (root / "res" / "layout" / "main.xml").write_text(
        '<LinearLayout><TextView android:text="hi"/></LinearLayout>', encoding="utf-8")
    rep = {}
    neut = ra.patch_resources(root, rep)
    check("ad layout neutralised", len(neut) == 1 and "ad_banner" in neut[0], json.dumps(neut))
    check("clean layout kept", "TextView" in (root / "res" / "layout" / "main.xml").read_text())

print("FAILURES:", FAIL)
sys.exit(1 if FAIL else 0)
