import psycopg

from common.config import DATABASE_URL


def connect() -> psycopg.Connection:
    # Bangkok time on every session, whatever the server default is (the VM's Postgres was set up as
    # Asia/Shanghai, +08), so timestamps read back and date math in SQL are in Thai time.
    return psycopg.connect(DATABASE_URL, options="-c TimeZone=Asia/Bangkok")
