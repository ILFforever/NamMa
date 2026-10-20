-- A place saved from a station's page on the web ("แจ้งเตือนสถานี C.35 #2609" sent to the LINE OA): alerts follow
-- that station alone, not the riskiest station within NEAR_KM of the point (agent/alerts.py). NULL for every other
-- place (GPS shares, place names), which keep the nearest-station rule.
ALTER TABLE user_locations ADD COLUMN station_id INTEGER REFERENCES stations(id) ON DELETE SET NULL;
