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
  python remove_ads.py a.apk b.apk -o out/   # batch (folder output)
  python remove_ads.py --list-sdks       # show the ad SDKs it knows
  python remove_ads.py --setup           # install / download what is missing
  python remove_ads.py --check-deps      # show what is available

Pipeline: apktool decode -> strip ad components from the manifest ->
stub known ad-SDK methods in smali -> neutralise ad layouts ->
verify -> rebuild + sign (installable on stock devices, no root).

What it handles: client-side ad SDKs across ~55 networks (AdMob, Unity Ads,
AppLovin, Facebook Audience, ironSource, Vungle, StartApp, InMobi, Chartboost,
MoPub, Tapjoy, AnyThink/TopOn, Pangle, GDT, KsAd, Mintegral/Sigmob, Amazon,
Huawei, Smaato, Criteo, Ogury, Teads, BidMachine, Moloco, ...).
What it flags but does NOT auto-remove: server-driven ads / sponsored
content, server-validated rewards. Those need a per-app patch; the script
lists the suspicious spots in the report.

Downloads are SHA-256 verified, and the result is structurally verified before
it is reported as done.
"""

import argparse
import hashlib
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

__version__ = "2.0.0"

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

# SHA-256 of the exact release artefacts above. Downloads are verified against
# these so a tampered mirror or an HTML error page can never be executed.
# apktool publishes no checksum file, so the value below was computed from the
# official release asset; uber-apk-signer's matches its published
# checksum-sha256.txt. Both are immutable for a fixed release tag.
TOOL_CHECKSUMS = {
    APKTOOL_JAR: "7956eb04194300ce0d0a84ad18771eebc94b89fb8d1ddcce8ea4c056818646f4",
    UBER_JAR: "e1299fd6fcf4da527dd53735b56127e8ea922a321128123b9c32d619bba1d835",
}
# Toggled off by --no-verify-downloads.
VERIFY_DOWNLOADS = True

ANDROID_NS = "http://schemas.android.com/apk/res/android"
ET.register_namespace("android", ANDROID_NS)
ANDROID_NAME = f"{{{ANDROID_NS}}}name"

# ---------------------------------------------------------------------------
# Known ad markers (unchanged patch core)
# ---------------------------------------------------------------------------
AD_SDK_SMALI_PREFIXES = {
    # Google / AdMob / IMA
    "com/google/android/gms/ads": "AdMob",
    "com/google/ads": "AdMob-legacy",
    "com/google/ads/interactivemedia": "GoogleIMA",
    # Unity / ironSource / AppLovin
    "com/unity3d/ads": "UnityAds",
    "com/unity3d/services/ads": "UnityAds-services",
    "com/applovin": "AppLovin",
    "com/ironsource": "ironSource",
    # Meta / big western networks
    "com/facebook/ads": "Facebook-Audience",
    "com/vungle": "Vungle",
    "com/startapp": "StartApp",
    "com/inmobi": "InMobi",
    "com/chartboost": "Chartboost",
    "com/mopub": "MoPub",
    "com/tapjoy": "Tapjoy",
    "com/adcolony": "AdColony",
    "com/appnext": "AppNext",
    "com/fyber": "Fyber",
    "com/madvertise": "Madvertise",
    "com/millennialmedia": "MillennialMedia",
    "com/flurry": "Flurry-ads",
    # Chinese / APAC networks
    "com/anythink": "AnyThink-TopOn",
    "com/bytedance/sdk/openadsdk": "Pangle",
    "com/qq/e": "GDT",
    "com/kwad": "KsAd",
    "com/mintegral": "Mintegral",
    "com/mbridge": "Mintegral-MBridge",
    "com/sigmob": "Sigmob",
    "com/baidu/mobads": "Baidu-MobAds",
    # --- added in v2 ---
    "com/amazon/device/ads": "AmazonAds",
    "com/amazon/aps": "AmazonAPS",
    "com/huawei/hms/ads": "HuaweiAds",
    "com/smaato": "Smaato",
    "com/criteo": "Criteo",
    "com/pubnative": "PubNative",
    "com/ogury": "Ogury",
    "com/teads": "Teads",
    "com/loopme": "LoopMe",
    "com/mobfox": "MobFox",
    "com/admost": "AdMost",
    "com/yandex/mobile/ads": "YandexAds",
    "com/magnite": "Magnite",
    "com/rubiconproject": "Magnite-Rubicon",
    "com/bidmachine": "BidMachine",
    "com/hyprmx": "HyprMX",
    "com/aerserv": "AerServ",
    "com/tremor": "Tremor",
    "com/verizon": "VerizonMedia",
    "com/oath": "VerizonMedia-Oath",
    "com/superawesome": "SuperAwesome",
    "com/kidoz": "Kidoz",
    "com/moloco": "Moloco",
    "com/liftoff": "Liftoff",
    "com/digitalturbine": "DigitalTurbine",
    "com/zucks": "Zucks",
    "com/zedo": "Zedo",
}

AD_MANIFEST_MARKERS = [
    ("com.google.android.gms.ads.adactivity", "AdMob"),
    ("com.google.android.gms.ads", "AdMob"),
    ("com.google.ads.interactivemedia", "GoogleIMA"),
    ("com.google.ads", "AdMob-legacy"),
    ("com.unity3d.ads", "UnityAds"),
    ("com.applovin", "AppLovin"),
    ("com.ironsource", "ironSource"),
    ("com.facebook.ads", "Facebook-Audience"),
    ("com.vungle", "Vungle"),
    ("com.startapp", "StartApp"),
    ("com.inmobi", "InMobi"),
    ("com.chartboost", "Chartboost"),
    ("com.mopub", "MoPub"),
    ("com.tapjoy", "Tapjoy"),
    ("com.adcolony", "AdColony"),
    ("com.appnext", "AppNext"),
    ("com.anythink", "AnyThink-TopOn"),
    ("com.bytedance.sdk.openadsdk", "Pangle"),
    ("com.qq.e.", "GDT"),
    ("gdt_plugin", "GDT"),
    ("com.kwad", "KsAd"),
    ("com.mintegral", "Mintegral"),
    ("com.mbridge", "Mintegral-MBridge"),
    ("com.sigmob", "Sigmob"),
    ("com.baidu.mobads", "Baidu-MobAds"),
    ("ttad", "Pangle"),
    ("openadsdk", "Pangle"),
    ("pangolin", "Pangle"),
    # --- added in v2 ---
    ("com.amazon.device.ads", "AmazonAds"),
    ("com.amazon.aps", "AmazonAPS"),
    ("com.huawei.hms.ads", "HuaweiAds"),
    ("com.smaato", "Smaato"),
    ("com.criteo", "Criteo"),
    ("com.pubnative", "PubNative"),
    ("com.ogury", "Ogury"),
    ("com.teads", "Teads"),
    ("com.loopme", "LoopMe"),
    ("com.mobfox", "MobFox"),
    ("com.admost", "AdMost"),
    ("com.yandex.mobile.ads", "YandexAds"),
    ("com.magnite", "Magnite"),
    ("com.rubiconproject", "Magnite-Rubicon"),
    ("com.bidmachine", "BidMachine"),
    ("com.hyprmx", "HyprMX"),
    ("com.aerserv", "AerServ"),
    ("com.tremor", "Tremor"),
    ("com.verizon", "VerizonMedia"),
    ("com.oath", "VerizonMedia-Oath"),
    ("com.superawesome", "SuperAwesome"),
    ("com.kidoz", "Kidoz"),
    ("com.moloco", "Moloco"),
    ("com.liftoff", "Liftoff"),
    ("com.digitalturbine", "DigitalTurbine"),
    ("com.zucks", "Zucks"),
    ("com.zedo", "Zedo"),
]

AD_ONLY_PERMISSIONS = {
    "com.google.android.gms.permission.AD_ID",
    "com.android.providers.tv.permission.READ_EPG_DATA",
    "com.google.android.gms.permission.AD_ID_NOTIFICATION",
    # v2 additions
    "com.huawei.hms.permission.AD_ID",
    "com.samsung.android.ads.permission.AD_ID",
}
AD_ONLY_PERMISSION_SUBSTR = ("AD_ID", "ACCESS_ADSERVICES", "AD_SERVICES")


def is_ad_permission(name):
    """True when a permission exists only to serve ads or expose the ad ID."""
    if not name:
        return False
    if name in ("android.permission.INTERNET",
                "android.permission.ACCESS_NETWORK_STATE"):
        return False
    return name in AD_ONLY_PERMISSIONS or any(
        s in name for s in AD_ONLY_PERMISSION_SUBSTR)


import re as _re

READINESS_METHOD_RE = _re.compile(
    r"^(isLoaded|isReady|isAdLoaded|isAdReady|isRewarded|isInterstitialReady|"
    r"isRewardedVideoAvailable|isRewardedVideoReady|canShow|canShowAd|hasAd|"
    r"isAdAvailable|isAvailable|isCached|isAdCached|isReadyToShow|isShowing|"
    r"isVideoAvailable|hasRewardedVideo|isSplashLoaded)$"
)
# Loading / init lifecycle: neutralised in every mode.
LOAD_METHOD_RE = _re.compile(
    r"^(init|initialize|initialise|start|loadAd|loadAds|load|loadInterstitial|"
    r"loadBanner|loadReward|loadRewarded|loadRewardedAd|loadRewardedVideo|"
    r"loadSplash|loadSplashAd|preload|preloadAd|precache|cacheAd|fetchAd|"
    r"prepareAd|prepare|requestAd|refreshAd)$",
    _re.IGNORECASE,
)
# Display calls: only neutralised in --aggressive (these can hang a splash).
SHOW_METHOD_RE = _re.compile(
    r"^(show|showAd|showInterstitial|showBanner|showReward|showRewarded|"
    r"showRewardedAd|showRewardedVideo|showSplash|showSplashAd|showVideo|"
    r"displayAd|display|playVideo|resumeAd)$",
    _re.IGNORECASE,
)
# Backwards-compatible alias (older callers / docs refer to NOOP_METHOD_RE).
NOOP_METHOD_RE = LOAD_METHOD_RE
SERVER_AD_HINT_RE = _re.compile(
    r"/(adverts?|adv|ads?|banner|splashad|promotion|promotions|rewardinfo|"
    r"rewards?|adconfig|ad_config)(/|\"|$)|"
    r"(Advertisement|Advert|Banner|Promotion|SplashAd|NativeAd|RewardedAd)"
    r"[A-Za-z]*",
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
    """Prompt until y/n/Enter. Returns bool.

    Enter returns the default. EOF / Ctrl+C returns False (decline) so a piped
    or unattended run never triggers a side effect such as a package install.
    """
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
        return False


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
    elif plat["is_macos"]:
        jh = "/usr/libexec/java_home"
        if Path(jh).is_file():
            p = run([jh])
            home = (p.stdout or "").strip()
            if p.returncode == 0 and home:
                cand = Path(home) / "bin" / "java"
                if cand.is_file():
                    return str(cand)
    else:
        # Linux / Termux: common JVM layouts when java is off-PATH
        for base in ("/usr/lib/jvm", "/usr/java", "/opt/java",
                     str(Path(os.environ.get("PREFIX", "/usr")) / "lib/jvm")):
            p = Path(base)
            if not p.is_dir():
                continue
            try:
                for d in sorted(p.iterdir(), reverse=True):
                    cand = d / "bin" / "java"
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


def _sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _verify_download(tmp, expected_sha256=None, verify_zip=True):
    """Raise RuntimeError if a freshly downloaded file is not trustworthy."""
    if verify_zip:
        with open(tmp, "rb") as f:
            magic = f.read(2)
        if magic != b"PK":
            raise RuntimeError(
                "downloaded file is not a jar/zip (the server returned "
                "something else, e.g. an error page)")
    if expected_sha256:
        got = _sha256_file(tmp)
        if got != expected_sha256:
            raise RuntimeError(
                f"checksum mismatch: expected {expected_sha256}, got {got}")


def _download_with_progress(url, dest, expected_sha256=None, verify_zip=True):
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
        if sys.stdout.isatty():
            print()
        _verify_download(tmp, expected_sha256, verify_zip)
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(
            f"download failed or failed verification ({url}): {e}")
    tmp.replace(dest)
    return dest


def ensure_jar(name, url, tools_dir, auto_yes=False, verify=None):
    """Return Path to jar, downloading it into tools_dir if needed.

    The download is checked against TOOL_CHECKSUMS (SHA-256) and must be a
    real zip/jar, so a tampered mirror or an HTML error page is never run.
    Pass verify=False (or --no-verify-downloads) to skip the checks.
    """
    jar = Path(tools_dir) / name
    if verify is None:
        verify = VERIFY_DOWNLOADS
    expected = TOOL_CHECKSUMS.get(name) if verify else None
    if jar.is_file() and jar.stat().st_size > 1024:
        if not verify or not expected or _sha256_file(jar) == expected:
            return jar
        warn(f"{name} failed checksum verification; re-downloading.")
        try:
            jar.unlink()
        except OSError:
            return None
    print(f"  {name} is missing. Free download, ~20 MB, saved to {tools_dir}.")
    if auto_yes or ask_yes_no(f"Download {name} now?", default=True):
        print(f"  Downloading {name} ...")
        try:
            _download_with_progress(url, jar, expected_sha256=expected,
                                    verify_zip=verify)
        except RuntimeError as e:
            fail(str(e))
            fail("Check your connection and try again, or install manually.")
            return None
        ok(f"{name} saved." + (" (checksum verified)" if expected else ""))
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
        "  Ubuntu/Debian: sudo apt install -y openjdk-17-jre-headless\n"
        "  Fedora/RHEL:   sudo dnf install -y java-17-openjdk-headless\n"
        "  Arch:          sudo pacman -S --needed jre-openjdk-headless\n"
        "  openSUSE:      sudo zypper install -y java-17-openjdk\n"
        "  Alpine:        sudo apk add openjdk17-jre-headless"
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


# Package manager -> (java package, install argv tail). Used by --setup.
JAVA_PACKAGES = {
    "apt": (["openjdk-17-jre-headless"], ["install", "-y"]),
    "apt-get": (["openjdk-17-jre-headless"], ["install", "-y"]),
    "dnf": (["java-17-openjdk-headless"], ["install", "-y"]),
    "yum": (["java-17-openjdk-headless"], ["install", "-y"]),
    "pacman": (["jre-openjdk-headless"], ["-S", "--noconfirm", "--needed"]),
    "zypper": (["java-17-openjdk"], ["install", "-y"]),
    "apk": (["openjdk17-jre-headless"], ["add"]),
    "pkg": (["openjdk-17"], ["install", "-y"]),  # Termux
}


def _sudo_prefix(plat):
    if plat["is_windows"]:
        return []
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    sudo = shutil.which("sudo")
    return [sudo] if sudo else []


def try_install_java(plat, auto_yes=False):
    """Best-effort Java install via the platform package manager.

    Returns True if java is available afterwards. Never raises.
    """
    # Never install unprompted in a non-interactive shell without -y.
    if not auto_yes and not sys.stdin.isatty():
        return False
    # Windows: winget / choco
    if plat["is_windows"]:
        winget = shutil.which("winget")
        choco = shutil.which("choco")
        if winget and (auto_yes or ask_yes_no(
                "Java 17 is missing. Install it with winget now?", default=True)):
            run([winget, "install", "-e", "--accept-source-agreements",
                 "--accept-package-agreements", "--id",
                 "EclipseAdoptium.Temurin.17.JRE"])
        elif choco and (auto_yes or ask_yes_no(
                "Java 17 is missing. Install it with Chocolatey now?",
                default=True)):
            run([choco, "install", "temurin17jre", "-y"])
        else:
            return False
        return find_java(plat) is not None

    # macOS: Homebrew
    if plat["is_macos"]:
        brew = shutil.which("brew")
        if not brew:
            return False
        if not (auto_yes or ask_yes_no(
                "Java 17 is missing. Install it with Homebrew now?", default=True)):
            return False
        run([brew, "install", "openjdk@17"])
        # brew openjdk is keg-only; link it so `java` is on PATH
        run([brew, "link", "--force", "--overwrite", "openjdk@17"])
        return find_java(plat) is not None

    # Linux + Termux: find a package manager
    mgr = None
    for name in ("apt-get", "apt", "dnf", "yum", "pacman", "zypper", "apk",
                 "pkg"):
        if shutil.which(name):
            mgr = name
            break
    if not mgr:
        return False
    pkgs, tail = JAVA_PACKAGES.get(mgr, (None, None))
    if not pkgs:
        return False
    if not (auto_yes or ask_yes_no(
            f"Java 17 is missing. Install it with '{mgr}' now? "
            "(needs your password)", default=True)):
        return False
    cmd = [*_sudo_prefix(plat), mgr, *tail, *pkgs]
    if mgr in ("apt", "apt-get"):
        run([*_sudo_prefix(plat), "apt-get", "update"])
    if mgr == "pkg":
        run([mgr, "update", "-y"])
    print(f"  Running: {' '.join(cmd)}")
    p = run(cmd)
    if p.returncode != 0:
        warn(f"'{mgr}' install did not succeed. You may need to run it "
             "yourself; see the hint below.")
        return False
    return find_java(plat) is not None



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
        if is_ad_permission(pname):
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
    lines = [header + f"    .registers {max(regs,1)}\n",
             "    # ads-remover: ad call neutralised\n"]
    if grant_rewards:
        lines.append("    # ads-remover: reward granting is app-specific and "
                     "NOT auto-patched here (the server may validate it)\n")
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
        ret = ret.strip()
        if READINESS_METHOD_RE.match(mname) and ret.startswith("Z"):
            n_false += 1
            new_body, _ = stub_method_body(header, body, "false", aggressive,
                                           grant_rewards)
            return new_body + ".end method"
        if LOAD_METHOD_RE.match(mname) and ret.startswith("V"):
            n_void += 1
            new_body, _ = stub_method_body(header, body, "void", aggressive,
                                           grant_rewards)
            return new_body + ".end method"
        if aggressive and SHOW_METHOD_RE.match(mname) and ret.startswith("V"):
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
    r"AdView|AdLayout|AdBanner|AdContainer|AdFrameLayout|NativeAdView|"
    r"RewardedAdView|BannerAdView|com\.google\.android\.gms\.ads|"
    r"com\.facebook\.ads|com\.amazon\.device\.ads|com\.huawei\.hms\.ads|"
    r"applovin|ironsource|vungle|startapp|inmobi|chartboost|mopub|tapjoy|"
    r"anythink|openadsdk|TTAd|GDTAd|mintegral|smaato|criteo|ogury|teads|"
    r"loopme|mobfox|yandex\.mobile\.ads|bidmachine|pangle|kwad|baidu\.mobads",
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
        print("\n" + c("Java 17+ is required (apktool needs it).", "yellow"))
        installed = try_install_java(plat, auto_yes=auto_yes)
        if not installed:
            fail("Could not install Java automatically.")
            print(java_hint(plat))
            print("After installing it yourself, re-run:  "
                  "python remove_ads.py --setup")
            return 1
        java = find_java(plat)
        ok(f"Java installed: {java}")
        deps = check_deps(tools_dir, plat)

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
    tries = 0
    while True:
        raw = ask_path("Step 1/3 - drag your .apk here (or type its path)")
        if not raw.strip():
            # empty input / EOF / Ctrl+C: do not loop forever
            fail("No file given.")
            raise KeyboardInterrupt
        good, msg = validate_apk(raw)
        if good:
            args.input = raw
            size_mb = Path(raw).stat().st_size / 1_000_000
            ok(f"Got it: {Path(raw).name} ({size_mb:.1f} MB)")
            break
        tries += 1
        if tries >= 5:
            fail("Too many invalid attempts. Run again, or pass the file: "
                 "python remove_ads.py yourfile.apk")
            raise KeyboardInterrupt
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
# Optional config file (.ads-remover.json) and SDK selection
# ---------------------------------------------------------------------------

CONFIG_NAMES = (".ads-remover.json", "ads-remover.json")


def load_config(path=None):
    """Load a JSON config. Explicit path wins, else env, cwd, then script dir."""
    candidates = []
    if path:
        candidates.append(Path(path).expanduser())
    else:
        env = os.environ.get("ADS_REMOVER_CONFIG")
        if env:
            candidates.append(Path(env).expanduser())
        for name in CONFIG_NAMES:
            candidates.append(Path.cwd() / name)
            candidates.append(SCRIPT_DIR / name)
    for cand in candidates:
        if cand.is_file():
            try:
                cfg = json.loads(cand.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise RuntimeError(f"invalid config file {cand}: {e}")
            if not isinstance(cfg, dict):
                raise RuntimeError(
                    f"invalid config file {cand}: not a JSON object")
            cfg["_path"] = str(cand)
            return cfg
    return {}


def apply_config(cfg):
    """Merge extra SDKs/permissions from a config file into the live tables."""
    global AD_ONLY_PERMISSION_SUBSTR
    for prefix, label in (cfg.get("extra_sdk_prefixes") or {}).items():
        AD_SDK_SMALI_PREFIXES.setdefault(prefix, label)
    for item in cfg.get("extra_manifest_markers") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            AD_MANIFEST_MARKERS.append((item[0], item[1]))
    for perm in cfg.get("extra_permissions") or []:
        AD_ONLY_PERMISSIONS.add(perm)
    for sub in cfg.get("extra_permission_substrings") or []:
        AD_ONLY_PERMISSION_SUBSTR = tuple(
            sorted(set(AD_ONLY_PERMISSION_SUBSTR) | {sub}))
    return cfg


def restrict_to_sdks(labels):
    """Keep only the named SDK labels (case-insensitive) in the patch tables.

    Returns how many smali prefixes remain (0 means nothing matched).
    """
    global AD_SDK_SMALI_PREFIXES, AD_MANIFEST_MARKERS
    wanted = {s.lower() for s in labels if s}
    if not wanted:
        return len(AD_SDK_SMALI_PREFIXES)
    AD_SDK_SMALI_PREFIXES = {k: v for k, v in AD_SDK_SMALI_PREFIXES.items()
                             if v.lower() in wanted}
    AD_MANIFEST_MARKERS = [(m, v) for m, v in AD_MANIFEST_MARKERS
                           if v.lower() in wanted]
    return len(AD_SDK_SMALI_PREFIXES)


def known_sdk_labels():
    return sorted(set(AD_SDK_SMALI_PREFIXES.values())
                  | {v for _, v in AD_MANIFEST_MARKERS})


def print_sdks():
    labels = known_sdk_labels()
    print(c(f"ads-remover {__version__} - {len(labels)} ad SDKs known", "bold"))
    for label in labels:
        prefixes = sorted(k for k, v in AD_SDK_SMALI_PREFIXES.items()
                          if v == label)
        detail = ", ".join(prefixes) or "(manifest only)"
        print(f"  {label:22} {detail}")
    print("\nUse --sdks NAME[,NAME] to patch only some of these.")


# ---------------------------------------------------------------------------
# Post-patch verification
# ---------------------------------------------------------------------------

def verify_manifest_clean(manifest_path):
    """Return ad components/permissions still present after patching."""
    remaining = []
    try:
        root = ET.parse(manifest_path).getroot()
    except (OSError, ET.ParseError):
        return ["<manifest unreadable>"]
    for elem in root.iter():
        cname = elem.get(ANDROID_NAME, "")
        if cname and manifest_label(cname):
            remaining.append(cname)
    for elem in root.findall("uses-permission"):
        pname = elem.get(ANDROID_NAME, "")
        if is_ad_permission(pname):
            remaining.append(pname)
    return remaining


def verify_output_apk(path):
    """Structurally check the rebuilt APK. Never raises."""
    p = Path(path)
    res = {"exists": False, "valid_zip": False, "has_manifest": False,
           "has_dex": False, "signed": False, "bytes": None}
    if not p.is_file():
        return res
    res["exists"] = True
    try:
        res["bytes"] = p.stat().st_size
    except OSError:
        pass
    try:
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
        res["valid_zip"] = True
        res["has_manifest"] = "AndroidManifest.xml" in names
        res["has_dex"] = any(n.endswith(".dex") for n in names)
        res["signed"] = any(
            n.startswith("META-INF/") and n.rsplit(".", 1)[-1].upper()
            in ("RSA", "DSA", "EC", "SF") for n in names)
    except Exception as e:  # report, never crash
        res["error"] = str(e)
        return res
    # v2+ signatures live in the APK Signing Block, not a zip entry.
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8 * 1024 * 1024))
            if b"APK Sig Block 42" in f.read():
                res["signed"] = True
    except OSError:
        pass
    return res


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        description="Remove ads from an APK file (Linux, Windows, macOS, Termux). "
                    "Run with no arguments for the step-by-step wizard.")
    ap.add_argument("input", nargs="*",
                    help="input .apk file(s), or a folder of them (omit for wizard)")
    ap.add_argument("-o", "--output", default=None,
                    help="output .apk path; with several inputs, an output folder")
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
    ap.add_argument("--config", default=None,
                    help="JSON config file (default: ./.ads-remover.json)")
    ap.add_argument("--sdks", default=None,
                    help="comma-separated SDK names to patch (see --list-sdks)")
    ap.add_argument("--list-sdks", action="store_true",
                    help="list the ad SDKs this tool knows and exit")
    ap.add_argument("--check-deps", action="store_true",
                    help="show available tools and exit")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output (--check-deps, or the final report)")
    ap.add_argument("--setup", action="store_true",
                    help="download missing helper files, show install hints")
    ap.add_argument("--no-verify-downloads", action="store_true",
                    help="skip SHA-256 checks on downloaded helper jars")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="non-interactive: auto-accept downloads, never prompt")
    ap.add_argument("--keep-workdir", action="store_true",
                    help="keep the unpacked app folder for inspection")
    ap.add_argument("--verbose", action="store_true",
                    help="show tool output while working")
    ap.add_argument("--version", action="version",
                    version=f"ads-remover {__version__}")
    return ap


def expand_inputs(inputs):
    """Expand a mix of files and folders into a sorted list of .apk Paths."""
    files = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            files += sorted(p.glob("*.apk"))
        else:
            files.append(p)
    return files


def prepare_tools(args, plat, tools_dir):
    """Resolve Java + apktool + signer once. Returns (ctx, exit_code)."""
    total = 5 if not args.dry_run else 4
    step(1, total, "Checking tools ...")
    java = find_java(plat)
    if not java:
        warn("Java 17+ was not found - it is required to unpack apps.")
        if args.yes:
            print("Non-interactive mode: run 'python remove_ads.py --setup -y' "
                  "to install it first.")
            print(java_hint(plat))
            return None, 3
        if try_install_java(plat, auto_yes=False):
            java = find_java(plat)
            ok(f"Java installed: {java}")
        else:
            fail("Java is still missing.")
            print(java_hint(plat))
            return None, 3
    ok(f"Java: {java}")
    prefix, how = apktool_prefix(java, find_apktool_native(plat), tools_dir,
                                 auto_yes=args.yes)
    if prefix is None:
        fail(f"apktool unavailable ({how}).")
        print("Run:  python remove_ads.py --setup")
        return None, 3
    ok(f"apktool: {how}")
    bt = find_build_tools(plat)
    uber = (tools_dir / UBER_JAR) if (tools_dir / UBER_JAR).is_file() else None
    if not args.dry_run and not (bt["zipalign"] and bt["apksigner"]) \
            and not uber and not shutil.which("jarsigner"):
        warn("No system signer found. The bundled fallback needs one download.")
        uber_dl = ensure_jar(UBER_JAR, UBER_URL, tools_dir, auto_yes=args.yes)
        if uber_dl is None and not args.yes:
            fail("Cannot sign without a signer. Run --setup first.")
            return None, 3
        uber = uber_dl
    ctx = {"apktool_prefix": prefix, "java": java,
           "zipalign": bt["zipalign"], "apksigner": bt["apksigner"],
           "jarsigner": shutil.which("jarsigner"),
           "uber_jar": str(uber) if uber else None,
           "tools_dir": str(tools_dir), "auto_yes": args.yes,
           "verbose": args.verbose}
    return ctx, 0


def process_one(input_path, output_path, args, plat, ctx, report_path=None,
                label=None):
    """Patch a single APK. Returns (exit_code, report)."""
    total = 5 if not args.dry_run else 4
    if label:
        print(c(f"\n=== {label} ===", "bold"))
    print(c("\nads-remover", "bold") + f"  ({plat['system'] or 'os'}"
          + (" / termux" if plat["is_termux"] else "") + ")")
    print(f"Input : {input_path}\nOutput: {output_path}\n")

    report = {"version": __version__, "input": str(input_path),
              "aggressive": args.aggressive,
              "grant_rewards": args.grant_rewards,
              "platform": ("termux" if plat["is_termux"] else plat["system"])}

    step(2, total, "Unpacking the app (apktool, may take a minute) ...")
    workdir = Path(tempfile.mkdtemp(prefix="ads-remover-"))
    decoded = workdir / "decoded"
    prefix = ctx["apktool_prefix"]
    try:
        if args.verbose:
            rc = run_live([*prefix, "d", "-f", str(input_path), "-o",
                           str(decoded)])
        else:
            p = run([*prefix, "d", "-f", str(input_path), "-o", str(decoded)])
            rc = p.returncode
            if rc != 0:
                tail = ((p.stdout or "") + (p.stderr or ""))[-2000:]
                fail("Unpacking failed:")
                print(tail, file=sys.stderr)
                print("This usually means the APK needs a newer apktool "
                      "(-run --setup to refresh) or the file is corrupted.")
                return 4, report
        if rc != 0 or not (decoded / "AndroidManifest.xml").is_file():
            fail("Unpacking failed or produced no manifest.")
            return 4, report
        ok("Unpacked.")

        step(3, total, "Removing ads ...")
        patch_manifest(decoded / "AndroidManifest.xml", report)
        patch_smali_tree(decoded, args.aggressive, args.grant_rewards, report)
        patch_resources(decoded, report)
        scan_server_hints(decoded, report)
        residual = verify_manifest_clean(decoded / "AndroidManifest.xml")
        report["verification"] = {"manifest_residual_ad_entries": residual}
        ok(f"manifest entries removed: "
           f"{len(report.get('manifest_removed_components', []))}, "
           f"code sections disabled: {report.get('smali_patched_files', 0)}, "
           f"layouts emptied: {len(report.get('layouts_neutralised', []))}.")
        if residual:
            warn(f"{len(residual)} ad entr(ies) still present in the manifest "
                 "(see report verification).")

        if args.dry_run:
            step(4, total, "Writing report (dry run, no rebuild) ...")
            out_report = (Path(report_path) if report_path
                          else Path("ads-report.json"))
            out_report.write_text(json.dumps(report, indent=2),
                                  encoding="utf-8")
            ok(f"Report: {out_report}")
            print(f"Unpacked app kept at: {decoded} (delete manually)")
            try:
                print_summary(report, "(no output - dry run)",
                              out_report, plat)
            except OSError:
                pass
            print(f"\nWork folder kept: {workdir}")
            return 0, report

        step(4, total, "Rebuilding the app ...")
        try:
            rebuild_and_sign(decoded, Path(output_path),
                             Path(args.keystore).expanduser(), ctx)
        except RuntimeError as e:
            fail(str(e))
            return 5, report
        step(5, total, "Writing report ...")
        out_apk = Path(output_path)
        report["output"] = str(out_apk)
        try:
            report["output_bytes"] = out_apk.stat().st_size
        except OSError:
            report["output_bytes"] = None
        report["verification"]["output"] = verify_output_apk(out_apk)
        out_report = (Path(report_path) if report_path
                      else out_apk.with_suffix(".report.json"))
        try:
            out_report.write_text(json.dumps(report, indent=2),
                                  encoding="utf-8")
        except OSError as e:
            warn(f"Could not write report: {e}")
        print_summary(report, out_apk, out_report, plat)
        v = report["verification"]["output"]
        if v.get("valid_zip") and v.get("has_manifest") and v.get("signed"):
            ok("Verified: output is a valid, signed APK.")
        else:
            warn("Output verification incomplete - see the report.")
        return 0, report
    except KeyboardInterrupt:
        fail("Cancelled. Nothing was installed or changed on your phone.")
        return 130, report
    finally:
        if not (args.dry_run or args.keep_workdir):
            shutil.rmtree(workdir, ignore_errors=True)
        elif not args.dry_run and args.keep_workdir:
            print(f"\nWork folder kept: {workdir}")


def main(argv=None):
    global VERIFY_DOWNLOADS
    plat = detect_platform()
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.no_verify_downloads:
        VERIFY_DOWNLOADS = False
    tools_dir = Path(args.tools_dir).expanduser()

    # Optional config file + SDK selection (applied before anything else).
    try:
        cfg = load_config(args.config)
        if cfg:
            apply_config(cfg)
            if not (args.json or args.check_deps):
                print(f"Config: {cfg['_path']}")
    except RuntimeError as e:
        fail(str(e))
        return 2
    if args.sdks:
        matched = restrict_to_sdks(
            [s.strip() for s in args.sdks.split(",") if s.strip()])
        if matched == 0:
            warn(f"No known SDK matched --sdks {args.sdks!r}; "
                 "run --list-sdks to see the names.")

    if args.list_sdks:
        print_sdks()
        return 0

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

    inputs = list(args.input)
    interactive = not inputs and not args.yes and sys.stdin.isatty()
    if not inputs:
        if interactive:
            try:
                args = run_wizard(args, plat)
            except KeyboardInterrupt:
                print("\nCancelled. Nothing was changed.")
                return 130
            inputs = [args.input]
        else:
            ap.print_help()
            print("\nTip: run with no arguments for the step-by-step wizard, "
                  "or give an APK:  python remove_ads.py app.apk")
            return 2

    files = expand_inputs(inputs)
    if not files:
        fail("No .apk files found in the given path(s).")
        return 2
    for f in files:
        good, msg = validate_apk(str(f))
        if not good:
            fail(msg)
            if "not found" in msg.lower():
                print("Tip: drag-and-drop the file onto this window to "
                      "paste its path.")
            return 2

    ctx, code = prepare_tools(args, plat, tools_dir)
    if ctx is None:
        return code

    single = len(files) == 1
    outdir = None
    if not single and args.output:
        outdir = Path(args.output)
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            fail(f"Cannot create output folder: {e}")
            return 2

    results = []
    for i, f in enumerate(files):
        if single:
            out = args.output or default_output_path(f)
            rp = None
            label = None
        else:
            out = str(outdir / (f.stem + "-noads.apk")) if outdir \
                else default_output_path(f)
            rp = str(Path(out).with_suffix(".report.json"))
            label = f"[{i + 1}/{len(files)}] {f.name}"
        code, report = process_one(str(f), out, args, plat, ctx,
                                   report_path=rp, label=label)
        results.append((f, code, report))

    if not single:
        done = sum(1 for _, c, _ in results if c == 0)
        print(c(f"\nBatch finished: {done}/{len(results)} succeeded.", "bold"))
        for f, c, _ in results:
            print(f"  {'OK ' if c == 0 else 'XX '} {f.name} (exit {c})")

    if args.json:
        payload = ([r for _, _, r in results] if not single
                   else (results[0][2] if results else None))
        print(json.dumps(payload, indent=2))

    return max((c for _, c, _ in results), default=0)


if __name__ == "__main__":
    sys.exit(main())
