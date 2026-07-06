-- Sample Bulgarian cities for the integration test suite.
--
-- Production city data is imported from Geonames (`agency-audit import-geonames`),
-- which is far too large to load in tests. This fixed sample gives the
-- integration tests a known, deterministic set of cities to work with.
--
-- Loaded by scripts/seed-test-db.py (used locally and by both CI pipelines).
INSERT INTO cities (country, label, slug, population) VALUES
    ('BG', 'Sofia', 'sofia', 1286383),
    ('BG', 'Plovdiv', 'plovdiv', 346893),
    ('BG', 'Varna', 'varna', 334870),
    ('BG', 'Burgas', 'burgas', 200271),
    ('BG', 'Ruse', 'ruse', 149642),
    ('BG', 'Stara Zagora', 'stara-zagora', 138272),
    ('BG', 'Pleven', 'pleven', 106954),
    ('BG', 'Dobrich', 'dobrich', 91030),
    ('BG', 'Sliven', 'sliven', 91470),
    ('BG', 'Shumen', 'shumen', 80855),
    ('BG', 'Pernik', 'pernik', 80191),
    ('BG', 'Yambol', 'yambol', 74132),
    ('BG', 'Haskovo', 'haskovo', 76397),
    ('BG', 'Pazardzhik', 'pazardzhik', 75846),
    ('BG', 'Blagoevgrad', 'blagoevgrad', 70881),
    ('BG', 'Veliko Tarnovo', 'veliko-tarnovo', 68783),
    ('BG', 'Vratsa', 'vratsa', 60682),
    ('BG', 'Gabrovo', 'gabrovo', 58950),
    ('BG', 'Vidin', 'vidin', 48071),
    ('BG', 'Kardzhali', 'kardzhali', 43880)
ON CONFLICT (country, slug) DO NOTHING;
