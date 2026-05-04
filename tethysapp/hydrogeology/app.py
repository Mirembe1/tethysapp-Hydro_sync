import re

from tethys_sdk.components import ComponentBase
from tethys_sdk.components.utils import event, component
from time import sleep
from urllib.request import urlopen, Request
from tethys_sdk.app_settings import SecretCustomSetting
import pandas as pd
import sqlite3
from uuid import uuid4
import json
import base64
from datetime import datetime



class App(ComponentBase):
    """
    Tethys app class for Hydro sync.
    """

    name = "Hydro sync"
    description = "Field Assistant"
    package = "hydrogeology"  # WARNING: Do not change this value
    index = "home"
    icon = f"{package}/images/icon.png"
    root_url = "hydrogeology"
    color = "#109cf9"
    tags = "GIS","hydrogeology"
    enable_feedback = False
    feedback_emails = []
    exit_url = "/apps/"
    default_layout = "NavHeader"
    nav_links = "auto"

    def custom_settings(self):
        """
        Define custom settings for the app, including Gemini API key
        """
        return (
            SecretCustomSetting(
                name="GEMINI_API_KEY",
                description="API key for Google Gemini API",
                required=True,
            ),
        )

@App.page
def home(lib):
    return lib.tethys.Display(
        lib.tethys.Map()
    )

def _safe_identifier(name):
    """Sanitize field names to be SQL-safe"""
    sanitized = re.sub(r'[^a-zA-Z0-9_]', '_', str(name))
    if not re.match(r'^[a-zA-Z_]', sanitized):
        sanitized = '_' + sanitized
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", sanitized):
        raise ValueError(f"Invalid SQL identifier: {sanitized}")
    return sanitized


def _resolve_page_decorator():
    """Resolve the page decorator - returns App.page if available"""
    app_obj = globals().get("App")
    return app_obj.page if app_obj and hasattr(app_obj, "page") else (lambda func: func)


def delete_record_from_sqlite(db_fpath, table_name, record_id, id_col="created_at"):
    """Delete a record from SQLite based on id_col"""
    table_name = _safe_identifier(table_name)
    id_col = _safe_identifier(id_col)
    
    conn = sqlite3.connect(str(db_fpath))
    cursor = conn.cursor()
    
    try:
        delete_sql = f'DELETE FROM "{table_name}" WHERE "{id_col}" = ?'
        cursor.execute(delete_sql, (record_id,))
        conn.commit()
        print(f"✓ Record with {id_col}={record_id} deleted from {table_name}")
        
    except Exception as e:
        conn.rollback()
        print(f"✗ Delete Error: {e}")
        raise
    finally:
        conn.close()

def update_data_in_sqlite(db_fpath, table_name, data, id_col="created_at"):
    """Update existing records in SQLite based on id_col"""
    table_name = _safe_identifier(table_name)
    id_col = _safe_identifier(id_col)
    
    if not data or len(data) == 0:
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
            
            update_sql = f'UPDATE "{table_name}" SET {set_clause} WHERE "{id_col}" = ?'
            cursor.execute(update_sql, values)
        
        conn.commit()
        print(f"✓ Data updated in {table_name}")
        
    except Exception as e:
        conn.rollback()
        print(f"✗ Update Error: {e}")
        raise
    finally:
        conn.close()


def data_to_sqlite(db_fpath, table_name, data):
    """Save data to SQLite with dynamic schema management"""
    table_name = _safe_identifier(table_name)
    
    if not data or len(data) == 0:
        return
    
    conn = sqlite3.connect(str(db_fpath))
    cursor = conn.cursor()
    
    try:
        first_row = data[0]
        
        columns_orig = list(first_row.keys())
        columns_safe = [_safe_identifier(key) for key in columns_orig]
        columns_safe.append("created_at")
        
        cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
        table_exists = cursor.fetchone()
        
        if not table_exists:
            col_defs = ", ".join([f'"{col}" TEXT' for col in columns_safe])
            create_sql = f'CREATE TABLE "{table_name}" ({col_defs}, id INTEGER PRIMARY KEY AUTOINCREMENT)'
            cursor.execute(create_sql)
            print(f"✓ Created table: {table_name}")
        else:
            cursor.execute(f"PRAGMA table_info({table_name})")
            existing_cols = {row[1] for row in cursor.fetchall()}
            
            for col in columns_safe:
                if col not in existing_cols:
                    try:
                        cursor.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{col}" TEXT')
                        print(f"✓ Added column: {col}")
                    except sqlite3.OperationalError as oe:
                        print(f"Column {col} already exists or error: {oe}")
        
        for row in data:
            values = [str(row.get(orig_col, "")) if row.get(orig_col) is not None else "" for orig_col in columns_orig]
            values.append(pd.Timestamp.now().isoformat())
            
            placeholders = ", ".join(["?" for _ in values])
            col_names = ", ".join([f'"{col}"' for col in columns_safe])
            insert_sql = f'INSERT INTO "{table_name}" ({col_names}) VALUES ({placeholders})'
            
            cursor.execute(insert_sql, values)
        
        conn.commit()
        print(f"✓ Data saved to {table_name}")
        
    except Exception as e:
        conn.rollback()
        print(f"✗ Save Error: {e}")
        raise
    finally:
        conn.close()


