-- Amtsgraph — SQLite schema (correctness-first)
-- The DB is a build artifact: rebuilt from snapshots + overrides, never hand-edited.
--
-- Design notes (see docs/ARCHITECTURE.md):
--  * Court competence in Germany resolves per (PLZ, Ort, MATTER) — one PLZ can
--    hold 9 villages in 9 different court districts, and the competent court
--    depends on the legal matter (Mahnverfahren -> central Mahngericht etc.).
--  * For courts we store the EXACT instance chain as harvested from the
--    official Orts- und Gerichtsverzeichnis and serve it verbatim; the
--    edge graph is derived and cross-checked, never the primary answer.
--  * Every row knows where it came from and when (provenance), and anything
--    the source flags as ambiguous becomes a caveat the API must surface.

PRAGMA foreign_keys = ON;

-- ============================================================ GEO plane

CREATE TABLE land (
    code        TEXT PRIMARY KEY,            -- '09'
    name        TEXT NOT NULL                -- 'Bayern'
);

CREATE TABLE kreis (
    ags         TEXT PRIMARY KEY,            -- 5 digits, '09779'
    land_code   TEXT NOT NULL REFERENCES land(code),
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,               -- 'Landkreis' | 'Kreisfreie Stadt' | ...
    regierungsbezirk TEXT                    -- attribute, NOT a hierarchy level
);

CREATE TABLE gemeinde (
    ags         TEXT PRIMARY KEY,            -- 8 digits, '09779194'
    ars         TEXT NOT NULL UNIQUE,        -- 12 digits
    kreis_ags   TEXT NOT NULL REFERENCES kreis(ags),
    name        TEXT NOT NULL,               -- official: 'Nördlingen, GKSt'
    name_simple TEXT NOT NULL,               -- display:  'Nördlingen'
    name_norm   TEXT NOT NULL,               -- search:   'noerdlingen'
    kind        TEXT,
    population  INTEGER
);
CREATE INDEX idx_gemeinde_norm ON gemeinde(name_norm);

CREATE TABLE gemeinde_plz (                  -- many-to-many, both directions
    ags         TEXT NOT NULL REFERENCES gemeinde(ags),
    plz         TEXT NOT NULL,
    PRIMARY KEY (ags, plz)
);
CREATE INDEX idx_plz ON gemeinde_plz(plz);

-- Official place register of the Orts- und Gerichtsverzeichnis.
-- THE resolution unit for court competence is (plz, ortk), not PLZ alone.
CREATE TABLE jz_place (
    plz         TEXT NOT NULL,
    ortk        TEXT NOT NULL,               -- normalized key, 'NOERDLINGEN'
    ort         TEXT NOT NULL,               -- display, 'Nördlingen'
    ort_norm    TEXT NOT NULL,               -- our fold for search
    gs_key      TEXT NOT NULL,               -- gsbl|gsregbez|gskreis|gsort concatenated:
                                             -- places sharing gs_key share courts
    gebm        TEXT,
    gemeinde_ags TEXT REFERENCES gemeinde(ags),  -- best-effort link to GEO spine
    PRIMARY KEY (plz, ortk)
);
CREATE INDEX idx_jz_ort ON jz_place(ort_norm);
CREATE INDEX idx_jz_gs ON jz_place(gs_key);

-- ========================================================= MATTER taxonomy

-- Legal matters, codes = `ang` parameter of the official court finder.
CREATE TABLE matter (
    code        TEXT PRIMARY KEY,            -- 'zivil','familie','mahn','insolv',...
    label_de    TEXT NOT NULL,
    grp         TEXT NOT NULL,               -- 'ordentliche' | 'fach' | 'sonder'
    core        INTEGER NOT NULL DEFAULT 0   -- harvested by default when 1
);

-- ====================================================== AUTHORITY plane

CREATE TABLE authority (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,               -- see authority_kind
    name        TEXT NOT NULL,
    name_norm   TEXT NOT NULL,
    legal_form  TEXT,                        -- jobcenter: 'gE'|'zkT'
    street      TEXT, plz TEXT, city TEXT,
    postal_address TEXT,
    phone       TEXT, fax TEXT, email TEXT, web TEXT,
    hours       TEXT,
    erv_note    TEXT,                        -- electronic legal communication status
    lat REAL, lon REAL,
    -- provenance: any fact served to a client must be traceable
    source      TEXT NOT NULL,               -- 'justizadressen'|'pvog'|'ba'|'override'
    source_url  TEXT,
    fetched_at  TEXT NOT NULL,
    source_updated_at TEXT,
    valid_from  TEXT, valid_to TEXT          -- soft-retire merged/closed offices
);
CREATE INDEX idx_auth_kind ON authority(kind);
CREATE INDEX idx_auth_norm ON authority(name_norm);

