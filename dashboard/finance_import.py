"""Rules-based bank CSV import (Build 1, 18/11/2026). Replaces the Ollama-narrated import.

HARD BOUNDARIES (enforced by tests/finance_import_isolated.py and tests/finance_no_llm_guard.py):
  - Pure, deterministic Python. This module imports NO HTTP client, NO model SDK and NO socket
    library, and makes no outbound call of any kind. The guard test fails the build if it does.
  - Transaction data (merchant names, descriptions, amounts, dates) is NEVER logged. This module
    has no logger on purpose. Errors raised from here carry column names and counts only, never a
    cell value, so a caller that logs an exception still cannot leak a row.
  - The raw upload is processed in memory and never written anywhere. Only the parsed rows land
    in Postgres (transactions), with long digit runs masked out of the stored description.

Pipeline: decode -> detect delimiter -> pick a column mapping (csv_import_formats in Postgres, then
header aliases) -> parse date/amount -> normalise description into a merchant key -> categorise by
rules (exact, then contains, then regex; first match wins; matched rule id stored) -> dedupe by
sha256(date|amount|key|occurrence index) -> insert. Re-importing the same file changes nothing.
"""
import csv
import hashlib
import io
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

MAX_UPLOAD_BYTES = 2 * 1024 * 1024     # a year of ANZ statement lines is ~100 KB
MAX_ROWS = 50000
UNCATEGORISED = "uncategorised"
MATCH_TYPES = ("exact", "contains", "regex")

# Internal ANZ own-account transfer -> one fixed key with no account numbers in it.
TRANSFER_KEY = "ANZ INTERNAL TRANSFER"
_TRANSFER_RE = re.compile(r"^ANZ\s+M-BANKING\s+FUNDS\s+TFER\b.*\bTRANSFER\b", re.I)


class ImportError_(ValueError):
    """A CSV we refuse to guess at. Message names columns/counts only, never a cell value."""


# ---------------------------------------------------------------------------
# Column mappings. Seeded into csv_import_formats; Lea (or a later session) adds a bank by
# inserting a row there — no code change. Config keys:
#   headers:      list of header names that must ALL be present (case-insensitive) to pick it
#   has_header:   false for headerless exports; then `columns` gives positional roles
#   date / description / amount / debit / credit: header names (or roles when headerless)
#   date_formats: strptime formats tried in order
# ---------------------------------------------------------------------------
DEFAULT_FORMATS = [
    ("anz", 10, {"headers": ["date", "value", "details"], "has_header": True,
                 "date": "date", "amount": "value", "description": "details",
                 "date_formats": ["%d/%m/%Y"]}),
    # ANZ's own "Download transactions" CSV is headerless: Date, Amount, Description.
    ("anz_headerless", 20, {"has_header": False, "columns": ["date", "amount", "description"],
                            "date_formats": ["%d/%m/%Y"]}),
]

_DATE_ALIASES = ["date", "transaction date", "txn date", "posting date", "processed date", "value date"]
_DESC_ALIASES = ["description", "details", "narrative", "transaction details", "merchant", "memo"]
_AMOUNT_ALIASES = ["amount", "value", "transaction amount"]
_DEBIT_ALIASES = ["debit", "debit amount", "withdrawal", "withdrawals", "money out", "paid out"]
_CREDIT_ALIASES = ["credit", "credit amount", "deposit", "deposits", "money in", "paid in"]
_GENERIC_DATE_FORMATS = ["%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"]


def decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def detect_delimiter(text: str) -> str:
    sample = text[:4096]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def parse_amount(s) -> Decimal:
    """'-1,234.50' / '$1,234.50' / '(12.00)' / '12.00 DR' -> Decimal. Raises InvalidOperation."""
    t = str(s or "").strip().upper().replace("$", "").replace(",", "").replace(" ", "")
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1]
    if t.endswith("DR"):
        neg, t = True, t[:-2]
    elif t.endswith("CR"):
        t = t[:-2]
    if not t:
        raise InvalidOperation("empty")
    v = Decimal(t)
    return -abs(v) if neg else v


def parse_date(s, formats) -> date:
    t = str(s or "").strip()
    for f in formats:
        try:
            return datetime.strptime(t, f).date()
        except ValueError:
            continue
    raise ValueError("unparseable date")


def _looks_like_date(s, formats) -> bool:
    try:
        parse_date(s, formats)
        return True
    except ValueError:
        return False


