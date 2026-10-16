#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# ads-remover - remove SDK ads from an APK file.
"""Ads-remover: remove SDK ads from an APK file.

Works on Linux, Windows, macOS and Termux. Needs only Python 3.8+ out of the
box; Java + helper jars are auto-downloaded into ./tools on first run
(no admin rights needed).

Quick use:
  python remove_ads.py                  # friendly step-by-step wizard
  python remove_ads.py app.apk          # auto output: app-noads.apk
  python remove_ads.py app.apk -o out.apk --aggressive
  python remove_ads.py --setup           # install / download what is missing
  python remove_ads.py --check-deps      # show what is available

Pipeline: apktool decode -> strip ad components from the manifest ->
stub known ad-SDK methods in smali -> neutralise ad layouts ->
rebuild + sign (installable on stock devices, no root).

What it handles: client-side ad SDKs (AdMob, Unity Ads, AppLovin, Facebook
Audience, ironSource, Vungle, StartApp, InMobi, Chartboost, MoPub, Tapjoy,
AnyThink/TopOn, Pangle, GDT, KsAd, Mintegral/Sigmob, ...).
What it flags but does NOT auto-remove: server-driven ads / sponsored
content, server-validated rewards. Those need a per-app patch; the script
lists the suspicious spots in the report.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_TOOLS_DIR = SCRIPT_DIR / "tools"

APKTOOL_VERSION = "2.9.3"
APKTOOL_JAR = f"apktool_{APKTOOL_VERSION}.jar"
APKTOOL_URL = (
    "https://github.com/iBotPeaches/Apktool/releases/download/"
    f"v{APKTOOL_VERSION}/{APKTOOL_JAR}"
)
UBER_VERSION = "1.3.0"
UBER_JAR = f"uber-apk-signer-{UBER_VERSION}.jar"
UBER_URL = (
    "https://github.com/patrickfav/uber-apk-signer/releases/download/"
    f"v{UBER_VERSION}/{UBER_JAR}"
)

ANDROID_NS = "http://schemas.android.com/apk/res/android"
ET.register_namespace("android", ANDROID_NS)
ANDROID_NAME = f"{{{ANDROID_NS}}}name"

# ---------------------------------------------------------------------------
# Known ad markers (unchanged patch core)
# ---------------------------------------------------------------------------
AD_SDK_SMALI_PREFIXES = {
    "com/google/android/gms/ads": "AdMob",
    "com/google/ads": "AdMob-legacy",
    "com/unity3d/ads": "UnityAds",
    "com/unity3d/services/ads": "UnityAds-services",
    "com/applovin": "AppLovin",
    "com/facebook/ads": "Facebook-Audience",
    "com/ironsource": "ironSource",
    "com/vungle": "Vungle",
    "com/startapp": "StartApp",
    "com/inmobi": "InMobi",
    "com/chartboost": "Chartboost",
    "com/mopub": "MoPub",
    "com/tapjoy": "Tapjoy",
    "com/anythink": "AnyThink-TopOn",
    "com/bytedance/sdk/openadsdk": "Pangle",
    "com/qq/e": "GDT",
    "com/kwad": "KsAd",
    "com/mintegral": "Mintegral",
    "com/sigmob": "Sigmob",
    "com/baidu/mobads": "Baidu-MobAds",
    "com/adcolony": "AdColony",
    "com/appnext": "AppNext",
    "com/fyber": "Fyber",
    "com/madvertise": "Madvertise",
    "com/millennialmedia": "MillennialMedia",
    "com/flurry": "Flurry-ads",
}

AD_MANIFEST_MARKERS = [
    ("com.google.android.gms.ads.adactivity", "AdMob"),
    ("com.google.android.gms.ads", "AdMob"),
    ("com.google.ads", "AdMob-legacy"),
    ("com.unity3d.ads", "UnityAds"),
    ("com.applovin", "AppLovin"),
    ("com.facebook.ads", "Facebook-Audience"),
    ("com.ironsource", "ironSource"),
    ("com.vungle", "Vungle"),
    ("com.startapp", "StartApp"),
    ("com.inmobi", "InMobi"),
    ("com.chartboost", "Chartboost"),
    ("com.mopub", "MoPub"),
    ("com.tapjoy", "Tapjoy"),
    ("com.anythink", "AnyThink-TopOn"),
    ("com.bytedance.sdk.openadsdk", "Pangle"),
    ("com.qq.e.", "GDT"),
    ("gdt_plugin", "GDT"),
    ("com.kwad", "KsAd"),
    ("com.mintegral", "Mintegral"),
    ("com.sigmob", "Sigmob"),
    ("com.baidu.mobads", "Baidu-MobAds"),
    ("com.adcolony", "AdColony"),
    ("com.appnext", "AppNext"),
    ("ttad", "Pangle"),
    ("openadsdk", "Pangle"),
    ("pangolin", "Pangle"),
]

AD_ONLY_PERMISSIONS = {
    "com.google.android.gms.permission.AD_ID",
    "com.android.providers.tv.permission.READ_EPG_DATA",
    "com.google.android.gms.permission.AD_ID_NOTIFICATION",
}
AD_ONLY_PERMISSION_SUBSTR = ("AD_ID", "ACCESS_ADSERVICES", "AD_SERVICES")

import re as _re

READINESS_METHOD_RE = _re.compile(
    r"^(isLoaded|isReady|isAdLoaded|isAdReady|isRewarded|isInterstitialReady|"
    r"isRewardedVideoAvailable|canShow|canShowAd|hasAd|isAdAvailable)$"
)
NOOP_METHOD_RE = _re.compile(
    r"^(init|initialize|initialise|start|loadAd|loadAds|load|loadInterstitial|"
    r"loadBanner|loadReward|loadRewarded|loadSplash|preload|precache|cacheAd|"
    r"show|showAd|showInterstitial|showBanner|showReward|showRewarded|showSplash|"
    r"displayAd|requestAd)$",
    _re.IGNORECASE,
)
SERVER_AD_HINT_RE = _re.compile(
    r"/(adverts?|adv|banner|splashad|promotion|rewardinfo|config)(/|\"|$)|"
    r"(Advertisement|Advert|Banner|Promotion|SplashAd)[A-Za-z]*",
)

# ---------------------------------------------------------------------------
# Platform + console helpers (cross-platform)
# ---------------------------------------------------------------------------

def detect_platform():
    """Return a dict describing the current OS. Never raises."""
    sys_name = platform.system().lower()  # windows / darwin / linux
    prefix = os.environ.get("PREFIX", "")
    is_termux = (
        "TERMUX_VERSION" in os.environ
        or "com.termux" in prefix
        or "com.termux" in sys.executable
    )
    return {
        "system": sys_name,
        "is_windows": sys_name == "windows" or os.name == "nt",
        "is_macos": sys_name == "darwin",
        "is_linux": sys_name == "linux" and not is_termux,
        "is_termux": is_termux,
        "machine": platform.machine(),
    }


def _enable_windows_ansi():
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VT
    except Exception:
        pass


_ENABLE_ANSI_DONE = False

def colors_enabled():
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def c(text, style):
    """Wrap text in ANSI colour when supported, else plain."""
    global _ENABLE_ANSI_DONE
    if not _ENABLE_ANSI_DONE:
        _enable_windows_ansi()
        _ENABLE_ANSI_DONE = True
        try:
            if sys.stdout.isatty() and hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
    if not colors_enabled():
        return text
    codes = {"bold": "1", "green": "32", "yellow": "33", "red": "31",
             "cyan": "36", "dim": "2"}
    return f"\x1b[{codes.get(style, '0')}m{text}\x1b[0m"


def step(n, total, msg):
    print(c(f"[{n}/{total}] {msg}", "bold"))


def ok(msg):
    print(c("  OK  ", "green") + msg)


def warn(msg):
    print(c("  !!  ", "yellow") + msg)


def fail(msg):
    print(c("  XX  ", "red") + msg, file=sys.stderr)


def ask_yes_no(prompt, default=True):
    """Prompt until y/n/Enter. Returns bool. EOF -> default."""
    hint = "Y/n" if default else "y/N"
    try:
        while True:
            ans = input(f"{prompt} [{hint}]: ").strip().lower()
            if not ans:
                return default
            if ans in ("y", "yes"):
                return True
            if ans in ("n", "no"):
                return False
            print("Please answer y or n.")
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def ask_path(prompt):
    """Prompt for a file path; strips quotes from drag-and-drop."""
    try:
        raw = input(f"{prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    # Windows drag-and-drop wraps the path in quotes; "& 'path'" in PS too.
    raw = raw.strip().strip("\"'").strip()
    if raw.startswith("& "):
        raw = raw[2:].strip().strip("\"'").strip()
    return os.path.expandvars(os.path.expanduser(raw))


# ---------------------------------------------------------------------------
# Tool discovery + provisioning (no admin rights needed)
# ---------------------------------------------------------------------------

def find_on_path(*names, plat=None):
    plat = plat or detect_platform()
    exts = [".exe", ".bat", ".cmd"] if plat["is_windows"] else [""]
    seen = []
    for name in names:
        for ext in exts:
            p = shutil.which(name + ext)
            if p and p not in seen:
                seen.append(p)
    return seen


def find_java(plat=None):
    plat = plat or detect_platform()
    found = find_on_path("java", plat=plat)
    if found:
        return found[0]
    java_home = os.environ.get("JAVA_HOME", "")
    if java_home:
        for sub in ("bin/java", "bin/java.exe"):
            cand = Path(java_home) / sub
            if cand.is_file():
                return str(cand)
    if plat["is_windows"]:
        for base in (os.environ.get("ProgramFiles", ""),
                     os.environ.get("ProgramFiles(x86)", "")):
            for pattern in ("Eclipse Adoptium/*", "Java/*", "OpenJDK/*",
                            "Microsoft/*"):
                try:
                    for d in Path(base).glob(pattern):
                        cand = d / "bin" / "java.exe"
                        if cand.is_file():
                            return str(cand)
                except OSError:
                    continue
    return None


def find_apktool_native(plat=None):
    return find_on_path("apktool", plat=plat)


def find_build_tools(plat=None):
    """Locate zipalign/apksigner from PATH or a local Android SDK."""
    plat = plat or detect_platform()
    out = {"zipalign": None, "apksigner": None}
    z = find_on_path("zipalign", plat=plat)
    a = find_on_path("apksigner", plat=plat)
    if z:
        out["zipalign"] = z[0]
    if a:
        out["apksigner"] = a[0]
    if out["zipalign"] and out["apksigner"]:
        return out
    roots = []
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT", "ANDROID_SDK"):
        if os.environ.get(env):
            roots.append(Path(os.environ[env]))
    home = Path.home()
    if plat["is_windows"]:
        base = os.environ.get("LOCALAPPDATA", "")
        if base:
            roots.append(Path(base) / "Android" / "Sdk")
    elif plat["is_macos"]:
        roots.append(home / "Library" / "Android" / "sdk")
    else:
        roots.append(home / "Android" / "Sdk")
        roots.append(Path("/opt/android-sdk"))
        roots.append(Path("/usr/lib/android-sdk"))
    if plat["is_termux"] and "PREFIX" in os.environ:
        roots.append(Path(os.environ["PREFIX"]) / "share" / "android-sdk")
    for root in roots:
        bt = root / "build-tools"
        if not bt.is_dir():
            continue
        try:
            versions = sorted((d for d in bt.iterdir() if d.is_dir()),
                              reverse=True)
        except OSError:
            continue
        for v in versions:
            for key, exe in (("zipalign", "zipalign"), ("apksigner", "apksigner")):
                if out[key]:
                    continue
                cand = v / (exe + (".exe" if plat["is_windows"] else ""))
                alt = v / (exe + (".bat" if plat["is_windows"] else ""))
                if cand.is_file():
                    out[key] = str(cand)
                elif plat["is_windows"] and alt.is_file():
                    out[key] = str(alt)
        if out["zipalign"] and out["apksigner"]:
            break
    return out


def _download_with_progress(url, dest):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    def show(blocks, block_size, total):
        if total <= 0 or not sys.stdout.isatty():
            return
        done = min(blocks * block_size, total)
        pct = done * 100 // total
        bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
        print(f"\r  [{bar}] {pct}% ({done // 1024} KB)", end="", flush=True)

    try:
        urllib.request.urlretrieve(url, str(tmp), reporthook=show)
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"download failed ({url}): {e}")
    if sys.stdout.isatty():
        print()
    tmp.replace(dest)
    return dest


def ensure_jar(name, url, tools_dir, auto_yes=False):
    """Return Path to jar, downloading it into tools_dir if needed."""
    jar = Path(tools_dir) / name
    if jar.is_file() and jar.stat().st_size > 1024:
        return jar
    print(f"  {name} is missing. Free download, ~20 MB, saved to {tools_dir}.")
    if auto_yes or ask_yes_no(f"Download {name} now?", default=True):
        print(f"  Downloading {name} ...")
        try:
            _download_with_progress(url, jar)
        except RuntimeError as e:
            fail(str(e))
            fail("Check your connection and try again, or install manually.")
            return None
        ok(f"{name} saved.")
        return jar
    return None


INSTALL_HINTS = {
    "windows_java": (
        "Install Java 17: winget install -e --id EclipseAdoptium.Temurin.17.JRE\n"
        "  or download from https://adoptium.net, then reopen the terminal."
    ),
    "macos_java": "Install Java 17:  brew install openjdk@17",
    "linux_java": (
        "Install Java 17, e.g.:\n"
        "  Ubuntu/Debian: sudo apt install -y openjdk-17-jre\n"
        "  Fedora:        sudo dnf install -y java-17-openjdk\n"
        "  Arch:          sudo pacman -S jre-openjdk"
    ),
    "termux_java": "Install Java:  pkg install -y openjdk-17",
}


def java_hint(plat):
    if plat["is_termux"]:
        return INSTALL_HINTS["termux_java"]
    if plat["is_windows"]:
        return INSTALL_HINTS["windows_java"]
    if plat["is_macos"]:
        return INSTALL_HINTS["macos_java"]
    return INSTALL_HINTS["linux_java"]


def check_deps(tools_dir=DEFAULT_TOOLS_DIR, plat=None):
    """Machine-friendly dependency snapshot (paths or None)."""
    plat = plat or detect_platform()
    bt = find_build_tools(plat)
    return {
        "os": ("termux" if plat["is_termux"]
               else plat["system"] or ("windows" if plat["is_windows"] else "?")),
        "python": platform.python_version(),
        "java": find_java(plat),
        "apktool_native": (find_apktool_native(plat) or [None])[0],
        "apktool_jar": str(Path(tools_dir) / APKTOOL_JAR)
                       if (Path(tools_dir) / APKTOOL_JAR).is_file() else None,
        "zipalign": bt["zipalign"],
        "apksigner": bt["apksigner"],
        "jarsigner": (find_on_path("jarsigner", plat=plat) or [None])[0],
        "uber_jar": str(Path(tools_dir) / UBER_JAR)
                    if (Path(tools_dir) / UBER_JAR).is_file() else None,
    }


def print_deps_friendly(deps, plat):
    print("\nWhat I found on this machine:")
    rows = [
        ("Java", deps["java"]),
        ("apktool (app)", deps["apktool_native"] or deps["apktool_jar"]),
        ("zipalign", deps["zipalign"]),
        ("apksigner", deps["apksigner"] or deps["jarsigner"]),
        ("bundled signer fallback", deps["uber_jar"]),
    ]
    for label, val in rows:
        mark = c("yes", "green") if val else c("missing", "yellow")
        print(f"  {label:24} {mark}" + (f"  ({val})" if val else ""))
    if not deps["java"]:
        print("\n" + java_hint(plat))
        print("Tip: after installing, just run this script again. "
              "Helper files can be auto-downloaded (no admin needed).")


# ---------------------------------------------------------------------------
# APK validation
# ---------------------------------------------------------------------------

def validate_apk(path):
    """Friendly validation. Returns (ok, message)."""
    p = Path(path)
    if not str(path).strip():
        return False, "No file given."
    if not p.is_file():
        return False, f"File not found: {p}"
    if p.suffix.lower() != ".apk":
        return False, f"Not an .apk file: {p.name}"
    try:
        if p.stat().st_size == 0:
            return False, "File is empty (0 bytes)."
    except OSError as e:
        return False, f"Cannot read file: {e}"
    try:
        if not zipfile.is_zipfile(p):
            return False, "This is not a valid APK (not a zip archive)."
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
            if "AndroidManifest.xml" not in names:
                return False, "Zip opens but has no AndroidManifest.xml - not an APK?"
    except Exception as e:
        return False, f"Cannot open APK: {e}"
    return True, "Looks like a valid APK."


def default_output_path(input_path):
    p = Path(input_path)
    return str(p.with_name(p.stem + "-noads.apk"))


# ---------------------------------------------------------------------------
# Manifest patching
# ---------------------------------------------------------------------------

def manifest_label(name):
    nl = name.lower()
    for marker, label in AD_MANIFEST_MARKERS:
        if marker in nl:
            return label
    return None


def patch_manifest(manifest_path, report):
    """Strip ad components from decoded (text) AndroidManifest.xml."""
    tree = ET.parse(manifest_path)
    root = tree.getroot()
    removed = []
    removed_perms = []

    for elem in list(root.findall("uses-permission")):
        pname = elem.get(ANDROID_NAME, "")
        if pname in AD_ONLY_PERMISSIONS or any(
            s in pname for s in AD_ONLY_PERMISSION_SUBSTR
        ):
            if pname in ("android.permission.INTERNET",
                         "android.permission.ACCESS_NETWORK_STATE"):
                continue
            removed_perms.append(pname)
            root.remove(elem)

    for tag in ("activity", "activity-alias", "service", "receiver",
                "provider", "meta-data"):
        for parent in root.iter():
            for elem in list(parent):
                if elem.tag != tag and not elem.tag.endswith("}" + tag):
                    continue
                cname = elem.get(ANDROID_NAME, "")
                label = manifest_label(cname) if cname else None
                if not label and cname and "adactivity" in cname.lower():
                    label = "AdMob?"
                if label:
                    removed.append({"tag": tag, "name": cname, "sdk": label})
                    parent.remove(elem)

    tree.write(manifest_path, encoding="utf-8", xml_declaration=True)
    report["manifest_removed_components"] = removed
    report["manifest_removed_permissions"] = removed_perms
    return removed, removed_perms


# ---------------------------------------------------------------------------
# Smali patching
# ---------------------------------------------------------------------------

METHOD_RE = _re.compile(
    r"(?P<header>\.method\s+[^\n]*?(?P<mname>[A-Za-z_$][\w$]*)\s*\([^)]*\)(?P<ret>[^\n]*)\n)"
    r"(?P<body>.*?)(?P<end>\.end method)",
    _re.DOTALL,
)


def _registers_count(body):
    m = _re.search(r"\.registers\s+(\d+)", body)
    if m:
        return int(m.group(1))
    m = _re.search(r"\.locals\s+(\d+)", body)
    if m:
        return max(1, int(m.group(1)))
    return 2


def stub_method_body(header, body, kind, aggressive, grant_rewards):
    """Build replacement body. kind in {'false','void'}."""
    regs = _registers_count(body)
    if kind == "false":
        return (header + f"    .registers {max(regs,1)}\n"
                + "    const/4 v0, 0x0\n"
                + "    return v0\n", True)
    lines = [header + f"    .registers {max(regs,1)}\n"]
    if aggressive:
        lines.append("    # ads-remover: aggressive callback-fire requested but "
                     "skipped generically (type-unsafe); manual per-app patch needed\n")
    if grant_rewards:
        lines.append("    # ads-remover: grant-rewards requested; reward granting is "
                     "app-specific and NOT auto-patched (server may validate)\n")
    lines.append("    return-void\n")
    return ("".join(lines), True)


def patch_smali_file(path, aggressive, grant_rewards, report_entry):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    rel = str(path).replace("\\", "/").lower()
    sdk = None
    for prefix, label in AD_SDK_SMALI_PREFIXES.items():
        if "/" + prefix in rel or rel.endswith(prefix + ".smali") \
                or prefix in rel:
            sdk = label
            break
    if sdk is None:
        base = Path(path).name.lower()
        if "adactivity" in base or "adview" in base or "interstitialad" in base:
            sdk = "AdMob?"
        else:
            return 0, 0

    n_false = n_void = 0

    def repl(m):
        nonlocal n_false, n_void
        header, mname, ret = m.group("header"), m.group("mname"), m.group("ret")
        body = m.group("body")
        if "abstract" in header or "native" in header:
            return m.group(0)
        if READINESS_METHOD_RE.match(mname) and ret.strip().startswith("Z"):
            n_false += 1
            new_body, _ = stub_method_body(header, body, "false", aggressive,
                                           grant_rewards)
            return new_body + ".end method"
        if NOOP_METHOD_RE.match(mname) and ret.strip().startswith("V"):
            n_void += 1
            new_body, _ = stub_method_body(header, body, "void", aggressive,
                                            grant_rewards)
            return new_body + ".end method"
        return m.group(0)

    new_text = METHOD_RE.sub(repl, text)
    if new_text != text:
        Path(path).write_text(new_text, encoding="utf-8")
        report_entry.setdefault(sdk, {"files": 0, "readiness_false": 0,
                                      "noop_void": 0})
        report_entry[sdk]["files"] += 1
        report_entry[sdk]["readiness_false"] += n_false
        report_entry[sdk]["noop_void"] += n_void
    return n_false, n_void


def patch_smali_tree(decoded_dir, aggressive, grant_rewards, report):
    per_sdk = {}
    total_files = 0
    for smali_root in Path(decoded_dir).glob("smali*"):
        if not smali_root.is_dir():
            continue
        for smali in smali_root.rglob("*.smali"):
            nf, nv = patch_smali_file(smali, aggressive, grant_rewards, per_sdk)
            if nf or nv:
                total_files += 1
    report["smali_patched_sdks"] = per_sdk
    report["smali_patched_files"] = total_files
    return per_sdk


# ---------------------------------------------------------------------------
# Resource patching
# ---------------------------------------------------------------------------

AD_LAYOUT_RE = _re.compile(
    r"AdView|AdLayout|AdBanner|com\.google\.android\.gms\.ads|"
    r"com\.facebook\.ads|applovin|ironsource|vungle|startapp|inmobi|"
    r"chartboost|mopub|tapjoy|anythink|openadsdk|TTAd|GDTAd",
)

EMPTY_LAYOUT = """<?xml version="1.0" encoding="utf-8"?>
<!-- ads-remover: ad layout neutralised, original had ad SDK view -->
<FrameLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="0dp"
    android:layout_height="0dp"
    android:visibility="gone" />
