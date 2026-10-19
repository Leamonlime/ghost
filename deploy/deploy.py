"""Ghost deploy + post-deploy verification (used by `ghost-deploy`).

Extracted from the retired self-coding module (selfcode.py, removed 19/10/2026) so the
deploy path no longer carries any proposal/apply machinery. Each service has ONE fixed
rebuild: bot/scheduler go through the root-owned /usr/local/sbin/ghost-rebuild wrapper via
its scoped NOPASSWD sudo entry; the dashboard is a configmap refresh + rollout (kubectl only).
A deploy only counts when the verification afterwards passes.

CLI:
    python3 deploy.py bot|scheduler|dashboard
"""
import os
import sys
import subprocess
import time
import json
import hashlib
from datetime import datetime, timezone, timedelta

import httpx

GHOST_ROOT = os.environ.get("GHOST_ROOT") or os.path.expanduser("~/ghost")
SERVICE_DIRS = {
    "bot": os.path.join(GHOST_ROOT, "bot"),
    "scheduler": os.path.join(GHOST_ROOT, "scheduler"),
    "dashboard": os.path.join(GHOST_ROOT, "dashboard"),
}


# --------------------------------------------------------------------------
# Fixed rebuild actions. service is validated before any of these run; no
# variable is interpolated into a shell string.
# --------------------------------------------------------------------------
def _run(argv, cwd=None, stdin_bytes=None):
    p = subprocess.run(argv, cwd=cwd, input=stdin_bytes,
                       capture_output=True, timeout=600)
    return p.returncode, (p.stdout or b"").decode(errors="ignore"), (p.stderr or b"").decode(errors="ignore")


REBUILD_WRAPPER = "/usr/local/sbin/ghost-rebuild"

def _rebuild_image_service(service: str, image: str):
    """bot / scheduler: delegate the whole build+import+rollout to the fixed
    root-owned wrapper via a single scoped NOPASSWD sudo call. `service` is one of
    the two literals validated by the caller and by the wrapper itself — no shell,
    no interpolation. Fails cleanly (returning False) when the sudo grant/wrapper
    isn't installed."""
    if service not in ("bot", "scheduler"):
        return False, f"refusing rebuild for '{service}'"
    if not os.path.exists(REBUILD_WRAPPER):
        return False, (f"{REBUILD_WRAPPER} not installed — the scoped sudo rebuild grant "
                       f"is not set up yet (see deploy/README-sudo.md).")
    rc, out, err = _run(["sudo", "-n", REBUILD_WRAPPER, service])
    return rc == 0, f"$ sudo -n {REBUILD_WRAPPER} {service}\n{(out + err).strip()}"


def _git_build_info():
    """{commit, commit_subject, dirty} for the GHOST_ROOT checkout. Any git failure gives
    nulls — build info is a display nicety and must never block a deploy."""
    info = {"commit": None, "commit_subject": None, "dirty": None}
    try:
        rc, out, _ = _run(["git", "rev-parse", "--short", "HEAD"], cwd=GHOST_ROOT)
        if rc == 0 and out.strip():
            info["commit"] = out.strip()
        rc, out, _ = _run(["git", "log", "-1", "--format=%s"], cwd=GHOST_ROOT)
        if rc == 0:
            info["commit_subject"] = (out.strip().splitlines() or [None])[0]
        rc, out, _ = _run(["git", "status", "--porcelain"], cwd=GHOST_ROOT)
        if rc == 0:
            info["dirty"] = bool(out.strip())
    except Exception:
        pass
    return info