def resolve_mapping(header, first_row, formats):
    """-> (mapping, has_header). mapping = {date, description, amount|debit|credit (int idx),
    date_formats, name}. Formats from the DB are tried first (priority order), then aliases."""
    lower = [str(h).strip().lower() for h in header]
    for name, cfg in formats:
        if cfg.get("has_header", True):
            need = [h.lower() for h in cfg.get("headers", [])]
            if need and all(h in lower for h in need):
                m = {"name": name, "date_formats": cfg.get("date_formats") or _GENERIC_DATE_FORMATS}
                for role in ("date", "description", "amount", "debit", "credit"):
                    if cfg.get(role):
                        m[role] = lower.index(cfg[role].lower())
                return m, True
        else:
            cols = cfg.get("columns") or []
            fmts = cfg.get("date_formats") or _GENERIC_DATE_FORMATS
            if cols and len(header) >= len(cols) and "date" in cols and \
                    _looks_like_date(header[cols.index("date")], fmts):
                m = {"name": name, "date_formats": fmts}
                for i, role in enumerate(cols):
                    m[role] = i
                return m, False

    def pick(aliases):
        for a in aliases:
            if a in lower:
                return lower.index(a)
        return None

    m = {"name": "auto", "date_formats": _GENERIC_DATE_FORMATS,
         "date": pick(_DATE_ALIASES), "description": pick(_DESC_ALIASES),
         "amount": pick(_AMOUNT_ALIASES), "debit": pick(_DEBIT_ALIASES), "credit": pick(_CREDIT_ALIASES)}
    missing = [r for r in ("date", "description") if m[r] is None]
    if m["amount"] is None and m["debit"] is None and m["credit"] is None:
        missing.append("amount (or debit/credit)")
    if missing:
        # Header names are column labels, not transaction data, so they are safe to echo.
        raise ImportError_("couldn't find " + ", ".join(missing) + " column(s); headers were: "
                           + ", ".join(str(h)[:40] for h in header[:12]))
    return {k: v for k, v in m.items() if v is not None}, True


def parse_csv(raw: bytes, formats=None):
    """bytes -> (rows, info). rows = [{date, amount(Decimal), description}]; info has the mapping
    name, delimiter and a skipped-row count. Nothing here is logged or written."""
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ImportError_(f"file is over the {MAX_UPLOAD_BYTES // 1024} KB limit")
    text = decode(raw)
    delim = detect_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    all_rows = [r for r in reader if any(c.strip() for c in r)]
    if not all_rows:
        raise ImportError_("CSV has no rows")
    if len(all_rows) > MAX_ROWS:
        raise ImportError_(f"CSV has more than {MAX_ROWS} rows")
    fmts = formats if formats is not None else [(n, c) for n, _, c in DEFAULT_FORMATS]
    mapping, has_header = resolve_mapping(all_rows[0], all_rows[1] if len(all_rows) > 1 else None, fmts)
    body = all_rows[1:] if has_header else all_rows
    out, skipped = [], 0
    for r in body:
        try:
            d = parse_date(r[mapping["date"]], mapping["date_formats"])
            if "amount" in mapping:
                amt = parse_amount(r[mapping["amount"]])
            else:
                deb = abs(parse_amount(r[mapping["debit"]])) if "debit" in mapping and r[mapping["debit"]].strip() else Decimal(0)
                cred = abs(parse_amount(r[mapping["credit"]])) if "credit" in mapping and r[mapping["credit"]].strip() else Decimal(0)
                amt = cred - deb
            desc = r[mapping["description"]].strip()
        except (IndexError, ValueError, InvalidOperation):
            skipped += 1
            continue
        out.append({"date": d, "amount": amt.quantize(Decimal("0.01")), "description": desc})
    return out, {"format": mapping["name"], "delimiter": delim, "skipped": skipped}


