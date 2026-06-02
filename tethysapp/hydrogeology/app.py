import asyncio
import re
import math
from tethys_sdk.components import ComponentBase
from tethys_sdk.components.utils import event, component
from urllib.request import urlopen, Request
from tethys_sdk.app_settings import SecretCustomSetting
import pandas as pd
import sqlite3
from uuid import uuid4
import json
import base64
from datetime import datetime


class App(ComponentBase):
    name = "Hydro sync"
    description = "Field Assistant"
    package = "hydrogeology"
    index = "home"
    icon = f"{package}/images/icon.png"
    root_url = "hydrogeology"
    color = "#109cf9"
    tags = "GIS", "hydrogeology"
    enable_feedback = False
    feedback_emails = []
    exit_url = "/apps/"
    default_layout = "NavHeader"
    nav_links = "auto"

    def custom_settings(self):
        return (
            SecretCustomSetting(
                name="GEMINI_API_KEY",
                description="API key for Google Gemini API",
                required=True,
            ),
            SecretCustomSetting(
                name="SYNC_DB_URL",
                description=(
                    "PostgreSQL connection string for cloud sync. "
                    "Format: postgresql://user:password@host:5432/dbname  "
                    "(Supabase or Railway). Leave blank to disable sync."
                ),
                required=False,
            ),
        )


# ---------------------------------------------------------------------------
# Coordinate helper  (Web Mercator → WGS84, no external lib needed)
# ---------------------------------------------------------------------------

def _merc_to_wgs84(x, y):
    lon = x / 20037508.342789244 * 180.0
    lat = math.degrees(
        2.0 * math.atan(math.exp(y / 20037508.342789244 * math.pi)) - math.pi / 2.0
    )
    return lon, lat


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

def _safe_identifier(name):
    sanitized = re.sub(r'[^a-zA-Z0-9_]', '_', str(name))
    if not re.match(r'^[a-zA-Z_]', sanitized):
        sanitized = '_' + sanitized
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", sanitized):
        raise ValueError(f"Invalid SQL identifier: {sanitized}")
    return sanitized


def delete_record_from_sqlite(db_fpath, table_name, record_id, id_col="created_at"):
    table_name = _safe_identifier(table_name)
    id_col = _safe_identifier(id_col)
    conn = sqlite3.connect(str(db_fpath))
    cursor = conn.cursor()
    try:
        cursor.execute(f'DELETE FROM "{table_name}" WHERE "{id_col}" = ?', (record_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_data_in_sqlite(db_fpath, table_name, data, id_col="created_at"):
    table_name = _safe_identifier(table_name)
    id_col = _safe_identifier(id_col)
    if not data:
        return
    conn = sqlite3.connect(str(db_fpath))
    cursor = conn.cursor()
    try:
        for row in data:
            record_id = row.get(id_col)
            if record_id is None:
                continue
            columns = [col for col in row.keys() if col != id_col]
            sanitized_columns = [_safe_identifier(col) for col in columns]
            set_clause = ", ".join([f'"{col}" = ?' for col in sanitized_columns])
            values = [str(row.get(col, "")) if row.get(col) is not None else "" for col in columns]
            values.append(record_id)
            cursor.execute(f'UPDATE "{table_name}" SET {set_clause} WHERE "{id_col}" = ?', values)
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()


def data_to_sqlite(db_fpath, table_name, data):
    table_name = _safe_identifier(table_name)
    if not data:
        return
    conn = sqlite3.connect(str(db_fpath))
    cursor = conn.cursor()
    try:
        first_row = data[0]
        columns_orig = list(first_row.keys())
        columns_safe = [_safe_identifier(k) for k in columns_orig] + ["created_at"]

        cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
        if not cursor.fetchone():
            col_defs = ", ".join([f'"{c}" TEXT' for c in columns_safe])
            cursor.execute(
                f'CREATE TABLE "{table_name}" ({col_defs}, id INTEGER PRIMARY KEY AUTOINCREMENT)'
            )
        else:
            cursor.execute(f'PRAGMA table_info("{table_name}")')
            existing = {r[1] for r in cursor.fetchall()}
            for col in columns_safe:
                if col not in existing:
                    try:
                        cursor.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{col}" TEXT')
                    except sqlite3.OperationalError:
                        pass

        for row in data:
            values = [
                str(row.get(c, "")) if row.get(c) is not None else ""
                for c in columns_orig
            ]
            values.append(pd.Timestamp.now().isoformat())
            placeholders = ", ".join(["?" for _ in values])
            col_names = ", ".join([f'"{c}"' for c in columns_safe])
            cursor.execute(
                f'INSERT INTO "{table_name}" ({col_names}) VALUES ({placeholders})', values
            )

        conn.commit()
    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()


def data_from_sqlite(db_fpath, table_name):
    table_name = _safe_identifier(table_name)
    if not db_fpath.exists():
        return []
    conn = sqlite3.connect(str(db_fpath))
    try:
        df = pd.read_sql_query(
            f'SELECT * FROM "{table_name}" ORDER BY created_at DESC', conn
        )
        return df.to_dict(orient="records")
    except Exception as e:
        print(f"Error reading {table_name}: {e}")
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Chat helpers  — lightweight SQLite-backed chatroom
# ---------------------------------------------------------------------------

def chat_messages_from_sqlite(db_fpath):
    """Load the last 100 chat messages, oldest first for display."""
    if not db_fpath.exists():
        return []
    conn = sqlite3.connect(str(db_fpath))
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_messages'"
        )
        if not cursor.fetchone():
            return []
        df = pd.read_sql_query(
            "SELECT * FROM chat_messages ORDER BY ts DESC LIMIT 100", conn
        )
        # Reverse so oldest is at top, newest at bottom
        return df.iloc[::-1].to_dict(orient="records")
    except Exception:
        return []
    finally:
        conn.close()


def chat_message_to_sqlite(db_fpath, sender, text, ts=None):
    """Append a single chat message."""
    conn = sqlite3.connect(str(db_fpath))
    cursor = conn.cursor()
    try:
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS chat_messages "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, sender TEXT, text TEXT)"
        )
        cursor.execute(
            "INSERT INTO chat_messages (ts, sender, text) VALUES (?, ?, ?)",
            (ts or datetime.now().isoformat(), sender, text),
        )
        conn.commit()
    finally:
        conn.close()


def chat_clear_sqlite(db_fpath):
    """Delete all messages."""
    if not db_fpath.exists():
        return
    conn = sqlite3.connect(str(db_fpath))
    try:
        conn.execute("DELETE FROM chat_messages")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sync helpers  (SQLite  <-->  shared PostgreSQL via psycopg2)
# ---------------------------------------------------------------------------

SYNC_TABLES = {
    "Map_Location":        "map_location.sqlite",
    "VES_FORM":            "ves_survey_data.sqlite",
    "resistivity_survey":  "resistivity_survey.sqlite",
    "Image_Analysis":      "image_analysis.sqlite",
}


def _pg_connect(dsn):
    try:
        import psycopg2
        return psycopg2.connect(dsn)
    except ImportError:
        raise RuntimeError("psycopg2 not installed. Run:  pip install psycopg2-binary")


def _push_table(db_fpath, table_name, dsn):
    rows = data_from_sqlite(db_fpath, table_name)
    if not rows:
        return 0
    pg  = _pg_connect(dsn)
    cur = pg.cursor()
    tbl = _safe_identifier(table_name)
    try:
        cols      = list(rows[0].keys())
        safe_cols = [_safe_identifier(c) for c in cols]
        other_cols = [c for c in safe_cols if c != "created_at"]
        col_defs   = ", ".join([f'"{c}" TEXT' for c in other_cols])
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS "{tbl}" (
                "created_at" TEXT PRIMARY KEY,
                {col_defs}
            )
        """)
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (tbl,)
        )
        existing_pg = {r[0] for r in cur.fetchall()}
        for c in safe_cols:
            if c not in existing_pg:
                cur.execute(f'ALTER TABLE "{tbl}" ADD COLUMN "{c}" TEXT')
        n = 0
        for row in rows:
            rs  = {_safe_identifier(k): (str(v) if v is not None else "") for k, v in row.items()}
            cl  = list(rs.keys())
            ph  = ", ".join(["%s"] * len(cl))
            cs  = ", ".join([f'"{c}"' for c in cl])
            upd = ", ".join([f'"{c}" = EXCLUDED."{c}"' for c in cl if c != "created_at"])
            cur.execute(
                f'INSERT INTO "{tbl}" ({cs}) VALUES ({ph}) '
                f'ON CONFLICT ("created_at") DO UPDATE SET {upd}',
                list(rs.values())
            )
            n += 1
        pg.commit()
        return n
    except Exception:
        pg.rollback()
        raise
    finally:
        cur.close()
        pg.close()


def _pull_table(db_fpath, table_name, dsn):
    pg  = _pg_connect(dsn)
    cur = pg.cursor()
    tbl = _safe_identifier(table_name)
    try:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name=%s", (tbl,)
        )
        if not cur.fetchone():
            return 0
        cur.execute(f'SELECT * FROM "{tbl}"')
        colnames = [d[0] for d in cur.description]
        rows     = [dict(zip(colnames, r)) for r in cur.fetchall()]
        if not rows:
            return 0
        conn = sqlite3.connect(str(db_fpath))
        lc   = conn.cursor()
        try:
            safe_cols = [_safe_identifier(c) for c in colnames]
            lc.execute(
                f'CREATE TABLE IF NOT EXISTS "{tbl}" '
                f'({", ".join([chr(34)+c+chr(34)+" TEXT" for c in safe_cols])}, '
                f'id INTEGER PRIMARY KEY AUTOINCREMENT)'
            )
            ex = {r[1] for r in lc.execute(f'PRAGMA table_info("{tbl}")').fetchall()}
            for c in safe_cols:
                if c not in ex:
                    lc.execute(f'ALTER TABLE "{tbl}" ADD COLUMN "{c}" TEXT')
            inserted = 0
            for row in rows:
                rs = {_safe_identifier(k): (str(v) if v is not None else "")
                      for k, v in row.items() if k != "id"}
                cs = ", ".join([f'"{c}"' for c in rs])
                ph = ", ".join(["?" for _ in rs])
                lc.execute(
                    f'INSERT OR IGNORE INTO "{tbl}" ({cs}) VALUES ({ph})',
                    list(rs.values())
                )
                inserted += lc.rowcount
            conn.commit()
            return inserted
        finally:
            lc.close()
            conn.close()
    except Exception:
        raise
    finally:
        cur.close()
        pg.close()


def sync_all_push(resources_path, dsn):
    results = {}
    for tbl, fname in SYNC_TABLES.items():
        try:
            n = _push_table(resources_path / fname, tbl, dsn)
            results[tbl] = {"status": "ok", "rows": n}
        except Exception as e:
            results[tbl] = {"status": "error", "message": str(e)}
    return results


def sync_all_pull(resources_path, dsn):
    results = {}
    for tbl, fname in SYNC_TABLES.items():
        try:
            n = _pull_table(resources_path / fname, tbl, dsn)
            results[tbl] = {"status": "ok", "rows": n}
        except Exception as e:
            results[tbl] = {"status": "error", "message": str(e)}
    return results


# ---------------------------------------------------------------------------
# Generalised database hook
# ---------------------------------------------------------------------------

def use_db_state(lib, db_fpath, table_name, id_col="created_at"):
    displayed_data,  set_displayed_data  = lib.hooks.use_state([])
    submit_success,  set_submit_success  = lib.hooks.use_state(None)
    success_message, set_success_message = lib.hooks.use_state("")
    error_message,   set_error_message   = lib.hooks.use_state(None)
    is_loading,      set_is_loading      = lib.hooks.use_state(False)
    form_key,        set_form_key        = lib.hooks.use_state(str(uuid4()))
    data_loaded,     set_data_loaded     = lib.hooks.use_state(False)

    def _reload():
        data = data_from_sqlite(db_fpath, table_name)
        set_displayed_data(data)
        return data

    def _auto_load():
        if not data_loaded:
            try:
                _reload()
            except Exception as err:
                print(f"Auto-load error ({table_name}): {err}")
            set_data_loaded(True)

    lib.hooks.use_effect(_auto_load, [])

    def _clear_status():
        set_submit_success(None)
        set_error_message(None)

    def _show_success(msg):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        set_submit_success(True)
        set_success_message(f"{msg} at {timestamp}")
        lib.utils.background_execute(_clear_status, delay_seconds=4)

    def _show_error(err):
        set_submit_success(False)
        set_error_message(f"❌ Error: {str(err)[:120]}")

    def save(rows):
        set_is_loading(True)
        set_error_message(None)
        try:
            data_to_sqlite(db_fpath, table_name, rows)
            _reload()
            set_form_key(str(uuid4()))
            _show_success("✓ Saved successfully")
        except Exception as err:
            _show_error(err)
        finally:
            set_is_loading(False)

    def update(rows):
        set_is_loading(True)
        set_error_message(None)
        try:
            update_data_in_sqlite(db_fpath, table_name, rows, id_col=id_col)
            _reload()
            _show_success("✓ Changes saved successfully")
        except Exception as err:
            _show_error(err)
        finally:
            set_is_loading(False)

    def delete(record_id):
        set_is_loading(True)
        set_error_message(None)
        try:
            delete_record_from_sqlite(db_fpath, table_name, record_id, id_col=id_col)
            _reload()
            _show_success("✓ Record deleted successfully")
        except Exception as err:
            _show_error(err)
        finally:
            set_is_loading(False)

    return {
        "displayed_data":     displayed_data,
        "set_displayed_data": set_displayed_data,
        "submit_success":     submit_success,
        "success_message":    success_message,
        "error_message":      error_message,
        "is_loading":         is_loading,
        "form_key":           form_key,
        "reload":             _reload,
        "save":               save,
        "update":             update,
        "delete":             delete,
        "clear_status":       _clear_status,
    }


# ---------------------------------------------------------------------------
# Shared CSS
# ---------------------------------------------------------------------------

SHARED_CSS = """
    @keyframes spin {
        from { transform: rotate(0deg); }
        to   { transform: rotate(360deg); }
    }
    .spinner {
        display: inline-block;
        animation: spin 1s linear infinite;
        margin-right: 8px;
    }
    @keyframes slideDown {
        from { opacity: 0; transform: translateY(-20px); }
        to   { opacity: 1; transform: translateY(0); }
    }
    .success-alert { animation: slideDown 0.5s ease-out; }
