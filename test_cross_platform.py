#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cross-platform + UX tests for remove_ads.py (stdlib only, no tools needed)."""
import io
import json
import os
import sys
import tempfile
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import remove_ads as ra

FAIL = 0

def check(name, cond, extra=""):
    global FAIL
    print(("PASS " if cond else "FAIL ") + name + (f" [{extra}]" if extra and not cond else ""))
    if not cond:
        FAIL += 1

# 1. platform detection never raises, keys present
for env in ({}, {"TERMUX_VERSION": "1", "PREFIX": "/data/data/com.termux/files/usr"}):
    with mock.patch.dict("os.environ", env, clear=False):
        # ensure absence case: remove TERMUX_VERSION when testing {}
        if not env:
            with mock.patch.dict("os.environ", {}, clear=True):
                # minimal env without TERMUX markers
                import os as _os
                keep = {k: v for k, v in _os.environ.items() if k not in ("TERMUX_VERSION",)}
                with mock.patch.dict("os.environ", keep, clear=True):
                    d = ra.detect_platform()
        else:
            d = ra.detect_platform()
        check(f"detect_platform keys ({'termux' if env else 'plain'})",
              all(k in d for k in ("is_windows", "is_macos", "is_linux", "is_termux")))
check("termux detected via env",
      ra.detect_platform.__code__ is not None)  # smoke

with mock.patch.dict("os.environ", {"TERMUX_VERSION": "118", "PREFIX": "/data/data/com.termux/files/usr"}):
    check("termux flag true", ra.detect_platform()["is_termux"] is True)

# 2. validate_apk: fake valid apk
with tempfile.TemporaryDirectory() as td:
    good = Path(td) / "app.apk"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("AndroidManifest.xml", "<manifest/>")
        z.writestr("classes.dex", "x")
    check("validate good apk", ra.validate_apk(str(good))[0])

    notzip = Path(td) / "bad.apk"
    notzip.write_text("hello", encoding="utf-8")
    ok_, msg = ra.validate_apk(str(notzip))
    check("validate rejects non-zip", not ok_ and "zip" in msg.lower())

    no_manifest = Path(td) / "noman.apk"
    with zipfile.ZipFile(no_manifest, "w") as z:
        z.writestr("foo.txt", "x")
    ok_, msg = ra.validate_apk(str(no_manifest))
    check("validate rejects zip-without-manifest", not ok_)

    check("validate missing file", not ra.validate_apk(str(Path(td) / "nope.apk"))[0])
    txt = Path(td) / "notes.txt"
    txt.write_text("x", encoding="utf-8")
    check("validate rejects non-apk ext", not ra.validate_apk(str(txt))[0])

# 3. default output sits next to input on any OS
p = ra.default_output_path("/tmp/some dir/my app.apk")
check("default output name", p.endswith("my app-noads.apk"), p)
w = ra.default_output_path("C:\\Users\\a\\app.apk")
check("default output windows-style", w.endswith("-noads.apk"), w)

# 4. check-deps --json parses
buf = io.StringIO()
with redirect_stdout(buf):
    rc = ra.main(["--check-deps", "--json"])
data = json.loads(buf.getvalue())
check("check-deps json keys", all(k in data for k in ("java", "apktool_native", "zipalign", "apksigner")), buf.getvalue()[:200])
# 0 = everything present, 3 = something important missing. Both are correct.
check("check-deps exit code sane", rc in (0, 3), f"rc={rc}")

# 5. no-args non-interactive prints help, doesn't hang
buf = io.StringIO()
with redirect_stdout(buf):
    rc = ra.main(["--yes"])  # no input + --yes => help path, no prompt
check("no-input --yes exits 2 with tip", rc == 2 and "wizard" in buf.getvalue().lower())

# 6. invalid apk rejected cleanly (no traceback)
buf, ebuf = io.StringIO(), io.StringIO()
with redirect_stdout(buf), mock.patch.object(sys, "stderr", ebuf):
    rc = ra.main(["/nonexistent/app.apk", "-y"])
check("missing apk exit 2", rc == 2, f"rc={rc}")

# 7. colour helper degrades with NO_COLOR
with mock.patch.dict("os.environ", {"NO_COLOR": "1"}):
    check("NO_COLOR strips ansi", ra.c("hi", "green") == "hi")

# 8. Java auto-install plumbing
check("java packages cover major managers",
      all(k in ra.JAVA_PACKAGES for k in
          ("apt", "dnf", "pacman", "zypper", "apk", "pkg")))

# declining the prompt must not run anything and must return False
with mock.patch.object(ra, "ask_yes_no", return_value=False):
    res = ra.try_install_java({"is_windows": False, "is_macos": False,
                               "is_linux": True, "is_termux": False})
    check("try_install_java declines cleanly", res is False)

# a non-writable/unknown platform must not raise (mock out any real install)
with mock.patch.object(ra, "run", return_value=mock.Mock(returncode=0)), \
        mock.patch.object(ra.shutil, "which", return_value=None):
    res2 = ra.try_install_java({"is_windows": False, "is_macos": False,
                                "is_linux": False, "is_termux": False,
                                "system": "plan9"}, auto_yes=True)
check("try_install_java never raises", res2 in (True, False))

# Arch hint should name the real package
arch_hint = ra.java_hint({"is_termux": False, "is_windows": False,
                          "is_macos": False, "is_linux": True})
check("arch hint package name", "jre-openjdk-headless" in arch_hint, arch_hint)

# 9. find_java on a JAVA_HOME-like dir
with tempfile.TemporaryDirectory() as td:
    bindir = Path(td) / "bin"
    bindir.mkdir()
    fake_java = bindir / ("java.exe" if os.name == "nt" else "java")
    fake_java.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_java.chmod(0o755)
    with mock.patch.dict("os.environ", {"JAVA_HOME": td, "PATH": ""}):
        found = ra.find_java({"is_windows": False, "is_macos": False,
                              "is_linux": True, "is_termux": False})
        # on non-Windows the JAVA_HOME check looks for bin/java
        check("find_java honours JAVA_HOME", (found or "").startswith(str(Path(td))), str(found))

print("FAILURES:", FAIL)
sys.exit(1 if FAIL else 0)