def data_from_sqlite(db_fpath, table_name):
    """Retrieve all data from SQLite table ordered by created_at DESC"""
    table_name = _safe_identifier(table_name)
    if not db_fpath.exists():
        return []
    conn = sqlite3.connect(str(db_fpath))
    try:
        df = pd.read_sql_query(f'SELECT * FROM "{table_name}" ORDER BY created_at DESC', conn)
        records = df.to_dict("records")
        print(f"✓ Retrieved {len(records)} records from {table_name}")
        return records
    except Exception as e:
        print(f"Error reading from database: {e}")
        return []
    finally:
        conn.close()


# NOW DEFINE THE PAGE DECORATOR USAGE
@_resolve_page_decorator()
def map_location(lib):
    displayed_data, set_displayed_data = lib.hooks.use_state([])
    submit_success, set_submit_success = lib.hooks.use_state(None)
    success_message, set_success_message = lib.hooks.use_state("")
    error_message, set_error_message = lib.hooks.use_state(None)
    form_key, set_form_key = lib.hooks.use_state(str(uuid4()))
    is_loading, set_is_loading = lib.hooks.use_state(False)
    data_loaded, set_data_loaded = lib.hooks.use_state(False)
    view_mode, set_view_mode = lib.hooks.use_state("list")  # "list" or "detail"
    selected_record_id, set_selected_record_id = lib.hooks.use_state(None)

    resources = lib.hooks.use_resources()
    db_fpath = resources.path / "map_location.sqlite"
    table_name = "Map_Location"

    def auto_load_data():
        if not data_loaded:
            try:
                data = data_from_sqlite(db_fpath, table_name)
                if data:
                    set_displayed_data(data)
                    print("✓ Map Location data auto-loaded on initialization")
                set_data_loaded(True)
            except Exception as err:
                print(f"Auto-load error: {err}")
                set_data_loaded(True)

    lib.hooks.use_effect(auto_load_data, [])

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

    form_rows = []
    for row in form_fields:
        row_elements = []
        for field_name, label_text in row:
            row_elements.append(
                lib.bs.Col()(
                    lib.html.label(
                        style=lib.Style(display="block", fontWeight="bold", marginBottom="5px", fontSize="14px"),
                        for_=field_name,
                    )(f"{label_text}:"),
                    lib.html.input(
                        name=field_name,
                        type="text",
                        className="form-control",
                        style=lib.Style(width="100%", padding="8px", marginBottom="10px"),
                    ),
                )
            )
        form_rows.append(lib.bs.Row()(*row_elements))

    def handle_submit(e):
        if is_loading:
            return
        
        set_is_loading(True)
        set_error_message(None)
        set_submit_success(None)
        try:
            form_data = e["formData"]
            data_to_sqlite(db_fpath, table_name, [form_data])
            
            now = datetime.now()
            timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
            
            set_submit_success(True)
            set_success_message(f"✓ Form submitted successfully at {timestamp}")
            
            set_form_key(str(uuid4()))
            
            new_data = data_from_sqlite(db_fpath, table_name)
            set_displayed_data(new_data)
            
            lib.utils.background_execute(
                lambda: set_submit_success(None), 
                delay_seconds=4
            )
        except Exception as err:
            print(f"Submit error: {err}")
            set_error_message(f"❌ Error: {str(err)[:100]}")
            set_submit_success(False)
        finally:
            set_is_loading(False)

    lib.register("sketch_canvas.js", "sc", host="/static/component_playground/js", default_export="SketchCanvas")
    lib.register("react-tabs", "tabs", styles=["https://esm.sh/react-tabs@6.1.0/style/react-tabs.css"])

    color, set_color = lib.hooks.use_state("#100a0a")
    width, set_width = lib.hooks.use_state(4)
    
    # DETAIL VIEW PAGE
    def render_detail_view():
        record = next((r for r in displayed_data if str(r.get("created_at")) == str(selected_record_id)), None)
        
        if not record:
            return lib.html.div(
                style=lib.Style(padding="20px", textAlign="center")
            )(
                lib.html.h3("Record not found"),
                lib.bs.Button(onClick=lambda e: set_view_mode("list"))("← Back to List")
            )
        
        sketch_data = record.get("sketch", "")
        
        return lib.html.div(
            style=lib.Style(padding="20px", maxWidth="1200px", margin="0 auto")
        )(
            lib.html.div(
                style=lib.Style(display="flex", justifyContent="space-between", alignItems="center", marginBottom="20px")
            )(
                lib.html.h2(f"🔍 Survey Details - {record.get('village', 'N/A')}"),
                lib.bs.Button(
                    variant="secondary",
                    onClick=lambda e: set_view_mode("list")
                )("← Back to List")
            ),
            
            lib.html.hr(),
            
            lib.html.div(
                style=lib.Style(backgroundColor="white", padding="20px", borderRadius="8px", marginBottom="20px", border="1px solid #ddd")
            )(
                lib.html.h3("📋 Survey Information"),
                
                lib.html.div(
                    style=lib.Style(display="grid", gridTemplateColumns="repeat(2, 1fr)", gap="15px")
                )(
                    *[
                        lib.html.div(style=lib.Style(padding="12px", backgroundColor="#f9f9f9", borderRadius="4px", border="1px solid #eee"))(
                            lib.html.strong(style=lib.Style(display="block", marginBottom="5px", color="#333", fontSize="13px"))(
                                f"{key.replace('_', ' ').title()}:"
                            ),
                            lib.html.span(style=lib.Style(color="#666", fontSize="14px"))(
                                str(value) if value else "—"
                            )
                        )
                        for key, value in record.items() 
                        if key not in ["sketch", "created_at", "id"] and value
                    ]
                )
            ),
            
            lib.html.div(
                style=lib.Style(backgroundColor="white", padding="20px", borderRadius="8px", border="1px solid #ddd", marginBottom="20px")
            )(
                lib.html.h3("🎨 Location Map Sketch"),
                lib.html.div(
                    style=lib.Style(
                        width="100%",
                        height="500px",
                        border="2px solid #ddd",
                        borderRadius="4px",
                        overflow="hidden",
                        backgroundColor="#f5f5f5"
                    )
                )(
                    lib.html.img(
                        src=sketch_data,
                        style=lib.Style(
                            width="100%",
                            height="100%",
                            objectFit="contain"
                        ),
                        alt="Location Map Sketch"
                    ) if sketch_data else lib.html.div(
                        style=lib.Style(
                            display="flex",
                            alignItems="center",
                            justifyContent="center",
                            height="100%",
                            color="#999",
                            fontSize="16px"
                        )
                    )("📭 No sketch available")
                )
            ),
            
            lib.html.div(
                style=lib.Style(
                    padding="12px",
                    backgroundColor="#f0f0f0",
                    borderRadius="4px",
                    fontSize="12px",
                    color="#666"
                )
            )(
                lib.html.p()(
                    f"📅 Submitted: {record.get('created_at', 'N/A')}"
                )
            ),
        )
    
    # SUMMARY TABLE VIEW
    def render_summary_table():
        if not displayed_data or len(displayed_data) == 0:
            return lib.html.div(
                style=lib.Style(
                    padding="20px",
                    textAlign="center",
                    color="#999",
                    fontSize="16px"
                )
            )("📭 No data submitted yet. Submit a form to see data here.")
        
        table_rows = []
        for record in displayed_data:
            record_id = record.get("created_at")
            table_rows.append(
                lib.html.tr(
                    style=lib.Style(
                        borderBottom="1px solid #ddd",
                        backgroundColor="#fff"
                    )
                )(
                    lib.html.td(style=lib.Style(padding="12px", borderRight="1px solid #eee"))(
                        record.get("village", "—")
                    ),
                    lib.html.td(style=lib.Style(padding="12px", borderRight="1px solid #eee"))(
                        record.get("mapped_by", "—")
                    ),
                    lib.html.td(style=lib.Style(padding="12px", borderRight="1px solid #eee"))(
                        record.get("created_at", "—")
                    ),
                    lib.html.td(style=lib.Style(padding="12px"))(
                        lib.bs.Button(
                            variant="info",
                            size="sm",
                            onClick=lambda e, rec_id=record_id: (
                                set_selected_record_id(rec_id),
                                set_view_mode("detail")
                            )
                        )("👁️ View")
                    ),
                )
            )
        
        return lib.html.div(
            style=lib.Style(
                border="1px solid #ddd",
                borderRadius="4px",
                overflow="hidden",
                backgroundColor="white"
            )
        )(
            lib.html.p(style=lib.Style(fontSize="12px", color="#666", marginBottom="15px", padding="15px"))(
                f"💡 Total Records: {len(displayed_data)} | Click 'View' to see full details"
            ),
            lib.html.table(
                style=lib.Style(
                    width="100%",
                    borderCollapse="collapse"
                )
            )(
                lib.html.thead(
                    style=lib.Style(backgroundColor="#f5f5f5", borderBottom="2px solid #ddd")
                )(
                    lib.html.tr()(
                        lib.html.th(style=lib.Style(padding="12px", textAlign="left", fontWeight="bold", borderRight="1px solid #ddd"))("Village"),
                        lib.html.th(style=lib.Style(padding="12px", textAlign="left", fontWeight="bold", borderRight="1px solid #ddd"))("Mapped By"),
                        lib.html.th(style=lib.Style(padding="12px", textAlign="left", fontWeight="bold", borderRight="1px solid #ddd"))("Date"),
                        lib.html.th(style=lib.Style(padding="12px", textAlign="left", fontWeight="bold"))("Action"),
                    )
                ),
                lib.html.tbody()(
                    *table_rows
                )
            )
        )
    
    return lib.html.div()(
        lib.html.style()("""
            @keyframes spin {
                from { transform: rotate(0deg); }
                to { transform: rotate(360deg); }
            }
            .spinner {
                display: inline-block;
                animation: spin 1s linear infinite;
                margin-right: 8px;
            }
            @keyframes slideDown {
                from {
                    opacity: 0;
                    transform: translateY(-20px);
                }
                to {
                    opacity: 1;
                    transform: translateY(0);
                }
            }
            .success-alert {
                animation: slideDown 0.5s ease-out;
            }
        """),   
        lib.tabs.Tabs(
            lib.tabs.TabList(
                lib.tabs.Tab("Add Data"),
                lib.tabs.Tab("View Data")
            ),
            
            lib.tabs.TabPanel(
                lib.html.div(style=lib.Style(padding="20px"))(
                    lib.html.h2("Map Location Survey Form"),
                    
                    lib.bs.Alert(
                        variant="success",
                        className="success-alert",
                        style=lib.Style(
                            marginBottom="20px",
                            borderLeft="4px solid #28a745",
                            boxShadow="0 2px 4px rgba(40, 167, 69, 0.2)"
                        )
                    )(
                        lib.html.div(style=lib.Style(display="flex", alignItems="center", gap="10px"))(
                            lib.html.span(style=lib.Style(fontSize="20px"))("✓"),
                            lib.html.div()(
                                lib.html.strong(success_message),
                                lib.html.br(),
                                lib.html.small(style=lib.Style(color="#666"))(
                                    f"Record saved and data table updated"
                                ) if submit_success else None,
                            )
                        )
                    ) if submit_success else None,
                    
                    lib.bs.Alert(variant="danger")(error_message) if error_message else None,
                    
                    lib.bs.Form(key=form_key, onSubmit=handle_submit)(
                        lib.bs.Container()(
                            *form_rows,
                            lib.html.div(style=lib.Style(padding="20px"))(
                                lib.html.h1("LOCATION MAP"),
                                lib.bs.Row(
                                    lib.bs.Col(
                                        lib.html.label("Draw Color:"),
                                        lib.html.input(
                                            type="color",
                                            value=color,
                                            onChange=lambda e: set_color(e.target.value),
                                            style=lib.Style(marginRight="10px"),
                                        ),
                                        lib.html.label("Brush Width:"),
                                        lib.html.input(
                                            type="range",
                                            min="1",
                                            max="10",
                                            value=width,
                                            onChange=lambda e: set_width(int(e.target.value)),
                                        ),
                                    ),
                                ),
                                lib.bs.Row(
                                    lib.bs.Col(
                                        lib.sc.SketchCanvas(
                                            name="sketch",
                                            style=lib.Style(border="0.0625rem solid #9c9c9c", borderRadius="0.25rem", width="100%", height="500px"),
                                            width="100%",
                                            height="500px",
                                            strokeWidth=width,
                                            strokeColor=color,
                                        )
                                    ),
                                ),
                            ),
                            lib.bs.Button(
                                type="submit",
                                variant="primary",
                                size="lg",
                                disabled=is_loading,
                                style=lib.Style(
                                    opacity="0.7" if is_loading else "1",
                                    cursor="not-allowed" if is_loading else "pointer",
                                    width="200px",
                                    padding="12px 24px",
                                    fontSize="16px",
                                    fontWeight="600"
                                )
                            )(
                                lib.html.span(className="spinner")("⟳ ") if is_loading else "📤",
                                "Submitting..." if is_loading else "Submit Form"
                            )
                        ),
                    ),
                )
            ),
            lib.tabs.TabPanel(
                lib.html.div(style=lib.Style(padding="20px"))(
                    lib.html.h2("📊 Form Data - All Submissions"),
                    
                    lib.bs.Alert(
                        variant="success",
                        className="success-alert",
                        style=lib.Style(
                            marginBottom="20px",
                            borderLeft="4px solid #28a745",
                            boxShadow="0 2px 4px rgba(40, 167, 69, 0.2)"
                        )
                    )(
                        lib.html.div(style=lib.Style(display="flex", alignItems="center", gap="10px"))(
                            lib.html.span(style=lib.Style(fontSize="20px"))("✓"),
                            lib.html.div()(
                                lib.html.strong(success_message),
                                lib.html.br(),
                                lib.html.small(style=lib.Style(color="#666"))(
                                    f"Total records: {len(displayed_data)}"
                                ) if submit_success else None,
                            )
                        )
                    ) if submit_success else None,
                    
                    lib.bs.Alert(variant="danger")(error_message) if error_message else None,
                    
                    render_summary_table()
                ) if view_mode != "detail" else render_detail_view()
            ),
        ),
    )