"""

# CSS for the Google-Maps-style home page
HOME_CSS = """
    .gm-shell {
        display: flex;
        height: calc(100vh - 56px);
        overflow: hidden;
        font-family: 'Segoe UI', Arial, sans-serif;
    }
    .gm-sidebar {
        width: 340px;
        flex-shrink: 0;
        background: #fff;
        box-shadow: 2px 0 8px rgba(0,0,0,0.12);
        display: flex;
        flex-direction: column;
        overflow: hidden;
        z-index: 10;
    }
    .gm-sidebar-header {
        background: linear-gradient(135deg, #1a73e8, #0d47a1);
        color: #fff;
        padding: 16px 18px 14px;
        flex-shrink: 0;
    }
    .gm-sidebar-header h2 { margin: 0; font-size: 20px; font-weight: 700; letter-spacing: 0.3px; }
    .gm-sidebar-header p  { margin: 2px 0 0; font-size: 12px; opacity: 0.82; }
    .gm-sidebar-body { flex: 1; overflow-y: auto; padding: 14px 16px; }
    .coord-card {
        background: linear-gradient(135deg, #1a73e8, #0d47a1);
        color: #fff;
        border-radius: 12px;
        padding: 16px 18px;
        margin-bottom: 14px;
        box-shadow: 0 3px 12px rgba(26,115,232,0.35);
    }
    .coord-row { display: flex; align-items: baseline; gap: 8px; margin-bottom: 4px; }
    .coord-axis {
        font-size: 11px; font-weight: 700; opacity: 0.7;
        text-transform: uppercase; letter-spacing: 0.8px; min-width: 28px;
    }
    .coord-val {
        font-family: 'Courier New', monospace;
        font-size: 18px; font-weight: 700; letter-spacing: 0.5px;
    }
    .coord-divider { border: none; border-top: 1px solid rgba(255,255,255,0.2); margin: 10px 0 8px; }
    .coord-merc { font-family: 'Courier New', monospace; font-size: 11px; opacity: 0.82; line-height: 1.7; }
    .coord-place {
        font-size: 11px; opacity: 0.88; margin-top: 8px; line-height: 1.5;
        border-top: 1px solid rgba(255,255,255,0.15); padding-top: 8px;
    }
    .gps-waiting {
        background: #e8f0fe; border: 2px dashed #90b4fb; border-radius: 12px;
        padding: 24px 16px; text-align: center; color: #1a56c4; margin-bottom: 14px; font-size: 13px;
    }
    .hover-pill {
        background: rgba(10,10,20,0.88); color: #dce9ff; border-radius: 8px;
        padding: 9px 14px; font-family: 'Courier New', monospace; font-size: 12px;
        line-height: 1.6; margin-bottom: 12px; border: 1px solid rgba(120,180,255,0.25);
    }
    .hover-pill b { color: #7ec8ff; }
    .sidebar-section-title {
        font-size: 10px; font-weight: 700; text-transform: uppercase;
        letter-spacing: 1px; color: #888; margin: 14px 0 8px;
    }
    .sync-log {
        background: #0d1117; color: #58a6ff; font-family: 'Courier New', monospace;
        font-size: 12px; padding: 12px 14px; border-radius: 8px; max-height: 180px;
        overflow-y: auto; margin-top: 10px; line-height: 1.8; border: 1px solid #30363d;
    }
    .info-box {
        background: #f0f7ff; border-left: 4px solid #1a73e8; border-radius: 6px;
        padding: 10px 14px; font-size: 12px; color: #1a3a5c; line-height: 1.6; margin-bottom: 12px;
    }
    .setup-box {
        background: #fffbea; border-left: 4px solid #f5a623; border-radius: 6px;
        padding: 10px 14px; font-size: 12px; color: #5c3d00; line-height: 1.8; margin-bottom: 12px;
    }
    .setup-box code {
        background: rgba(0,0,0,0.07); border-radius: 3px;
        padding: 1px 5px; font-family: monospace; font-size: 11px;
    }
    .react-tabs__tab-list { border-bottom: 2px solid #e0e7f0 !important; margin: 0 0 12px !important; padding: 0 !important; }
    .react-tabs__tab { font-size: 12px !important; padding: 6px 12px !important; }
    .react-tabs__tab--selected { border-color: #1a73e8 !important; color: #1a73e8 !important; }
    .gm-map-panel { flex: 1; min-width: 0; position: relative; }
    .gm-map-panel > div, .gm-map-panel > div > div { height: 100% !important; }

    /* GPS save button pulse */
    @keyframes gps-pulse {
        0%   { box-shadow: 0 0 0 0 rgba(26,115,232,0.5); }
        70%  { box-shadow: 0 0 0 8px rgba(26,115,232,0); }
        100% { box-shadow: 0 0 0 0 rgba(26,115,232,0); }
    }
    .gps-save-btn {
        animation: gps-pulse 2s infinite;
        width: 100%;
        margin-top: 10px;
        font-weight: 600;
        font-size: 13px;
    }
    .gps-saved-badge {
        background: #d4edda; color: #155724; border: 1px solid #c3e6cb;
        border-radius: 6px; padding: 6px 10px; font-size: 12px;
        margin-top: 8px; text-align: center;
        animation: slideDown 0.4s ease-out;
    }
"""

# ---------------------------------------------------------------------------
# Chat page CSS
# ---------------------------------------------------------------------------

CHAT_CSS = """
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Sora:wght@400;600;700&display=swap');

    :root {
        --chat-bg: #0f1117;
        --chat-panel: #1a1d27;
        --chat-border: #2a2d3a;
        --chat-accent: #3b82f6;
        --chat-accent2: #10b981;
        --chat-text: #e2e8f0;
        --chat-muted: #64748b;
        --chat-bubble-me: linear-gradient(135deg, #1d4ed8, #3b82f6);
        --chat-bubble-other: #1e2130;
        --chat-input-bg: #1e2130;
        --chat-header: linear-gradient(135deg, #1a1d27, #0f1117);
    }

    .chat-root {
        display: flex;
        flex-direction: column;
        height: calc(100vh - 56px);
        background: var(--chat-bg);
        font-family: 'Sora', sans-serif;
        color: var(--chat-text);
        overflow: hidden;
    }

    /* ── Header ── */
    .chat-header {
        background: var(--chat-header);
        border-bottom: 1px solid var(--chat-border);
        padding: 14px 24px;
        display: flex;
        align-items: center;
        gap: 14px;
        flex-shrink: 0;
    }
    .chat-header-icon {
        width: 40px; height: 40px;
        background: linear-gradient(135deg, #3b82f6, #06b6d4);
        border-radius: 50%;
        display: flex; align-items: center; justify-content: center;
        font-size: 18px;
        box-shadow: 0 0 16px rgba(59,130,246,0.4);
    }
    .chat-header-title {
        font-family: 'Sora', sans-serif;
        font-size: 17px; font-weight: 700;
        background: linear-gradient(90deg, #60a5fa, #34d399);
        -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    }
    .chat-header-sub {
        font-size: 11px; color: var(--chat-muted); font-family: 'JetBrains Mono', monospace;
    }
    .chat-online-dot {
        width: 8px; height: 8px; border-radius: 50%;
        background: #10b981;
        box-shadow: 0 0 6px #10b981;
        margin-left: auto;
        flex-shrink: 0;
    }

    /* ── Name bar ── */
    .chat-name-bar {
        background: #131620;
        border-bottom: 1px solid var(--chat-border);
        padding: 10px 24px;
        display: flex;
        align-items: center;
        gap: 12px;
        flex-shrink: 0;
    }
    .chat-name-input {
        background: var(--chat-input-bg);
        border: 1px solid var(--chat-border);
        border-radius: 8px;
        color: var(--chat-text);
        padding: 7px 12px;
        font-size: 13px;
        font-family: 'Sora', sans-serif;
        width: 200px;
        outline: none;
        transition: border-color 0.2s;
    }
    .chat-name-input:focus { border-color: var(--chat-accent); }
    .chat-name-label {
        font-size: 12px; color: var(--chat-muted);
        font-family: 'JetBrains Mono', monospace;
    }

    /* ── Messages area ── */
    .chat-messages {
        flex: 1;
        overflow-y: auto;
        padding: 20px 24px;
        display: flex;
        flex-direction: column;
        gap: 12px;
        scroll-behavior: smooth;
        scroll-snap-type: y mandatory;
    }
    .chat-messages > :last-child { 
        scroll-snap-align: start;
        scroll-initial-target: nearest;
    }
    .chat-messages::-webkit-scrollbar { width: 4px; }
    .chat-messages::-webkit-scrollbar-track { background: transparent; }
    .chat-messages::-webkit-scrollbar-thumb { background: var(--chat-border); border-radius: 2px; }

    /* ── Message bubble ── */
    .chat-msg-row {
        display: flex;
        align-items: flex-end;
        gap: 8px;
    }
    .chat-msg-row.me { flex-direction: row-reverse; }

    .chat-avatar {
        width: 30px; height: 30px; border-radius: 50%;
        background: linear-gradient(135deg, #475569, #334155);
        display: flex; align-items: center; justify-content: center;
        font-size: 13px; font-weight: 600; flex-shrink: 0;
        color: #94a3b8;
        font-family: 'JetBrains Mono', monospace;
    }
    .chat-msg-row.me .chat-avatar {
        background: linear-gradient(135deg, #1d4ed8, #3b82f6);
        color: #fff;
    }

    .chat-bubble-wrap { display: flex; flex-direction: column; max-width: 68%; }
    .chat-msg-row.me .chat-bubble-wrap { align-items: flex-end; }

    .chat-sender {
        font-size: 10px; font-family: 'JetBrains Mono', monospace;
        color: var(--chat-muted); margin-bottom: 3px; padding: 0 4px;
    }
    .chat-msg-row.me .chat-sender { color: #60a5fa; }

    .chat-bubble {
        background: var(--chat-bubble-other);
        border: 1px solid var(--chat-border);
        border-radius: 16px 16px 16px 4px;
        padding: 10px 14px;
        font-size: 14px;
        line-height: 1.5;
        color: var(--chat-text);
        word-break: break-word;
        box-shadow: 0 2px 8px rgba(0,0,0,0.3);
    }
    .chat-msg-row.me .chat-bubble {
        background: var(--chat-bubble-me);
        border-color: transparent;
        border-radius: 16px 16px 4px 16px;
        color: #fff;
        box-shadow: 0 2px 12px rgba(59,130,246,0.3);
    }

    .chat-ts {
        font-size: 10px; font-family: 'JetBrains Mono', monospace;
        color: var(--chat-muted); margin-top: 4px; padding: 0 4px;
    }

    /* System / GPS coordinate messages */
    .chat-system-msg {
        text-align: center;
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: var(--chat-muted);
        background: rgba(59,130,246,0.05);
        border: 1px solid rgba(59,130,246,0.12);
        border-radius: 8px;
        padding: 6px 12px;
        margin: 4px auto;
        max-width: 90%;
    }

    /* ── Input area ── */
    .chat-input-bar {
        background: var(--chat-panel);
        border-top: 1px solid var(--chat-border);
        padding: 14px 20px;
        display: flex;
        align-items: center;
        gap: 10px;
        flex-shrink: 0;
    }
    .chat-text-input {
        flex: 1;
        background: var(--chat-input-bg);
        border: 1px solid var(--chat-border);
        border-radius: 24px;
        color: var(--chat-text);
        padding: 10px 18px;
        font-size: 14px;
        font-family: 'Sora', sans-serif;
        outline: none;
        transition: border-color 0.2s, box-shadow 0.2s;
        resize: none;
        min-height: 42px;
        max-height: 120px;
    }
    .chat-text-input:focus {
        border-color: var(--chat-accent);
        box-shadow: 0 0 0 3px rgba(59,130,246,0.15);
    }
    .chat-text-input::placeholder { color: var(--chat-muted); }

    .chat-send-btn {
        width: 42px; height: 42px;
        background: linear-gradient(135deg, #1d4ed8, #3b82f6);
        border: none; border-radius: 50%;
        color: white; font-size: 18px;
        display: flex; align-items: center; justify-content: center;
        cursor: pointer;
        box-shadow: 0 2px 8px rgba(59,130,246,0.4);
        transition: transform 0.15s, box-shadow 0.15s;
        flex-shrink: 0;
    }
    .chat-send-btn:hover {
        transform: scale(1.08);
        box-shadow: 0 4px 16px rgba(59,130,246,0.6);
    }
    .chat-send-btn:active { transform: scale(0.96); }
    .chat-send-btn:disabled {
        background: #2a2d3a; color: #475569; cursor: not-allowed;
        box-shadow: none; transform: none;
    }

    /* GPS coords share button inside chat */
    .chat-gps-btn {
        width: 42px; height: 42px;
        background: linear-gradient(135deg, #065f46, #10b981);
        border: none; border-radius: 50%;
        color: white; font-size: 16px;
        display: flex; align-items: center; justify-content: center;
        cursor: pointer;
        box-shadow: 0 2px 8px rgba(16,185,129,0.3);
        transition: transform 0.15s;
        flex-shrink: 0;
    }
    .chat-gps-btn:hover { transform: scale(1.08); }
    .chat-gps-btn:disabled { background: #2a2d3a; color: #475569; cursor: not-allowed; transform: none; }

    /* Clear button */
    .chat-clear-btn {
        background: none; border: 1px solid var(--chat-border);
        border-radius: 8px; color: var(--chat-muted);
        font-size: 11px; padding: 4px 10px; cursor: pointer;
        font-family: 'JetBrains Mono', monospace;
        transition: color 0.2s, border-color 0.2s;
    }
    .chat-clear-btn:hover { color: #ef4444; border-color: #ef4444; }

    /* Empty state */
    .chat-empty {
        display: flex; flex-direction: column;
        align-items: center; justify-content: center;
        flex: 1; color: var(--chat-muted); text-align: center;
        gap: 10px;
    }
    .chat-empty-icon { font-size: 48px; opacity: 0.3; }
    .chat-empty-text { font-size: 14px; font-family: 'JetBrains Mono', monospace; }

    @keyframes msgPop {
        from { opacity: 0; transform: scale(0.9) translateY(8px); }
        to   { opacity: 1; transform: scale(1) translateY(0); }
    }
    .chat-msg-row { animation: msgPop 0.25s ease-out; }
"""


# ---------------------------------------------------------------------------
# Shared UI components
# ---------------------------------------------------------------------------

@component
def status_alerts(lib, submit_success, success_message, error_message, extra_detail=None):
    return lib.html.div()(
        lib.bs.Alert(
            variant="success",
            className="success-alert",
            style=lib.Style(
                marginBottom="20px",
                borderLeft="4px solid #28a745",
                boxShadow="0 2px 4px rgba(40,167,69,0.2)"
            )
        )(
            lib.html.div(style=lib.Style(display="flex", alignItems="center", gap="10px"))(
                lib.html.span(style=lib.Style(fontSize="20px"))("✓"),
                lib.html.div()(
                    lib.html.strong(success_message),
                    lib.html.br(),
                    lib.html.small(style=lib.Style(color="#666"))(extra_detail) if extra_detail else None,
                )
            )
        ) if submit_success else None,
        lib.bs.Alert(variant="danger")(error_message) if error_message else None,
    )


# ---------------------------------------------------------------------------
# Generalised SummaryTable + FormView
# ---------------------------------------------------------------------------

def make_record_manager(
    lib, db, form_fields, summary_cols,
    page_title="Survey Form", extra_form_content=None, id_col="created_at",
):
    view_mode,           set_view_mode           = lib.hooks.use_state("list")
    selected_record_id,  set_selected_record_id  = lib.hooks.use_state(None)
    selected_rows,       set_selected_rows       = lib.hooks.use_state(set())
    edit_mode,           set_edit_mode           = lib.hooks.use_state(False)
    delete_confirm_open, set_delete_confirm_open = lib.hooks.use_state(False)

    def SummaryTable():
        displayed_data = db["displayed_data"]
        if not displayed_data:
            return lib.html.div(
                style=lib.Style(padding="20px", textAlign="center", color="#999", fontSize="16px")
            )("📭 No data submitted yet. Submit a form to see data here.")

        def toggle(record_id):
            new_sel = set(selected_rows)
            if record_id in new_sel:
                new_sel.discard(record_id)
            else:
                new_sel.add(record_id)
            set_selected_rows(new_sel)

        def handle_view():
            if len(selected_rows) == 1:
                set_selected_record_id(list(selected_rows)[0])
                set_edit_mode(False)
                set_view_mode("detail")

        def confirm_delete(e):
            for rid in list(selected_rows):
                db["delete"](rid)
            set_selected_rows(set())
            set_delete_confirm_open(False)

        table_rows = [
            lib.html.tr(
                style=lib.Style(
                    borderBottom="1px solid #ddd",
                    backgroundColor="#e8f4f8" if record.get(id_col) in selected_rows else "#fff"
                )
            )(
                lib.html.td(
                    style=lib.Style(padding="12px", borderRight="1px solid #eee", textAlign="center")
                )(
                    lib.html.input(
                        type="checkbox",
                        checked=record.get(id_col) in selected_rows,
                        onChange=lambda e, rid=record.get(id_col): toggle(rid),
                        style=lib.Style(cursor="pointer", width="18px", height="18px")
                    )
                ),
                *[
                    lib.html.td(style=lib.Style(padding="12px", borderRight="1px solid #eee"))(
                        str(record.get(field, "—"))
                    )
                    for field, _ in summary_cols
                ],
                lib.html.td(style=lib.Style(padding="12px"))(str(record.get(id_col, "—"))),
            )
            for record in displayed_data
        ]

        return lib.html.div(
            style=lib.Style(border="1px solid #ddd", borderRadius="4px",
                            overflow="hidden", backgroundColor="white")
        )(
            lib.html.p(
                style=lib.Style(fontSize="12px", color="#666", marginBottom="15px", padding="15px")
            )(f"💡 Total Records: {len(displayed_data)} | Selected: {len(selected_rows)}"),
            lib.html.div(
                style=lib.Style(padding="15px", borderBottom="1px solid #ddd",
                                display="flex", gap="10px")
            )(
                lib.bs.Button(variant="info", size="sm", onClick=lambda e: handle_view(),
                              disabled=len(selected_rows) != 1)("👁️ View"),
                lib.bs.Button(variant="danger", size="sm",
                              onClick=lambda e: set_delete_confirm_open(True),
                              disabled=len(selected_rows) == 0)(
                    f"🗑️ Delete ({len(selected_rows)})"),
            ),
            lib.bs.Modal(show=delete_confirm_open,
                         onHide=lambda: set_delete_confirm_open(False))(
                lib.bs.ModalHeader()("Confirm Delete?"),
                lib.bs.ModalBody()(f"Delete {len(selected_rows)} record(s)? Cannot be undone."),
                lib.bs.ModalFooter()(
                    lib.bs.Button(variant="secondary",
                                  onClick=lambda e: set_delete_confirm_open(False))("Cancel"),
                    lib.bs.Button(variant="danger", onClick=confirm_delete,
                                  disabled=db["is_loading"])("Delete"),
                ),
            ),
            lib.html.table(style=lib.Style(width="100%", borderCollapse="collapse"))(
                lib.html.thead(
                    style=lib.Style(backgroundColor="#f5f5f5", borderBottom="2px solid #ddd")
                )(
                    lib.html.tr()(
                        lib.html.th(style=lib.Style(padding="12px", textAlign="center",
                                                    fontWeight="bold", borderRight="1px solid #ddd"))("☑️"),
                        *[lib.html.th(style=lib.Style(padding="12px", textAlign="left",
                                                      fontWeight="bold", borderRight="1px solid #ddd"))(h)
                          for _, h in summary_cols],
                        lib.html.th(style=lib.Style(padding="12px", textAlign="left",
                                                    fontWeight="bold"))("Date"),
                    )
                ),
                lib.html.tbody()(*table_rows),
            ),
        )

    def FormView(existing_id=None, form_edit_mode=True):
        selected_record_data = None
        if db["displayed_data"] and existing_id:
            selected_record_data = next(
                (r for r in db["displayed_data"] if str(r.get(id_col)) == str(existing_id)), None
            )
        is_readonly = existing_id is not None and not form_edit_mode

        form_rows = [
            lib.bs.Row()(
                *[
                    lib.bs.Col()(
                        lib.html.label(
                            style=lib.Style(display="block", fontWeight="bold",
                                            marginBottom="5px", fontSize="14px"),
                            for_=field_name,
                        )(f"{label_text}:"),
                        lib.html.input(
                            name=field_name, type="text", className="form-control",
                            defaultValue=(selected_record_data.get(field_name, "")
                                          if selected_record_data else ""),
                            style=lib.Style(width="100%", padding="8px", marginBottom="10px"),
                            disabled=is_readonly,
                        ),
                    )
                    for field_name, label_text in row
                ]
            )
            for row in form_fields
        ]

        def handle_submit(e):
            db["save"]([dict(e["formData"])])

        def handle_save_changes(e):
            form_data = dict(e["formData"])
            form_data[id_col] = existing_id
            db["update"]([form_data])
            set_edit_mode(False)

        return lib.bs.Container(
            lib.html.h2(page_title),
            lib.html.div(style=lib.Style(display="flex", gap="10px", marginBottom="15px"))(
                lib.bs.Button(variant="warning", onClick=lambda e: set_edit_mode(not edit_mode),
                              disabled=db["is_loading"])(
                    "✏️ Edit" if not edit_mode else "⏹️ Cancel Edit"),
                lib.bs.Button(variant="danger",
                              onClick=lambda e: set_delete_confirm_open(True))("🗑️ Delete"),
                lib.bs.Button(variant="secondary",
                              onClick=lambda e: set_view_mode("list"))("← Back to List"),
            ) if existing_id else lib.html.div(),
            status_alerts(lib, submit_success=db["submit_success"],
                          success_message=db["success_message"],
                          error_message=db["error_message"],
                          extra_detail="Record saved" if db["submit_success"] else None),
            lib.bs.Modal(show=delete_confirm_open and existing_id is not None,
                         onHide=lambda: set_delete_confirm_open(False))(
                lib.bs.ModalHeader()("Confirm Delete?"),
                lib.bs.ModalBody()("Delete this record? Cannot be undone."),
                lib.bs.ModalFooter()(
                    lib.bs.Button(variant="secondary",
                                  onClick=lambda e: set_delete_confirm_open(False))("Cancel"),
                    lib.bs.Button(variant="danger", disabled=db["is_loading"],
                                  onClick=lambda e: (
                                      db["delete"](existing_id),
                                      set_delete_confirm_open(False),
                                      set_view_mode("list"),
                                      set_selected_rows(set()),
                                  ))("Delete"),
                ),
            ) if existing_id else None,
            lib.bs.Form(
                key=f"{db['form_key']}-{existing_id}-{edit_mode}",
                onSubmit=handle_save_changes if existing_id else handle_submit,
            )(
                *form_rows,
                extra_form_content(lib, existing_id, form_edit_mode) if extra_form_content else None,
                lib.bs.Button(
                    type="submit", variant="primary", size="lg",
                    disabled=db["is_loading"] or is_readonly,
                    style=lib.Style(
                        opacity="0.7" if db["is_loading"] else "1",
                        cursor="not-allowed" if db["is_loading"] or is_readonly else "pointer",
                        width="220px", padding="12px 24px", fontSize="16px", fontWeight="600",
                    ),
                )(
                    lib.html.span(className="spinner")("⟳ ") if db["is_loading"] else (
                        "💾 " if existing_id else "📤 "),
                    "Saving..." if db["is_loading"] and existing_id else
                    "Submitting..." if db["is_loading"] else
                    "Save Changes" if existing_id else "Submit Form",
                ) if not is_readonly else None,
            ),
        )

    def TabView():
        return lib.html.div()(
            lib.html.style()(SHARED_CSS),
            lib.tabs.Tabs(
                lib.tabs.TabList(lib.tabs.Tab("Add Data"), lib.tabs.Tab("View Data")),
                lib.tabs.TabPanel(FormView()),
                lib.tabs.TabPanel(
                    lib.html.div(style=lib.Style(padding="20px"))(
                        lib.html.h2(f"📊 {page_title} — All Submissions"),
                        status_alerts(lib, submit_success=db["submit_success"],
                                      success_message=db["success_message"],
                                      error_message=db["error_message"],
                                      extra_detail=(f"Total records: {len(db['displayed_data'])}"
                                                    if db["submit_success"] else None)),
                        SummaryTable(),
                    ) if view_mode != "detail" else FormView(selected_record_id, form_edit_mode=edit_mode)
                ),
            ),
        )

    return SummaryTable, FormView, TabView


# ===========================================================================
#  HOME PAGE  —  Google Maps–style layout
# ===========================================================================

@App.page
def home(lib):
    lib.register(
        "geolocation.js", "geo",
        host="/static/hydrogeology/js",
        default_export="Geolocation",
    )
    lib.register(
        "react-tabs", "tabs",
        styles=["https://esm.sh/react-tabs@6.1.0/style/react-tabs.css"],
    )
    lib.bs.Toast()
    lib.bs.ToastHeader()
    lib.bs.ToastBody()

    resources = lib.hooks.use_resources()

    try:
        sync_db_url = lib.hooks.use_setting("SYNC_DB_URL")
    except Exception:
        sync_db_url = None

    # ── State ──────────────────────────────────────────────────────────────
    location,      set_location      = lib.hooks.use_state(None)
    error,         set_error         = lib.hooks.use_state(None)
    hover_props,   set_hover_props   = lib.hooks.use_state({})
    place_name,    set_place_name    = lib.hooks.use_state(None)
    place_loaded,  set_place_loaded  = lib.hooks.use_state(False)
    sync_log,      set_sync_log      = lib.hooks.use_state([])
    sync_running,  set_sync_running  = lib.hooks.use_state(False)
    # GPS save state
    gps_saving,    set_gps_saving    = lib.hooks.use_state(False)
    gps_saved_msg, set_gps_saved_msg = lib.hooks.use_state(None)

    # ── Reverse geocode ────────────────────────────────────────────────────
    def _reverse_geocode(x, y):
        try:
            lon, lat = _merc_to_wgs84(x, y)
            req = Request(
                f"https://nominatim.openstreetmap.org/reverse"
                f"?lat={lat:.7f}&lon={lon:.7f}&format=json",
                headers={"User-Agent": "HydroSync/1.0"},
            )
            with urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode())
            set_place_name(data.get("display_name", "Unknown location"))
        except Exception:
            set_place_name(None)
        set_place_loaded(True)

    def on_geo_change(e):
        pos = e.target.values_.position
        if pos:
            set_location(pos)
            if not place_loaded:
                _reverse_geocode(pos[0], pos[1])

    # ── GeoJSON for map dot ────────────────────────────────────────────────
    features = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:3857"}},
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": location},
            "properties": {"x": location[0], "y": location[1]},
        }],
    } if location else []

    lon84, lat84 = _merc_to_wgs84(location[0], location[1]) if location else (None, None)
    hx = hover_props.get("x")
    hy = hover_props.get("y")
    h_lon, h_lat = _merc_to_wgs84(float(hx), float(hy)) if hx is not None else (None, None)

    # ── Save GPS to Map_Location sheet ────────────────────────────────────
    def _save_gps_to_sheets():
        """Write current GPS coordinates into map_location.sqlite."""
        if not location:
            return
        set_gps_saving(True)
        try:
            lon, lat = _merc_to_wgs84(location[0], location[1])
            record = {
                "grid_east":  f"{lon:.6f}",
                "grid_north": f"{lat:.6f}",
                "altitude":   "",
                "village":    place_name or "",
                "mapped_by":  "GPS Auto-Capture",
                "date_of_survey": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source_name_2": f"EPSG:3857  X={location[0]:.2f}  Y={location[1]:.2f}",
            }
            data_to_sqlite(resources.path / "map_location.sqlite", "Map_Location", [record])
            set_gps_saved_msg(
                f"✅ Saved  {lat:+.6f}°, {lon:+.6f}°  →  Map_Location"
            )

            def _clear_gps_msg():
                set_gps_saved_msg(None)

            lib.utils.background_execute(_clear_gps_msg, delay_seconds=5)
        except Exception as ex:
            set_gps_saved_msg(f"❌ {str(ex)[:80]}")
        finally:
            set_gps_saving(False)

    def handle_save_gps(e):
        lib.utils.background_execute(_save_gps_to_sheets)

    # ── Sync handlers — run in background so UI never freezes ─────────────
    def _do_push_bg():
        log = [f"⬆  Pushing  —  {datetime.now().strftime('%H:%M:%S')}"]
        try:
            for tbl, res in sync_all_push(resources.path, sync_db_url).items():
                log.append(
                    f"✓  {tbl}: {res['rows']} rows" if res["status"] == "ok"
                    else f"✗  {tbl}: {res['message']}"
                )
            log.append("✅  Push complete")
        except Exception as ex:
            log.append(f"❌  {ex}")
        set_sync_log(log)
        set_sync_running(False)

    def _do_pull_bg():
        log = [f"⬇  Pulling  —  {datetime.now().strftime('%H:%M:%S')}"]
        try:
            for tbl, res in sync_all_pull(resources.path, sync_db_url).items():
                log.append(
                    f"✓  {tbl}: {res['rows']} new rows" if res["status"] == "ok"
                    else f"✗  {tbl}: {res['message']}"
                )
            log.append("✅  Pull complete")
        except Exception as ex:
            log.append(f"❌  {ex}")
        set_sync_log(log)
        set_sync_running(False)

    def do_push(e):
        if not sync_db_url:
            set_sync_log(["❌  SYNC_DB_URL not configured — see setup below."])
            return
        set_sync_running(True)
        set_sync_log([f"⬆  Push started  —  {datetime.now().strftime('%H:%M:%S')}  ⟳"])
        lib.utils.background_execute(_do_push_bg)

    def do_pull(e):
        if not sync_db_url:
            set_sync_log(["❌  SYNC_DB_URL not configured — see setup below."])
            return
        set_sync_running(True)
        set_sync_log([f"⬇  Pull started  —  {datetime.now().strftime('%H:%M:%S')}  ⟳"])
        lib.utils.background_execute(_do_pull_bg)

    # ── Render ─────────────────────────────────────────────────────────────
    return lib.tethys.Display(
        lib.html.div()(
            lib.html.style()(SHARED_CSS + HOME_CSS),

            lib.geo.Geolocation(
                trackingOptions=lib.Props(enableHighAccuracy=True),
                tracking=True,
                projection="EPSG:3857",
                onError=lambda e: set_error(e.message),
                onChange=on_geo_change,
            ),

            lib.bs.Toast(
                style=lib.Style(position="fixed", top="70px", right="16px", zIndex="2000"),
                className="d-inline-block m-1",
                bg="danger",
                onClose=lambda e: set_error(None),
                show=error is not None,
            )(
                lib.bs.ToastHeader(lib.html.strong(className="me-auto")("Geolocation Error")),
                lib.bs.ToastBody("Access was denied or is not available."),
            ) if error else None,

            lib.html.div(className="gm-shell")(

                # ══ LEFT SIDEBAR ══════════════════════════════════════════
                lib.html.div(className="gm-sidebar")(
                    lib.html.div(className="gm-sidebar-header")(
                        lib.html.div(style=lib.Style(display="flex", alignItems="center", gap="10px"))(
                            lib.html.span(style=lib.Style(fontSize="28px"))("💧"),
                            lib.html.div()(
                                lib.html.h2("Hydro Sync"),
                                lib.html.p("Field ↔ Office · Live GPS"),
                            ),
                        ),
                    ),
                    lib.html.div(className="gm-sidebar-body")(
                        lib.tabs.Tabs(
                            lib.tabs.TabList(
                                lib.tabs.Tab("📍 Location"),
                                lib.tabs.Tab("🔄 Sync"),
                            ),

                            # ── Tab 1: GPS coordinates + Save button ───────
                            lib.tabs.TabPanel(
                                lib.html.div(style=lib.Style(paddingTop="10px"))(

                                    lib.html.div(className="gps-waiting")(
                                        lib.html.div(style=lib.Style(fontSize="32px"))("📡"),
                                        lib.html.strong(
                                            style=lib.Style(display="block", marginTop="6px", fontSize="13px")
                                        )("Waiting for GPS fix…"),
                                        lib.html.span(
                                            style=lib.Style(fontSize="12px", display="block",
                                                            marginTop="4px", color="#1a56c4")
                                        )("Allow location access when prompted."),
                                    ) if not location else None,

                                    lib.html.div(className="coord-card")(
                                        lib.html.div(className="coord-row")(
                                            lib.html.span(className="coord-axis")("X"),
                                            lib.html.span(className="coord-val")(f"{lon84:+.6f}°"),
                                        ),
                                        lib.html.div(className="coord-row")(
                                            lib.html.span(className="coord-axis")("Y"),
                                            lib.html.span(className="coord-val")(f"{lat84:+.6f}°"),
                                        ),
                                        lib.html.div(
                                            style=lib.Style(fontSize="9px", opacity="0.6",
                                                            marginTop="2px", letterSpacing="0.5px")
                                        )("WGS 84 / decimal degrees"),
                                        lib.html.hr(className="coord-divider"),
                                        lib.html.div(
                                            style=lib.Style(fontSize="9px", opacity="0.6",
                                                            marginBottom="3px", letterSpacing="0.5px")
                                        )("EPSG:3857 — Web Mercator (metres)"),
                                        lib.html.div(className="coord-merc")(f"X  {location[0]:,.2f}"),
                                        lib.html.div(className="coord-merc")(f"Y  {location[1]:,.2f}"),
                                        lib.html.div(className="coord-place")(
                                            f"📌  {place_name}" if place_name else "⏳  Resolving place name…"
                                        ),
                                    ) if location else None,

                                    # ── Save GPS to Sheets button ──────────
                                    lib.bs.Button(
                                        variant="primary",
                                        className="gps-save-btn",
                                        onClick=handle_save_gps,
                                        disabled=not location or gps_saving,
                                        style=lib.Style(
                                            opacity="0.7" if gps_saving else "1",
                                        ),
                                    )(
                                        lib.html.span(className="spinner")("⟳ ") if gps_saving else "📍 ",
                                        "Saving…" if gps_saving else "Save GPS → Map Location Sheet",
                                    ) if location else None,

                                    # Saved confirmation badge
                                    lib.html.div(className="gps-saved-badge")(gps_saved_msg)
                                    if gps_saved_msg else None,

                                    # Hover tooltip
                                    lib.html.div(className="hover-pill")(
                                        lib.html.div()(lib.html.b()("Hovered point")),
                                        lib.html.div()(f"X (Lon)  {h_lon:+.6f}°"),
                                        lib.html.div()(f"Y (Lat)   {h_lat:+.6f}°"),
                                        lib.html.div(
                                            style=lib.Style(opacity="0.65", fontSize="11px", marginTop="2px")
                                        )(f"EPSG:3857  X {float(hx):,.1f}   Y {float(hy):,.1f}"),
                                    ) if h_lon is not None else None,

                                    lib.html.div(
                                        style=lib.Style(fontSize="11px", color="#999",
                                                        textAlign="center", marginTop="8px")
                                    )(f"Last fix: {datetime.now().strftime('%H:%M:%S')}") if location else None,
                                )
                            ),

                            # ── Tab 2: Sync ────────────────────────────────
                            lib.tabs.TabPanel(
                                lib.html.div(style=lib.Style(paddingTop="10px"))(

                                    lib.html.div(className="info-box")(
                                        lib.html.strong("How it works"),
                                        lib.html.br(),
                                        "Field devices save locally in SQLite. "
                                        "Push sends all rows to a shared Postgres database. "
                                        "The office Pulls to receive field data. "
                                        "Duplicates are skipped automatically.",
                                    ),

                                    lib.html.div(className="setup-box")(
                                        lib.html.strong("⚙ One-time setup"),
                                        lib.html.ol(
                                            style=lib.Style(marginTop="8px", paddingLeft="16px")
                                        )(
                                            lib.html.li()("Create a free Postgres DB on ",
                                                           lib.html.strong()("Supabase"),
                                                           " or ", lib.html.strong()("Railway"), "."),
                                            lib.html.li()("Copy the connection string:  ",
                                                           lib.html.code()("postgresql://user:pass@host/db")),
                                            lib.html.li()("Tethys Admin › App Settings › ",
                                                           lib.html.strong()("SYNC_DB_URL")),
                                            lib.html.li()(lib.html.code()("pip install psycopg2-binary")),
                                        ),
                                    ) if not sync_db_url else None,

                                    lib.html.div(
                                        style=lib.Style(
                                            fontSize="11px", fontWeight="600",
                                            color="#155724" if sync_db_url else "#856404",
                                            background="#d4edda" if sync_db_url else "#fff3cd",
                                            border=f"1px solid {'#c3e6cb' if sync_db_url else '#ffeeba'}",
                                            borderRadius="20px", padding="4px 12px",
                                            display="inline-block", marginBottom="12px",
                                        )
                                    )(
                                        "✅ Cloud DB configured" if sync_db_url else "⚠ Cloud DB not configured"
                                    ),

                                    lib.html.div(style=lib.Style(display="flex", gap="8px",
                                                                  flexWrap="wrap", marginBottom="6px"))(
                                        lib.bs.Button(
                                            variant="success", size="sm",
                                            onClick=do_push,
                                            disabled=sync_running or not sync_db_url,
                                            style=lib.Style(flex="1", fontWeight="600"),
                                        )(
                                            lib.html.span(className="spinner")("⟳ ")
                                            if sync_running else "⬆ Push to Cloud"
                                        ),
                                        lib.bs.Button(
                                            variant="primary", size="sm",
                                            onClick=do_pull,
                                            disabled=sync_running or not sync_db_url,
                                            style=lib.Style(flex="1", fontWeight="600"),
                                        )(
                                            lib.html.span(className="spinner")("⟳ ")
                                            if sync_running else "⬇ Pull from Cloud"
                                        ),
                                    ),

                                    lib.html.div(className="sync-log")(
                                        *[lib.html.div()(line) for line in sync_log]
                                    ) if sync_log else None,

                                    lib.html.div(className="sidebar-section-title")("Tables synced"),
                                    *[
                                        lib.html.div(
                                            style=lib.Style(
                                                display="flex", justifyContent="space-between",
                                                fontSize="11px", color="#555",
                                                borderBottom="1px solid #eee", padding="5px 0",
                                            )
                                        )(
                                            lib.html.span(style=lib.Style(fontWeight="600"))(tbl),
                                            lib.html.span(style=lib.Style(color="#999",
                                                                           fontFamily="monospace"))(fname),
                                        )
                                        for tbl, fname in SYNC_TABLES.items()
                                    ],
                                )
                            ),
                        ),
                    ),
                ),

                # ══ RIGHT MAP PANEL ═══════════════════════════════════════
                lib.html.div(className="gm-map-panel")(
                    lib.tethys.Map(key="map")(
                        lib.ol.layer.Vector(
                            onPointerFeatureChange=lambda e: set_hover_props(
                                (e.feature or {}).get("properties", {})
                                if e.feature else {}
                            )
                        )(
                            lib.ol.source.Vector(
                                options=lib.Props(
                                    features=features,
                                    format="GeoJSON",
                                )
                            )
                        )
                    )
                ),
            ),
        )
    )


# ===========================================================================
#  LIVE CHATROOM
# ===========================================================================

@App.page
def chatroom(lib):
    """
    Field team chatroom — messages stored in SQLite, polled every 4 seconds
    so all devices on the same Tethys instance see the same conversation.
    """
    lib.register("textarea.js", "ta", host="/static/hydrogeology/js", default_export="TextArea")
    resources = lib.hooks.use_resources()
    db_fpath  = resources.path / "chatroom.sqlite"

    # ── State ──────────────────────────────────────────────────────────────
    messages,     set_messages     = lib.hooks.use_state([])
    draft,        set_draft        = lib.hooks.use_state("")
    confirm_clear, set_confirm_clear = lib.hooks.use_state(False)
    user = lib.hooks.use_user()
    sender_name = user.username

    # Attempt to get current GPS from a shared location state (best-effort)
    # We store it in a separate tiny SQLite so both pages can see it
    gps_location, set_gps_location = lib.hooks.use_state(None)

    async def receive_message(message):
        set_messages(chat_messages_from_sqlite(db_fpath))

    sender = lib.hooks.use_channel_layer(group_name="hydro_sync_chat", receiver=receive_message)

    # ── Load GPS from last saved Map_Location record (best-effort) ─────────
    def _load_last_gps():
        try:
            rows = data_from_sqlite(resources.path / "map_location.sqlite", "Map_Location")
            if rows:
                latest = rows[0]  # already DESC by created_at
                e = latest.get("grid_east", "")
                n = latest.get("grid_north", "")
                if e and n:
                    set_gps_location({"lon": e, "lat": n})
        except Exception:
            pass
    
    def _initialize_messages():
        msgs = chat_messages_from_sqlite(db_fpath)
        set_messages(msgs)

    lib.hooks.use_effect(_load_last_gps, [])
    lib.hooks.use_effect(_initialize_messages, [])

    # ── Send a message ─────────────────────────────────────────────────────
    def send_message(text=None):
        if not text:
            text = draft.strip()
        if not text:
            return
        ts = datetime.now().isoformat()
        asyncio.create_task(
            sender(
                lib.Props(
                    ts=ts,
                    text=text,
                    user=sender_name,
                )
            )
        )
        chat_message_to_sqlite(db_fpath, sender_name, text, ts)

    # ── Share GPS coordinates as a message ─────────────────────────────────
    def share_gps(e):
        if not gps_location:
            return
        text = (
            f"📍 GPS Fix — Lat: {gps_location['lat']}°  Lon: {gps_location['lon']}°  "
            f"[ {datetime.now().strftime('%H:%M:%S')} ]"
        )
        send_message(text)

    # ── Clear all messages ─────────────────────────────────────────────────
    def do_clear(e):
        chat_clear_sqlite(db_fpath)
        set_messages([])
        set_confirm_clear(False)

    # ── Render helpers ─────────────────────────────────────────────────────
    def _avatar_initial(name):
        return (name or "?")[0].upper()

    def _fmt_ts(ts_str):
        try:
            return datetime.fromisoformat(ts_str).strftime("%H:%M")
        except Exception:
            return ""

    def _is_gps_msg(text):
        return text.startswith("📍 GPS Fix")

    def MessageRow(msg):
        is_me  = msg.get("sender", "") == sender_name.strip()
        text   = str(msg.get("text", ""))
        sender = str(msg.get("sender", "?"))
        ts     = _fmt_ts(str(msg.get("ts", "")))

        if _is_gps_msg(text):
            return lib.html.div(className="chat-system-msg")(text)

        row_cls = "chat-msg-row me" if is_me else "chat-msg-row"

        return lib.html.div(className=row_cls)(
            lib.html.div(className="chat-avatar")(_avatar_initial(sender)),
            lib.html.div(className="chat-bubble-wrap")(
                lib.html.div(className="chat-sender")(sender),
                lib.html.div(className="chat-bubble")(text),
                lib.html.div(className="chat-ts")(ts),
            ),
        )

    # ── Page ───────────────────────────────────────────────────────────────
    return lib.tethys.Display(
        lib.html.div()(
            lib.html.style()(CHAT_CSS),

            lib.html.div(className="chat-root")(

                # Header
                lib.html.div(className="chat-header")(
                    lib.html.div(className="chat-header-icon")("💬"),
                    lib.html.div()(
                        lib.html.div(className="chat-header-title")("Field Chatroom"),
                        lib.html.div(className="chat-header-sub")(
                            f"HydroSync · {len(messages)} message(s)"
                        ),
                    ),
                    lib.html.div(className="chat-online-dot"),
                    # Clear button (top right)
                    lib.html.button(
                        className="chat-clear-btn",
                        onClick=lambda e: set_confirm_clear(True),
                        style=lib.Style(marginLeft="12px"),
                    )("clear"),
                ),

                # Confirm-clear modal
                lib.bs.Modal(show=confirm_clear,
                              onHide=lambda: set_confirm_clear(False))(
                    lib.bs.ModalHeader()("Clear Chat?"),
                    lib.bs.ModalBody()("This will delete all messages permanently."),
                    lib.bs.ModalFooter()(
                        lib.bs.Button(variant="secondary",
                                      onClick=lambda e: set_confirm_clear(False))("Cancel"),
                        lib.bs.Button(variant="danger", onClick=do_clear)("Clear All"),
                    ),
                ),

                # Name bar
                lib.html.div(className="chat-name-bar")(
                    lib.html.span(className="chat-name-label")("Sending as:"),
                    lib.m.Badge(color="blue")(sender_name),
                    lib.html.span(
                        style=lib.Style(
                            fontSize="11px",
                            fontFamily="'JetBrains Mono', monospace",
                            color="#10b981",
                            marginLeft="auto",
                        )
                    )(
                        f"GPS: {gps_location['lat']}°, {gps_location['lon']}°"
                        if gps_location else "GPS: not loaded"
                    ),
                ),

                # Messages
                lib.html.div(className="chat-messages", id="chat-scroll")(
                    *[MessageRow(m) for m in messages]
                ) if messages else lib.html.div(className="chat-messages")(
                    lib.html.div(className="chat-empty")(
                        lib.html.div(className="chat-empty-icon")("💬"),
                        lib.html.div(className="chat-empty-text")("No messages yet. Say hello!"),
                    )
                ),

                # Input bar
                lib.html.div(className="chat-input-bar")(
                    # Share GPS button
                    lib.html.button(
                        className="chat-gps-btn",
                        onClick=share_gps,
                        disabled=not gps_location,
                        title="Share last saved GPS coordinates",
                    )("📍"),

                    lib.ta.TextArea(
                        className="chat-text-input",
                        placeholder="Type a message…  (Enter to send)",
                        onInput=lambda e: set_draft(e),
                        onEnterKey=lambda _: (send_message(), set_draft("")),
                        rows="1",
                    ),

                    lib.html.button(
                        className="chat-send-btn",
                        onClick=lambda _: (send_message(), set_draft("")),
                        disabled=not draft.strip(),
                        title="Send",
                    )("➤"),
                ),
            ),
        )
    )


# ===========================================================================
#  Map Location
# ===========================================================================

@App.page
def map_location(lib):
    lib.register("sketch_canvas.js", "sc",
                 host="/static/hydrogeology/js", default_export="SketchCanvas")
    lib.register("react-tabs", "tabs",
                 styles=["https://esm.sh/react-tabs@6.1.0/style/react-tabs.css"])

    resources  = lib.hooks.use_resources()
    db_fpath   = resources.path / "map_location.sqlite"
    table_name = "Map_Location"
    db = use_db_state(lib, db_fpath, table_name)

    color, set_color = lib.hooks.use_state("#100a0a")
    width, set_width = lib.hooks.use_state(4)

    form_fields = [
        [("village", "Village"), ("ves_no", "VES No."), ("map_sheet_no", "Map Sheet No."), ("mapped_by", "Mapped By")],
        [("parish", "Parish"), ("subcounty", "Sub-County"), ("county", "County"), ("district", "District")],
        [("grid_east", "Grid East (°Lon)"), ("grid_north", "Grid North (°Lat)"), ("altitude", "Altitude")],
        [("village_code", "Village Code"), ("date_of_survey", "Date of Survey"), ("source_name_2", "Source Name")],
        [("proposed_type_of_water_source", "Proposed Type of Water Source")],
        [("expected_depth_to_rock_m", "Expected Depth to Rock (m)"), ("expected_depth_to_water_m", "Expected Depth to Water (m)")],
        [("expected_formation", "Expected Formation")],
        [("expected_borehole_depth_m", "Expected Borehole Depth (m)"), ("accessibility_to_site", "Accessibility to Site")],
        [("expected_depth_to_screen_m", "Expected Depth to Screen (m)")],
    ]
    summary_cols = [("village", "Village"), ("mapped_by", "Mapped By"),
                    ("grid_east", "Grid East"), ("grid_north", "Grid North")]

    def sketch_content(lib, existing_id, form_edit_mode):
        selected_record_data = None
        if db["displayed_data"] and existing_id:
            selected_record_data = next(
                (r for r in db["displayed_data"] if str(r.get("created_at")) == str(existing_id)), None
            )
        existing_sketch = selected_record_data.get("sketch", "") if selected_record_data else ""
        is_editable     = form_edit_mode

        return lib.html.div(style=lib.Style(padding="20px"))(
            lib.html.h1("LOCATION MAP"),
            lib.bs.Row(
                lib.bs.Col(
                    lib.html.label("Draw Color:"),
                    lib.html.input(type="color", value=color,
                                   onChange=lambda e: set_color(e.target.value),
                                   style=lib.Style(marginRight="10px"),
                                   disabled=not is_editable),
                    lib.html.label("Brush Width:"),
                    lib.html.input(type="range", min="1", max="10", value=width,
                                   onChange=lambda e: set_width(int(e.target.value)),
                                   disabled=not is_editable),
                ) if is_editable else lib.bs.Col(),
            ),
            lib.bs.Row(
                lib.bs.Col(
                    lib.sc.SketchCanvas(
                        name="sketch",
                        style=lib.Style(border="0.0625rem solid #9c9c9c",
                                        borderRadius="0.25rem", width="100%", height="500px"),
                        width="100%", height="500px",
                        strokeWidth=width, strokeColor=color,
                        backgroundImage=existing_sketch if existing_id else "",
                    ) if is_editable else lib.html.img(
                        src=existing_sketch,
                        style=lib.Style(width="100%", border="1px solid black"),
                    )
                ),
            ),
        )

    _, _, TabView = make_record_manager(
        lib, db, form_fields=form_fields, summary_cols=summary_cols,
        page_title="Map Location Survey Form", extra_form_content=sketch_content,
    )
    return TabView()


# ===========================================================================
#  VES Form
# ===========================================================================

@App.page
def VES_FORM(lib):
    lib.register("react-tabs", "tabs",
                 styles=["https://esm.sh/react-tabs@6.1.0/style/react-tabs.css"])

    resources  = lib.hooks.use_resources()
    db_fpath   = resources.path / "ves_survey_data.sqlite"
    table_name = "VES_FORM"
    db = use_db_state(lib, db_fpath, table_name)

    row_data_1, set_row_data_1 = lib.hooks.use_state(
        [{"station": x, "reading": "", "apparent_resistivity": "", "remarks": ""}
         for x in range(21)]
    )

    form_fields = [
        [("Project_Name", "Project Name"), ("profile", "Profile")],
        [("Area", "Area"), ("Coordinates", "Coordinates")],
        [("Date", "Date"), ("Orientation", "Orientation")],
        [("Configuration", "Configuration"), ("Station_Interval", "Station Interval")],
        [("half_AB", "1/2 AB"), ("half_MN", "1/2 MN")],
    ]
    summary_cols = [("Project_Name", "Project Name"), ("Area", "Area"), ("Date", "Date")]

    def ves_extra(lib, existing_id, form_edit_mode):
        return lib.html.div()(
            lib.html.div(style=lib.Style(backgroundColor="white", padding="20px",
                                         borderRadius="8px", marginTop="20px"))(
                lib.html.h3("Data Grid - Stations 0-20"),
                lib.html.div(style=lib.Style(height="400px", border="1px solid #ddd"))(
                    lib.ag.AgGridReact(
                        rowData=row_data_1,
                        columnDefs=[
                            {"field": "station",              "editable": False},
                            {"field": "reading",              "editable": True},
                            {"field": "apparent_resistivity", "editable": True},
                            {"field": "remarks",              "editable": True},
                        ],
                        defaultColDef=lib.Props(flex=1),
                    ),
                ),
            ),
            lib.html.div(style=lib.Style(backgroundColor="white", padding="20px",
                                         borderRadius="8px", marginTop="20px"))(
                lib.html.h3("Reading vs Station"),
                lib.tethys.Chart(data=row_data_1, height=500, width=900,
                                 x_label="Station", y_label="Reading",
                                 x_attr="station", y_attr="reading"),
            ),
        )

    _, _, TabView = make_record_manager(
        lib, db, form_fields=form_fields, summary_cols=summary_cols,
        page_title="VES FORM — Vertical Electrical Sounding", extra_form_content=ves_extra,
    )
    return TabView()


# ===========================================================================
#  Resistivity Survey
# ===========================================================================

@App.page
def resistivity_survey_form(lib):
    lib.register("react-tabs", "tabs",
                 styles=["https://esm.sh/react-tabs@6.1.0/style/react-tabs.css"])

    resources  = lib.hooks.use_resources()
    db_fpath   = resources.path / "resistivity_survey.sqlite"
    table_name = "resistivity_survey"
    db = use_db_state(lib, db_fpath, table_name)

    log_spacings = [
        1, 2.1, 3.0, 4.4, 6.3, 9.1, 13.2, 13.2, 19.0, 19.0,
        27.5, 27.5, 40, 58, 58, 83, 83, 120, 120, 175, 250, 375, 525, 750,
    ]

    survey_data, set_survey_data = lib.hooks.use_state({
        "location_point": "",
        "mn2_value": "0.5",
        "readings": [
            {"spacing": s, "reading_1": "", "reading_2": "", "average": "", "notes": ""}
            for s in log_spacings
        ],
    })

    form_fields  = [[("location_point", "Location Point (Site ID)")]]
    summary_cols = [("location_point", "Location Point"), ("mn2_value", "MN/2")]

    def update_reading(index, field, value):
        new_readings = survey_data["readings"].copy()
        new_readings[index] = {**new_readings[index], field: value}
        r   = float(new_readings[index]["reading_2"]) if new_readings[index]["reading_2"] else 0
        mn2 = float(survey_data["mn2_value"]) if survey_data["mn2_value"] else 0.5
        if r and mn2:
            new_readings[index]["average"] = str(r * mn2)
        set_survey_data({**survey_data, "readings": new_readings})

    def update_mn2(value):
        new_readings = survey_data["readings"].copy()
        mn2 = float(value) if value else 0.5
        for i in range(len(new_readings)):
            r = float(new_readings[i]["reading_2"]) if new_readings[i]["reading_2"] else 0
            if r and mn2:
                new_readings[i]["average"] = str(r * mn2)
        set_survey_data({**survey_data, "mn2_value": value, "readings": new_readings})

    plot_data = []
    for i, spacing in enumerate(log_spacings):
        if i < len(survey_data["readings"]):
            reading = survey_data["readings"][i]
            if reading["average"]:
                try:
                    plot_data.append({"depth": spacing, "resistivity": float(reading["average"])})
                except Exception:
                    pass

    def resistivity_extra(lib, existing_id, form_edit_mode):
        is_readonly = existing_id is not None and not form_edit_mode
        return lib.html.div()(
            lib.html.div(style=lib.Style(display="flex", gap="20px", margin="20px 0"))(
                lib.html.div(style=lib.Style(flex="0 0 300px"))(
                    lib.html.label("MN/2 (constant):"),
                    lib.html.select(
                        style=lib.Style(width="100%", padding="8px", marginTop="5px"),
                        value=survey_data["mn2_value"],
                        onChange=lambda e: update_mn2(e.target.value),
                        disabled=is_readonly,
                    )(
                        lib.html.option(value="0.5")("0.5 m"),
                        lib.html.option(value="5.0")("5.0 m"),
                        lib.html.option(value="25")("25 m"),
                    ),
                ),
            ),
            lib.html.div(style=lib.Style(display="flex", gap="20px", marginBottom="20px"))(
                lib.html.div(style=lib.Style(flex="0 0 550px", border="1px solid #999",
                                             padding="10px", backgroundColor="#f9f9f9"))(
                    lib.html.table(
                        style=lib.Style(width="100%", borderCollapse="collapse", fontSize="12px")
                    )(
                        lib.html.thead()(
                            lib.html.tr(style=lib.Style(backgroundColor="#ddd",
                                                        borderBottom="2px solid #999"))(
                                lib.html.th(style=lib.Style(padding="8px", border="1px solid #999"))("AB/2 (m)"),
                                lib.html.th(style=lib.Style(padding="8px", border="1px solid #999"))("Count"),
                                lib.html.th(style=lib.Style(padding="8px", border="1px solid #999"))("R (Ω)"),
                                lib.html.th(style=lib.Style(padding="8px", border="1px solid #999"))("Notes"),
                            ),
                        ),
                        lib.html.tbody()(
                            *[
                                lib.html.tr(
                                    style=lib.Style(borderBottom="1px solid #ddd",
                                                    backgroundColor="#fff" if i % 2 == 0 else "#f5f5f5")
                                )(
                                    lib.html.td(style=lib.Style(padding="6px", border="1px solid #ddd",
                                                                fontWeight="bold"))(str(spacing)),
                                    lib.html.td(style=lib.Style(padding="6px", border="1px solid #ddd",
                                                                textAlign="center", fontWeight="bold"))(str(idx + 1)),
                                    lib.html.td(style=lib.Style(padding="4px", border="1px solid #ddd"))(
                                        lib.html.input(
                                            type="number",
                                            value=survey_data["readings"][idx]["reading_2"],
                                            style=lib.Style(width="90%", padding="4px"),
                                            placeholder="0.0", disabled=is_readonly,
                                            onChange=lambda e, i=idx: update_reading(i, "reading_2", e.target.value),
                                        ),
                                    ),
                                    lib.html.td(style=lib.Style(padding="4px", border="1px solid #ddd"))(
                                        lib.html.input(
                                            type="text",
                                            value=survey_data["readings"][idx]["notes"],
                                            style=lib.Style(width="90%", padding="4px"),
                                            placeholder="Layer", disabled=is_readonly,
                                            onChange=lambda e, i=idx: update_reading(i, "notes", e.target.value),
                                        ),
                                    ),
                                )
                                for idx, spacing in enumerate(log_spacings)
                            ]
                        ),
                    ),
                ),
                lib.html.div(style=lib.Style(flex=1, minHeight="700px"))(
                    lib.html.div(style=lib.Style(height="700px"))(
                        lib.tethys.Chart(
                            data=plot_data if plot_data else [{"depth": 1, "resistivity": 50}],
                            height=700, width=900,
                            x_label="Apparent Resistivity ρa (Ω·m)",
                            y_label="Electrode Spacing AB/2 (m)",
                            x_attr="resistivity", y_attr="depth",
                        ),
                    ),
                    lib.html.p(style=lib.Style(fontSize="11px", color="#666", marginTop="10px"))(
                        "Schlumberger array: MN/2 constant, AB/2 varies. "
                        "ρa = R × (MN/2). Curve breaks = layer boundaries."
                    ),
                ),
            ),
        )

    _, _, TabView = make_record_manager(
        lib, db, form_fields=form_fields, summary_cols=summary_cols,
        page_title="Schlumberger Array VES Survey", extra_form_content=resistivity_extra,
    )
    return TabView()


# ===========================================================================
#  Gemini rock-image analysis
# ===========================================================================

async def analyze_rock_from_bytes(api_key, data_bytes, mime_type="image/jpeg"):
    if not api_key:
        return {"status": "error", "message": "Gemini API key is not configured."}
    if not data_bytes:
        return {"status": "error", "message": "No image data provided."}

    def _request_gemini():
        endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.5-flash:generateContent?key={api_key}"
        )
        payload = {
            "contents": [{
                "parts": [
                    {"text": (
                        "Identify the likely rock type in this image and provide a short, practical "
                        "field description with key observable features."
                    )},
                    {"inline_data": {
                        "mime_type": mime_type or "image/jpeg",
                        "data": base64.b64encode(data_bytes).decode("utf-8"),
                    }},
                ]
            }]
        }
        req = Request(endpoint, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req) as response:
            body = json.loads(response.read().decode("utf-8"))

        candidates = body.get("candidates", [])
        if not candidates:
            msg = body.get("error", {}).get("message", "No response from Gemini API.")
            return {"status": "error", "message": msg}

        parts    = candidates[0].get("content", {}).get("parts", [])
        analysis = "".join([p.get("text", "") for p in parts]).strip()
        if not analysis:
            return {"status": "error", "message": "Gemini returned an empty analysis."}
        return {"status": "success", "analysis": analysis}

    try:
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _request_gemini)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@App.page
def image_analysis(lib):
    lib.register("react-markdown", "md", default_export="Markdown")
    lib.register("react-tabs", "tabs",
                 styles=["https://esm.sh/react-tabs@6.1.0/style/react-tabs.css"])
    lib.md.Markdown()
    lib.bs.Alert()

    processing,       set_processing       = lib.hooks.use_state(False)
    analysis_results, set_analysis_results = lib.hooks.use_state(None)
    image,            set_image            = lib.hooks.use_state(None)

    archive_view,    set_archive_view    = lib.hooks.use_state("list")
    selected_record, set_selected_record = lib.hooks.use_state(None)
    selected_rows,   set_selected_rows   = lib.hooks.use_state(set())
    delete_confirm,  set_delete_confirm  = lib.hooks.use_state(False)

    gemini_api_key = lib.hooks.use_setting("GEMINI_API_KEY")

    resources  = lib.hooks.use_resources()
    db_fpath   = resources.path / "image_analysis.sqlite"
    table_name = "Image_Analysis"
    db = use_db_state(lib, db_fpath, table_name)

    async def handle_file_upload(e):
        set_processing(True)
        image_data = e["formData"].get("upload")
        set_image(image_data)
        with urlopen(image_data) as response:
            mime_type  = response.info().get_content_type()
            data_bytes = response.read()
        result = await analyze_rock_from_bytes(gemini_api_key, data_bytes, mime_type=mime_type)
        set_analysis_results(
            result["analysis"] if result["status"] == "success"
            else f"Error: {result['message']}"
        )
        set_processing(False)

    def handle_save_analysis(e):
        form_data = dict(e["formData"])
        db["save"]([{
            "village":   form_data.get("village",   ""),
            "formation": form_data.get("formation", ""),
            "image":     image,
            "analysis":  analysis_results,
        }])
        set_analysis_results(None)
        set_image(None)

    def toggle_row(rid):
        new_sel = set(selected_rows)
        if rid in new_sel:
            new_sel.discard(rid)
        else:
            new_sel.add(rid)
        set_selected_rows(new_sel)

    def open_detail():
        if len(selected_rows) == 1:
            rid    = list(selected_rows)[0]
            record = next(
                (r for r in db["displayed_data"] if str(r.get("created_at")) == str(rid)), None
            )
            if record:
                set_selected_record(record)
                set_archive_view("detail")

    def do_delete():
        for rid in list(selected_rows):
            db["delete"](rid)
        set_selected_rows(set())
        set_delete_confirm(False)

    def ArchiveList():
        data = db["displayed_data"]
        if not data:
            return lib.html.div(
                style=lib.Style(padding="40px", textAlign="center", color="#999", fontSize="16px")
            )("📭 No analyses saved yet.")

        rows = [
            lib.html.tr(
                style=lib.Style(
                    borderBottom="1px solid #ddd",
                    backgroundColor="#e8f4f8" if record.get("created_at") in selected_rows else "#fff"
                )
            )(
                lib.html.td(style=lib.Style(padding="10px", textAlign="center"))(
                    lib.html.input(
                        type="checkbox",
                        checked=record.get("created_at") in selected_rows,
                        onChange=lambda e, rid=record.get("created_at"): toggle_row(rid),
                        style=lib.Style(cursor="pointer", width="16px", height="16px"),
                    )
                ),
                lib.html.td(style=lib.Style(padding="10px"))(str(record.get("village",   "—"))),
                lib.html.td(style=lib.Style(padding="10px"))(str(record.get("formation", "—"))),
                lib.html.td(
                    style=lib.Style(padding="10px", maxWidth="300px", overflow="hidden",
                                    whiteSpace="nowrap", textOverflow="ellipsis")
                )(
                    (str(record.get("analysis", "—"))[:80] + "…")
                    if len(str(record.get("analysis", ""))) > 80
                    else str(record.get("analysis", "—"))
                ),
                lib.html.td(style=lib.Style(padding="10px", fontSize="12px", color="#666"))(
                    str(record.get("created_at", "—"))
                ),
            )
            for record in data
        ]

        return lib.html.div()(
            lib.html.div(style=lib.Style(display="flex", gap="10px", marginBottom="15px",
                                         alignItems="center"))(
                lib.html.span(style=lib.Style(fontSize="13px", color="#666"))(
                    f"💡 {len(data)} record(s) | {len(selected_rows)} selected"
                ),
                lib.bs.Button(variant="info", size="sm", onClick=lambda e: open_detail(),
                              disabled=len(selected_rows) != 1,
                              style=lib.Style(marginLeft="auto"))("👁️ View"),
                lib.bs.Button(variant="danger", size="sm",
                              onClick=lambda e: set_delete_confirm(True),
                              disabled=len(selected_rows) == 0)(
                    f"🗑️ Delete ({len(selected_rows)})"),
            ),
            lib.bs.Modal(show=delete_confirm, onHide=lambda: set_delete_confirm(False))(
                lib.bs.ModalHeader()("Confirm Delete"),
                lib.bs.ModalBody()(f"Delete {len(selected_rows)} record(s)? Cannot be undone."),
                lib.bs.ModalFooter()(
                    lib.bs.Button(variant="secondary",
                                  onClick=lambda e: set_delete_confirm(False))("Cancel"),
                    lib.bs.Button(variant="danger", disabled=db["is_loading"],
                                  onClick=lambda e: do_delete())("Delete"),
                ),
            ),
            lib.html.div(style=lib.Style(border="1px solid #ddd", borderRadius="4px",
                                         overflow="hidden"))(
                lib.html.table(style=lib.Style(width="100%", borderCollapse="collapse"))(
                    lib.html.thead(
                        style=lib.Style(backgroundColor="#f5f5f5", borderBottom="2px solid #ddd")
                    )(
                        lib.html.tr()(
                            lib.html.th(style=lib.Style(padding="10px", textAlign="center",
                                                        borderRight="1px solid #ddd"))("☑️"),
                            lib.html.th(style=lib.Style(padding="10px", textAlign="left",
                                                        borderRight="1px solid #ddd"))("Village"),
                            lib.html.th(style=lib.Style(padding="10px", textAlign="left",
                                                        borderRight="1px solid #ddd"))("Formation"),
                            lib.html.th(style=lib.Style(padding="10px", textAlign="left",
                                                        borderRight="1px solid #ddd"))("Analysis (preview)"),
                            lib.html.th(style=lib.Style(padding="10px", textAlign="left"))("Saved At"),
                        )
                    ),
                    lib.html.tbody()(*rows),
                )
            ),
        )

    def ArchiveDetail():
        rec = selected_record
        if not rec:
            return lib.html.div()("No record selected.")

        return lib.html.div(style=lib.Style(padding="20px", maxWidth="860px"))(
            lib.html.div(style=lib.Style(display="flex", gap="10px", marginBottom="20px",
                                         alignItems="center"))(
                lib.bs.Button(variant="secondary",
                              onClick=lambda e: (set_archive_view("list"),
                                                  set_selected_record(None)))("← Back to Archive"),
                lib.bs.Button(variant="danger",
                              onClick=lambda e: set_delete_confirm(True))("🗑️ Delete This Record"),
            ),
            lib.bs.Modal(show=delete_confirm, onHide=lambda: set_delete_confirm(False))(
                lib.bs.ModalHeader()("Confirm Delete"),
                lib.bs.ModalBody()("Delete this record? Cannot be undone."),
                lib.bs.ModalFooter()(
                    lib.bs.Button(variant="secondary",
                                  onClick=lambda e: set_delete_confirm(False))("Cancel"),
                    lib.bs.Button(variant="danger", disabled=db["is_loading"],
                                  onClick=lambda e: (
                                      db["delete"](rec.get("created_at")),
                                      set_delete_confirm(False),
                                      set_archive_view("list"),
                                      set_selected_record(None),
                                      set_selected_rows(set()),
                                  ))("Delete"),
                ),
            ) if delete_confirm else None,
            lib.html.h2("🔬 Analysis Record"),
            lib.html.div(
                style=lib.Style(display="flex", gap="30px", flexWrap="wrap",
                                backgroundColor="#f8f9fa", padding="14px 18px",
                                borderRadius="6px", marginBottom="20px",
                                border="1px solid #dee2e6")
            )(
                *[
                    lib.html.div()(
                        lib.html.span(style=lib.Style(fontWeight="bold", color="#555",
                                                      fontSize="12px", textTransform="uppercase",
                                                      letterSpacing="0.5px"))(label),
                        lib.html.div(style=lib.Style(fontSize="16px", marginTop="4px"))(
                            str(rec.get(key, "—"))
                        ),
                    )
                    for key, label in [("village", "Village"), ("formation", "Formation"),
                                       ("created_at", "Saved At")]
                ]
            ),
            lib.html.div(style=lib.Style(display="flex", gap="24px", flexWrap="wrap",
                                         alignItems="flex-start"))(
                lib.html.div(style=lib.Style(flex="0 0 380px", minWidth="260px"))(
                    lib.html.h5(style=lib.Style(marginBottom="10px", color="#333"))("📷 Rock Image"),
                    lib.html.img(
                        src=rec.get("image", ""),
                        style=lib.Style(width="100%", borderRadius="6px",
                                        border="1px solid #ccc", display="block"),
                    ) if rec.get("image") else lib.html.div(
                        style=lib.Style(padding="40px", textAlign="center", color="#999",
                                        border="1px dashed #ccc", borderRadius="6px")
                    )("No image stored"),
                ),
                lib.html.div(
                    style=lib.Style(flex="1", minWidth="260px", backgroundColor="#fff",
                                    border="1px solid #dee2e6", borderRadius="6px", padding="18px")
                )(
                    lib.html.h5(style=lib.Style(marginBottom="12px", color="#333"))(
                        "🤖 Gemini AI Analysis"
                    ),
                    lib.md.Markdown(rec.get("analysis", "*No analysis text stored.*")),
                ),
            ),
        )

    return lib.tethys.Display(
        lib.html.div()(
            lib.html.style()(SHARED_CSS),
            lib.tabs.Tabs(
                lib.tabs.TabList(
                    lib.tabs.Tab("Perform Analysis"),
                    lib.tabs.Tab("Analysis Archive"),
                ),

                lib.tabs.TabPanel(
                    lib.html.div(style=lib.Style(padding="20px", maxWidth="800px",
                                                 margin="0 auto", fontFamily="Arial, sans-serif"))(
                        lib.html.h1("Rock Identifier with Gemini AI"),

                        lib.lo.LoadingOverlay(active=processing, spinner=True)(
                            lib.bs.Form(
                                onSubmit=event(handle_file_upload,
                                               prevent_default=True, stop_propagation=True)
                            )(
                                lib.html.h3("Upload Image"),
                                lib.html.input(key=id(analysis_results), type="file",
                                               name="upload", accept="image/*"),
                                lib.html.button(type="submit")("Analyze File"),
                            )
                        ) if not analysis_results else

                        lib.bs.Form(onSubmit=handle_save_analysis)(
                            lib.bs.Button(
                                variant="outline-secondary",
                                style=lib.Style(marginBottom="16px"),
                                onClick=lambda e: (set_analysis_results(None), set_image(None)),
                            )("← Analyze Another"),

                            lib.html.h3("Analysis Result"),
                            lib.html.img(
                                src=image,
                                style=lib.Style(maxWidth="100%", marginTop="10px",
                                                borderRadius="6px", border="1px solid #ccc"),
                            ),
                            lib.html.hr(),

                            lib.html.div(
                                style=lib.Style(backgroundColor="#f8f9fa",
                                                border="1px solid #dee2e6",
                                                borderRadius="6px", padding="16px",
                                                marginBottom="20px")
                            )(
                                lib.html.h5(style=lib.Style(color="#333", marginBottom="8px"))(
                                    "🤖 Gemini AI Analysis"
                                ),
                                lib.md.Markdown(analysis_results),
                            ),

                            lib.bs.Row(style=lib.Style(marginBottom="16px"))(
                                lib.bs.Col()(
                                    lib.html.label(
                                        style=lib.Style(display="block", fontWeight="bold",
                                                        marginBottom="5px", fontSize="14px"),
                                        for_="village",
                                    )("Village:"),
                                    lib.html.input(
                                        name="village", type="text", className="form-control",
                                        placeholder="Enter village name",
                                        style=lib.Style(width="100%", padding="8px"),
                                    ),
                                ),
                                lib.bs.Col()(
                                    lib.html.label(
                                        style=lib.Style(display="block", fontWeight="bold",
                                                        marginBottom="5px", fontSize="14px"),
                                        for_="formation",
                                    )("Formation:"),
                                    lib.html.input(
                                        name="formation", type="text", className="form-control",
                                        placeholder="Enter formation type",
                                        style=lib.Style(width="100%", padding="8px"),
                                    ),
                                ),
                            ),

                            status_alerts(lib, submit_success=db["submit_success"],
                                          success_message=db["success_message"],
                                          error_message=db["error_message"]),

                            lib.bs.Button(
                                type="submit", variant="primary", size="lg",
                                disabled=db["is_loading"],
                                style=lib.Style(
                                    opacity="0.7" if db["is_loading"] else "1",
                                    cursor="not-allowed" if db["is_loading"] else "pointer",
                                    width="220px", padding="12px 24px",
                                    fontSize="16px", fontWeight="600", marginTop="8px",
                                ),
                            )(
                                lib.html.span(className="spinner")("⟳ ")
                                if db["is_loading"] else "💾 ",
                                "Saving…" if db["is_loading"] else "Save Analysis",
                            ),
                        ),
                    ),
                ),

                lib.tabs.TabPanel(
                    lib.html.div(style=lib.Style(padding="20px"))(
                        lib.html.h2("📊 Analysis Archive"),
                        status_alerts(lib, submit_success=db["submit_success"],
                                      success_message=db["success_message"],
                                      error_message=db["error_message"]),
                        ArchiveList() if archive_view == "list" else ArchiveDetail(),
                    )
                ),
            ),
        )
    )