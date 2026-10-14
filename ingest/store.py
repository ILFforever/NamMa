"""Upserts for ThaiWater data. Readings are idempotent on (station_id, ts)."""
import psycopg

STATION_COLS = ["id", "oldcode", "name_th", "name_en", "lat", "lon", "basin", "river", "province",
                "amphoe", "tambon", "left_bank", "right_bank", "min_bank", "ground_level",
                "critical_level_msl", "agency", "is_key_station"]
READING_COLS = ["station_id", "ts", "level_msl", "diff_wl_bank", "situation_level", "discharge"]
RAIN_STATION_COLS = ["id", "oldcode", "name_th", "lat", "lon", "province", "basin"]
RAIN_COLS = ["station_id", "ts", "rain_1h", "rain_24h"]


def _upsert(conn: psycopg.Connection, table: str, cols: list[str], key: list[str], rows: list[dict],
            update: bool = True) -> None:
    if not rows:
        return
    placeholders = ", ".join(f"%({c})s" for c in cols)
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in key)
    action = f"DO UPDATE SET {sets}" if update and sets else "DO NOTHING"
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) ON CONFLICT ({', '.join(key)}) {action}"
    with conn.cursor() as cur:
        cur.executemany(sql, rows)


def upsert_stations(conn, rows):
    _upsert(conn, "stations", STATION_COLS, ["id"], rows)


def upsert_readings(conn, rows):
    # Live readings carry situation_level that history lacks, so live wins on conflict.
    _upsert(conn, "waterlevel_readings", READING_COLS, ["station_id", "ts"], rows)


def insert_history(conn, rows):
    # Never overwrite a live reading with a history row.
    _upsert(conn, "waterlevel_readings", READING_COLS, ["station_id", "ts"], rows, update=False)


def upsert_rain_stations(conn, rows):
    _upsert(conn, "rain_stations", RAIN_STATION_COLS, ["id"], rows)


def upsert_rain(conn, rows):
    _upsert(conn, "rain_readings", RAIN_COLS, ["station_id", "ts"], rows)
