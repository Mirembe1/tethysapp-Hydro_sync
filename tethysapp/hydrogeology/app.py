import re
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
        )


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
        print(f"✓ Deleted {id_col}={record_id} from {table_name}")
    except Exception as e:
        conn.rollback()
        print(f"✗ Delete Error: {e}")
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
        print(f"✓ Updated {table_name}")
    except Exception as e:
        conn.rollback()
        print(f"✗ Update Error: {e}")
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
            print(f"✓ Created table: {table_name}")
        else:
            cursor.execute(f'PRAGMA table_info("{table_name}")')
            existing = {r[1] for r in cursor.fetchall()}
            for col in columns_safe:
                if col not in existing:
                    try:
                        cursor.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{col}" TEXT')
                        print(f"✓ Added column: {col}")
                    except sqlite3.OperationalError as oe:
                        print(f"Column {col} already exists or error: {oe}")

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
        print(f"✓ Saved to {table_name}")
    except Exception as e:
        conn.rollback()
        print(f"✗ Save Error: {e}")
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
                print(f"✓ {table_name} auto-loaded")
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
    lib,
    db,
    form_fields,
    summary_cols,
    page_title="Survey Form",
    extra_form_content=None,
    id_col="created_at",
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
            style=lib.Style(
                border="1px solid #ddd", borderRadius="4px",
                overflow="hidden", backgroundColor="white"
            )
        )(
            lib.html.p(
                style=lib.Style(fontSize="12px", color="#666", marginBottom="15px", padding="15px")
            )(f"💡 Total Records: {len(displayed_data)} | Selected: {len(selected_rows)}"),

            lib.html.div(
                style=lib.Style(padding="15px", borderBottom="1px solid #ddd", display="flex", gap="10px")
            )(
                lib.bs.Button(
                    variant="info", size="sm",
                    onClick=lambda e: handle_view(),
                    disabled=len(selected_rows) != 1
                )("👁️ View"),
                lib.bs.Button(
                    variant="danger", size="sm",
                    onClick=lambda e: set_delete_confirm_open(True),
                    disabled=len(selected_rows) == 0
                )(f"🗑️ Delete ({len(selected_rows)})"),
            ),

            lib.bs.Modal(
                show=delete_confirm_open,
                onHide=lambda: set_delete_confirm_open(False)
            )(
                lib.bs.ModalHeader()("Confirm Delete?"),
                lib.bs.ModalBody()(
                    f"Delete {len(selected_rows)} record(s)? This cannot be undone."
                ),
                lib.bs.ModalFooter()(
                    lib.bs.Button(
                        variant="secondary",
                        onClick=lambda e: set_delete_confirm_open(False)
                    )("Cancel"),
                    lib.bs.Button(
                        variant="danger",
                        onClick=confirm_delete,
                        disabled=db["is_loading"]
                    )("Delete")
                )
            ),

            lib.html.table(style=lib.Style(width="100%", borderCollapse="collapse"))(
                lib.html.thead(
                    style=lib.Style(backgroundColor="#f5f5f5", borderBottom="2px solid #ddd")
                )(
                    lib.html.tr()(
                        lib.html.th(
                            style=lib.Style(padding="12px", textAlign="center",
                                            fontWeight="bold", borderRight="1px solid #ddd")
                        )("☑️"),
                        *[
                            lib.html.th(
                                style=lib.Style(padding="12px", textAlign="left",
                                                fontWeight="bold", borderRight="1px solid #ddd")
                            )(header)
                            for _, header in summary_cols
                        ],
                        lib.html.th(
                            style=lib.Style(padding="12px", textAlign="left", fontWeight="bold")
                        )("Date"),
                    )
                ),
                lib.html.tbody()(*table_rows)
            )
        )

    def FormView(existing_id=None, form_edit_mode=True):
        if existing_id is None and not form_edit_mode:
            raise Exception("Cannot view a non-existent record in read-only mode")

        selected_record_data = None
        if db["displayed_data"] and existing_id:
            selected_record_data = next(
                (r for r in db["displayed_data"] if str(r.get(id_col)) == str(existing_id)),
                None
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
                            name=field_name,
                            type="text",
                            className="form-control",
                            defaultValue=(
                                selected_record_data.get(field_name, "")
                                if selected_record_data else ""
                            ),
                            style=lib.Style(width="100%", padding="8px", marginBottom="10px"),
                            disabled=is_readonly
                        ),
                    )
                    for field_name, label_text in row
                ]
            )
            for row in form_fields
        ]

        def handle_submit(e):
            form_data = dict(e["formData"])
            db["save"]([form_data])

        def handle_save_changes(e):
            form_data = dict(e["formData"])
            form_data[id_col] = existing_id
            db["update"]([form_data])
            set_edit_mode(False)

        return lib.bs.Container(
            lib.html.h2(page_title),

            lib.html.div(
                style=lib.Style(display="flex", gap="10px", marginBottom="15px")
            )(
                lib.bs.Button(
                    variant="warning",
                    onClick=lambda e: set_edit_mode(not edit_mode),
                    disabled=db["is_loading"]
                )("✏️ Edit" if not edit_mode else "⏹️ Cancel Edit"),
                lib.bs.Button(
                    variant="danger",
                    onClick=lambda e: set_delete_confirm_open(True)
                )("🗑️ Delete"),
                lib.bs.Button(
                    variant="secondary",
                    onClick=lambda e: set_view_mode("list")
                )("← Back to List"),
            ) if existing_id else lib.html.div(),

            status_alerts(
                lib,
                submit_success=db["submit_success"],
                success_message=db["success_message"],
                error_message=db["error_message"],
                extra_detail="Record saved and data table updated" if db["submit_success"] else None,
            ),

            lib.bs.Modal(
                show=delete_confirm_open and existing_id is not None,
                onHide=lambda: set_delete_confirm_open(False)
            )(
                lib.bs.ModalHeader()("Confirm Delete?"),
                lib.bs.ModalBody()("Delete this record? This cannot be undone."),
                lib.bs.ModalFooter()(
                    lib.bs.Button(
                        variant="secondary",
                        onClick=lambda e: set_delete_confirm_open(False)
                    )("Cancel"),
                    lib.bs.Button(
                        variant="danger",
                        disabled=db["is_loading"],
                        onClick=lambda e: (
                            db["delete"](existing_id),
                            set_delete_confirm_open(False),
                            set_view_mode("list"),
                            set_selected_rows(set()),
                        )
                    )("Delete")
                )
            ) if existing_id else None,

            lib.bs.Form(
                key=f"{db['form_key']}-{existing_id}-{edit_mode}",
                onSubmit=handle_save_changes if existing_id else handle_submit
            )(
                *form_rows,

                extra_form_content(lib, existing_id, form_edit_mode) if extra_form_content else None,

                lib.bs.Button(
                    type="submit",
                    variant="primary",
                    size="lg",
                    disabled=db["is_loading"] or is_readonly,
                    style=lib.Style(
                        opacity="0.7" if db["is_loading"] else "1",
                        cursor="not-allowed" if db["is_loading"] or is_readonly else "pointer",
                        width="220px", padding="12px 24px",
                        fontSize="16px", fontWeight="600"
                    )
                )(
                    lib.html.span(className="spinner")("⟳ ") if db["is_loading"] else (
                        "💾 " if existing_id else "📤 "
                    ),
                    "Saving..." if db["is_loading"] and existing_id else
                    "Submitting..." if db["is_loading"] else
                    "Save Changes" if existing_id else
                    "Submit Form"
                ) if not is_readonly else None,
            ),
        )

    def TabView():
        return lib.html.div()(
            lib.html.style()(SHARED_CSS),
            lib.tabs.Tabs(
                lib.tabs.TabList(
                    lib.tabs.Tab("Add Data"),
                    lib.tabs.Tab("View Data"),
                ),
                lib.tabs.TabPanel(FormView()),
                lib.tabs.TabPanel(
                    lib.html.div(style=lib.Style(padding="20px"))(
                        lib.html.h2(f"📊 {page_title} — All Submissions"),
                        status_alerts(
                            lib,
                            submit_success=db["submit_success"],
                            success_message=db["success_message"],
                            error_message=db["error_message"],
                            extra_detail=(
                                f"Total records: {len(db['displayed_data'])}"
                                if db["submit_success"] else None
                            ),
                        ),
                        SummaryTable()
                    ) if view_mode != "detail" else FormView(selected_record_id, form_edit_mode=edit_mode)
                ),
            ),
        )

    return SummaryTable, FormView, TabView


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@App.page
def home(lib):
    return lib.tethys.Display(lib.tethys.Map())