CREATE TABLE authority_kind (
    kind        TEXT PRIMARY KEY,
    label_de    TEXT NOT NULL
);
INSERT INTO authority_kind VALUES
    ('amtsgericht','Amtsgericht'), ('landgericht','Landgericht'),
    ('oberlandesgericht','Oberlandesgericht'), ('oberstes_landesgericht','Oberstes Landesgericht'),
    ('bundesgericht','Bundesgericht'), ('verfassungsgericht','Verfassungsgericht'),
    ('sozialgericht','Sozialgericht'), ('landessozialgericht','Landessozialgericht'),
    ('verwaltungsgericht','Verwaltungsgericht'), ('oberverwaltungsgericht','Oberverwaltungsgericht'),
    ('arbeitsgericht','Arbeitsgericht'), ('landesarbeitsgericht','Landesarbeitsgericht'),
    ('finanzgericht','Finanzgericht'),
    ('staatsanwaltschaft','Staatsanwaltschaft'), ('generalstaatsanwaltschaft','Generalstaatsanwaltschaft'),
    ('jobcenter','Jobcenter'), ('arbeitsagentur','Agentur für Arbeit'),
    ('familienkasse','Familienkasse'),
    ('auslaenderbehoerde','Ausländerbehörde'), ('buergeramt','Bürgeramt'),
    ('sozialamt','Sozialamt'), ('jugendamt','Jugendamt'),
    ('asylblg_behoerde','Leistungsbehörde nach AsylbLG'),
    ('wohngeldstelle','Wohngeldstelle'),
    ('standesamt','Standesamt'), ('gewerbeamt','Gewerbeamt'),
    ('aufsichtsbehoerde','Aufsichtsbehörde'), ('ministerium','Ministerium'),
    ('bamf','Bundesamt für Migration und Flüchtlinge (BAMF)'),
    ('bundespolizei','Bundespolizei'),
    ('eu_institution','Organ der Europäischen Union'),
    ('eu_body','Einrichtung der Europäischen Union'),
    ('eu_court','Gericht der Europäischen Union'),
    ('justizbehoerde','Sonstige Justizbehörde'), ('sonstige','Sonstige Behörde');

-- NB: not unique per (scheme,value) — a court department (Insolvenzgericht
-- of an Amtsgericht, often at a different filing address!) shares the
-- parent's XJustiz-ID but must stay a separate authority row.
CREATE TABLE authority_external_id (
    authority_id INTEGER NOT NULL REFERENCES authority(id),
    scheme      TEXT NOT NULL,               -- 'xjustiz'|'pvog_oe'|'ba'|'abh_kennung'
    value       TEXT NOT NULL,
    PRIMARY KEY (scheme, value, authority_id)
);
CREATE INDEX idx_ext_auth ON authority_external_id(authority_id);

-- Non-court competence (PVOG/BA sourced): who serves which area *for which
-- kind*. kind lives HERE, not only on authority: one office wears many hats
-- (a Landratsamt can be Ausländerbehörde, Sozialamt and AsylbLG office at
-- once), and for heterogeneous kinds (AsylbLG!) several offices can be
-- legitimately competent in one Gemeinde — the API then returns candidates
-- plus a caveat instead of guessing.
CREATE TABLE competence (
    authority_id INTEGER NOT NULL REFERENCES authority(id),
    kind        TEXT NOT NULL REFERENCES authority_kind(kind),
    level       TEXT NOT NULL CHECK (level IN ('land','kreis','gemeinde','plz')),
    area        TEXT NOT NULL,               -- land code | kreis AGS | AGS | PLZ
    rank        INTEGER NOT NULL DEFAULT 0,  -- 0 = primary/local office,
                                             -- 1 = übergeordnete/Aufsichts-
                                             --     behörde (e.g. Regierung von
                                             --     Oberbayern for AsylbLG
                                             --     accommodation) — shown as
                                             --     supervisory, never as THE answer
    PRIMARY KEY (authority_id, kind, level, area)
);
CREATE INDEX idx_comp_area ON competence(kind, level, area);

