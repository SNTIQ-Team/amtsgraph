# Amtsgraph — worked examples

All examples run against the shipped `data/atlas.db`. Every result below
was verified against the live official registers at build time.

## SQL (sqlite3 data/atlas.db)

### Which court handles a civil claim at Berlin 10115?

```sql
SELECT cc.position, cc.role, a.name, e.value AS xjustiz_id,
       a.street, a.postal_address, a.erv_note
FROM court_chain cc
JOIN authority a ON a.id = cc.authority_id
LEFT JOIN authority_external_id e
       ON e.authority_id = a.id AND e.scheme = 'xjustiz'
WHERE cc.plz = '10115' AND cc.ortk = 'BERLIN' AND cc.matter = 'zivil'
ORDER BY cc.role DESC, cc.position;
-- 1. Amtsgericht Mitte (F1112) -> 2. Landgericht Berlin II (F6529)
-- -> 3. Kammergericht (F1000), plus Staatsanwaltschaft entries
```

### …but a dunning procedure (Mahnverfahren) at the same address?

```sql
SELECT a.name FROM court_chain cc
JOIN authority a ON a.id = cc.authority_id
WHERE cc.plz = '10115' AND cc.ortk = 'BERLIN'
  AND cc.matter = 'mahn' AND cc.role = 'court';
-- Amtsgericht Wedding — Zentrales Mahngericht Berlin-Brandenburg
-- (competence depends on the MATTER, not just the place)
```

### One postal code, nine villages, different courts

```sql
SELECT jp.ort, a.name AS amtsgericht
FROM jz_place jp
JOIN court_chain cc ON cc.plz = jp.plz AND cc.ortk = jp.ortk
     AND cc.matter = 'zivil' AND cc.role = 'court' AND cc.position = 1
JOIN authority a ON a.id = cc.authority_id
WHERE jp.plz = '25712';
-- 9 localities resolving to different Amtsgerichte — why Amtsgraph
-- never groups by postal code alone
```

### The AsylbLG office for Landkreis Erding, with its supervisor

```sql
SELECT c.rank, a.name, a.email, a.web
FROM competence c JOIN authority a ON a.id = c.authority_id
WHERE c.kind = 'asylblg_behoerde' AND c.area = '09177'
  AND a.valid_to IS NULL
ORDER BY c.rank;
-- rank 0: Landratsamt Erding - Fachbereich 24 - Asylmanagement
-- rank 1: Regierung von Oberbayern - SG 14.1 (supervisory, not for filing)
```

### Walk the organisational web upward (who supervises whom)

```sql
WITH RECURSIVE up(id, name, lvl) AS (
  SELECT id, name, 0 FROM authority
  WHERE name LIKE '%Fachbereich 24 - Asylmanagement%' AND valid_to IS NULL
  UNION ALL
  SELECT a.id, a.name, up.lvl + 1
  FROM up
  JOIN authority_edge e ON e.from_authority = up.id
       AND e.relation = 'parent'
  JOIN authority a ON a.id = e.to_authority
  WHERE up.lvl < 6
)
SELECT printf('%.*c↑ ', lvl + 1, ' ') || name FROM up;
-- Fachbereich 24 ↑ Abteilung 2 - Jugend und Soziales ↑ Landratsamt Erding
```

### Jobcenter legal forms (gE vs zkT) nationwide

```sql
SELECT legal_form, COUNT(*) FROM authority
WHERE kind = 'jobcenter' AND valid_to IS NULL GROUP BY legal_form;
```

### Never serve an answer without its caveats

```sql
SELECT severity, text_de FROM caveat
WHERE (scope_level = 'gemeinde' AND scope_key = :ags)
   OR (scope_level = 'kreis'    AND scope_key = substr(:ags, 1, 5))
   OR (scope_level = 'jz_place' AND scope_key = :plz || '|' || :ortk);
```

## Python

```python
import sqlite3

db = sqlite3.connect("file:data/atlas.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row

def court_chain(plz: str, ortk: str, matter: str) -> list[dict]:
    """Full instance chain for a place and legal matter."""
    rows = db.execute("""
        SELECT cc.position, cc.role, a.name, a.street, a.postal_address,
               a.phone, a.email, a.erv_note
        FROM court_chain cc JOIN authority a ON a.id = cc.authority_id
        WHERE cc.plz = ? AND cc.ortk = ? AND cc.matter = ?
        ORDER BY cc.role DESC, cc.position""", (plz, ortk, matter))
    return [dict(r) for r in rows]

for step in court_chain("86720", "NOERDLINGEN", "insolv"):
    print(step["position"], step["role"], step["name"], "|", step["street"])
# Note: the Insolvenzgericht often files at a DIFFERENT address than the
# parent Amtsgericht — Amtsgraph keeps them as separate records.
```

## HTTP (the bundled browser / API)

```bash
python3 tools/browser.py &            # zero-dependency, port 8400

# place search with disambiguation context
curl 'http://127.0.0.1:8400/api/search?q=Neustadt'

# everything about a municipality: authorities, hats, caveats
curl 'http://127.0.0.1:8400/api/gemeinde?ags=09177117'

# court resolution, matter-aware
curl 'http://127.0.0.1:8400/api/court?plz=12555&ortk=BERLIN&matter=betreu'
# -> Amtsgericht Köpenick (NOT Mitte — Berlin splits by postal code)
```

The FastAPI service (`uvicorn api.main:app`) exposes the same logic with
`needs_ort` disambiguation, supervisory separation and provenance per
record — see [api/main.py](../api/main.py).