# ── Map Location ────────────────────────────────────────────────────────────

@App.page
def map_location(lib):
    lib.register(
        "sketch_canvas.js", "sc",
        host="/static/component_playground/js",
        default_export="SketchCanvas"
    )
    lib.register(
        "react-tabs", "tabs",
        styles=["https://esm.sh/react-tabs@6.1.0/style/react-tabs.css"]
    )

    resources  = lib.hooks.use_resources()
    db_fpath   = resources.path / "map_location.sqlite"
    table_name = "Map_Location"
    db = use_db_state(lib, db_fpath, table_name)

    color, set_color = lib.hooks.use_state("#100a0a")
    width, set_width = lib.hooks.use_state(4)

    form_fields = [
        [("village", "Village"), ("ves_no", "VES No."), ("map_sheet_no", "Map Sheet No."), ("mapped_by", "Mapped By")],
        [("parish", "Parish"), ("subcounty", "Sub-County"), ("county", "County"), ("district", "District")],
        [("grid_east", "Grid East"), ("grid_north", "Grid North"), ("altitude", "Altitude")],
        [("village_code", "Village Code"), ("date_of_survey", "Date of Survey"), ("source_name_2", "Source Name")],
        [("proposed_type_of_water_source", "Proposed Type of Water Source")],
        [("expected_depth_to_rock_m", "Expected Depth to Rock (m)"), ("expected_depth_to_water_m", "Expected Depth to Water (m)")],
        [("expected_formation", "Expected Formation")],
        [("expected_borehole_depth_m", "Expected Borehole Depth (m)"), ("accessibility_to_site", "Accessibility to Site")],
        [("expected_depth_to_screen_m", "Expected Depth to Screen (m)")],
    ]

    summary_cols = [
        ("village",    "Village"),
        ("mapped_by",  "Mapped By"),
    ]

    def sketch_content(lib, existing_id, form_edit_mode):
        selected_record_data = None
        if db["displayed_data"] and existing_id:
            selected_record_data = next(
                (r for r in db["displayed_data"] if str(r.get("created_at")) == str(existing_id)),
                None
            )
        existing_sketch = selected_record_data.get("sketch", "") if selected_record_data else ""
        is_editable = form_edit_mode

        return lib.html.div(style=lib.Style(padding="20px"))(
            lib.html.h1("LOCATION MAP"),
            lib.bs.Row(
                lib.bs.Col(
                    lib.html.label("Draw Color:"),
                    lib.html.input(
                        type="color", value=color,
                        onChange=lambda e: set_color(e.target.value),
                        style=lib.Style(marginRight="10px"),
                        disabled=not is_editable
                    ),
                    lib.html.label("Brush Width:"),
                    lib.html.input(
                        type="range", min="1", max="10", value=width,
                        onChange=lambda e: set_width(int(e.target.value)),
                        disabled=not is_editable
                    ),
                ) if is_editable else lib.bs.Col(),
            ),
            lib.bs.Row(
                lib.bs.Col(
                    lib.sc.SketchCanvas(
                        name="sketch",
                        style=lib.Style(
                            border="0.0625rem solid #9c9c9c",
                            borderRadius="0.25rem",
                            width="100%", height="500px"
                        ),
                        width="100%", height="500px",
                        strokeWidth=width, strokeColor=color,
                        backgroundImage=existing_sketch if existing_id else ""
                    ) if is_editable else lib.html.img(
                        src=existing_sketch,
                        style=lib.Style(width="100%", border="1px solid black")
                    )
                ),
            ),
        )

    _, _, TabView = make_record_manager(
        lib, db,
        form_fields=form_fields,
        summary_cols=summary_cols,
        page_title="Map Location Survey Form",
        extra_form_content=sketch_content,
    )
    return TabView()


