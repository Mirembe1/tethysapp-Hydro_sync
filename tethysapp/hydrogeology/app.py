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
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"Invalid SQL identifier: {name}")
    return name


def _resolve_page_decorator():
    app_obj = globals().get("App")
    return app_obj.page if app_obj and hasattr(app_obj, "page") else (lambda func: func)

def delete_record_from_sqlite(db_fpath, table_name, record_id, id_col="id"):
    """Delete a record from SQLite based on id_col"""
    table_name = _safe_identifier(table_name)
    
    conn = sqlite3.connect(str(db_fpath))
    cursor = conn.cursor()
    
    try:
        delete_sql = f'DELETE FROM "{table_name}" WHERE "{id_col}" = ?'
        cursor.execute(delete_sql, (record_id,))
        conn.commit()
        print(f"✓ Record with {id_col}={record_id} deleted from {table_name}")
        
    except Exception as e:
        conn.rollback()
        print(f"✗ Error: {e}")
        raise
    finally:
        conn.close()

def update_data_in_sqlite(db_fpath, table_name, data, id_col="id"):
    """Update existing records in SQLite based on id_col"""
    table_name = _safe_identifier(table_name)
    
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
            set_clause = ", ".join([f'"{col}" = ?' for col in columns])
            values = [str(row.get(col, "")) if row.get(col) is not None else "" for col in columns]
            values.append(record_id)
            
            update_sql = f'UPDATE "{table_name}" SET {set_clause} WHERE "{id_col}" = ?'
            cursor.execute(update_sql, values)
        
        conn.commit()
        print(f"✓ Data updated in {table_name}")
        
    except Exception as e:
        conn.rollback()
        print(f"✗ Error: {e}")
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
        
        columns = [_safe_identifier(key) for key in first_row.keys()]
        columns.append("created_at")
        
        cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
        table_exists = cursor.fetchone()
        
        if not table_exists:
            col_defs = ", ".join([f'"{col}" TEXT' for col in columns])
            create_sql = f'CREATE TABLE "{table_name}" ({col_defs}, id INTEGER PRIMARY KEY AUTOINCREMENT)'
            cursor.execute(create_sql)
            print(f"✓ Created table: {table_name}")
        else:
            cursor.execute(f"PRAGMA table_info({table_name})")
            existing_cols = {row[1] for row in cursor.fetchall()}
            
            for col in columns:
                if col not in existing_cols:
                    try:
                        cursor.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{col}" TEXT')
                        print(f"✓ Added column: {col}")
                    except sqlite3.OperationalError:
                        pass
        
        for row in data:
            values = [str(row.get(col, "")) if row.get(col) is not None else "" for col in columns[:-1]]
            values.append(pd.Timestamp.now().isoformat())
            
            placeholders = ", ".join(["?" for _ in values])
            col_names = ", ".join([f'"{col}"' for col in columns])
            insert_sql = f'INSERT INTO "{table_name}" ({col_names}) VALUES ({placeholders})'
            
            cursor.execute(insert_sql, values)
        
        conn.commit()
        print(f"✓ Data saved to {table_name}")
        
    except Exception as e:
        conn.rollback()
        print(f"✗ Error: {e}")
        raise
    finally:
        conn.close()


def data_from_sqlite(db_fpath, table_name):
    table_name = _safe_identifier(table_name)
    if not db_fpath.exists():
        return []
    conn = sqlite3.connect(str(db_fpath))
    try:
        return pd.read_sql_query(f'SELECT * FROM "{table_name}"', conn).to_dict("records")
    except Exception:
        return []
    finally:
        conn.close()

