-- 007_add_website_url_key.sql — One row per website, matched by websites.url_key
--
-- Google Maps gives each branch of an agency its own website link with its own
-- tracking parameters, so the same website was stored once per branch.
-- websites.url_key is the website identity; websites.url stays the address the
-- auditor fetches, without tracking parameters.  The Python functions
-- agency_audit.urls.website_key and clean_website_url define both values; the
-- pg_temp functions below repeat them once, for the rows that exist today, and
-- disappear with the session.

CREATE FUNCTION pg_temp.is_tracking(segment TEXT) RETURNS BOOLEAN
LANGUAGE sql IMMUTABLE AS $$
    SELECT lower(split_part(segment, '=', 1)) LIKE 'utm\_%'
        OR lower(split_part(segment, '=', 1)) IN (
            'gclid', 'gbraid', 'wbraid', 'dclid', 'fbclid', 'msclkid',
            'yclid', 'igshid', 'mc_cid', 'mc_eid', '_ga', '_gl'
        )
$$;

-- Same split as urllib.parse.urlsplit: scheme, netloc, path, query (fragment dropped).
CREATE FUNCTION pg_temp.split_url(url TEXT) RETURNS TEXT[]
LANGUAGE sql IMMUTABLE AS $$
    SELECT regexp_match(
        btrim(url),
        '^(?:([A-Za-z][A-Za-z0-9+.-]*):)?(?://([^/?#]*))?([^?#]*)(?:\?([^#]*))?'
    )
$$;

CREATE FUNCTION pg_temp.website_key(url TEXT) RETURNS TEXT
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    m      TEXT[] := pg_temp.split_url(url);
    scheme TEXT   := lower(coalesce(m[1], ''));
    host   TEXT   := lower(regexp_replace(coalesce(m[2], ''), '^.*@', ''));
    port   TEXT   := substring(host FROM ':([^:]*)$');
    segs   TEXT;
BEGIN
    host := regexp_replace(host, ':[^:]*$', '');
    IF host LIKE 'www.%' THEN
        host := substr(host, 5);
    END IF;
    IF port ~ '^[0-9]{1,5}$' AND port::INTEGER <= 65535 THEN
        IF NOT ((scheme = 'http' AND port::INTEGER = 80)
                OR (scheme = 'https' AND port::INTEGER = 443)) THEN
            host := host || ':' || port::INTEGER;
        END IF;
    ELSIF port IS NOT NULL AND port <> '' THEN
        host := host || ':' || port;  -- invalid port: kept as written
    END IF;

    SELECT string_agg(s, '&' ORDER BY s COLLATE "C") INTO segs
    FROM unnest(string_to_array(coalesce(m[4], ''), '&')) AS s
    WHERE s <> '' AND NOT pg_temp.is_tracking(s);

    RETURN host || rtrim(coalesce(m[3], ''), '/') || coalesce('?' || segs, '');
END
$$;

CREATE FUNCTION pg_temp.clean_website_url(url TEXT) RETURNS TEXT
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    m    TEXT[] := pg_temp.split_url(url);
    segs TEXT;
BEGIN
    SELECT string_agg(s, '&' ORDER BY ord) INTO segs
    FROM unnest(string_to_array(coalesce(m[4], ''), '&')) WITH ORDINALITY AS t(s, ord)
    WHERE s <> '' AND NOT pg_temp.is_tracking(s);

    RETURN coalesce(lower(m[1]) || ':', '')
        || coalesce('//' || m[2], '')
        || coalesce(m[3], '')
        || coalesce('?' || segs, '');
END
$$;

ALTER TABLE websites ADD COLUMN url_key TEXT;
UPDATE websites SET url_key = pg_temp.website_key(url);

-- Keep one row per key: the most recently audited row, else the oldest row.
CREATE TEMPORARY TABLE website_merge ON COMMIT DROP AS
SELECT id, keep_id
FROM (
    SELECT id,
           first_value(id) OVER (
               PARTITION BY url_key
               ORDER BY (audit_status = 'audited') DESC, last_audited_at DESC NULLS LAST, id
           ) AS keep_id
    FROM websites
) ranked
WHERE id <> keep_id;

INSERT INTO website_cities (website_id, city_id, discovered_via)
SELECT m.keep_id, wc.city_id, wc.discovered_via
FROM website_cities wc
JOIN website_merge m ON m.id = wc.website_id
ON CONFLICT DO NOTHING;

UPDATE discovery_log d
SET website_id = m.keep_id
FROM website_merge m
WHERE d.website_id = m.id;

DELETE FROM websites w USING website_merge m WHERE w.id = m.id;

UPDATE websites
SET url = pg_temp.clean_website_url(url)
WHERE url IS DISTINCT FROM pg_temp.clean_website_url(url);

ALTER TABLE websites ALTER COLUMN url_key SET NOT NULL;
ALTER TABLE websites ADD CONSTRAINT websites_url_key_key UNIQUE (url_key);
ALTER TABLE websites DROP CONSTRAINT websites_url_key;
