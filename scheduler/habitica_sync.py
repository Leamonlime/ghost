"""Habitica <-> vault two-way sync (Session 23).

Joins Habitica atoms to vault checkbox lines by hidden `<!--hab:TASK-->` /
`<!--hab:TASK/ITEM-->` anchors, never by text (Session 22 proved text-matching
corrupts the note). Only anchored lines are ever read or written; everything
else is invisible and byte-preserved.

Ships DRY-RUN first (GHOST_SYNC_DRYRUN=1, the default): computes every intended
write and logs it, changes nothing in the vault or Habitica. Going live is a
deliberate flip after a full day's dry-run review — and only then does the
scheduler's vault mount become read-write.

Directions and the conflict rule are documented on decide_atom() below.
"""
import os
import re
import glob
import json
import logging
import asyncio
import asyncpg

logger = logging.getLogger("habitica_sync")

# Momentum signal (Session 57): one row per completion this sync confirms. Bucket crosswalk
# approved by Lea S57. derive_bucket is duplicated in dashboard.py (no module both the
# scheduler and dashboard import); keep the two copies in step. record_momentum never raises
# into the caller — a momentum miss must not break a real sync write.
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "ghost-postgres-postgresql.ghost.svc.cluster.local")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
_BUCKET_PROJECT = {
    "sidequesting": "Sidequests",
    "darkandwild": "Business", "website": "Business",
    "writing": "Creative/Relax", "novel": "Creative/Relax",
    "ghost": "Work", "hob": "Work", "rook": "Work", "cephalopod": "Work",
    "offgoogle": "Work", "3dprinting": "Work", "vinylcutter": "Work",
}

def derive_bucket(text, source="", rel=""):
    t = (text or "").lower()
    m = re.search(r"#project/([a-z0-9_-]+)", t)
    if m and m.group(1) in _BUCKET_PROJECT:
        return _BUCKET_PROJECT[m.group(1)]
    if source == "quest":
        return "Sidequests"
    if any(w in t for w in ("morning routine", "evening routine", "bedtime", "breakfast",
                            "lunch", "dinner", "tidy", "skincare", "make bed", "supplements")):
        return "Maintenance"
    if any(w in t for w in ("exercise", "yoga", "weights", "handstand", "splits", "water", "workout", "habit")):
        return "Habits"
    if "career" in t or "job" in t or "resume" in t or "linkedin" in t:
        return "Work"
    if any(w in t for w in ("dark and wild", "tdaw", "lea the witch", "leathewitch", "shopify", "shop", "adsense", "substack")):
        return "Business"
    if any(w in t for w in ("witch", "spell", "ritual", "novel", "fanfic", "writing", "blog")):
        return "Creative/Relax"
    if "The Dark and Wild" in rel or "Website Index" in rel:
        return "Business"
    if "Money Moves" in rel:
        return "Work"
    return "unsorted"