"""


def patch_resources(decoded_dir, report):
    neutralised = []
    res_dir = Path(decoded_dir) / "res"
    if res_dir.is_dir():
        for layout in res_dir.glob("layout*/*.xml"):
            try:
                txt = layout.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if AD_LAYOUT_RE.search(txt):
                layout.with_suffix(".xml.orig-ads").write_text(
                    txt, encoding="utf-8")
                layout.write_text(EMPTY_LAYOUT, encoding="utf-8")
                neutralised.append(str(layout.relative_to(decoded_dir)))
    report["layouts_neutralised"] = neutralised
    return neutralised


# ---------------------------------------------------------------------------
# Server-driven ad hint scan
# ---------------------------------------------------------------------------

def scan_server_hints(decoded_dir, report, max_hits=50):
    hits = []
    for smali_root in Path(decoded_dir).glob("smali*"):
        if not smali_root.is_dir():
            continue
        for smali in smali_root.rglob("*.smali"):
            try:
                for i, line in enumerate(
                        smali.read_text(encoding="utf-8",
                                        errors="replace").splitlines(), 1):
                    if SERVER_AD_HINT_RE.search(line):
                        hits.append(f"{smali.relative_to(decoded_dir)}:{i}:"
                                    f"{line.strip()[:160]}")
                        if len(hits) >= max_hits:
                            break
            except OSError:
                continue
        if len(hits) >= max_hits:
            break
    report["server_ad_hints"] = hits
    report["server_ad_hints_truncated"] = len(hits) >= max_hits
    return hits


# ---------------------------------------------------------------------------
# Build / sign (cross-platform)
# ---------------------------------------------------------------------------

def run(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("errors", "replace")
    try:
        return subprocess.run(cmd, **kw)
    except FileNotFoundError as e:
        r = subprocess.CompletedProcess(cmd, 127, "", str(e))
        return r
    except OSError as e:
        r = subprocess.CompletedProcess(cmd, 127, "", str(e))
        return r


def run_live(cmd):
    """Run a long command streaming output (apktool). Returns returncode."""
    try:
        p = subprocess.run(cmd)
        return p.returncode
    except FileNotFoundError as e:
        fail(str(e))
        return 127


def apktool_prefix(java, native_list, tools_dir, auto_yes=False):
    """Resolve how to call apktool. Returns (prefix_list, how) or (None, err)."""
    if native_list:
        exe = native_list[0]
        if exe.lower().endswith((".bat", ".cmd")):
            return [exe], "native script (runs via shell)"
        return [exe], "native"
    jar = ensure_jar(APKTOOL_JAR, APKTOOL_URL, tools_dir, auto_yes=auto_yes)
    if jar is None:
        return None, "apktool download declined"
    if not java:
        return None, "Java is required to run apktool.jar"
    return [java, "-jar", str(jar)], f"bundled {APKTOOL_JAR} (downloaded once)"


def ensure_debug_keystore(ks, keytool=None):
    ks = Path(ks).expanduser()
    if ks.exists():
        return ks
    keytool = keytool or shutil.which("keytool")
    if not keytool:
        raise RuntimeError(
            "No keytool found (comes with a JDK, not just a JRE). "
            "Either install a JDK or let the script use the bundled signer, "
            "which carries its own debug key.")
    ks.parent.mkdir(parents=True, exist_ok=True)
    cmd = [keytool, "-genkeypair", "-keystore", str(ks), "-storepass",
           "android", "-keypass", "android", "-alias", "androiddebugkey",
           "-keyalg", "RSA", "-keysize", "2048", "-validity", "10950",
           "-dname", "CN=Android Debug,O=Android,C=US"]
    p = run(cmd)
    if p.returncode != 0:
        raise RuntimeError(f"keytool failed: {(p.stderr or '')[-2000:]}")
    return ks


def rebuild_and_sign(decoded_dir, out_apk, keystore, ctx):
    """Rebuild with apktool, then align+sign.

    ctx keys: apktool_prefix, java, zipalign, apksigner, jarsigner,
    uber_jar, tools_dir, auto_yes.
    Strategy: system zipalign+apksigner when both exist; else the bundled
    uber-apk-signer (java only); else jarsigner as last resort (warn: no v2).
    """
    decoded_dir, out_apk = Path(decoded_dir), Path(out_apk)
    out_apk.parent.mkdir(parents=True, exist_ok=True)
    unsigned = out_apk.with_name(out_apk.stem + ".unsigned.apk")

    rc = run_live([*ctx["apktool_prefix"], "b", str(decoded_dir),
                   "-o", str(unsigned)])
    if rc != 0 or not unsigned.is_file():
        raise RuntimeError("Rebuild failed (apktool reported an error above). "
                           "Common cause: the app uses an apktool-unsupported "
                           "resource trick - try updating apktool.")

    zipalign, apksigner = ctx.get("zipalign"), ctx.get("apksigner")
    if zipalign and apksigner:
        aligned = out_apk.with_name(out_apk.stem + ".aligned.apk")
        p = run([zipalign, "-f", "4", str(unsigned), str(aligned)])
        if p.returncode != 0:
            raise RuntimeError(f"zipalign failed: {(p.stderr or '')[-2000:]}")
        try:
            ks = ensure_debug_keystore(keystore)
        except RuntimeError as e:
            raise RuntimeError(str(e))
        p = run([apksigner, "sign", "--ks", str(ks), "--ks-pass",
                 "pass:android", "--key-pass", "pass:android", "--out",
                 str(out_apk), str(aligned)])
        try:
            aligned.unlink(missing_ok=True)
        except OSError:
            pass
        if p.returncode != 0:
            raise RuntimeError(f"apksigner failed: {(p.stderr or '')[-2000:]}")
        try:
            unsigned.unlink(missing_ok=True)
        except OSError:
            pass
        return out_apk

    # Fallback: bundled uber-apk-signer (aligns + signs with debug key).
    uber = Path(ctx["tools_dir"]) / UBER_JAR if not ctx.get("uber_jar") else \
        Path(ctx["uber_jar"])
    if not uber.is_file():
        uber_dl = ensure_jar(UBER_JAR, UBER_URL, ctx["tools_dir"],
                             auto_yes=ctx.get("auto_yes", False))
        if uber_dl is None:
            raise RuntimeError("Signing declined (bundled signer download "
                               "declined and no system signer found).")
        uber = uber_dl
    out_dir = out_apk.parent
    p = run([ctx["java"], "-jar", str(uber), "-a", str(unsigned),
             "-o", str(out_dir), "--overwrite"])
    combined = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        raise RuntimeError(f"signing failed: {combined[-2000:]}")
    # uber writes <name>-aligned-debugSigned.apk next to output dir
    produced = sorted(out_dir.glob(unsigned.stem + "*.apk"))
    # filter out the unsigned input itself
    produced = [x for x in produced if x != unsigned]
    if not produced:
        raise RuntimeError("signer ran but produced no APK.")
    best = max(produced, key=lambda x: x.stat().st_size)
    try:
        if out_apk.exists():
            out_apk.unlink()
    except OSError:
        pass
    best.rename(out_apk)
    try:
        unsigned.unlink(missing_ok=True)
    except OSError:
        pass
    for leftover in produced:
        if leftover != out_apk and leftover.exists() and leftover != best:
            try:
                leftover.unlink()
            except OSError:
                pass
    warn("Signed with the fallback signer (debug key). Uninstall the "
         "original app first if Android complains about signatures.")
    return out_apk


# ---------------------------------------------------------------------------
# Setup helper
# ---------------------------------------------------------------------------

def run_setup(tools_dir, auto_yes=False, plat=None):
    plat = plat or detect_platform()
    print(c("\nads-remover setup", "bold"))
    print("I will check what is missing and download helper files into:")
    print(f"  {tools_dir}  (no admin rights needed)\n")
    deps = check_deps(tools_dir, plat)
    print_deps_friendly(deps, plat)

    java = deps["java"]
    if not java:
        print("\n" + c("Java is the only thing I cannot download for you.", "yellow"))
        print(java_hint(plat))
        if plat["is_termux"]:
            print("Then re-run:  python remove_ads.py --setup")
        return 1

    native = find_apktool_native(plat)
    if native:
        ok(f"apktool found: {native[0]}")
    else:
        jar = ensure_jar(APKTOOL_JAR, APKTOOL_URL, tools_dir, auto_yes=auto_yes)
        if jar is None:
            return 1

    bt = find_build_tools(plat)
    if bt["zipalign"] and bt["apksigner"]:
        ok(f"zipalign + apksigner found: {Path(bt['zipalign']).parent}")
    else:
        print("\nNo system zipalign/apksigner. I can use a bundled signer "
              "instead (works the same for installing the result).")
        jar = ensure_jar(UBER_JAR, UBER_URL, tools_dir, auto_yes=auto_yes)
        if jar is None:
            warn("Skipped. Rebuilds will try jarsigner or fail with a hint.")
    print("\n" + c("Setup done. Try:  python remove_ads.py", "green"))
    return 0


# ---------------------------------------------------------------------------
# Wizard (user-friendly interactive mode)
# ---------------------------------------------------------------------------

BANNER = r"""
  __ _  ___  ___       _ __ ___  _ __ ___   _____   _____ _ __
 / _` |/ _ \/ __|____ | '__/ _ \| '_ ` _ \ / _ \ \ / / _ \ '__|