@App.page
def VES_FORM(lib):
    lib.register('react-tabs', 'tabs', styles=['https://esm.sh/react-tabs@6.1.0/style/react-tabs.css'])
    
    # State management - EXACTLY LIKE MAP_LOCATION
    displayed_data, set_displayed_data = lib.hooks.use_state([])
    submit_success, set_submit_success = lib.hooks.use_state(None)
    success_message, set_success_message = lib.hooks.use_state("")
    error_message, set_error_message = lib.hooks.use_state(None)
    form_key, set_form_key = lib.hooks.use_state(str(uuid4()))
    is_loading, set_is_loading = lib.hooks.use_state(False)
    data_loaded, set_data_loaded = lib.hooks.use_state(False)
    
    row_data_1, set_row_data_1 = lib.hooks.use_state(
        [{"station": x, "reading": "", "apparent_resistivity": "", "remarks": ""} for x in range(21)]
    )
    
    resources = lib.hooks.use_resources()
    db_fpath = resources.path / "ves_survey_data.sqlite"
    table_name = "VES_FORM"

    # Auto-load data on component mount - EXACTLY LIKE MAP_LOCATION
    def auto_load_data():
        if not data_loaded:
            try:
                data = data_from_sqlite(db_fpath, table_name)
                if data:
                    set_displayed_data(data)
                    print(f"✓ VES Form data auto-loaded: {len(data)} records")
                set_data_loaded(True)
            except Exception as err:
                print(f"VES Form auto-load error: {err}")
                set_data_loaded(True)

    lib.hooks.use_effect(auto_load_data, [])
    
    form_fields = [
        [("Project_Name", "Project Name"), ("profile", "Profile")],
        [("Area", "Area"), ("Coordinates", "Coordinates")],
        [("Date", "Date"), ("Orientation", "Orientation")],
        [("Configuration", "Configuration"), ("Station_Interval", "Station Interval")],
        [("half_AB", "1/2 AB"), ("half_MN", "1/2 MN")],
    ]

    form_rows = []
    for row in form_fields:
        row_elements = []
        for field_id, label_text in row:
            row_elements.append(
                lib.html.div(style=lib.Style(display="flex", flexDirection="column", flex=1))(
                    lib.html.label(for_=field_id)(f"{label_text}:"),
                    lib.html.input(id=field_id, type="text", className="form-control", name=field_id),
                )
            )
        form_rows.append(
            lib.html.div(style=lib.Style(display="flex", gap="20px", marginBottom="15px"))(*row_elements)
        )

    def handle_submit(e):
        if is_loading:
            return
        
        set_is_loading(True)
        set_error_message(None)
        set_submit_success(None)
        try:
            form_data = e["formData"]
            data_to_sqlite(db_fpath, table_name, [form_data])
            
            # Get current timestamp
            now = datetime.now()
            timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
            
            # Set success feedback
            set_submit_success(True)
            set_success_message(f"✓ Form submitted successfully at {timestamp}")
            
            # Reset form
            set_form_key(str(uuid4()))
            
            # Auto-reload the View Data table
            new_data = data_from_sqlite(db_fpath, table_name)
            set_displayed_data(new_data)
            
            # Hide success message after 4 seconds
            lib.utils.background_execute(
                lambda: set_submit_success(None), 
                delay_seconds=4
            )
        except Exception as err:
            print(f"Submit error: {err}")
            set_error_message(f"❌ Error: {str(err)[:100]}")
            set_submit_success(False)
        finally:
            set_is_loading(False)

    return lib.html.div()(
        lib.html.style()("""
            @keyframes spin {
                from { transform: rotate(0deg); }
                to { transform: rotate(360deg); }
            }
            .spinner {
                display: inline-block;
                animation: spin 1s linear infinite;
                margin-right: 8px;
            }
            @keyframes slideDown {
                from {
                    opacity: 0;
                    transform: translateY(-20px);
                }
                to {
                    opacity: 1;
                    transform: translateY(0);
                }
            }
            .success-alert {
                animation: slideDown 0.5s ease-out;
            }
        """),
        
        lib.tabs.Tabs(
            lib.tabs.TabList(
                lib.tabs.Tab("VES Survey Form"),
                lib.tabs.Tab("View Saved Data"),
            ),
            
            lib.tabs.TabPanel(
                lib.html.div(style=lib.Style(padding="20px", display="flex", flexDirection="column", gap="20px"))(
                    lib.html.h1("VES FORM - Vertical Electrical Sounding"),
                    
                    # Enhanced Success Alert - EXACTLY LIKE MAP_LOCATION
                    lib.bs.Alert(
                        variant="success",
                        className="success-alert",
                        style=lib.Style(
                            marginBottom="20px",
                            borderLeft="4px solid #28a745",
                            boxShadow="0 2px 4px rgba(40, 167, 69, 0.2)"
                        )
                    )(
                        lib.html.div(style=lib.Style(display="flex", alignItems="center", gap="10px"))(
                            lib.html.span(style=lib.Style(fontSize="20px"))("✓"),
                            lib.html.div()(
                                lib.html.strong(success_message),
                                lib.html.br(),
                                lib.html.small(style=lib.Style(color="#666"))(
                                    f"Data stored in database and table updated"
                                ) if submit_success else None,
                            )
                        )
                    ) if submit_success else None,
                    
                    # Error Alert - EXACTLY LIKE MAP_LOCATION
                    lib.bs.Alert(variant="danger")(error_message) if error_message else None,
                    
                    lib.html.div(style=lib.Style(backgroundColor="white", padding="20px", borderRadius="8px"))(
                        lib.html.h3("Survey Details"),
                        lib.bs.Form(key=form_key, onSubmit=handle_submit)(
                            *form_rows,
                            lib.bs.Button(
                                type="submit",
                                variant="primary",
                                size="lg",
                                disabled=is_loading,
                                style=lib.Style(
                                    opacity="0.7" if is_loading else "1",
                                    cursor="not-allowed" if is_loading else "pointer",
                                    width="200px",
                                    padding="12px 24px",
                                    fontSize="16px",
                                    fontWeight="600",
                                    marginTop="20px"
                                )
                            )(
                                lib.html.span(className="spinner")("⟳ ") if is_loading else "📤",
                                "Submitting..." if is_loading else "Submit Survey"
                            )
                        ),
                    ),
                    
                    lib.html.div(style=lib.Style(backgroundColor="white", padding="20px", borderRadius="8px"))(
                        lib.html.h3("Data Grid - Stations 0-20"),
                        lib.html.div(style=lib.Style(height="400px", border="1px solid #ddd"))(
                            lib.ag.AgGridReact(
                                rowData=row_data_1,
                                columnDefs=[
                                    {"field": "station", "editable": False},
                                    {"field": "reading", "editable": True},
                                    {"field": "apparent_resistivity", "editable": True},
                                    {"field": "remarks", "editable": True},
                                ],
                                defaultColDef=lib.Props(flex=1),
                            ),
                        ),
                    ),
                    
                    lib.html.div(style=lib.Style(backgroundColor="white", padding="20px", borderRadius="8px"))(
                        lib.html.h3("Reading vs Station"),
                        lib.tethys.Chart(
                            data=row_data_1,
                            height=500,
                            width=900,
                            x_label="Station",
                            y_label="Reading",
                            x_attr="station",
                            y_attr="reading"
                        ),
                    ),
                )
            ),
            
            lib.tabs.TabPanel(
                lib.html.div(style=lib.Style(padding="20px"))(
                    lib.html.h2("📊 Saved VES Survey Records"),
                    
                    # Success Alert for saves - EXACTLY LIKE MAP_LOCATION
                    lib.bs.Alert(
                        variant="success",
                        className="success-alert",
                        style=lib.Style(
                            marginBottom="20px",
                            borderLeft="4px solid #28a745",
                            boxShadow="0 2px 4px rgba(40, 167, 69, 0.2)"
                        )
                    )(
                        lib.html.div(style=lib.Style(display="flex", alignItems="center", gap="10px"))(
                            lib.html.span(style=lib.Style(fontSize="20px"))("✓"),
                            lib.html.div()(
                                lib.html.strong(success_message),
                                lib.html.br(),
                                lib.html.small(style=lib.Style(color="#666"))(
                                    f"Total records: {len(displayed_data)}"
                                ) if submit_success else None,
                            )
                        )
                    ) if submit_success else None,
                    
                    # Error Alert
                    lib.bs.Alert(variant="danger")(error_message) if error_message else None,
                    
                    # Data table with edit/delete - EXACTLY LIKE MAP_LOCATION
                    create_editable_data_table(lib, displayed_data, set_displayed_data, "created_at", db_fpath, table_name)
                )
            ),
        ),
    )