-- ===================================== COURT CHAINS (primary truth for courts)

-- Exact instance chain as returned by the official court finder for
-- (place, matter). position 1 = first instance, ascending = higher instances.
-- Keyed per (plz, ortk) — NO grouping: the portal resolves by the PLZ
-- itself (10115 and 12555 Berlin share the register's structural key but
-- different Amtsgerichte), so every place is harvested directly.
CREATE TABLE court_chain (
    plz         TEXT NOT NULL,
    ortk        TEXT NOT NULL,
    matter      TEXT NOT NULL REFERENCES matter(code),
    position    INTEGER NOT NULL,            -- 1,2,3,...
    authority_id INTEGER NOT NULL REFERENCES authority(id),
    role        TEXT NOT NULL,               -- 'court' | 'prosecution'
    note        TEXT,                        -- e.g. 'Kammer für Handelssachen ist eingerichtet.'
    PRIMARY KEY (plz, ortk, matter, role, position)
);
CREATE INDEX idx_chain_auth ON court_chain(authority_id);

-- ========================================================== GRAPH plane

-- Derived/auxiliary relations. For courts these are DERIVED from court_chain
-- and cross-checked by validate.py — never the primary source of an answer.
-- Edges carry QFS-inspired traversal semantics:
--   delta  in [0,1]: flow directionality. 1.0 = strictly directed (appeal:
--          you can only appeal upward), ~0.45 = semi-directed (parent:
--          department -> parent is an easy hop, parent -> one of many
--          departments is a costly fan-out), 0.0 = symmetric.
--   trust  in [0,1]: provenance confidence of the edge itself, seeded from
--          source_trust of whatever produced it.
-- Conductance of a hop from A to B: 0.5 + 0.5*delta*(role_A - role_B),
-- with role +1 at the edge's from-side and -1 at its to-side; traversal
-- cost = -log(conductance * trust). See api /graph endpoints.
CREATE TABLE authority_edge (
    from_authority INTEGER NOT NULL REFERENCES authority(id),
    to_authority   INTEGER NOT NULL REFERENCES authority(id),
    relation    TEXT NOT NULL CHECK (relation IN
                  ('appeal','supervision','parent','successor',
                   'institutional_part','political_accountability',
                   'judicial_review','cooperation','co_legislation',
                   'reporting_accountability','financial_audit',
                   'maladministration_review','sectoral_oversight')),
    matter      TEXT REFERENCES matter(code),    -- appeal edges are matter-specific
    note        TEXT,
    delta       REAL NOT NULL DEFAULT 1.0,
    trust       REAL NOT NULL DEFAULT 0.8,
    source      TEXT,                       -- provenance of the relation itself
    source_url  TEXT,
    PRIMARY KEY (from_authority, to_authority, relation, matter)
);
-- SQLite treats NULL values as distinct even inside a composite UNIQUE/PK,
-- while most non-matter relations deliberately use matter=NULL.  Close that
-- hole explicitly so repeated loaders cannot create duplicate graph edges.
CREATE UNIQUE INDEX authority_edge_identity
    ON authority_edge(from_authority, to_authority, relation,
                      COALESCE(matter, ''));

-- provenance confidence per source, used to seed edge/record trust
CREATE TABLE source_trust (
    source      TEXT PRIMARY KEY,
    trust       REAL NOT NULL,                -- 0..1
    rationale   TEXT
);
INSERT INTO source_trust VALUES
    ('justizadressen', 0.95, 'official federal/state court register, harvested per place x matter'),
    ('bayernportal',   0.90, 'official Land portal organigrams'),
    ('ba',             0.95, 'official BA SGB-II Traeger register'),
    ('pvog',           0.75, 'federal aggregate of Land editorial systems; quality varies by Land'),
    ('destatis',       0.98, 'official municipal register'),
    ('eu_curated',     0.98, 'curated from EU primary law and official EU institution pages'),
    ('override',       0.85, 'manually verified correction with documented source');