# ── VES Form ─────────────────────────────────────────────────────────────────

@App.page
def VES_FORM(lib):
    lib.register(
        "react-tabs", "tabs",
        styles=["https://esm.sh/react-tabs@6.1.0/style/react-tabs.css"]
    )

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

    summary_cols = [
        ("Project_Name", "Project Name"),
        ("Area",         "Area"),
        ("Date",         "Date"),
    ]

    def ves_extra(lib, existing_id, form_edit_mode):
        return lib.html.div()(
            lib.html.div(
                style=lib.Style(backgroundColor="white", padding="20px", borderRadius="8px", marginTop="20px")
            )(
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
            lib.html.div(
                style=lib.Style(backgroundColor="white", padding="20px", borderRadius="8px", marginTop="20px")
            )(
                lib.html.h3("Reading vs Station"),
                lib.tethys.Chart(
                    data=row_data_1, height=500, width=900,
                    x_label="Station", y_label="Reading",
                    x_attr="station", y_attr="reading"
                ),
            ),
        )

    _, _, TabView = make_record_manager(
        lib, db,
        form_fields=form_fields,
        summary_cols=summary_cols,
        page_title="VES FORM — Vertical Electrical Sounding",
        extra_form_content=ves_extra,
    )
    return TabView()


# ── Resistivity Survey ────────────────────────────────────────────────────────

@App.page
def resistivity_survey_form(lib):
    lib.register(
        "react-tabs", "tabs",
        styles=["https://esm.sh/react-tabs@6.1.0/style/react-tabs.css"]
    )

    resources  = lib.hooks.use_resources()
    db_fpath   = resources.path / "resistivity_survey.sqlite"
    table_name = "resistivity_survey"
    db = use_db_state(lib, db_fpath, table_name)

    log_spacings = [
        1, 2.1, 3.0, 4.4, 6.3, 9.1, 13.2, 13.2, 19.0, 19.0,
        27.5, 27.5, 40, 58, 58, 83, 83, 120, 120, 175, 250, 375, 525, 750
    ]

    survey_data, set_survey_data = lib.hooks.use_state({
        "location_point": "",
        "mn2_value": "0.5",
        "readings": [
            {"spacing": s, "reading_1": "", "reading_2": "", "average": "", "notes": ""}
            for s in log_spacings
        ]
    })

    form_fields = [
        [("location_point", "Location Point (Site ID)")],
    ]

    summary_cols = [
        ("location_point", "Location Point"),
        ("mn2_value",      "MN/2"),
    ]

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
                        disabled=is_readonly
                    )(
                        lib.html.option(value="0.5")("0.5 m"),
                        lib.html.option(value="5.0")("5.0 m"),
                        lib.html.option(value="25")("25 m"),
                    ),
                ),
            ),
            lib.html.div(style=lib.Style(display="flex", gap="20px", marginBottom="20px"))(
                lib.html.div(style=lib.Style(
                    flex="0 0 550px", border="1px solid #999",
                    padding="10px", backgroundColor="#f9f9f9"
                ))(
                    lib.html.table(
                        style=lib.Style(width="100%", borderCollapse="collapse", fontSize="12px")
                    )(
                        lib.html.thead()(
                            lib.html.tr(
                                style=lib.Style(backgroundColor="#ddd", borderBottom="2px solid #999")
                            )(
                                lib.html.th(style=lib.Style(padding="8px", border="1px solid #999"))("AB/2 (m)"),
                                lib.html.th(style=lib.Style(padding="8px", border="1px solid #999"))("Count"),
                                lib.html.th(style=lib.Style(padding="8px", border="1px solid #999"))("R (Ω)"),
                                lib.html.th(style=lib.Style(padding="8px", border="1px solid #999"))("Notes"),
                            ),
                        ),
                        lib.html.tbody()(
                            *[
                                lib.html.tr(
                                    style=lib.Style(
                                        borderBottom="1px solid #ddd",
                                        backgroundColor="#fff" if i % 2 == 0 else "#f5f5f5"
                                    )
                                )(
                                    lib.html.td(style=lib.Style(padding="6px", border="1px solid #ddd", fontWeight="bold"))(str(spacing)),
                                    lib.html.td(style=lib.Style(padding="6px", border="1px solid #ddd", textAlign="center", fontWeight="bold"))(str(idx + 1)),
                                    lib.html.td(style=lib.Style(padding="4px", border="1px solid #ddd"))(
                                        lib.html.input(
                                            type="number",
                                            value=survey_data["readings"][idx]["reading_2"],
                                            style=lib.Style(width="90%", padding="4px"),
                                            placeholder="0.0",
                                            disabled=is_readonly,
                                            onChange=lambda e, i=idx: update_reading(i, "reading_2", e.target.value)
                                        ),
                                    ),
                                    lib.html.td(style=lib.Style(padding="4px", border="1px solid #ddd"))(
                                        lib.html.input(
                                            type="text",
                                            value=survey_data["readings"][idx]["notes"],
                                            style=lib.Style(width="90%", padding="4px"),
                                            placeholder="Layer",
                                            disabled=is_readonly,
                                            onChange=lambda e, i=idx: update_reading(i, "notes", e.target.value)
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
                            x_attr="resistivity", y_attr="depth"
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
        lib, db,
        form_fields=form_fields,
        summary_cols=summary_cols,
        page_title="Schlumberger Array VES Survey",
        extra_form_content=resistivity_extra,
    )
    return TabView()


# ---------------------------------------------------------------------------
# Gemini rock analysis
# ---------------------------------------------------------------------------

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
        req = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
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
    lib.register(
        "react-tabs", "tabs",
        styles=["https://esm.sh/react-tabs@6.1.0/style/react-tabs.css"]
    )

    # ── State ──────────────────────────────────────────────────────────────
    processing,       set_processing       = lib.hooks.use_state(False)
    analysis_results, set_analysis_results = lib.hooks.use_state(None)  # Gemini text
    image,            set_image            = lib.hooks.use_state(None)   # data-URL

    # Archive navigation state
    archive_view,     set_archive_view     = lib.hooks.use_state("list")      # "list" | "detail"
    selected_record,  set_selected_record  = lib.hooks.use_state(None)        # full record dict
    selected_rows,    set_selected_rows    = lib.hooks.use_state(set())
    delete_confirm,   set_delete_confirm   = lib.hooks.use_state(False)

    gemini_api_key = lib.hooks.use_setting("GEMINI_API_KEY")

    # ── DB ─────────────────────────────────────────────────────────────────
    resources  = lib.hooks.use_resources()
    db_fpath   = resources.path / "image_analysis.sqlite"
    table_name = "Image_Analysis"
    db = use_db_state(lib, db_fpath, table_name)

    # ── Upload & analyse ───────────────────────────────────────────────────
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

    # ── Save: formation + village from form, image + analysis from state ───
    def handle_save_analysis(e):
        form_data  = dict(e["formData"])
        db["save"]([{
            "village":   form_data.get("village", ""),
            "formation": form_data.get("formation", ""),
            "image":     image,             # data-URL already in state
            "analysis":  analysis_results,  # Gemini text already in state
        }])

    # ── Archive helpers ────────────────────────────────────────────────────
    def toggle_row(rid):
        new_sel = set(selected_rows)
        if rid in new_sel:
            new_sel.discard(rid)
        else:
            new_sel.add(rid)
        set_selected_rows(new_sel)

    def open_detail():
        if len(selected_rows) == 1:
            rid = list(selected_rows)[0]
            record = next(
                (r for r in db["displayed_data"] if str(r.get("created_at")) == str(rid)),
                None
            )
            if record:
                set_selected_record(record)
                set_archive_view("detail")

    def do_delete():
        for rid in list(selected_rows):
            db["delete"](rid)
        set_selected_rows(set())
        set_delete_confirm(False)

    # ── Archive list view ──────────────────────────────────────────────────
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
                        style=lib.Style(cursor="pointer", width="16px", height="16px")
                    )
                ),
                lib.html.td(style=lib.Style(padding="10px"))(str(record.get("village", "—"))),
                lib.html.td(style=lib.Style(padding="10px"))(str(record.get("formation", "—"))),
                lib.html.td(style=lib.Style(padding="10px", maxWidth="300px", overflow="hidden",
                                             whiteSpace="nowrap", textOverflow="ellipsis"))(
                    str(record.get("analysis", "—"))[:80] + "…"
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
            # Toolbar
            lib.html.div(style=lib.Style(display="flex", gap="10px", marginBottom="15px",
                                          alignItems="center"))(
                lib.html.span(style=lib.Style(fontSize="13px", color="#666"))(
                    f"💡 {len(data)} record(s) | {len(selected_rows)} selected"
                ),
                lib.bs.Button(
                    variant="info", size="sm",
                    onClick=lambda e: open_detail(),
                    disabled=len(selected_rows) != 1,
                    style=lib.Style(marginLeft="auto")
                )("👁️ View"),
                lib.bs.Button(
                    variant="danger", size="sm",
                    onClick=lambda e: set_delete_confirm(True),
                    disabled=len(selected_rows) == 0
                )(f"🗑️ Delete ({len(selected_rows)})"),
            ),

            # Delete confirmation modal
            lib.bs.Modal(
                show=delete_confirm,
                onHide=lambda: set_delete_confirm(False)
            )(
                lib.bs.ModalHeader()("Confirm Delete"),
                lib.bs.ModalBody()(f"Delete {len(selected_rows)} record(s)? This cannot be undone."),
                lib.bs.ModalFooter()(
                    lib.bs.Button(variant="secondary",
                                  onClick=lambda e: set_delete_confirm(False))("Cancel"),
                    lib.bs.Button(variant="danger",
                                  disabled=db["is_loading"],
                                  onClick=lambda e: do_delete())("Delete"),
                )
            ),

            # Table
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

    # ── Archive detail view ────────────────────────────────────────────────
    def ArchiveDetail():
        rec = selected_record
        if not rec:
            return lib.html.div()("No record selected.")

        return lib.html.div(style=lib.Style(padding="20px", maxWidth="860px"))(
            # Toolbar
            lib.html.div(style=lib.Style(display="flex", gap="10px", marginBottom="20px",
                                          alignItems="center"))(
                lib.bs.Button(
                    variant="secondary",
                    onClick=lambda e: (set_archive_view("list"), set_selected_record(None))
                )("← Back to Archive"),
                lib.bs.Button(
                    variant="danger",
                    onClick=lambda e: set_delete_confirm(True)
                )("🗑️ Delete This Record"),
            ),

            # Delete confirmation modal (detail context)
            lib.bs.Modal(
                show=delete_confirm,
                onHide=lambda: set_delete_confirm(False)
            )(
                lib.bs.ModalHeader()("Confirm Delete"),
                lib.bs.ModalBody()("Delete this record? This cannot be undone."),
                lib.bs.ModalFooter()(
                    lib.bs.Button(variant="secondary",
                                  onClick=lambda e: set_delete_confirm(False))("Cancel"),
                    lib.bs.Button(
                        variant="danger",
                        disabled=db["is_loading"],
                        onClick=lambda e: (
                            db["delete"](rec.get("created_at")),
                            set_delete_confirm(False),
                            set_archive_view("list"),
                            set_selected_record(None),
                            set_selected_rows(set()),
                        )
                    )("Delete"),
                )
            ),

            lib.html.h2("🔬 Analysis Record"),

            # Metadata strip
            lib.html.div(
                style=lib.Style(
                    display="flex", gap="30px", flexWrap="wrap",
                    backgroundColor="#f8f9fa", padding="14px 18px",
                    borderRadius="6px", marginBottom="20px",
                    border="1px solid #dee2e6"
                )
            )(
                lib.html.div()(
                    lib.html.span(style=lib.Style(fontWeight="bold", color="#555",
                                                   fontSize="12px", textTransform="uppercase",
                                                   letterSpacing="0.5px"))("Village"),
                    lib.html.div(style=lib.Style(fontSize="16px", marginTop="4px"))(
                        str(rec.get("village", "—"))
                    ),
                ),
                lib.html.div()(
                    lib.html.span(style=lib.Style(fontWeight="bold", color="#555",
                                                   fontSize="12px", textTransform="uppercase",
                                                   letterSpacing="0.5px"))("Formation"),
                    lib.html.div(style=lib.Style(fontSize="16px", marginTop="4px"))(
                        str(rec.get("formation", "—"))
                    ),
                ),
                lib.html.div()(
                    lib.html.span(style=lib.Style(fontWeight="bold", color="#555",
                                                   fontSize="12px", textTransform="uppercase",
                                                   letterSpacing="0.5px"))("Saved At"),
                    lib.html.div(style=lib.Style(fontSize="14px", color="#666", marginTop="4px"))(
                        str(rec.get("created_at", "—"))
                    ),
                ),
            ),

            # Image + Analysis side by side
            lib.html.div(
                style=lib.Style(display="flex", gap="24px", flexWrap="wrap", alignItems="flex-start")
            )(
                # Image panel
                lib.html.div(style=lib.Style(flex="0 0 380px", minWidth="260px"))(
                    lib.html.h5(style=lib.Style(marginBottom="10px", color="#333"))("📷 Rock Image"),
                    lib.html.img(
                        src=rec.get("image", ""),
                        style=lib.Style(
                            width="100%", borderRadius="6px",
                            border="1px solid #ccc", display="block"
                        )
                    ) if rec.get("image") else lib.html.div(
                        style=lib.Style(padding="40px", textAlign="center",
                                         color="#999", border="1px dashed #ccc",
                                         borderRadius="6px")
                    )("No image stored"),
                ),

                # Analysis panel
                lib.html.div(
                    style=lib.Style(
                        flex="1", minWidth="260px",
                        backgroundColor="#fff", border="1px solid #dee2e6",
                        borderRadius="6px", padding="18px"
                    )
                )(
                    lib.html.h5(style=lib.Style(marginBottom="12px", color="#333"))("🤖 Gemini Analysis"),
                    lib.md.Markdown(rec.get("analysis", "*No analysis text stored.*")),
                ),
            ),
        )

    # ── Page render ────────────────────────────────────────────────────────
    return lib.tethys.Display(
        lib.html.div()(
            lib.html.style()(SHARED_CSS),
            lib.tabs.Tabs(
                lib.tabs.TabList(
                    lib.tabs.Tab("Perform Analysis"),
                    lib.tabs.Tab("Analysis Archive"),
                ),

                # ── Tab 1: Upload & analyse ────────────────────────────────
                lib.tabs.TabPanel(
                    lib.html.div(
                        style=lib.Style(
                            padding="20px", maxWidth="800px",
                            margin="0 auto", fontFamily="Arial, sans-serif"
                        )
                    )(
                        lib.html.h1("Rock Identifier with Gemini AI"),

                        # Upload form (shown when no result yet)
                        lib.lo.LoadingOverlay(active=processing, spinner=True)(
                            lib.bs.Form(
                                onSubmit=event(handle_file_upload,
                                               prevent_default=True, stop_propagation=True)
                            )(
                                lib.html.h3("Upload Image"),
                                lib.html.input(
                                    key=id(analysis_results), type="file",
                                    name="upload", accept="image/*"
                                ),
                                lib.html.button(type="submit")("Analyze File"),
                            )
                        ) if not analysis_results else

                        # Results + save form (shown after Gemini responds)
                        lib.bs.Form(
                            onSubmit=handle_save_analysis,
                        )(
                            lib.bs.Button(
                                variant="outline-secondary",
                                style=lib.Style(marginBottom="16px"),
                                onClick=lambda e: (
                                    set_analysis_results(None),
                                    set_image(None)
                                )
                            )("← Analyze Another"),

                            lib.html.h3("Analysis Result"),
                            lib.html.img(
                                src=image,
                                style=lib.Style(maxWidth="100%", marginTop="10px",
                                                borderRadius="6px", border="1px solid #ccc")
                            ),
                            lib.html.hr(),

                            # Gemini text displayed read-only
                            lib.html.div(
                                style=lib.Style(
                                    backgroundColor="#f8f9fa", border="1px solid #dee2e6",
                                    borderRadius="6px", padding="16px", marginBottom="20px"
                                )
                            )(
                                lib.html.h5(style=lib.Style(color="#333", marginBottom="8px"))(
                                    "🤖 Gemini Analysis"
                                ),
                                lib.md.Markdown(analysis_results),
                            ),

                            # Village + Formation inputs side by side
                            lib.bs.Row(style=lib.Style(marginBottom="16px"))(
                                lib.bs.Col()(
                                    lib.html.label(
                                        style=lib.Style(display="block", fontWeight="bold",
                                                        marginBottom="5px", fontSize="14px"),
                                        for_="village"
                                    )("Village:"),
                                    lib.html.input(
                                        name="village",
                                        type="text",
                                        className="form-control",
                                        placeholder="Enter village name",
                                        style=lib.Style(width="100%", padding="8px"),
                                    ),
                                ),
                                lib.bs.Col()(
                                    lib.html.label(
                                        style=lib.Style(display="block", fontWeight="bold",
                                                        marginBottom="5px", fontSize="14px"),
                                        for_="formation"
                                    )("Formation:"),
                                    lib.html.input(
                                        name="formation",
                                        type="text",
                                        className="form-control",
                                        placeholder="Enter formation type",
                                        style=lib.Style(width="100%", padding="8px"),
                                    ),
                                ),
                            ),

                            status_alerts(
                                lib,
                                submit_success=db["submit_success"],
                                success_message=db["success_message"],
                                error_message=db["error_message"],
                            ),

                            lib.bs.Button(
                                type="submit",
                                variant="primary",
                                size="lg",
                                disabled=db["is_loading"],
                                style=lib.Style(
                                    opacity="0.7" if db["is_loading"] else "1",
                                    cursor="not-allowed" if db["is_loading"] else "pointer",
                                    width="220px", padding="12px 24px",
                                    fontSize="16px", fontWeight="600",
                                    marginTop="8px"
                                )
                            )(
                                lib.html.span(className="spinner")("⟳ ") if db["is_loading"] else "💾 ",
                                "Saving…" if db["is_loading"] else "Save Analysis"
                            ),
                        ),  # closes Form()(...)
                    ),
                ),

                # ── Tab 2: Archive (list or detail) ───────────────────────
                lib.tabs.TabPanel(
                    lib.html.div(style=lib.Style(padding="20px"))(
                        lib.html.h2("📊 Analysis Archive"),
                        status_alerts(
                            lib,
                            submit_success=db["submit_success"],
                            success_message=db["success_message"],
                            error_message=db["error_message"],
                        ),
                        ArchiveList() if archive_view == "list" else ArchiveDetail(),
                    )
                ),
            ),
        )
    )