def _write_build_info():
    """Write build_info.json to a temp file (never committed) for the dashboard configmap.
    Returns the path, or None if even the temp write fails (deploy proceeds without it)."""
    try:
        import tempfile
        fd, path = tempfile.mkstemp(prefix="ghost-build-info-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(_git_build_info(), f)
        return path
    except Exception:
        return None


def _rebuild_dashboard():
    """dashboard: refresh configmap from dashboard.py + rollout. kubectl only, no sudo."""
    dpy = os.path.join(SERVICE_DIRS["dashboard"], "dashboard.py")
    log = []
    bi_path = _write_build_info()
    argv = ["kubectl", "create", "configmap", "ghost-dashboard-code", f"--from-file={dpy}"]
    if bi_path:
        argv.append(f"--from-file=build_info.json={bi_path}")
    rc, cm_yaml, err = _run(argv + ["-n", "ghost", "--dry-run=client", "-o", "yaml"])
    if bi_path:
        try:
            os.unlink(bi_path)
        except OSError:
            pass
    if rc != 0:
        return False, "configmap render failed:\n" + err
    rc, out, err = _run(["kubectl", "apply", "-f", "-"], stdin_bytes=cm_yaml.encode())
    log.append(f"$ kubectl apply configmap\n{err.strip() or out.strip()}")
    if rc != 0:
        return False, "\n".join(log)
    rc, out, err = _run(["kubectl", "rollout", "restart", "deployment/ghost-dashboard", "-n", "ghost"])
    log.append(f"$ kubectl rollout restart deployment/ghost-dashboard\n{err.strip() or out.strip()}")
    return rc == 0, "\n".join(log)


# --------------------------------------------------------------------------
# Session 44 — real post-deploy verification. The root ghost-rebuild wrapper (and
# the old configmap path) declared success the moment `kubectl rollout restart` was
# ISSUED, never confirming the new pod is actually healthy and running the new code —
# the gap that let Session 10's rebuild go unconfirmed. These checks (rollout status,
# pod health, md5 disk==pod for images; rollout + live version-stamp + 200 for the
# dashboard) are the same three every session has run by hand, now built into the
# tooling so they can't be skipped. Failure is loud and non-zero; auto-rollback is
# deliberately out of scope.
# --------------------------------------------------------------------------
_IMAGE_FILES = {"bot": ["bot.py", "habitica.py", "fatsecret.py", "decisions.py"],
                "scheduler": ["scheduler.py", "habitica.py", "habitica_sync.py", "fatsecret.py", "decisions.py"]}

def _newest_pod(app_label: str) -> str:
    rc, out, err = _run(["kubectl", "get", "pods", "-n", "ghost", "-l", f"app={app_label}",
                         "--sort-by=.metadata.creationTimestamp",
                         "-o", "jsonpath={.items[-1:].metadata.name}"])
    return out.strip() if rc == 0 else ""

def _verify_image_deploy(service: str, image: str, since_dt):
    """(ok, log, problems) — rollout completed, newest pod Running/Ready/0-restarts/fresh,
    and md5(disk)==md5(/app/…) for every copied source file."""
    problems, log = [], []
    rc, out, err = _run(["kubectl", "rollout", "status", f"deployment/{image}",
                         "-n", "ghost", "--timeout=180s"])
    log.append(f"rollout status: {(out + err).strip() or 'ok'}")
    if rc != 0:
        problems.append(f"rollout status did not complete: {(err or out).strip()[:120]}")
    pod = _newest_pod(image)
    if not pod:
        problems.append(f"no pod found for deployment/{image}")
    else:
        rc, js, err = _run(["kubectl", "get", "pod", pod, "-n", "ghost", "-o", "json"])
        try:
            d = json.loads(js)
        except Exception:
            d = {}
        st = d.get("status", {})
        phase = st.get("phase")
        cs = (st.get("containerStatuses") or [{}])[0]
        ready = cs.get("ready")
        restarts = cs.get("restartCount", 0)
        created = d.get("metadata", {}).get("creationTimestamp", "")
        fresh = False
        try:
            cdt = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            fresh = cdt >= since_dt
        except Exception:
            pass
        log.append(f"pod {pod}: phase={phase} ready={ready} restarts={restarts} created={created} fresh={fresh}")
        if phase != "Running":
            problems.append(f"pod {pod} phase={phase} (want Running)")
        if not ready:
            problems.append(f"pod {pod} not Ready (crash-looping or still starting?)")
        if restarts and int(restarts) > 0:
            problems.append(f"pod {pod} restartCount={restarts} (crash-looping on start?)")
        if not fresh:
            problems.append(f"pod {pod} is NOT fresh (created {created}) — the old pod never cycled")
        for fn in _IMAGE_FILES.get(service, []):
            disk = os.path.join(SERVICE_DIRS[service], fn)
            try:
                dm = hashlib.md5(open(disk, "rb").read()).hexdigest()
            except Exception as e:
                problems.append(f"md5 disk read failed {fn}: {e}")
                continue
            rc, pm, err = _run(["kubectl", "exec", pod, "-n", "ghost", "--", "md5sum", f"/app/{fn}"])
            pmh = pm.split()[0] if pm.split() else ""
            match = dm == pmh
            log.append(f"md5 {fn}: disk={dm[:8]} pod={pmh[:8]} {'OK' if match else 'MISMATCH'}")
            if not match:
                problems.append(f"md5 mismatch {fn}: disk {dm[:8]} != pod {pmh[:8]}")
    return (len(problems) == 0), "\n".join(log), problems

def _verify_dashboard_deploy(since_dt):
    """(ok, log, problems) — rollout completed, and the LIVE serving process reports the
    just-deployed source md5 via /api/version (closes the Session-31 caching/pip-timing
    gap: not just that a page loads, but that the NEW page loads), plus a 200 on /."""
    problems, log = [], []
    rc, out, err = _run(["kubectl", "rollout", "status", "deployment/ghost-dashboard",
                         "-n", "ghost", "--timeout=180s"])
    log.append(f"rollout status: {(out + err).strip() or 'ok'}")
    if rc != 0:
        problems.append(f"rollout status did not complete: {(err or out).strip()[:120]}")
    disk = os.path.join(SERVICE_DIRS["dashboard"], "dashboard.py")
    try:
        disk_md5 = hashlib.md5(open(disk, "rb").read()).hexdigest()
    except Exception as e:
        return False, f"disk md5 read failed: {e}", ["disk md5 read failed"]
    rc, cip, err = _run(["kubectl", "get", "svc", "ghost-dashboard", "-n", "ghost",
                         "-o", "jsonpath={.spec.clusterIP}"])
    cip = cip.strip()
    if not cip:
        problems.append("could not resolve dashboard clusterIP")
        return False, "\n".join(log), problems
    base = f"http://{cip}:8080"
    matched, last = False, ""
    for _ in range(45):  # ~45 * 4s = 180s; dashboard pip-installs at startup before serving
        try:
            r = httpx.get(f"{base}/api/version", timeout=5)
            last = r.text.strip()[:120]
            if r.status_code == 200 and disk_md5 in r.text:
                matched = True
                break
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:50]}"
        time.sleep(4)
    log.append(f"/api/version: {'MATCH ' + disk_md5[:8] if matched else 'NO MATCH'} (last: {last})")
    if not matched:
        problems.append("dashboard /api/version never reported the deployed source md5 "
                        "(stale/old serving process, or startup failed)")
    try:
        r = httpx.get(f"{base}/", timeout=8)
        log.append(f"GET / -> {r.status_code}")
        if r.status_code != 200:
            problems.append(f"GET / returned {r.status_code} (want 200)")
    except Exception as e:
        problems.append(f"GET / failed: {type(e).__name__}")
    return (len(problems) == 0), "\n".join(log), problems