@App.page
def resistivity_survey_form(lib):
    lib.register('react-tabs', 'tabs', styles=['https://esm.sh/react-tabs@6.1.0/style/react-tabs.css'])
    
    # State management - EXACTLY LIKE MAP_LOCATION
    displayed_data, set_displayed_data = lib.hooks.use_state([])
    form_key, set_form_key = lib.hooks.use_state(str(uuid4()))
    submit_success, set_submit_success = lib.hooks.use_state(None)
    success_message, set_success_message = lib.hooks.use_state("")
    error_message, set_error_message = lib.hooks.use_state(None)
    is_loading, set_is_loading = lib.hooks.use_state(False)
    data_loaded, set_data_loaded = lib.hooks.use_state(False)
    
    survey_data, set_survey_data = lib.hooks.use_state({
        "location_point": "",
        "mn2_value": "0.5",
        "readings": [
            {"spacing": spacing, "reading_1": "", "reading_2": "", "average": "", "notes": ""}
            for spacing in [1, 2.1, 3.0, 4.4, 6.3, 9.1, 13.2, 13.2, 19.0, 19.0, 27.5, 27.5, 40, 58, 58, 83, 83, 120, 120, 175, 250, 375, 525, 750]
        ]
    })
    
    resources = lib.hooks.use_resources()
    db_fpath = resources.path / "resistivity_survey.sqlite"
    table_name = "resistivity_survey"

    # Auto-load data on component mount - EXACTLY LIKE MAP_LOCATION
    def auto_load_data():
        if not data_loaded:
            try:
                data = data_from_sqlite(db_fpath, table_name)
                if data:
                    set_displayed_data(data)
                    print(f"✓ Resistivity Survey data auto-loaded: {len(data)} records")
                set_data_loaded(True)
            except Exception as err:
                print(f"Resistivity Survey auto-load error: {err}")
                set_data_loaded(True)

    lib.hooks.use_effect(auto_load_data, [])

    log_spacings = [1, 2.1, 3.0, 4.4, 6.3, 9.1, 13.2, 13.2, 19.0, 19.0, 27.5, 27.5, 40, 58, 58, 83, 83, 120, 120, 175, 250, 375, 525, 750]
    
    plot_data = []
    for i, spacing in enumerate(log_spacings):
        if i < len(survey_data["readings"]):
            reading = survey_data["readings"][i]
            if reading["average"]:
                try:
                    plot_data.append({"depth": spacing, "resistivity": float(reading["average"])})
                except:
                    pass

    def update_reading(index, field, value):
        new_readings = survey_data["readings"].copy()
        new_readings[index] = {**new_readings[index], field: value}
        
        r = float(new_readings[index]["reading_2"]) if new_readings[index]["reading_2"] else 0
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

    def handle_submit(e):
        if is_loading:
            return
        
        set_is_loading(True)
        set_error_message(None)
        set_submit_success(None)
        try:
            data_to_sqlite(db_fpath, table_name, [survey_data])
            
            # Get current timestamp
            now = datetime.now()
            timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
            
            # Set success feedback
            set_submit_success(True)
            set_success_message(f"✓ Form submitted successfully at {timestamp}")
            
            # Reset form
            set_form_key(str(uuid4()))
            
            # Auto-reload the View Data table
            new_data = data_from_sqlite(db_fpath, table_name)
            set_displayed_data(new_data)
            
            # Hide success message after 4 seconds
            lib.utils.background_execute(
                lambda: set_submit_success(None), 
                delay_seconds=4
            )
        except Exception as err:
            print(f"Submit error: {err}")
            set_error_message(f"❌ Error: {str(err)[:100]}")
            set_submit_success(False)
        finally:
            set_is_loading(False)

    return lib.html.div()(
        lib.html.style()("""
            @keyframes spin {
                from { transform: rotate(0deg); }
                to { transform: rotate(360deg); }
            }
            .spinner {
                display: inline-block;
                animation: spin 1s linear infinite;
                margin-right: 8px;
            }
            @keyframes slideDown {
                from {
                    opacity: 0;
                    transform: translateY(-20px);
                }
                to {
                    opacity: 1;
                    transform: translateY(0);
                }
            }
            .success-alert {
                animation: slideDown 0.5s ease-out;
            }
        """),
        
        lib.html.div(style=lib.Style(
            display="flex",
            flexDirection="column",
            gap="20px",
            padding="20px",
            fontFamily="Arial, sans-serif",
            maxWidth="1400px",
            margin="0 auto"
        ))(
            # Header
            lib.html.div(style=lib.Style(textAlign="center", marginBottom="20px"))(
                lib.html.h2("Schlumberger Array VES Survey"),
                lib.html.p("Log-Log Resistivity Plot - Apparent Resistivity vs Electrode Spacing"),
            ),
            
            # Enhanced Success Alert - EXACTLY LIKE MAP_LOCATION
            lib.bs.Alert(
                variant="success",
                className="success-alert",
                style=lib.Style(
                    marginBottom="20px",
                    borderLeft="4px solid #28a745",
                    boxShadow="0 2px 4px rgba(40, 167, 69, 0.2)"
                )
            )(
                lib.html.div(style=lib.Style(display="flex", alignItems="center", gap="10px"))(
                    lib.html.span(style=lib.Style(fontSize="20px"))("✓"),
                    lib.html.div()(
                        lib.html.strong(success_message),
                        lib.html.br(),
                        lib.html.small(style=lib.Style(color="#666"))(
                            f"Data stored in database"
                        ) if submit_success else None,
                    )
                )
            ) if submit_success else None,
            
            # Error Alert
            lib.bs.Alert(variant="danger")(error_message) if error_message else None,
            
            # Location/Site Info
            lib.html.div(style=lib.Style(display="flex", gap="20px", marginBottom="20px"))(
                lib.html.div(style=lib.Style(flex=1))(
                    lib.html.label("Location Point (Site ID):"),
                    lib.html.input(
                        id="location_point",
                        type="text",
                        value=survey_data["location_point"],
                        onChange=lambda e: set_survey_data({**survey_data, "location_point": e.target.value}),
                        style=lib.Style(width="100%", padding="8px", marginTop="5px"),
                    ),
                ),
                lib.html.div(style=lib.Style(flex="0 0 300px"))(
                    lib.html.label("MN/2 (constant):"),
                    lib.html.select(
                        style=lib.Style(width="100%", padding="8px", marginTop="5px"),
                        value=survey_data["mn2_value"],
                        onChange=lambda e: update_mn2(e.target.value)
                    )(
                        lib.html.option(value="0.5")("0.5 m"),
                        lib.html.option(value="5.0")("5.0 m"),
                        lib.html.option(value="25")("25 m"),
                    ),
                ),
            ),
            
            # Save Button
            lib.html.div(style=lib.Style(display="flex", gap="10px", marginBottom="20px"))(
                lib.bs.Button(
                    variant="primary",
                    size="lg",
                    onClick=handle_submit,
                    disabled=is_loading,
                    style=lib.Style(
                        opacity="0.7" if is_loading else "1",
                        cursor="not-allowed" if is_loading else "pointer",
                        width="200px",
                        padding="12px 24px",
                        fontSize="16px",
                        fontWeight="600"
                    )
                )(
                    lib.html.span(className="spinner")("⟳ ") if is_loading else "💾",
                    "Saving..." if is_loading else "Save Survey to Database"
                ),
            ),
            
            # Main Content: Data Table + Chart side by side
            lib.html.div(style=lib.Style(display="flex", gap="20px", marginBottom="20px"))(
                # Left: Data Entry Table
                lib.html.div(style=lib.Style(
                    flex="0 0 550px",
                    border="1px solid #999",
                    padding="10px",
                    backgroundColor="#f9f9f9"
                ))(
                    lib.html.table(style=lib.Style(
                        width="100%",
                        borderCollapse="collapse",
                        fontSize="12px"
                    ))(
                        lib.html.thead()(
                            lib.html.tr(style=lib.Style(backgroundColor="#ddd", borderBottom="2px solid #999"))(
                                lib.html.th(style=lib.Style(padding="8px", border="1px solid #999", textAlign="left"))("AB/2 (m)"),
                                lib.html.th(style=lib.Style(padding="8px", border="1px solid #999", textAlign="left"))("Count"),
                                lib.html.th(style=lib.Style(padding="8px", border="1px solid #999", textAlign="left"))("R (Ω)"),
                                lib.html.th(style=lib.Style(padding="8px", border="1px solid #999", textAlign="left"))("Notes"),
                            ),
                        ),
                        lib.html.tbody()(
                            *[
                                lib.html.tr(style=lib.Style(
                                    borderBottom="1px solid #ddd",
                                    backgroundColor="#fff" if i % 2 == 0 else "#f5f5f5"
                                ))(
                                    lib.html.td(style=lib.Style(padding="6px", border="1px solid #ddd", fontWeight="bold"))(
                                        str(spacing)
                                    ),
                                    lib.html.td(style=lib.Style(padding="6px", border="1px solid #ddd", textAlign="center", fontWeight="bold"))(
                                        str(idx + 1)
                                    ),
                                    lib.html.td(style=lib.Style(padding="4px", border="1px solid #ddd"))(
                                        lib.html.input(
                                            type="number",
                                            value=survey_data["readings"][idx]["reading_2"],
                                            style=lib.Style(width="90%", padding="4px"),
                                            placeholder="0.0",
                                            onChange=lambda e, i=idx: update_reading(i, "reading_2", e.target.value)
                                        ),
                                    ),
                                    lib.html.td(style=lib.Style(padding="4px", border="1px solid #ddd"))(
                                        lib.html.input(
                                            type="text",
                                            value=survey_data["readings"][idx]["notes"],
                                            style=lib.Style(width="90%", padding="4px"),
                                            placeholder="Layer",
                                            onChange=lambda e, i=idx: update_reading(i, "notes", e.target.value)
                                        ),
                                    ),
                                )
                                for idx, spacing in enumerate(log_spacings)
                            ]
                        ),
                    ),
                ),
                
                # Right: Log-Log Chart
                lib.html.div(style=lib.Style(flex=1, minHeight="700px"))(                
                    lib.html.div(style=lib.Style(height="700px"))(
                        lib.tethys.Chart(
                            data=plot_data if plot_data else [{"depth": 1, "resistivity": 50}],
                            height=700,
                            width=900,
                            x_label="Apparent Resistivity ρa (Ω·m)",
                            y_label="Electrode Spacing AB/2 (m)",
                            x_attr="resistivity",
                            y_attr="depth"
                        ),
                    ),
                    lib.html.p(
                        style=lib.Style(fontSize="11px", color="#666", marginTop="10px")
                    )(
                        "Schlumberger array: MN/2 constant, AB/2 varies. ρa = R × (MN/2). Curve breaks = layer boundaries."
                    ),
                ),
            ),
            
            # Saved Data Section - EXACTLY LIKE MAP_LOCATION
            lib.html.div(style=lib.Style(
                border="1px solid #ddd",
                padding="15px",
                backgroundColor="#f9f9f9",
                marginTop="20px",
                borderRadius="4px"
            ))(
                lib.html.h3("📁 Saved Surveys"),
                
                # Success Alert for saves
                lib.bs.Alert(
                    variant="success",
                    className="success-alert",
                    style=lib.Style(
                        marginBottom="20px",
                        borderLeft="4px solid #28a745",
                        boxShadow="0 2px 4px rgba(40, 167, 69, 0.2)"
                    )
                )(
                    lib.html.div(style=lib.Style(display="flex", alignItems="center", gap="10px"))(
                        lib.html.span(style=lib.Style(fontSize="20px"))("✓"),
                        lib.html.div()(
                            lib.html.strong(success_message),
                            lib.html.br(),
                            lib.html.small(style=lib.Style(color="#666"))(
                                f"Total records: {len(displayed_data)}"
                            ) if submit_success else None,
                        )
                    )
                ) if submit_success else None,
                
                # Error Alert
                lib.bs.Alert(variant="danger")(error_message) if error_message else None,
                
                # Data table with edit/delete - EXACTLY LIKE MAP_LOCATION
                create_editable_data_table(lib, displayed_data, set_displayed_data, "created_at", db_fpath, table_name)
            ),
        ),
    )