@component
def create_editable_data_table(lib, row_data, set_row_data, id_col):
    """Create editable tabulated view of form data using AgGridReact"""
    selected_row, set_selected_row = lib.hooks.use_state(None)
    edit_mode, set_edit_mode = lib.hooks.use_state(False)

    def handle_record_delete(e):
        if selected_row is not None:
            deleted_row = None
            new_row_data = []
            for row in row_data:
                if str(row.get(id_col)) == str(selected_row):
                    deleted_row = row
                else:
                    new_row_data.append(row)
            
            # Delete record from database
            delete_record_from_sqlite(lib.hooks.use_resources().path / "my_database.sqlite", "Map_Location", deleted_row.get(id_col), id_col=id_col)
            # Updates table view to omit deleted row
            set_row_data(new_row_data)
            # Since the selected row is now deleted, it can no longer be selected
            set_selected_row(None)
    
    if not row_data or len(row_data) == 0:
        return lib.html.div(
            style=lib.Style(
                padding="20px",
                textAlign="center",
                color="#999",
                fontSize="16px"
            )
        )("📭 No data submitted yet. Submit a form to see data here.")
    
    first_row = row_data[0]
    
    col_defs = []
    for key in first_row.keys():
        value = first_row[key]
        col_type = "agNumberColumnFilter" if isinstance(value, (int, float)) else "agTextColumnFilter"
        
        col_defs.append({
            "field": key,
            "filter": col_type,
            "sortable": True,
            "resizable": True,
            "minWidth": 120,
            "editable": edit_mode,
            "wrapText": True,
            "autoHeight": True,
        })
    
    default_col_def = lib.Props(
        flex=1,
        resizable=True,
        sortable=True,
        filter=True,
        editable=False,
        wrapText=True,
        autoHeight=True
    )
    
    def handle_cell_edit(e):
        """Handle cell editing and call parent callback"""
        if set_row_data and callable(set_row_data):
            updated_row_id = e.data.created_at
            updated_row = e.data
            new_row_data = [row if str(row.get(id_col)) != str(updated_row_id) else updated_row for row in row_data]
            set_row_data(new_row_data)
            update_data_in_sqlite(lib.hooks.use_resources().path / "my_database.sqlite", "Map_Location", [updated_row], id_col=id_col)
    
    return lib.html.div(
        style=lib.Style(
            height="600px",
            border="1px solid #ddd",
            borderRadius="4px",
            overflow="hidden",
            backgroundColor="white"
        ),
    )(
        lib.html.div(
            lib.bs.Button(
                variant="secondary",
                onClick=lambda e: set_edit_mode(lambda val: not val)
            )(
                "Edit" if not edit_mode else "Stop Editing"
            ),
            lib.bs.Button(
                variant="danger",
                onClick=handle_record_delete,
            )("Delete"),
        ) if selected_row else None,
        lib.ag.AgGridReact(
            key="my-table",
            rowData=row_data,
            columnDefs=col_defs,
            rowId=id_col,
            defaultColDef=default_col_def,
            pagination=True,
            paginationPageSize=20,
            paginationPageSizeSelector=[10, 20, 50, 100],
            domLayout="autoHeight",
            enableBrowserTooltips=True,
            rowSelection=lib.Props(
                mode='singleRow',
                checkboxes=True,
                enableClickSelection=True,
            ),
            onSelectionChanged=lambda e: set_selected_row(
                e.selectedNodes[0].id if e.selectedNodes else None
            ),
            onCellValueChanged=handle_cell_edit,
        )
    )