def rebuild_service(service: str):
    """Dispatch to the one fixed rebuild for a validated service, THEN verify the deploy
    genuinely landed (Session 44). Returns (ok, log) where ok is True ONLY if the rebuild
    ran AND verification passed — a rollout that never became healthy, a stale/crash-looping
    pod, an md5 mismatch, or a stale dashboard serving process all make ok False (loud)."""
    since = datetime.now(timezone.utc) - timedelta(seconds=5)  # freshness margin vs clock skew
    if service == "bot":
        ok, log = _rebuild_image_service("bot", "ghost-bot")
        if not ok:
            return False, log
        vok, vlog, probs = _verify_image_deploy("bot", "ghost-bot", since)
    elif service == "scheduler":
        ok, log = _rebuild_image_service("scheduler", "ghost-scheduler")
        if not ok:
            return False, log
        vok, vlog, probs = _verify_image_deploy("scheduler", "ghost-scheduler", since)
    elif service == "dashboard":
        ok, log = _rebuild_dashboard()
        if not ok:
            return False, log
        vok, vlog, probs = _verify_dashboard_deploy(since)
    else:
        return False, f"no rebuild defined for '{service}'"
    full = log + "\n--- post-deploy verification ---\n" + vlog
    if not vok:
        full += "\nFAILURE: " + "; ".join(probs)
    return vok, full


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) != 1 or args[0] not in SERVICE_DIRS:
        print("usage: deploy.py bot | scheduler | dashboard", file=sys.stderr)
        sys.exit(2)
    svc = args[0]
    ok, log = rebuild_service(svc)
    print(log)
    if ok:
        print(f"VERIFIED: {svc} rebuilt, rolled out, and confirmed running the new code.")
        sys.exit(0)
    print(f"FAILURE: {svc} deploy did NOT verify — do not trust it as deployed (see above).",
          file=sys.stderr)
    sys.exit(1)
