-- 010_add_country_discovery_rank.sql — The order in which countries are discovered
--
-- Rank 1 is discovered first. EU member states come first, then the EFTA
-- states, then the other European countries. Within a group, the countries
-- active when this migration was written (BE, BG, ES, RS) come first, and the
-- rest follow by national population, largest first (Eurostat, 1 January 2025).
--
-- On a fresh database the table is empty here; seed/countries.sql inserts the
-- same ranks.

ALTER TABLE countries ADD COLUMN discovery_rank INTEGER;

UPDATE countries SET discovery_rank = ranks.discovery_rank
FROM (VALUES
    -- EU
    ('ES', 1), ('BE', 2), ('BG', 3),
    ('DE', 4), ('FR', 5), ('IT', 6), ('PL', 7), ('RO', 8), ('NL', 9),
    ('CZ', 10), ('PT', 11), ('SE', 12), ('GR', 13), ('HU', 14), ('AT', 15),
    ('DK', 16), ('FI', 17), ('IE', 18), ('SK', 19), ('HR', 20), ('LT', 21),
    ('SI', 22), ('LV', 23), ('EE', 24), ('CY', 25), ('LU', 26), ('MT', 27),
    -- EFTA
    ('CH', 28), ('NO', 29), ('IS', 30), ('LI', 31),
    -- Other European countries
    ('RS', 32),
    ('TR', 33), ('GB', 34), ('UA', 35), ('BA', 36), ('MD', 37), ('AL', 38),
    ('MK', 39), ('XK', 40), ('ME', 41), ('AD', 42), ('MC', 43), ('SM', 44)
) AS ranks (iso, discovery_rank)
WHERE countries.iso = ranks.iso;

ALTER TABLE countries ALTER COLUMN discovery_rank SET NOT NULL;
ALTER TABLE countries ADD CONSTRAINT countries_discovery_rank_key UNIQUE (discovery_rank);