@_resolve_page_decorator()
def map_location(lib):
    displayed_data, set_displayed_data = lib.hooks.use_state([])
    submit_success, set_submit_success = lib.hooks.use_state(None)
    success_message, set_success_message = lib.hooks.use_state("")
    success_timestamp, set_success_timestamp = lib.hooks.use_state("")
    error_message, set_error_message = lib.hooks.use_state(None)
    form_key, set_form_key = lib.hooks.use_state(str(uuid4()))
    is_loading, set_is_loading = lib.hooks.use_state(False)
    data_loaded, set_data_loaded = lib.hooks.use_state(False)

    resources = lib.hooks.use_resources()
    db_fpath = resources.path / "my_database.sqlite"
    table_name = "Map_Location"

    # Auto-load data on component mount
    def auto_load_data():
        if not data_loaded:
            try:
                data = data_from_sqlite(db_fpath, table_name)
                if data:
                    set_displayed_data(data)
                    print("✓ Data auto-loaded on initialization")
                set_data_loaded(True)
            except Exception as err:
                print(f"Auto-load error: {err}")
                set_data_loaded(True)

    # Run auto-load once
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
            
            # Get current timestamp
            now = datetime.now()
            timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
            
            # Set success feedback
            set_submit_success(True)
            set_success_timestamp(timestamp)
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

    def handle_load_data():
        try:
            data = data_from_sqlite(db_fpath, table_name)
            if data:
                set_displayed_data(data)
                set_error_message(None)
            else:
                set_displayed_data([])
                set_error_message("No form data submitted yet")
        except Exception as err:
            set_error_message(f"Load error: {str(err)[:100]}")

    def handle_save_edited_data():
        if is_loading:
            return
        
        set_is_loading(True)
        set_error_message(None)
        set_submit_success(None)
        try:
            if displayed_data:
                data_to_sqlite(db_fpath, table_name, displayed_data)
                
                # Get current timestamp
                now = datetime.now()
                timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
                
                set_submit_success(True)
                set_success_timestamp(timestamp)
                set_success_message(f"✓ Edits saved successfully at {timestamp}")
                
                lib.utils.background_execute(
                    lambda: set_submit_success(None), 
                    delay_seconds=4
                )
            else:
                set_error_message("No data to save")
        except Exception as err:
            print(f"Save edited data error: {err}")
            set_error_message(f"❌ Error: {str(err)[:100]}")
        finally:
            set_is_loading(False)

    lib.register("sketch_canvas.js", "sc", host="/static/component_playground/js", default_export="SketchCanvas")
    lib.register("react-tabs", "tabs", styles=["https://esm.sh/react-tabs@6.1.0/style/react-tabs.css"])

    color, set_color = lib.hooks.use_state("#100a0a")
    width, set_width = lib.hooks.use_state(4)
    event_fn = globals().get("event")
    submit_handler = (
        event_fn(handle_submit, prevent_default=True, stop_propagation=True)
        if callable(event_fn)
        else handle_submit
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
                lib.tabs.Tab("Location Map"),
                lib.tabs.Tab("Add Data"),
                lib.tabs.Tab("View Data")
            ),
            lib.tabs.TabPanel(
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
                                style=lib.Style(border="0.0625rem solid #9c9c9c", borderRadius="0.25rem", width="100%", height="500px"),
                                width="100%",
                                height="500px",
                                strokeWidth=width,
                                strokeColor=color,
                            )
                        ),
                    ),
                )
            ),
            
            lib.tabs.TabPanel(
                lib.html.div(style=lib.Style(padding="20px"))(
                    lib.html.h2("Map Location Survey Form"),
                    
                    # Enhanced Success Alert
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
                    
                    # Error Alert
                    lib.bs.Alert(variant="danger")(error_message) if error_message else None,
                    
                    lib.bs.Form(key=form_key, onSubmit=submit_handler)(
                        lib.bs.Container()(
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
                    
                    lib.html.div(style=lib.Style(display="flex", gap="10px", marginBottom="20px"))(
                        lib.bs.Button(
                            onClick=lambda _: handle_load_data(),
                            variant="info",
                            size="lg"
                        )("🔄 Refresh Data"),
                        lib.bs.Button(
                            disabled=not displayed_data or is_loading,
                            onClick=lambda _: handle_save_edited_data(),
                            variant="success",
                            size="lg",
                            style=lib.Style(
                                opacity="0.7" if is_loading else "1",
                                cursor="not-allowed" if is_loading else "pointer"
                            )
                        )(
                            lib.html.span(className="spinner")("⟳ ") if is_loading else "💾",
                            "Saving..." if is_loading else "Save Edits"
                        ),
                    ),
                    
                    lib.html.p(style=lib.Style(fontSize="12px", color="#666", marginBottom="15px"))(
                        f"💡 Total Records: {len(displayed_data)} | Double-click cells to edit. Click 'Save Edits' to save changes."
                    ),                    
                    create_editable_data_table(lib, displayed_data, set_displayed_data, "created_at")
                )
            ),
        ),
    )