# ---------------------------------------------------------------------------
# Normaliser: messy bank text -> stable merchant key. Keeps the merchant name intact; strips
# payment prefixes, card tails, dates, reference tokens, 6+ digit runs, store numbers and the
# trailing suburb/state/AU. The key never holds an account number.
# ---------------------------------------------------------------------------
_PREFIXES = [
    re.compile(r"^VISA\s+DEBIT\s+PURCHASE\s+CARD\s+\d{4}\s+"),
    re.compile(r"^EFTPOS\s+"),
    re.compile(r"^PAYMENT\s+TO\s+"),
]
_STATES = {"VIC", "NSW", "QLD", "SA", "WA", "TAS", "NT", "ACT"}
_COUNTRY = {"AU", "AUS", "AUSTRALIA"}
_DATE_TOKEN = re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b")
_CARD_TAIL = re.compile(r"\bCARD\s+(?:NO\.?\s*)?[X*]*\d{4}\b")
_LONG_DIGITS = re.compile(r"\d{6,}")
_STORE_NO = re.compile(r"^\d+$")


def _is_ref_token(t: str) -> bool:
    if re.fullmatch(r"[X*]{2,}\d*", t):                     # XXXX1234 / ****
        return True
    if re.fullmatch(r"[A-Z]{1,4}\.[A-Z0-9.]+", t):           # DT.XXXX style references
        return True
    return len(t) >= 4 and bool(re.search(r"[A-Z]", t)) and bool(re.search(r"\d", t))


def normalise_description(raw: str) -> str:
    s = re.sub(r"\s+", " ", str(raw or "").upper()).strip()
    if _TRANSFER_RE.search(s):
        return TRANSFER_KEY
    changed = True
    while changed:                       # prefixes can stack (EFTPOS VISA DEBIT ...)
        changed = False
        for p in _PREFIXES:
            n = p.sub("", s, count=1)
            if n != s:
                s, changed = n, True
    # Card tails, dates and long digit runs become a boundary marker: AU bank text reads
    # "<MERCHANT> <ref/date/store#> <SUBURB> AU", so whatever follows a stripped token is location.
    s = _CARD_TAIL.sub(" | ", s)
    s = _DATE_TOKEN.sub(" | ", s)
    s = _LONG_DIGITS.sub(" | ", s)
    toks = s.split()
    while toks and toks[0] == "|":
        toks.pop(0)
    if not toks:
        return ""
    # The first token is always the merchant (never stripped, so "7ELEVEN" survives).
    kept = [toks[0]]
    for t in toks[1:]:
        if t == "|" or _STORE_NO.match(t) or _is_ref_token(t):
            break
        kept.append(t)
    # Trailing country, then state, then the one-word suburb that sat before the country code.
    had_country = len(kept) > 1 and kept[-1] in _COUNTRY
    if had_country:
        kept.pop()
    if len(kept) > 1 and kept[-1] in _STATES:
        kept.pop()
    if had_country and len(kept) > 2:
        kept.pop()
    key = " ".join(kept)
    key = re.sub(r"[^A-Z0-9&'*.\- ]+", " ", key)
    return re.sub(r"\s+", " ", key).strip(" .-*")[:120]


def mask_description(raw: str) -> str:
    """What gets stored as the display description: long digit runs (account/reference numbers)
    replaced, whitespace collapsed, length capped."""
    return re.sub(r"\s+", " ", _LONG_DIGITS.sub("######", str(raw or ""))).strip()[:500]


def norm_pattern(match_type: str, pattern: str) -> str:
    if match_type == "regex":
        return str(pattern or "").strip()
    return re.sub(r"\s+", " ", str(pattern or "").upper()).strip()


def match_rule(key: str, rules):
    """First match wins: every exact rule, then every contains rule, then every regex rule. Within a
    type, lower priority number first, then lower id. -> rule dict or None."""
    order = {t: i for i, t in enumerate(MATCH_TYPES)}
    for r in sorted(rules, key=lambda r: (order.get(r["match_type"], 9), r.get("priority", 100), r["id"])):
        mt, pat = r["match_type"], r["pattern"]
        if mt == "exact" and key == pat:
            return r
        if mt == "contains" and pat and pat in key:
            return r
        if mt == "regex":
            try:
                if re.search(pat, key, re.I):
                    return r
            except re.error:
                continue
    return None


def dedupe_hash(d: date, amount: Decimal, key: str, occurrence: int) -> str:
    return hashlib.sha256(f"{d.isoformat()}|{Decimal(amount):.2f}|{key}|{occurrence}".encode()).hexdigest()


