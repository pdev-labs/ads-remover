#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Local simulator for .github/workflows/process-apk.yml (no GitHub needed).

Renders the workflow's shell steps with fake ${{ }} context and an env block,
then checks: syntax, path resolution, and URL validation. Runs entirely offline.

Usage: python test_workflow.py
"""
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

try:
    import yaml
except ImportError:
    print("SKIP test_workflow.py (PyYAML not installed)")
    sys.exit(0)

ROOT = Path(__file__).parent
WF = ROOT / ".github" / "workflows" / "process-apk.yml"
FAIL = 0

def check(name, cond, extra=""):
    global FAIL
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAIL += 1

def render(text, ctx):
    return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda m: str(ctx.get(m.group(1).strip(), "")), str(text))

def load_steps():
    data = yaml.safe_load(WF.read_text())
    return {s.get("name"): s for s in data["jobs"]["process"]["steps"]}

def step_env(step, ctx, base):
    env = dict(base)
    for k, v in (step.get("env") or {}).items():
        env[k] = render(v, ctx)
    return env

def run_step(step, ctx, base_env, gh_output, cwd=None):
    env = step_env(step, ctx, base_env)
    open(os.path.join(cwd, gh_output) if cwd else gh_output, "w").close()
    env["GITHUB_OUTPUT"] = gh_output
    script = render(step["run"], ctx)
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, env=env, cwd=cwd)

def outputs(path):
    out = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.rstrip("\n")
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    return out

def base_ctx(event="workflow_dispatch", **over):
    ctx = {
        "github.event_name": event,
        "github.event.release.tag_name": "",
        "steps.req.outputs.url": "", "steps.req.outputs.path": "",
        "steps.req.outputs.out": "app-noads.apk",
        "steps.req.outputs.aggressive": "false",
        "steps.req.outputs.grant_rewards": "false",
        "steps.req.outputs.release": "false",
        "steps.get.outputs.input": "work/input.apk",
    }
    ctx.update(over)
    return ctx

def make_repo_with_apk(tmp):
    repo = Path(tmp) / "repo"
    (repo / "input").mkdir(parents=True)
    apk = repo / "input" / "MyApp.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("AndroidManifest.xml", "<m/>")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    (repo / "work").mkdir()
    return repo

def main():
    global FAIL
    steps = load_steps()
    base_env = {k: v for k, v in os.environ.items()}
    base_env["GITHUB_TOKEN"] = "dummy"
    base_env["GH_TOKEN"] = "dummy"

    # 1. every shell block parses
    for name, step in steps.items():
        if not step.get("run"):
            continue
        p = subprocess.run(["bash", "-n"], input=step["run"], text=True,
                           capture_output=True)
        check(f"shell syntax: {name}", p.returncode == 0, p.stderr.strip()[:120])

    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo_with_apk(tmp)
        gh = str(Path(tmp) / "ghout")

        # 2. push event -> finds the committed APK
        ctx = base_ctx("push")
        r = run_step(steps["Resolve request"], ctx, base_env, gh, cwd=str(repo))
        o = outputs(gh)
        check("push resolves committed apk", r.returncode == 0 and o.get("path", "").endswith("MyApp.apk"), str(o))

        # 3. download/locate copies it
        ctx2 = base_ctx("push", **{"steps.req.outputs.path": o.get("path", "")})
        r2 = run_step(steps["Download / locate APK"], ctx2, base_env, gh, cwd=str(repo))
        got = Path(tmp) / "repo" / "work" / "input.apk"
        check("download/locate copies apk",
              r2.returncode == 0 and got.is_file(), (r2.stdout + r2.stderr)[-200:])

        # 4. no input at all -> clear error
        ctx3 = base_ctx("workflow_dispatch")
        r3 = run_step(steps["Resolve request"], ctx3, base_env, gh)
        check("no input -> non-zero + error", r3.returncode != 0 and "No APK provided" in (r3.stdout + r3.stderr))

        # 5. non-https url rejected
        ctx4 = base_ctx("workflow_dispatch", **{"inputs.apk_url": "ftp://x/y.apk"})
        r4 = run_step(steps["Resolve request"], ctx4, base_env, gh)
        check("non-https url rejected", r4.returncode != 0 and "https://" in (r4.stdout + r4.stderr))

        # 6. valid https url accepted, flags carried
        ctx5 = base_ctx("workflow_dispatch", **{
            "inputs.apk_url": "https://example.com/app.apk",
            "inputs.aggressive": "true", "inputs.publish_release": "true"})
        r5 = run_step(steps["Resolve request"], ctx5, base_env, gh)
        o5 = outputs(gh)
        check("https url accepted + flags",
              r5.returncode == 0 and o5.get("url", "").startswith("https://")
              and o5.get("aggressive") == "true" and o5.get("release") == "true", str(o5))

        # 7. release event never re-publishes (loop guard)
        ctx6 = base_ctx("release", **{"github.event.release.tag_name": "v1"})
        # stub gh so we don't hit the network
        fake = Path(tmp) / "gh"
        fake.write_text("#!/bin/sh\necho https://example.com/asset.apk\n")
        fake.chmod(0o755)
        env6 = dict(base_env); env6["PATH"] = f"{tmp}:{env6['PATH']}"
        r6 = run_step(steps["Resolve request"], ctx6, env6, gh)
        o6 = outputs(gh)
        check("release event sets release=false",
              r6.returncode == 0 and o6.get("release") == "false", str(o6))

    print("FAILURES:", FAIL)
    sys.exit(1 if FAIL else 0)

if __name__ == "__main__":
    main()