-- QFS-style hyperedge view: a court chain (place x matter) is one
-- hyperedge whose endpoints carry roles — the place is the source (+1),
-- courts are sinks graded by instance (-position/10), prosecution offices
-- are near-neutral participants.
CREATE VIEW hyperedge_court AS
SELECT plz || '|' || ortk || '|' || matter          AS hyperedge_id,
       plz, ortk, matter, authority_id,
       CASE role WHEN 'prosecution' THEN -0.05
                 ELSE -CAST(position AS REAL) / 10 END AS endpoint_role,
       role, position
FROM court_chain;

-- ============================================================== CAVEATS

-- Anything the source flags as ambiguous or that our build cannot resolve
-- cleanly. The API MUST attach matching caveats to every answer it serves —
-- for legal use a silent wrong guess is worse than an honest warning.
CREATE TABLE caveat (
    id          INTEGER PRIMARY KEY,
    scope_level TEXT NOT NULL CHECK (scope_level IN
                  ('plz','jz_place','gemeinde','kreis','authority','matter','global')),
    scope_key   TEXT NOT NULL,               -- e.g. '25712' / '25712|BURG' / AGS / authority id
    matter      TEXT,                        -- optional narrowing
    severity    TEXT NOT NULL CHECK (severity IN ('info','warn','block')),
    text_de     TEXT NOT NULL,
    source      TEXT NOT NULL
);
CREATE INDEX idx_caveat_scope ON caveat(scope_level, scope_key);

-- ============================================================== search

CREATE VIRTUAL TABLE place_fts USING fts5(
    name_norm, ags UNINDEXED, content='gemeinde', content_rowid='rowid'
);

-- ============================================================ metadata

CREATE TABLE build_info (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);

-- matter seed: codes = official `ang` values of justizadressen.nrw.de
INSERT INTO matter (code, label_de, grp, core) VALUES
    ('zivil','Allgemeiner Gerichtsstand (Zivil)','ordentliche',1),
    ('familie','Familienrechtssachen','ordentliche',1),
    ('mahn','Mahnverfahren','ordentliche',1),
    ('insolv','Unternehmensinsolvenzsachen','ordentliche',1),
    ('insolvver','Verbraucherinsolvenzsachen','ordentliche',1),
    ('zvg','Zwangsversteigerungssachen','ordentliche',1),
    ('zwangsvoll','Zwangsvollstreckungssachen','ordentliche',1),
    ('grundbuch','Grundbuchsachen','ordentliche',1),
    ('nachlass','Nachlasssachen','ordentliche',1),
    ('betreu','Betreuungssachen','ordentliche',1),
    ('handelsreg','Handels- und Genossenschaftsregistersachen','ordentliche',1),
    ('gesell','Gesellschaftsregistersachen','ordentliche',0),
    ('partner','Partnerschaftsregistersachen','ordentliche',0),
    ('verein','Vereinsregistersachen','ordentliche',0),
    ('arbeit','Arbeitsgerichtssachen','fach',1),
    ('arbgermahn','Arbeitsgerichtliche Mahnverfahren','fach',0),
    ('sozial','Sozialgerichtssachen','fach',1),
    ('verwaltung','Verwaltungsrechtssachen','fach',1),
    ('finanz','Finanzgerichtssachen','fach',0),
    ('staatsanw','Angelegenheiten der Staatsanwaltschaften','sonder',0),
    ('ausuntge','Auslandsunterhaltsgesetz','sonder',0),
    ('berperst','Berichtigung Personenstandsregister','sonder',0),
    ('anwstand','Anweisung Standesamt','sonder',0),
    ('flurber','Flurbereinigungssachen','sonder',0),
    ('gvollzvert','Gerichtsvollzieherverteilerstellen','sonder',0),
    ('honorar','Honorarforderungen','sonder',0),
    ('kfh','Kammer für Handelssachen','sonder',0),
    ('landwirt','Landwirtschaftssachen','sonder',0),
    ('reise','Reisevertragssachen','sonder',0),
    ('ecsccj','Small Claims Allgemein','sonder',0),
    ('ecsccae','Small Claims Instanz','sonder',0),
    ('urheber','Urheberrechtssachen','sonder',0),
    ('versich','Versicherungsvertragssachen','sonder',0),
    ('vornagesch','Vornamen/Geschlechtszugehörigkeit','sonder',0),
    ('zbergweg','Zentrale Berufungskammern WEG','sonder',0),
    ('zentvollst','Zentrale Vollstreckungssachen','sonder',0);