def prepare_rows(rows):
    """Add key, occurrence index and hash. Occurrence counts identical (date, amount, key) rows in
    file order, so two genuine same-day same-amount purchases both import, and the same file twice
    yields the same hashes."""
    seen = {}
    out = []
    for r in rows:
        key = normalise_description(r["description"])
        k = (r["date"], r["amount"], key)
        occ = seen.get(k, 0)
        seen[k] = occ + 1
        out.append({**r, "key": key, "occurrence": occ,
                    "hash": dedupe_hash(r["date"], r["amount"], key, occ)})
    return out


# ---------------------------------------------------------------------------
# Postgres (asyncpg connection passed in; this module never opens a network client itself)
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS norm_key TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS occurrence_idx INTEGER;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS dedupe_hash TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS rule_id BIGINT;
ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_txn_date_description_amount_key;
CREATE UNIQUE INDEX IF NOT EXISTS transactions_dedupe_hash_key ON transactions (dedupe_hash);
CREATE INDEX IF NOT EXISTS transactions_norm_key_idx ON transactions (norm_key);
CREATE TABLE IF NOT EXISTS category_rules (
    id          BIGSERIAL PRIMARY KEY,
    match_type  TEXT NOT NULL CHECK (match_type IN ('exact','contains','regex')),
    pattern     TEXT NOT NULL,
    category    TEXT NOT NULL REFERENCES spending_categories(key) ON UPDATE CASCADE ON DELETE CASCADE,
    priority    INTEGER NOT NULL DEFAULT 100,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (match_type, pattern)
);
CREATE TABLE IF NOT EXISTS csv_import_formats (
    name      TEXT PRIMARY KEY,
    priority  INTEGER NOT NULL DEFAULT 100,
    enabled   BOOLEAN NOT NULL DEFAULT TRUE,
    config    JSONB NOT NULL
);
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'transactions_rule_id_fkey') THEN
    ALTER TABLE transactions ADD CONSTRAINT transactions_rule_id_fkey
      FOREIGN KEY (rule_id) REFERENCES category_rules(id) ON DELETE SET NULL;
  END IF;