@App.page
def VES_FORM(lib):
    lib.register('react-tabs', 'tabs', styles=['https://esm.sh/react-tabs@6.1.0/style/react-tabs.css'])
    
    row_data_1, set_row_data_1 = lib.hooks.use_state(
        [{"station": x, "reading": "", "apparent_resistivity": "", "remarks": ""} for x in range(21)]
    )
    
    displayed_data, set_displayed_data = lib.hooks.use_state([])
    submit_success, set_submit_success = lib.hooks.use_state(None)
    formKey, setFormKey = lib.hooks.use_state(str(uuid4()))
    
    resources = lib.hooks.use_resources()
    db_fpath = resources.path / "ves_survey_data.sqlite"
    
    col_defs = [
        {"field": "station", "editable": False},
        {"field": "reading", "editable": True},
        {"field": "apparent_resistivity", "editable": True},
        {"field": "remarks", "editable": True},
    ]
    
    form_fields = [
        [("Project_Name", "Project Name"), ("profile", "Profile")],
        [("Area", "Area"), ("Coordinates", "Coordinates")],
        [("Date", "Date"), ("Orientation", "Orientation")],
        [("Configuration", "Configuration"), ("Station_Interval", "Station Interval")],
        [("1/2_AB", "1/2 AB"), ("1/2_MN", "1/2 MN")],
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
        form_data = collect_form_data(lib)
        form_data["table_data"] = row_data_1
        data_to_sqlite(db_fpath, Table_name="VES_FORM", data=[form_data])
        set_submit_success(True)
        setFormKey(str(uuid4()))
        lib.utils.background_execute(lambda: set_submit_success(None), delay_seconds=3)

    def handle_cell_edit_stopped(e):
        if hasattr(e, 'node') and hasattr(e.node, 'beans'):
            set_row_data_1(e.node.beans.gridOptions.rowData)

    return lib.tabs.Tabs(
        lib.tabs.TabList(
            lib.tabs.Tab("VES Survey Form"),
            lib.tabs.Tab("View Saved Data"),
        ),
        
        lib.tabs.TabPanel(
            lib.html.div(style=lib.Style(padding="20px", display="flex", flexDirection="column", gap="20px"))(
                lib.html.h1("VES FORM - Vertical Electrical Sounding"),
                
                lib.html.div(style=lib.Style(backgroundColor="white", padding="20px", borderRadius="8px"))(
                    lib.html.h3("Survey Details"),
                    *form_rows,
                ),
                
                lib.bs.Alert(variant="success")("✓ Form submitted successfully!") if submit_success else None,
                
                lib.bs.Button(variant="primary", onClick=lambda e: handle_submit({}))("Submit Survey Data"),
                
                lib.html.div(style=lib.Style(backgroundColor="white", padding="20px", borderRadius="8px"))(
                    lib.html.h3("Data Grid - Stations 0-20"),
                    lib.html.div(style=lib.Style(height="400px", border="1px solid #ddd"))(
                        lib.ag.AgGridReact(
                            rowData=row_data_1,
                            columnDefs=col_defs,
                            defaultColDef=lib.Props(flex=1),
                            onCellEditingStopped=handle_cell_edit_stopped,
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
                lib.html.h2("Saved VES Survey Records"),
                lib.bs.Button(
                    disabled=not db_fpath.exists(),
                    onClick=lambda _: set_displayed_data(data_from_sqlite(db_fpath, "VES_FORM")),
                    variant="info"
                )("Load Data"),
                lib.html.pre(json.dumps(displayed_data, indent=2)) if displayed_data else 
                lib.html.div("No data to display."),
            )
        )
    )


def collect_form_data(lib):
    form_data = {}
    for field_id in ["Project_Name", "profile", "Area", "Coordinates", "Date", "Orientation", "Configuration", "Station_Interval", "1/2_AB", "1/2_MN"]:
        try:
            form_data[field_id] = lib.document.getElementById(field_id).value
        except:
            form_data[field_id] = ""
    return form_data


def data_from_sqlite(db_path, table_name):
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {table_name} ORDER BY created_at DESC")
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    except:
        return []

@App.page
def resistivity_survey_form(lib):
    # State for survey data
    survey_data, set_survey_data = lib.hooks.use_state({
        "location_point": "",
        "mn2_value": "0.5",
        "readings": [
            {"spacing": spacing, "reading_1": "", "reading_2": "", "average": "", "notes": ""}
            for spacing in [1, 2.1, 3.0, 4.4, 6.3, 9.1, 13.2, 13.2, 19.0, 19.0, 27.5, 27.5, 40, 58, 58, 83, 83, 120, 120, 175, 250, 375, 525, 750]
        ]
    })
    
    submit_success, set_submit_success = lib.hooks.use_state(None)
    displayed_data, set_displayed_data = lib.hooks.use_state([])
    resources = lib.hooks.use_resources()
    db_fpath = resources.path / "resistivity_survey.sqlite"

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

    def handle_submit():
        data_to_sqlite(db_fpath, Table_name="resistivity_survey", data=[{
            "location_point": survey_data["location_point"],
            "mn2_value": survey_data["mn2_value"],
            "readings": survey_data["readings"]
        }])
        set_submit_success(True)
        lib.utils.background_execute(lambda: set_submit_success(None), delay_seconds=3)

    return lib.html.div(style=lib.Style(
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
        
        # Success Alert
        lib.bs.Alert(variant="success")("✓ Survey saved successfully!") if submit_success else None,
        
        # Save Button
        lib.html.div(style=lib.Style(display="flex", gap="10px", marginBottom="20px"))(
            lib.bs.Button(
                variant="primary",
                size="lg",
                onClick=lambda e: handle_submit()
            )("💾 Save Survey to Database"),
            lib.bs.Button(
                variant="info",
                size="lg",
                onClick=lambda _: set_displayed_data(data_from_sqlite(db_fpath, "resistivity_survey"))
            )("📂 Load Saved Surveys"),
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
        
        # Saved Data Section
        lib.html.div(style=lib.Style(
            border="1px solid #999",
            padding="15px",
            backgroundColor="#f9f9f9",
            marginTop="20px"
        ))(
            lib.html.h3("📁 Saved Surveys"),
            lib.html.div(style=lib.Style(
                maxHeight="300px",
                overflowY="auto",
                backgroundColor="white",
                padding="10px",
                border="1px solid #ddd",
                borderRadius="4px"
            ))(
                lib.html.pre(json.dumps(displayed_data, indent=2)) if displayed_data else 
                lib.html.div(style=lib.Style(color="#999"))("No surveys saved yet."),
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
        form_data = e["formData"] # This is the form data as a JSON string
        file_data = form_data.get("upload") # Extract the uploaded file data
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

#from tethys_sdk.components.utils import event
#from tethysapp.component_playground.app import App

# @App.page
# def sqlite_db_integration(lib):
#     lib.register('react-tabs', 'tabs', styles=['https://esm.sh/react-tabs@6.1.0/style/react-tabs.css'])
#         # The use_workspace hook provides access to the app's workspace, which is a Tethys-managed directory on the server 
#     # where you can read/write files. We'll use this location to store our sqlite database file.
#     displayed_data, set_displayed_data = lib.hooks.use_state([])
#     submit_success, set_submit_success = lib.hooks.use_state(None)
#     formKey, setFormKey = lib.hooks.use_state(str(uuid4())) # This is used to reset the form after submission by changing the key
#     resources = lib.hooks.use_resources()
    
#     # Reaching this point means that the workspace is ready to use, so we can proceed with 
#     # reading/writing the sqlite database file.
#     db_fpath = resources.path / "my_database.sqlite"

#     def handle_submit(e):
#         form_data = e["formData"] # This is the form data as a JSON string
#         data_to_sqlite(db_fpath, [form_data]) # Load the form data into the SQLite database
#         set_submit_success(True) # Show success message
#         setFormKey(str(uuid4())) # Reset the form by changing its key, which forces it to remount
#         lib.utils.background_execute(lambda: set_submit_success(None), delay_seconds=3) # Hide success message after 3 seconds

#     return lib.tethys.Display(
#         lib.tabs.Tabs(
#             lib.tabs.TabList(
#                 lib.tabs.Tab("Add Data"),
#                 lib.tabs.Tab("View Data"),
#             ),
#             lib.tabs.TabPanel(
#                 lib.bs.Form(key=formKey, onSubmit=event(handle_submit, prevent_default=True, stop_propagation=True))(
#                     lib.bs.FormGroup(className="mb-3")(
#                         lib.bs.FormLabel("Location Name"),
#                         lib.bs.FormControl(type="text", name="location_name", placeholder="Enter location name here"),
#                     ),
#                     lib.bs.FormGroup(className="mb-3")(
#                         lib.bs.FormLabel("Rock Type"),
#                         lib.bs.FormControl(type="text", name="rock_type", placeholder="Enter rock type here"),
#                     ),
#                     lib.bs.FormGroup(className="mb-3")(
#                         lib.bs.FormLabel("Image"),
#                         lib.bs.FormControl(type="file", name="image", accept="image/*", capture="environment"),
#                     ),
#                     lib.bs.Button(type="submit", variant="primary")("Add"),
#                     lib.bs.Alert(variant="success")("Form submitted successfully!") if submit_success else None,
#                 )
#             ),
#             lib.tabs.TabPanel(
#                 lib.bs.Button(disabled=not db_fpath.exists(), onClick=lambda _: set_displayed_data(data_from_sqlite(db_fpath)))(f"{'Load' if not displayed_data else 'Reload'} Data from SQLite Database"),
#                 lib.html.div(
#                     lib.html.pre(str(displayed_data)) if displayed_data else None,
#                 ) if displayed_data else lib.html.div("No data to display. Please add some data in the 'Add Data' tab and submit the form.")
#             )
#         )
#     )

# def data_from_sqlite_paste(db_path, table_name):
#     try:
#         conn = sqlite3.connect(str(db_path))
#         cursor = conn.cursor()
#         cursor.execute(f"SELECT * FROM {table_name} ORDER BY created_at DESC")
#         columns = [d[0] for d in cursor.description]
#         return [dict(zip(columns, row)) for row in cursor.fetchall()]
#     except:
#         return []
    
# @App.page

# def reactive_table(lib):
#     import os
#     db = lib.hooks.use_resources().path / "aggrid.sqlite"
#     table = "aggrid_models"
#     cols = ["make", "model", "price"]

#     data, set_data = lib.hooks.use_state([])
#     sel, set_sel = lib.hooks.use_state(None)
#     modal, set_modal = lib.hooks.use_state(False)
#     edit, set_edit = lib.hooks.use_state(False)
#     form, set_form = lib.hooks.use_state({c: "" for c in cols})

#     def reload_data(): set_data(data_from_sqlite(db, table))
#     lib.hooks.use_effect(reload_data, [])

#     def persist(new_data): data_to_sqlite(db, table, new_data); set_data(new_data)

#     def open_modal(editing): 
#         set_edit(editing)
#         set_form(next((r for r in data if r["model"]==sel), {c:"" for c in cols}) if editing else {c:"" for c in cols})
#         set_modal(True)

#     def save():
#         d = form.copy()
#         existing = [r for r in data if r["model"] != d["model"]]
#         persist((existing + [d]) if not edit else [d if r["model"]==sel else r for r in data])
#         set_modal(False); set_sel(None)

#     def delete(): persist([r for r in data if r["model"]!=sel]); set_sel(None)

#     col_defs = [
#         {"field":"make", "headerName":"Make", "checkboxSelection":True},
#         {"field":"model","headerName":"Model"},
#         {"field":"price","headerName":"Price"},
#     ]

#     return lib.html.div()(
#         lib.bs.Button("Add", variant="success", onClick=lambda _: open_modal(False)),
#         lib.bs.Button("Edit", variant="secondary", onClick=lambda e: set_edit_mode(lambda val: not val))("Edit" if not edit_mode else "Stop Editing"),
#         lib.bs.Button("Delete", variant="danger", disabled=not sel, onClick=lambda _: delete()),
#         lib.ag.AgGridReact(
#             rowData=data, columnDefs=col_defs, rowSelection="single",
#             onSelectionChanged=lambda e: set_sel(e.selectedNodes[0].id if e.selectedNodes else None),
#             rowId="model", pagination=True, paginationPageSize=12,
#         ),
#         lib.bs.Modal(show=modal, onHide=lambda _: set_modal(False))(
#             lib.bs.ModalHeader()(lib.html.h5("Edit" if edit else "Add")),
#             lib.bs.ModalBody()(*(lib.bs.FormGroup()(
#                 lib.bs.FormLabel(c.capitalize()),
#                 lib.bs.FormControl(name=c, value=form[c], onChange=lambda e, f=c: set_form({**form, f:e.target.value}), readOnly=edit and c=="model")
#             ) for c in cols)),
#             lib.bs.ModalFooter()(
#                 lib.bs.Button("Cancel", variant="secondary", onClick=lambda _: set_modal(False)),
#                 lib.bs.Button("Save", variant="primary", onClick=lambda _: save())
#             )
#         ) if modal else None
#     )