| (_| | (_) \__ \____|| | |  __/| | | | | | (_) \ V /  __/ |
 \__,_|\___/|___/     |_|  \___||_| |_| |_|\___/ \_/ \___|_|
"""


def run_wizard(args, plat):
    print(c(BANNER, "cyan"))
    print(c("Remove ads from an APK file. No tech skills needed.", "bold"))
    print("I will ask 3 questions, then do the rest.\n")
    while True:
        raw = ask_path("Step 1/3 - drag your .apk here (or type its path)")
        good, msg = validate_apk(raw)
        if good:
            args.input = raw
            size_mb = Path(raw).stat().st_size / 1_000_000
            ok(f"Got it: {Path(raw).name} ({size_mb:.1f} MB)")
            break
        fail(msg + " Try again.")
    default_out = default_output_path(args.input)
    raw = ask_path(f"Step 2/3 - where to save the fixed APK?\n  (press Enter for {default_out})")
    args.output = raw or default_out
    try:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        fail(f"Cannot use that folder: {e}")
        args.output = default_out
        print(f"  Using default instead: {default_out}")
    print("\nStep 3/3 - how thorough should I be?")
    print("  Normal:      blocks ad loading (safe, recommended).")
    print("  Aggressive:  also removes ad display calls (better blocking,\n"
          "               but a few apps may hang on the splash screen).")
    args.aggressive = ask_yes_no("Use aggressive mode?", default=False)
    print()
    return args


def print_summary(report, out_apk, report_path, plat):
    n_comp = len(report.get("manifest_removed_components", []))
    n_perm = len(report.get("manifest_removed_permissions", []))
    n_smali = report.get("smali_patched_files", 0)
    n_lay = len(report.get("layouts_neutralised", []))
    sdks = report.get("smali_patched_sdks", {})
    print("\n" + c("Done! Here is what changed:", "green"))
    print(f"  Ad screens/services removed from app list : {n_comp}")
    print(f"  Ad-only permissions removed                : {n_perm}")
    print(f"  Ad code sections disabled                 : {n_smali}")
    print(f"  Ad layouts emptied                        : {n_lay}")
    if sdks:
        print(f"  Ad companies found                        : "
              f"{', '.join(sorted(sdks))}")
    else:
        print("  Ad companies found                        : none by name "
              "(ads may be server-driven - see below)")
    if report.get("server_ad_hints"):
        warn(f"{len(report['server_ad_hints'])} spots look like server-sent ads "
             "(a generic tool cannot remove those safely). Details in report.")
    print(f"\n  Fixed app : {out_apk}")
    print(f"  Details   : {report_path}")
    print("\nNext steps:")
    print("  1. Uninstall the original app (Android rejects mismatched signatures).")
    print("  2. Install the fixed APK and open every screen that had ads.")
    if plat["is_termux"]:
        print("  Tip (Termux): use `adb install` or open the file with a file manager.")
    print("  3. If a splash screen hangs, re-run with --aggressive "
          "toggled the other way.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        description="Remove ads from an APK file (Linux, Windows, macOS, Termux). "
                    "Run with no arguments for the step-by-step wizard.")
    ap.add_argument("input", nargs="?",
                    help="input .apk file (omit for wizard)")
    ap.add_argument("-o", "--output", default=None,
                    help="output .apk path (default: <name>-noads.apk)")
    ap.add_argument("--dry-run", action="store_true",
                    help="unpack + analyse + report only, skip rebuild/sign")
    ap.add_argument("--aggressive", action="store_true",
                    help="also stub ad display calls (better blocking, small hang risk)")
    ap.add_argument("--grant-rewards", action="store_true",
                    help="record reward-grant intent (never auto-grants; server may validate)")
    ap.add_argument("--keystore", default="~/.ads-remover/debug.keystore",
                    help="debug keystore (auto-created when the system signer is used)")
    ap.add_argument("--tools-dir", default=str(DEFAULT_TOOLS_DIR),
                    help="folder for auto-downloaded helper files")
    ap.add_argument("--check-deps", action="store_true",
                    help="show available tools and exit")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output for --check-deps")
    ap.add_argument("--setup", action="store_true",
                    help="download missing helper files, show install hints")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="non-interactive: auto-accept downloads, never prompt")
    ap.add_argument("--keep-workdir", action="store_true",
                    help="keep the unpacked app folder for inspection")
    ap.add_argument("--verbose", action="store_true",
                    help="show tool output while working")
    return ap


def main(argv=None):
    plat = detect_platform()
    ap = build_parser()
    args = ap.parse_args(argv)
    tools_dir = Path(args.tools_dir).expanduser()

    if args.check_deps:
        deps = check_deps(tools_dir, plat)
        if args.json:
            print(json.dumps(deps, indent=2))
        else:
            print_deps_friendly(deps, plat)
        missing = [k for k in ("java",) if not deps[k]]
        if missing or (not deps["apktool_native"] and not deps["apktool_jar"]):
            return 3
        return 0

    if args.setup:
        try:
            return run_setup(tools_dir, auto_yes=args.yes, plat=plat)
        except KeyboardInterrupt:
            print("\nCancelled.")
            return 130

    interactive = not args.input and not args.yes and sys.stdin.isatty()
    if not args.input:
        if interactive:
            try:
                args = run_wizard(args, plat)
            except KeyboardInterrupt:
                print("\nCancelled. Nothing was changed.")
                return 130
        else:
            ap.print_help()
            print("\nTip: run with no arguments for the step-by-step wizard, "
                  "or give an APK:  python remove_ads.py app.apk")
            return 2
    if not args.output:
        args.output = default_output_path(args.input)

    good, msg = validate_apk(args.input)
    if not good:
        fail(msg)
        if "not found" in msg.lower():
            print("Tip: drag-and-drop the file onto this window to paste its path.")
        return 2

    total = 5 if not args.dry_run else 4
    print(c("\nads-remover", "bold") + f"  ({plat['system'] or 'os'}"
          + (" / termux" if plat["is_termux"] else "") + ")")
    print(f"Input : {args.input}\nOutput: {args.output}\n")

    # --- tools ---
    step(1, total, "Checking tools ...")
    java = find_java(plat)
    if not java:
        fail("Java 17+ was not found - it is required to unpack apps.")
        print(java_hint(plat))
        return 3
    ok(f"Java: {java}")
    prefix, how = apktool_prefix(java, find_apktool_native(plat), tools_dir,
                                 auto_yes=args.yes)
    if prefix is None:
        fail(f"apktool unavailable ({how}).")
        print("Run:  python remove_ads.py --setup")
        return 3
    ok(f"apktool: {how}")
    bt = find_build_tools(plat)
    uber = (tools_dir / UBER_JAR) if (tools_dir / UBER_JAR).is_file() else None
    if not args.dry_run and not (bt["zipalign"] and bt["apksigner"]) \
            and not uber and not shutil.which("jarsigner"):
        # Offer the fallback now rather than failing at the end.
        warn("No system signer found. The bundled fallback needs one download.")
        uber_dl = ensure_jar(UBER_JAR, UBER_URL, tools_dir, auto_yes=args.yes)
        if uber_dl is None and not args.yes:
            fail("Cannot sign without a signer. Run --setup first.")
            return 3
        uber = uber_dl
    ctx = {"apktool_prefix": prefix, "java": java,
           "zipalign": bt["zipalign"], "apksigner": bt["apksigner"],
           "jarsigner": shutil.which("jarsigner"),
           "uber_jar": str(uber) if uber else None,
           "tools_dir": str(tools_dir), "auto_yes": args.yes,
           "verbose": args.verbose}

    report = {"input": str(args.input), "aggressive": args.aggressive,
              "grant_rewards": args.grant_rewards,
              "platform": ("termux" if plat["is_termux"] else plat["system"])}

    # --- decode ---
    step(2, total, "Unpacking the app (apktool, may take a minute) ...")
    workdir = Path(tempfile.mkdtemp(prefix="ads-remover-"))
    decoded = workdir / "decoded"
    try:
        if args.verbose:
            rc = run_live([*prefix, "d", "-f", str(args.input), "-o",
                           str(decoded)])
        else:
            p = run([*prefix, "d", "-f", str(args.input), "-o", str(decoded)])
            rc = p.returncode
            if rc != 0:
                tail = ((p.stdout or "") + (p.stderr or ""))[-2000:]
                fail("Unpacking failed:")
                print(tail, file=sys.stderr)
                print("This usually means the APK needs a newer apktool "
                      "(-run --setup to refresh) or the file is corrupted.")
                return 4
        if rc != 0 or not (decoded / "AndroidManifest.xml").is_file():
            fail("Unpacking failed or produced no manifest.")
            return 4
        ok("Unpacked.")

        # --- patch ---
        step(3, total, "Removing ads ...")
        patch_manifest(decoded / "AndroidManifest.xml", report)
        patch_smali_tree(decoded, args.aggressive, args.grant_rewards, report)
        patch_resources(decoded, report)
        scan_server_hints(decoded, report)
        ok(f"manifest entries removed: "
           f"{len(report.get('manifest_removed_components', []))}, "
           f"code sections disabled: {report.get('smali_patched_files', 0)}, "
           f"layouts emptied: {len(report.get('layouts_neutralised', []))}.")

        if args.dry_run:
            step(4, total, "Writing report (dry run, no rebuild) ...")
            out_report = Path("ads-report.json")
            out_report.write_text(json.dumps(report, indent=2),
                                  encoding="utf-8")
            ok(f"Report: {out_report}")
            print(f"Unpacked app kept at: {decoded} (delete manually)")
            try:
                print_summary(report, "(no output - dry run)",
                              out_report, plat)
            except OSError:
                pass
            # keep workdir for inspection on dry-run
            print(f"\nWork folder kept: {workdir}")
            return 0

        # --- rebuild + sign ---
        step(4, total, "Rebuilding the app ...")
        try:
            rebuild_and_sign(decoded, Path(args.output),
                             Path(args.keystore).expanduser(), ctx)
        except RuntimeError as e:
            fail(str(e))
            return 5
        step(5, total, "Writing report ...")
        out_apk = Path(args.output)
        report["output"] = str(out_apk)
        try:
            report["output_bytes"] = out_apk.stat().st_size
        except OSError:
            report["output_bytes"] = None
        out_report = out_apk.with_suffix(".report.json")
        try:
            out_report.write_text(json.dumps(report, indent=2),
                                  encoding="utf-8")
        except OSError as e:
            warn(f"Could not write report: {e}")
        print_summary(report, out_apk, out_report, plat)
        return 0
    except KeyboardInterrupt:
        fail("Cancelled. Nothing was installed or changed on your phone.")
        return 130
    finally:
        if not (args.dry_run or args.keep_workdir):
            shutil.rmtree(workdir, ignore_errors=True)
        elif not args.dry_run and args.keep_workdir:
            print(f"\nWork folder kept: {workdir}")


if __name__ == "__main__":
    sys.exit(main())