END $$;
"""


async def ensure_schema(conn):
    """Idempotent. Adds the import columns/tables, seeds formats, and backfills key/occurrence/hash
    on rows imported before this build so re-importing an old statement dedupes against them."""
    await conn.execute(SCHEMA_SQL)
    for name, prio, cfg in DEFAULT_FORMATS:
        await conn.execute(
            "INSERT INTO csv_import_formats (name, priority, config) VALUES ($1,$2,$3::jsonb) "
            "ON CONFLICT (name) DO NOTHING", name, prio, json.dumps(cfg))
    pending = await conn.fetch(
        "SELECT id, txn_date, amount, description FROM transactions WHERE dedupe_hash IS NULL ORDER BY id")
    if pending:
        taken = {r["dedupe_hash"] for r in await conn.fetch(
            "SELECT dedupe_hash FROM transactions WHERE dedupe_hash IS NOT NULL")}
        counts = {}
        async with conn.transaction():
            for r in pending:
                key = normalise_description(r["description"])
                k = (r["txn_date"], r["amount"], key)
                occ = counts.get(k, 0)
                h = dedupe_hash(r["txn_date"], r["amount"], key, occ)
                while h in taken:
                    occ += 1
                    h = dedupe_hash(r["txn_date"], r["amount"], key, occ)
                counts[k] = occ + 1
                taken.add(h)
                await conn.execute(
                    "UPDATE transactions SET norm_key=$2, occurrence_idx=$3, dedupe_hash=$4 WHERE id=$1",
                    r["id"], key, occ, h)


async def transfer_category(conn) -> str:
    """The existing own-account transfer category if there is one, else create 'transfers'."""
    row = await conn.fetchval(
        "SELECT key FROM spending_categories WHERE exclude_from_totals "
        "ORDER BY (key='savings_transfers') DESC, (key ILIKE '%transfer%' OR label ILIKE '%transfer%') DESC, "
        "sort_order LIMIT 1")
    if row:
        return row
    await conn.execute(
        "INSERT INTO spending_categories (key, label, sort_order, exclude_from_totals, cap_enabled) "
        "VALUES ('transfers','Transfer',99,TRUE,FALSE) ON CONFLICT (key) DO UPDATE SET exclude_from_totals=TRUE")
    return "transfers"


async def ensure_transfer_rule(conn):
    cat = await transfer_category(conn)
    await conn.execute(
        "INSERT INTO category_rules (match_type, pattern, category, priority) VALUES ('exact',$1,$2,0) "
        "ON CONFLICT (match_type, pattern) DO NOTHING", TRANSFER_KEY, cat)


async def load_formats(conn):
    rows = await conn.fetch(
        "SELECT name, config FROM csv_import_formats WHERE enabled ORDER BY priority, name")
    out = []
    for r in rows:
        cfg = r["config"]
        out.append((r["name"], json.loads(cfg) if isinstance(cfg, str) else cfg))
    return out


async def load_rules(conn):
    rows = await conn.fetch("SELECT id, match_type, pattern, category, priority FROM category_rules")
    return [dict(r) for r in rows]


async def import_csv(conn, raw: bytes, filename: str = "", legacy_category=None) -> dict:
    """Full import inside one transaction. Returns COUNTS ONLY. `legacy_category(description)` is the
    dashboard's pre-existing merchant-rule lookup (S85 rules keyed on _merchant_key); a row covered
    by one is not queued as uncategorised, but carries no rule id because those rules predate this
    table. The raw bytes are not kept: the caller drops them when this returns."""
    await ensure_schema(conn)
    await ensure_transfer_rule(conn)
    rows, info = parse_csv(raw, await load_formats(conn))
    rows = prepare_rows(rows)
    rules = await load_rules(conn)
    src = mask_description(filename)[:120] or "csv-upload"
    imported = duplicates = uncategorised = 0
    async with conn.transaction():
        for r in rows:
            rule = match_rule(r["key"], rules)
            category = rule["category"] if rule else UNCATEGORISED
            new_id = await conn.fetchval(
                """INSERT INTO transactions (txn_date, description, amount, category, source_file,
                                             norm_key, occurrence_idx, dedupe_hash, rule_id)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                   ON CONFLICT (dedupe_hash) DO NOTHING RETURNING id""",
                r["date"], mask_description(r["description"]), r["amount"], category, src,
                r["key"], r["occurrence"], r["hash"], rule["id"] if rule else None)
            if new_id is None:
                duplicates += 1
                continue
            imported += 1
            if rule is None and not (legacy_category and legacy_category(r["description"]) != UNCATEGORISED):
                uncategorised += 1
    return {"imported": imported, "duplicates": duplicates, "uncategorised": uncategorised,
            "skipped": info["skipped"], "rows_in_file": len(rows), "format": info["format"]}


async def create_rule(conn, match_type: str, pattern: str, category: str, priority: int = 100) -> int:
    if match_type not in MATCH_TYPES:
        raise ImportError_("match_type must be exact, contains or regex")
    pat = norm_pattern(match_type, pattern)
    if not pat:
        raise ImportError_("empty pattern")
    if match_type == "regex":
        try:
            re.compile(pat)
        except re.error:
            raise ImportError_("invalid regex")
    return await conn.fetchval(
        """INSERT INTO category_rules (match_type, pattern, category, priority) VALUES ($1,$2,$3,$4)
           ON CONFLICT (match_type, pattern) DO UPDATE SET category=EXCLUDED.category,
                                                          priority=EXCLUDED.priority
           RETURNING id""", match_type, pat, category, priority)


async def apply_rule(conn, rule_id: int, apply_past: bool, txn_ids=()) -> int:
    """Stamp a rule onto transactions. Always the given txn_ids (if they match); with apply_past,
    also every earlier non-manual row whose key matches and that no other rule has claimed. A manual
    pick is never overwritten. Returns rows changed."""
    rule = await conn.fetchrow(
        "SELECT id, match_type, pattern, category, priority FROM category_rules WHERE id=$1", rule_id)
    if rule is None:
        return 0
    rule = dict(rule)
    if apply_past:
        cands = await conn.fetch(
            "SELECT id, norm_key FROM transactions WHERE NOT manual AND norm_key IS NOT NULL "
            "AND (rule_id IS NULL OR rule_id=$1)", rule_id)
    else:
        cands = await conn.fetch(
            "SELECT id, norm_key FROM transactions WHERE NOT manual AND id = ANY($1::bigint[])",
            [int(i) for i in txn_ids])
    ids = [c["id"] for c in cands if match_rule(c["norm_key"] or "", [rule])]
    if not ids:
        return 0
    await conn.execute(
        "UPDATE transactions SET category=$1, rule_id=$2 WHERE id = ANY($3::bigint[])",
        rule["category"], rule_id, ids)
    return len(ids)