async def record_momentum(item_id, source, bucket):
    try:
        conn = await asyncpg.connect(host=POSTGRES_HOST, port=5432, user="postgres",
                                     password=POSTGRES_PASSWORD, database="ghost_db")
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS momentum_events (
                    id BIGSERIAL PRIMARY KEY, item_id TEXT NOT NULL, source TEXT NOT NULL,
                    bucket TEXT NOT NULL DEFAULT 'unsorted',
                    completed_at TIMESTAMPTZ NOT NULL DEFAULT now());
                CREATE INDEX IF NOT EXISTS momentum_events_completed_at_idx ON momentum_events (completed_at);
            """)
            await conn.execute(
                "INSERT INTO momentum_events (item_id, source, bucket) VALUES ($1,$2,$3)",
                str(item_id), source, bucket)
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"sync record_momentum error ({source}/{item_id}): {e}")

DRY_RUN = os.environ.get("GHOST_SYNC_DRYRUN", "1") != "0"
VAULT_PATH = os.environ.get("VAULT_PATH", "/vault")
BLAST_RADIUS_CAP = 12          # more intended writes than this in one poll => abort + alert
ANCHOR_RE = re.compile(r"<!--hab:([0-9a-f\-]+)(?:/([0-9a-f\-]+))?-->")
CHECKBOX_RE = re.compile(r"^(\s*- \[)([ xX])(\].*)$")

# The files sync may touch. Anchors can only ever appear in these.
# S86 (29/09/2026): the central mirror note `Areas/Habitica — Goals & Habits.md` was RETIRED
# (moved to `Archives/Goals archive/`) and dropped as a sync target — NOT re-pointed at the archive
# copy. The three remaining files stay in sync. Some Habitica atoms now have zero matching vault
# lines as a result; that's expected and no longer noisy (see the debug-level no-match log below).
SYNC_FILES = [
    "Areas/Career & Finance/Money Moves.md",    # renamed Finance.md->Money Moves.md 28/07; folder Goals->Areas S52
    "Projects/Lea The Witch/The Dark and Wild index.md",   # was Goals/Business/... (never existed there); real path fixed S42
    "Projects/Lea The Witch/Lea the Witch — Website Index.md",
]

SNAP_HAB = "habitica:sync:hab_snapshot"    # atom_id -> last-seen Habitica done (bool)
SNAP_REC = "habitica:sync:reconciled"      # atom_id -> last state sync reconciled to (bool)


def _abs(rel):
    p = os.path.join(VAULT_PATH, rel)
    if os.path.exists(p):
        return p
    # Folder-tolerant fallback for the Goals/<->Areas/ rename (S52): if the canonical path
    # is absent, try the other folder name so a rename in either direction can't silently
    # break the sync — the exact S24/S28/S42 breakage class this decoupling work targets.
    for a, b in (("Areas/", "Goals/"), ("Goals/", "Areas/")):
        if rel.startswith(a):
            alt = os.path.join(VAULT_PATH, b + rel[len(a):])
            if os.path.exists(alt):
                return alt
    return p  # non-existent; the caller logs the read error


def parse_anchor_lines():
    """Every anchored checkbox line across the sync files.
    Returns list of dicts: {file, rel, lineno, atom_id, done(bool), raw}."""
    out = []
    for rel in SYNC_FILES:
        path = _abs(rel)
        try:
            lines = open(path, encoding="utf-8").read().split("\n")
        except OSError as e:
            # S86: a missing/renamed sync file is a WARNING and is skipped — never an error and never
            # aborts the cycle, so one file moving can't break the sync for the others.
            logger.warning(f"sync: skipping {rel} — cannot read it ({e})")
            continue
        for i, line in enumerate(lines):
            m = ANCHOR_RE.search(line)
            cb = CHECKBOX_RE.match(line)
            if not m or not cb:
                continue
            atom_id = m.group(1) + ("/" + m.group(2) if m.group(2) else "")
            out.append({"file": path, "rel": rel, "lineno": i + 1,
                        "atom_id": atom_id, "done": cb.group(2).lower() == "x",
                        "raw": line})
    return out


async def fetch_habitica_state():
    """atom_id -> done(bool) for every task and checklist item in Habitica.
    Dailies are out of scope, but including them here is harmless (nothing anchors
    to them). `completedTodos` is fetched too (Session 24 fix): a todo that gets
    ticked leaves the open `todos` list and lands there, so without it an anchored
    item that was actually completed would be flagged "no matching atom" forever
    (it's resolved, not broken). Returns None on fetch failure so the caller
    fails closed."""
    try:
        import habitica  # existing module, already in the scheduler image
        import httpx
        state = {}
        async with httpx.AsyncClient() as client:
            for ttype in ("todos", "dailys", "completedTodos"):
                r = await client.get(f"{habitica.BASE_URL}/tasks/user?type={ttype}",
                                     headers=habitica.HEADERS, timeout=30.0)
                data = r.json()
                if not data.get("success"):
                    logger.error(f"sync: habitica fetch {ttype} failed")
                    return None
                for t in data["data"]:
                    state[t["id"]] = bool(t.get("completed"))
                    for c in (t.get("checklist") or []):
                        state[f"{t['id']}/{c['id']}"] = bool(c.get("completed"))
        return state
    except Exception as e:
        logger.error(f"sync: habitica fetch error: {e}")
        return None


def decide_atom(atom_id, hab_now, hab_last, note_now, reconciled_last):
    """Pure decision for one atom. Returns (action, target_state, reason).

    action ∈ {noop, to_note, to_habitica, conflict_done_wins, seed, flag_untick, flag_disagree}

    Inputs (each bool or None):
      hab_now         current Habitica done
      hab_last        Habitica done at previous poll (None = never seen => seeding)
      note_now        current note checkbox done (None = atom not present in any note)
      reconciled_last state sync last reconciled this atom to (None = never)

    CONFLICT RULE (task 3, chosen and documented):
      *Done wins.* If both sides changed since the last poll and disagree, the atom
      is set done on both. Chosen over last-writer-wins because there is no reliable
      per-line edit timestamp for the note, and the costs are asymmetric: silently
      dropping a real completion (the Session 22 harm) is worse than keeping a stale
      [x]. Completion is treated as a positive fact; un-completion as the weaker
      signal. Un-ticking therefore only ever happens on an unambiguous, single-sided
      done->open transition, never as the resolution of a conflict.
    """
    # First time we've seen this atom: seed snapshots, act only by logging any
    # existing disagreement. Never mass-write on first run.
    if hab_last is None or reconciled_last is None:
        if note_now is not None and note_now != hab_now:
            return ("flag_disagree", hab_now, "seed: note and Habitica disagree, not acting")
        return ("seed", hab_now, "seed snapshot")

    hab_changed = hab_now != hab_last
    note_changed = note_now is not None and note_now != reconciled_last

    if not hab_changed and not note_changed:
        return ("noop", note_now if note_now is not None else hab_now, "no change either side")

    if hab_changed and not note_changed:
        # Guard rule 5: a hand [x] is never un-ticked except on a real done->open
        # transition in Habitica. (That transition IS hab_changed, so it's allowed;
        # the guard is against acting on a mere current-open value, which can't reach
        # here because that isn't a change.)
        return ("to_note", hab_now, f"habitica {'done' if hab_now else 'open'} (transition)")

    if note_changed and not hab_changed:
        if note_now is False:
            # note went [x]->[ ]: un-completing in Habitica is destructive/rare; flag.
            return ("flag_untick", note_now, "note un-ticked; outbound un-complete not auto-applied")
        return ("to_habitica", note_now, "note ticked; push completion to habitica")

    # both changed
    if hab_now == note_now:
        return ("noop", hab_now, "both changed, already agree")
    return ("conflict_done_wins", True, "both changed & disagree -> done wins")


async def run_once(get_redis):
    """One poll cycle. get_redis() -> async redis client. Honors DRY_RUN."""
    r = await get_redis()
    try:
        anchors = parse_anchor_lines()
        if not anchors:
            logger.info("sync: no anchored lines found — nothing to do")
            return
        hab = await fetch_habitica_state()
        if hab is None:
            logger.info("sync: habitica unavailable this cycle — skipping (fail-closed)")
            return
        hab_last = json.loads(await r.get(SNAP_HAB) or "{}")
        rec_last = json.loads(await r.get(SNAP_REC) or "{}")

        # group anchored lines by atom (fan-out)
        by_atom = {}
        for a in anchors:
            by_atom.setdefault(a["atom_id"], []).append(a)

        planned = []   # (atom_id, action, target_state, reason, [lines])
        new_hab_snap = dict(hab_last)
        new_rec = dict(rec_last)

        for atom_id, lines in by_atom.items():
            if atom_id not in hab:
                locs = ", ".join(f"{ln['rel']}:{ln['lineno']}" for ln in lines)
                # S86: an anchor with no matching Habitica atom is DEBUG-only now, not surfaced at
                # info. After the 29/09 note retirement + line removals/merges, no-match churn is
                # expected and shouldn't be noise; the row is still skipped and the note untouched.
                logger.debug(f"sync: no matching Habitica atom for {atom_id} "
                             f"(deleted/recreated/merged); at {locs} — skipped, note untouched")
                continue
            hab_now = hab[atom_id]
            # note state: fan-out lines should agree; if they don't, take 'any done' and flag
            note_states = {ln["done"] for ln in lines}
            note_now = (True in note_states) if len(note_states) > 1 else next(iter(note_states))
            note_disagree = len(note_states) > 1

            action, target, reason = decide_atom(
                atom_id, hab_now, hab_last.get(atom_id),
                note_now, rec_last.get(atom_id))

            if note_disagree:
                reason += " [fan-out copies disagreed]"

            new_hab_snap[atom_id] = hab_now
            if action in ("to_note", "to_habitica", "conflict_done_wins", "seed", "noop"):
                new_rec[atom_id] = target
            if action in ("to_note", "to_habitica", "conflict_done_wins"):
                planned.append((atom_id, action, target, reason, lines))
            elif action in ("flag_disagree", "flag_untick"):
                locs = ", ".join(f"{ln['rel']}:{ln['lineno']}" for ln in lines)
                logger.info(f"sync FLAG {atom_id}: {reason} "
                            f"(habitica={hab_now}, note={note_now}) at {locs}")

        # blast-radius cap
        note_writes = [p for p in planned if p[1] in ("to_note", "conflict_done_wins")]
        if len(note_writes) > BLAST_RADIUS_CAP:
            logger.error(f"sync ABORT: {len(note_writes)} note writes exceeds cap {BLAST_RADIUS_CAP} "
                         f"— something is wrong (lost snapshot?). Writing nothing, snapshots unchanged.")
            return

        # execute / log
        for atom_id, action, target, reason, lines in planned:
            if action in ("to_note", "conflict_done_wins"):
                for ln in lines:
                    _log_write(ln["rel"], ln["lineno"], atom_id, ln["done"], target,
                               "habitica->note", reason)
                if not DRY_RUN:
                    _apply_note_writes(atom_id, lines, target)
            if action in ("to_habitica", "conflict_done_wins"):
                _log_write("(habitica)", "-", atom_id, None, target, "note->habitica", reason)
                if not DRY_RUN:
                    await _apply_habitica_write(atom_id, target)
            # Momentum (S57): a confirmed transition TO done, once per atom. Checklist-item
            # anchors (TASKID/CHECKITEMID) log as 'checklist', top-level tasks as 'habitica'.
            if target is True and not DRY_RUN:
                src = "checklist" if "/" in atom_id else "habitica"
                bucket = derive_bucket(lines[0].get("raw", ""), src, lines[0].get("rel", ""))
                await record_momentum(atom_id, src, bucket)

        # persist snapshots (even in dry-run: snapshots track OBSERVED state, so the
        # next cycle diffs correctly. Dry-run changes no vault/Habitica state, only
        # the observation baseline, which is exactly what a real run also updates.)
        await r.set(SNAP_HAB, json.dumps(new_hab_snap))
        await r.set(SNAP_REC, json.dumps(new_rec))
        logger.info(f"sync: cycle done ({'DRY-RUN' if DRY_RUN else 'LIVE'}), "
                    f"{len(planned)} action(s), {len(anchors)} anchors, {len(by_atom)} atoms")
    finally:
        await r.aclose()


def _log_write(rel, lineno, atom_id, old, new, direction, reason):
    logger.info(f"sync {'WOULD-WRITE' if DRY_RUN else 'WRITE'} "
                f"[{direction}] {rel}:{lineno} atom={atom_id} "
                f"{old}->{new} :: {reason}")


def _apply_note_writes(atom_id, lines, target):
    """Flip the checkbox char on every line whose anchor == atom_id, atomically,
    text otherwise byte-identical. Only reached when DRY_RUN is False.

    Matches by ANCHOR ID at write time, re-located in the freshly-read file — NOT
    by the line number captured at parse time. (Session 26 bug: a concurrent
    hand-edit that inserted lines between parse and write shifted the numbering, so
    a by-line-number write landed the correct state on the WRONG anchored line.
    Matching by id makes the write immune to any line movement.)"""
    ch = "x" if target else " "
    for path in {ln["file"] for ln in lines}:
        try:
            content = open(path, encoding="utf-8").read().split("\n")
            changed = False
            for i, line in enumerate(content):
                m = ANCHOR_RE.search(line)
                cb = CHECKBOX_RE.match(line)
                if not (m and cb):
                    continue
                line_atom = m.group(1) + ("/" + m.group(2) if m.group(2) else "")
                if line_atom != atom_id:
                    continue
                if cb.group(2) != ch:
                    content[i] = f"{cb.group(1)}{ch}{cb.group(3)}"
                    changed = True
            if changed:
                # Preserve the note's original owner/mode. The pod runs as root, so a
                # naive temp-file+rename leaves the file root:root 0644 and Obsidian
                # (the user's uid) can no longer save edits — silently breaking the
                # two-way sync it is meant to serve. Capture ownership before the write
                # and restore it after the rename (root can chown; a failure here must
                # not abort the sync, only be logged).
                st = os.stat(path)
                tmp = path + ".synctmp"
                open(tmp, "w", encoding="utf-8").write("\n".join(content))
                os.replace(tmp, path)
                try:
                    os.chown(path, st.st_uid, st.st_gid)
                    os.chmod(path, st.st_mode)
                except OSError as e:
                    logger.error(f"sync: could not restore owner/mode on {path}: {e}")
        except OSError as e:
            logger.error(f"sync: note write failed {path}: {e}")


async def _apply_habitica_write(atom_id, target):
    """Push completion to Habitica. Only reached when DRY_RUN is False.
    Only completion (target True) is auto-applied; un-completion is flagged upstream."""
    if not target:
        return
    try:
        import habitica, httpx
        if "/" in atom_id:
            task_id, item_id = atom_id.split("/", 1)
            url = f"{habitica.BASE_URL}/tasks/{task_id}/checklist/{item_id}/score"
        else:
            url = f"{habitica.BASE_URL}/tasks/{atom_id}/score/up"
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=habitica.HEADERS, timeout=30.0)
            if resp.status_code >= 300:
                logger.error(f"sync: habitica write {atom_id} failed {resp.status_code}")
    except Exception as e:
        logger.error(f"sync: habitica write error {atom_id}: {e}")
