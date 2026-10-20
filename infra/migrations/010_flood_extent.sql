-- What the API and the chat read for a period: each cell's largest flooded area in that layer or a shorter one.
-- GISTDA's figure for the same cell differs between its layers: on 2026-10-20 the 7-day layer's cells held 4,720 km²
-- in that layer but 3,676 km² in the 30-day one (ต.บางบาล: 3,125 rai in 7 days, 1,238 in 30), so the 30-day number
-- read as less water than the week's. Here a period's figure is the most water seen in each cell within it, so a
-- longer period never shows less than a shorter one. A period is filled only once its own layer has been fetched.
-- Rebuilt by ingest/gistda_flood.py (EXTENT_SQL) with every stored layer; flood_cells keeps each layer as sent.
CREATE TABLE flood_extent (LIKE flood_cells INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES);
ALTER TABLE flood_extent DROP COLUMN props;

INSERT INTO flood_extent (period, h3, h3_r5, h3_r6, h3_r7, h3_r8, lat, lon, area_km2, pv_code, ap_code, tb_code,
                          province, amphoe, tambon, observed_at, buildings, population, schools, hospitals)
SELECT DISTINCT ON (w.period, c.h3) w.period, c.h3, c.h3_r5, c.h3_r6, c.h3_r7, c.h3_r8, c.lat, c.lon, c.area_km2,
       c.pv_code, c.ap_code, c.tb_code, c.province, c.amphoe, c.tambon, c.observed_at, c.buildings, c.population,
       c.schools, c.hospitals
FROM (VALUES ('3days', 1), ('7days', 2), ('30days', 3)) AS w (period, len)
JOIN (VALUES ('3days', 1), ('7days', 2), ('30days', 3)) AS l (period, len) ON l.len <= w.len
JOIN flood_cells c ON c.period = l.period
WHERE w.period IN (SELECT period FROM flood_fetches)
ORDER BY w.period, c.h3, c.area_km2 DESC, l.len;