async def analyze_rock_from_bytes(api_key, data_bytes, mime_type="image/jpeg"):
    """
    Analyze rock image bytes with Gemini API.
    """
    if not api_key:
        return {"status": "error", "message": "Gemini API key is not configured."}
    if not data_bytes:
        return {"status": "error", "message": "No image data provided."}

    def _request_gemini():
        endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-1.5-flash:generateContent?key={api_key}"
        )
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                "Identify the likely rock type in this image and provide a short, practical "
                                "field description with key observable features."
                            )
                        },
                        {
                            "inline_data": {
                                "mime_type": mime_type or "image/jpeg",
                                "data": base64.b64encode(data_bytes).decode("utf-8"),
                            }
                        },
                    ]
                }
            ]
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
            error_message = body.get("error", {}).get("message", "No response from Gemini API.")
            return {"status": "error", "message": error_message}

        parts = candidates[0].get("content", {}).get("parts", [])
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
def webcam(lib):
    """
    Webcam page with Gemini API integration for rock identification
    Supports both live webcam capture and file upload
    """
    lib.register("react-markdown", "md", default_export="Markdown")
    processing, set_processing = lib.hooks.use_state(False)
    analysis_results, set_analysis_results = lib.hooks.use_state(None)
    gemini_api_key = lib.hooks.use_setting("GEMINI_API_KEY")

    async def handle_file_upload(e):
        """
        Handle uploaded file from file input
        """
        set_processing(True)
        form_data = e["formData"]
        file_data = form_data.get("upload")
        with urlopen(file_data) as response:
            mime_type = response.info().get_content_type()
            data_bytes = response.read()

        result = await analyze_rock_from_bytes(gemini_api_key, data_bytes, mime_type=mime_type)
        if result["status"] == "success":
            set_analysis_results(result["analysis"])
            set_processing(False)
        else:
            set_analysis_results(f"Error: {result['message']}")
            set_processing(False)
    
    return lib.tethys.Display(
        lib.html.div(
            style={
                "padding": "20px",
                "max-width": "800px",
                "margin": "0 auto",
                "font-family": "Arial, sans-serif"
            }
        )(
            lib.html.h1("Rock Identifier with Gemini AI"),
            # File upload section
            lib.lo.LoadingOverlay(active=processing, spinner=True)(
                lib.bs.Form(onSubmit=event(handle_file_upload, prevent_default=True, stop_propagation=True))(
                    lib.html.h3("Upload Image"),
                    lib.html.input(key=id(analysis_results), type="file", name="upload", accept="image/*"),
                    lib.html.button(type="submit")("Analyze File"),
                ),
            ),
            lib.html.hr(),
            lib.md.Markdown(analysis_results if analysis_results else "No analysis results yet.")
        ),
    )