-- 012_reset_city_viewport.sql — Clear viewports geocoded without a country filter
--
-- Viewports were geocoded from "{label}, {country}", which resolves an ISO
-- code that is also a regional abbreviation to that region: "Barcelona, ES"
-- gave Barcelona in Espírito Santo, Brazil.  Geocoding now restricts the
-- results with the country component filter; clearing the cache makes the
-- next discovery of each city geocode it again.

UPDATE cities
   SET viewport_low_lat  = NULL,
       viewport_low_lng  = NULL,
       viewport_high_lat = NULL,
       viewport_high_lng = NULL
 WHERE viewport_low_lat IS NOT NULL;
