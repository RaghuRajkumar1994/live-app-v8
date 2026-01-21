from flask import Flask, render_template_string, redirect, url_for, Response, request, jsonify
from flask_socketio import SocketIO, join_room, leave_room
import eventlet
from datetime import datetime
import pandas as pd
from collections import defaultdict
import io
import csv
import json
import os
from typing import List, Dict, Any
import getpass
import sys
import signal
import ctypes
import time


try:
    import openpyxl
except ImportError:
    print("Warning: openpyxl is not installed. Excel file uploads will fail.")


# --- Configuration ---
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024 
app.config['SECRET_KEY'] = 'your_super_secure_secret_key' 
app.config['SECRET_KEY'] = 'your_super_secure_secret_key'
SERVER_PASSWORD = "admin" # WARNING: Hardcoded password. Not for production use.
socketio = SocketIO(app, async_mode='eventlet')

# --- Persistence Filenames ---
PRODUCTION_FILE = 'production_log.json'
STATE_FILE = 'machine_state.json'
SCRAP_FILE = 'scrap_log.json'

# --- Persistence Helper Functions ---
def load_json_file(filename, default):
    if os.path.exists(filename):
        try:
            with open(filename, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            print(f"Warning: {filename} corrupted. Using default.")
    return default

def save_json_file(filename, data):
    try:
        with open(filename, 'w') as f:
            json.dump(data, f, indent=4)
    except IOError as e:
        print(f"Error saving {filename}: {e}")

def load_production_log():
    log = load_json_file(PRODUCTION_FILE, [])
    for entry in log:
        if 'datetime' in entry and isinstance(entry['datetime'], str):
            try:
                entry['datetime'] = datetime.fromisoformat(entry['datetime'])
            except ValueError:
                pass
    return log

def save_production_log(log):
    serializable_log = []
    for entry in log:
        new_entry = entry.copy()
        if 'datetime' in new_entry and isinstance(new_entry['datetime'], datetime):
            new_entry['datetime'] = new_entry['datetime'].isoformat()
        serializable_log.append(new_entry)
    save_json_file(PRODUCTION_FILE, serializable_log)

def load_machine_state():
    state = load_json_file(STATE_FILE, {})
    return state.get('plans', {}), state.get('queues', {}), state.get('history', {})

def save_machine_state(plans, queues, history):
    save_json_file(STATE_FILE, {'plans': plans, 'queues': queues, 'history': history})

def load_scrap_log():
    return load_json_file(SCRAP_FILE, [])

def save_scrap_log(log):
    save_json_file(SCRAP_FILE, log)

# --- Data Storage (In-memory/File-based Persistence) ---
SUBMISSION_LOG = load_production_log()
MACHINE_PLANS, MACHINE_PLAN_QUEUES, MACHINE_PLAN_HISTORY = load_machine_state()
SCRAP_LOG = load_scrap_log()
SID_TO_MACHINE: Dict[str, str] = {}

# --- NEW: Stock Persistence Functions ---
STOCK_FILE = 'cable_stock_data.json'

def load_stock_data() -> Dict[str, float]:
    """Loads cable stock data from a JSON file."""
    if os.path.exists(STOCK_FILE):
        try:
            with open(STOCK_FILE, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"Warning: {STOCK_FILE} is corrupted. Starting with empty stock.")
            return {}
    return {}

def save_stock_data(data: Dict[str, float]):
    """Saves cable stock data to a JSON file."""
    with open(STOCK_FILE, 'w') as f:
        json.dump(data, f, indent=4, sort_keys=True) 

# Initial load of the stock data on server startup
INITIAL_CABLE_STOCK = load_stock_data()
# ---------------------------------------------


# --- NEW: Downtime Persistence Functions ---
DOWNTIME_FILE = 'downtime_log_data.json'
DOWNTIME_LOG: List[Dict[str, Any]] = []

def load_downtime_data() -> List[Dict[str, Any]]:
    """Loads downtime log from a JSON file and converts time strings to datetime objects."""
    if os.path.exists(DOWNTIME_FILE):
        try:
            with open(DOWNTIME_FILE, 'r') as f:
                data = json.load(f)
                # Convert ISO strings back to datetime objects after loading
                for entry in data:
                    if 'start_time' in entry and entry['start_time']:
                        entry['start_time'] = datetime.fromisoformat(entry['start_time'])
                    if 'end_time' in entry and entry['end_time']:
                        entry['end_time'] = datetime.fromisoformat(entry['end_time'])
                return data
        except json.JSONDecodeError:
            print(f"Warning: {DOWNTIME_FILE} is corrupted. Starting with empty downtime log.")
            return []
    return []

def save_downtime_data(data: List[Dict[str, Any]]):
    """Saves downtime log to a JSON file, converting datetime objects to ISO strings."""
    serializable_data = []
    for entry in data:
        serializable_entry = entry.copy()
        # Convert datetime objects to ISO strings before saving
        if 'start_time' in serializable_entry and isinstance(serializable_entry['start_time'], datetime):
            serializable_entry['start_time'] = serializable_entry['start_time'].isoformat()
        if 'end_time' in serializable_entry and isinstance(serializable_entry['end_time'], datetime):
            serializable_entry['end_time'] = serializable_entry['end_time'].isoformat()
        serializable_data.append(serializable_entry)

    with open(DOWNTIME_FILE, 'w') as f:
        json.dump(serializable_data, f, indent=4)

# Initial load of the downtime data on server startup
DOWNTIME_LOG = load_downtime_data()
# ---------------------------------------------


# --- Function to broadcast the online machine list (UNCHANGED) ---
def broadcast_online_status():
    """Calculates the list of unique online machines and broadcasts it to all clients."""
    online_machines = list(set(SID_TO_MACHINE.values()))
    
    print(f"Currently online machines: {online_machines}")
    socketio.emit('update_machine_status', {'onlineMachines': online_machines})

# --- Helper Function to Get Data by Date (UNCHANGED) ---
def get_data_for_date(date_str):
    """Filters the submission log for a specific date (YYYY-MM-DD)."""
    if not date_str:
        return SUBMISSION_LOG

    try:
        filter_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return SUBMISSION_LOG

    filtered_log = [
        entry for entry in SUBMISSION_LOG 
        if entry.get('datetime') and entry['datetime'].date() == filter_date
    ]
    
    filtered_log.sort(key=lambda x: x['datetime'], reverse=True) 
    return filtered_log

# --- NEW Helper Function to Get Downtime Data by Date ---
def get_downtime_for_date(date_str) -> List[Dict[str, Any]]:
    """Filters the downtime log for a specific date (YYYY-MM-DD) based on start_time."""
    if not date_str:
        return DOWNTIME_LOG

    try:
        filter_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return DOWNTIME_LOG

    filtered_log = [
        entry for entry in DOWNTIME_LOG 
        if entry.get('start_time') and entry['start_time'].date() == filter_date
    ]
    
    filtered_log.sort(key=lambda x: x['start_time'], reverse=True) 
    return filtered_log

# --- Broadcast Function (UPDATED for Downtime and Stock Data) ---
def broadcast_data(date_str=None):
    """
    Broadcasts the data for the requested date to the dashboard.
    """
    global INITIAL_CABLE_STOCK, DOWNTIME_LOG

    if not date_str:
        date_str = datetime.now().strftime('%Y-%m-%d')
        
    log_to_send = get_data_for_date(date_str)
    downtime_log_to_send = get_downtime_for_date(date_str) # NEW: Get filtered downtime log
    
    data_to_send = []
    machine_qty_totals = defaultdict(int) 

    # --- Production Log Processing (Unchanged) ---
    for entry in log_to_send:
        # Note: The server combines Measured and Manual into a single field for display/export
        t1_crimp_height = entry.get('t1_crimp_height_manual') or entry.get('t1_crimp_height_measured')
        t1_insulation_height = entry.get('t1_insulation_height_manual') or entry.get('t1_insulation_height_measured')
        t1_crimp_width = entry.get('t1_crimp_width_manual') or entry.get('t1_crimp_width_measured')
        t1_insulation_width = entry.get('t1_insulation_width_manual') or entry.get('t1_insulation_width_measured')
        t1_pull_force = entry.get('t1_pull_force_manual') or entry.get('t1_pull_force_measured')
        
        t2_crimp_height = entry.get('t2_crimp_height_manual') or entry.get('t2_crimp_height_measured')
        t2_insulation_height = entry.get('t2_insulation_height_manual') or entry.get('t2_insulation_height_measured')
        t2_crimp_width = entry.get('t2_crimp_width_manual') or entry.get('t2_crimp_width_measured')
        t2_insulation_width = entry.get('t2_insulation_width_manual') or entry.get('t2_insulation_width_measured')
        t2_pull_force = entry.get('t2_pull_force_manual') or entry.get('t2_pull_force_measured')
        
        clean_entry = {
            'time_display': entry['datetime'].strftime("%Y-%m-%d %H:%M:%S"),
            'worker_name': entry.get('operator_name', 'N/A'), 
            'shift': entry.get('shift', 'N/A'), 
            'machine_name': entry.get('machine_name', 'N/A'),
            'fg_part_no': entry.get('fg_part_no', 'N/A'),
            'cable_id': entry.get('cable_id', 'N/A'),
            'produced_qty': entry.get('produced_qty', 0),
            'produced_length': entry.get('produced_length', 0.0),
            'qty_produced_hours': entry.get('qty_produced_hours', 0.0),
            't1_terminal_id': entry.get('t1_terminal_id', ''), 
            't1_apl_no': entry.get('t1_apl_no', ''), 
            't1_crimp_height': t1_crimp_height,
            't1_insulation_height': t1_insulation_height,
            't1_crimp_width': t1_crimp_width,
            't1_insulation_width': t1_insulation_width,
            't1_pull_force': t1_pull_force,
            't2_terminal_id': entry.get('t2_terminal_id', ''), 
            't2_apl_no': entry.get('t2_apl_no', ''), 
            't2_crimp_height': t2_crimp_height,
            't2_insulation_height': t2_insulation_height,
            't2_crimp_width': t2_crimp_width,
            't2_insulation_width': t2_insulation_width,
            't2_pull_force': t2_pull_force
        }
        data_to_send.append(clean_entry)
        
        try:
             qty = int(entry.get('produced_qty', 0))
        except (ValueError, TypeError):
             qty = 0
             
        machine_qty_totals[entry.get('machine_name', 'UNKNOWN')] += qty

    chart_data = [{'machine': k, 'total_qty': v} for k, v in machine_qty_totals.items()]

    # --- NEW: Downtime Log Processing ---
    serializable_downtime = []
    for entry in downtime_log_to_send:
        downtime_entry = entry.copy()
        # Convert datetime back to ISO string for transmission to the dashboard
        if 'start_time' in downtime_entry and downtime_entry['start_time']:
            downtime_entry['start_time'] = downtime_entry['start_time'].isoformat()
        if 'end_time' in downtime_entry and downtime_entry['end_time']:
            downtime_entry['end_time'] = downtime_entry['end_time'].isoformat()
        serializable_downtime.append(downtime_entry)
    # --- END NEW: Downtime Log Processing ---

    data = {
        'log': data_to_send,
        'chart_data': chart_data,
        'machines': sorted(list(MACHINE_PLANS.keys())),
        'initial_stock': INITIAL_CABLE_STOCK, 
        'downtime_log': serializable_downtime # NEW: Include downtime log
    }
    socketio.emit('update_dashboard', data) 
    
# --- Flask Routes (dashboard_page, index, upload_plan, export_data are unchanged/fixed) ---
@app.route('/worker')
def worker_page():
    try:
        with open('worker.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
        return render_template_string(html_content)
    except FileNotFoundError:
        return "Error: worker.html not found. Ensure it is in the same directory.", 404

@app.route('/dashboard')
def dashboard_page():
    try:
        with open('dashboard.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
        return render_template_string(html_content)
    except FileNotFoundError:
        return "Error: dashboard.html not found. Ensure it is in the same directory.", 404

@app.route('/')
def index():
    return redirect(url_for('dashboard_page'))

@app.route('/upload_plan', methods=['POST'])
def upload_plan():
    target_machine = request.form.get('target_machine')
    excel_file = request.files.get('plan_sheet')

    if not target_machine or not excel_file:
        return "Error: Missing machine name or file.", 400

    if not excel_file.filename.endswith(('.xlsx', '.xls')):
        return "Error: Invalid file format. Please upload an Excel file (.xlsx or .xls).", 400

    try:
        file_stream = io.BytesIO(excel_file.read())
        df = pd.read_excel(file_stream, sheet_name=0, header=0) 
        df = df.fillna('').astype(str)
        
        plan_data_raw = df.head(10).to_dict('records')
        plan_data_processed = []
        for i, item in enumerate(plan_data_raw):
            item['line_id'] = f"{target_machine}_{i+1}" 
            item['status'] = 'pending'
            plan_data_processed.append(item)
        
        # If there's an active plan with pending rows, enqueue this plan instead of replacing
        existing_plan = MACHINE_PLANS.get(target_machine)
        has_active_pending = any(p.get('status') != 'completed' for p in existing_plan) if existing_plan else False

        if has_active_pending:
            q = MACHINE_PLAN_QUEUES.setdefault(target_machine, [])
            q.append(plan_data_processed)
            save_machine_state(MACHINE_PLANS, MACHINE_PLAN_QUEUES, MACHINE_PLAN_HISTORY)
            # Notify worker(s) in the machine room about queue size
            socketio.emit('queued_plan_count', {'count': len(q), 'machineName': target_machine}, room=target_machine)
            broadcast_data(datetime.now().strftime('%Y-%m-%d'))
            return f"Success: Plan sheet for {target_machine} uploaded and queued (existing active plan).", 200
        else:
            # No active pending plan - make this the current plan
            MACHINE_PLANS[target_machine] = plan_data_processed
            save_machine_state(MACHINE_PLANS, MACHINE_PLAN_QUEUES, MACHINE_PLAN_HISTORY)
            socketio.emit('update_worker_plan', {'plan': plan_data_processed, 'machineName': target_machine}, room=target_machine)
            # Inform workers about the current queued size (could be zero)
            socketio.emit('queued_plan_count', {'count': len(MACHINE_PLAN_QUEUES.get(target_machine, [])), 'machineName': target_machine}, room=target_machine)

            broadcast_data(datetime.now().strftime('%Y-%m-%d'))
            return f"Success: Plan sheet for {target_machine} uploaded and sent to machine room.", 200

    except ImportError:
        return "Error processing file: Missing dependency 'openpyxl'. Please install it to enable Excel reading.", 500
    except Exception as e:
        print(f"File processing error: {e}")
        return f"Error processing file: {str(e)}", 500
        
# -------------------------------------
# STOCK UPLOAD ROUTE (UNCHANGED)
# -------------------------------------
@app.route('/upload_stock', methods=['POST'])
def upload_stock():
    global INITIAL_CABLE_STOCK

    if 'stock_sheet' not in request.files:
        return jsonify({'success': False, 'error': 'No file part in the request'}), 400
    
    file = request.files['stock_sheet']
    
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No selected file'}), 400
    
    if file and file.filename.endswith(('.xlsx', '.xls')):
        try:
            file_stream = io.BytesIO(file.read())
            df = pd.read_excel(file_stream, sheet_name=0, header=0, engine='openpyxl')
            
            df.columns = df.columns.str.strip()
            
            if 'Cable ID' not in df.columns or 'Initial Stock (M)' not in df.columns:
                return jsonify({
                    'success': False, 
                    'error': "Excel file must contain columns named 'Cable ID' and 'Initial Stock (M)'."
                }), 400
            
            new_stock_data = {}
            for index, row in df.iterrows():
                cable_id = str(row['Cable ID']).strip()
                initial_stock_raw = row['Initial Stock (M)']
                
                try:
                    initial_stock = float(initial_stock_raw) 
                except (ValueError, TypeError):
                    initial_stock = 0.0
                
                if cable_id:
                    new_stock_data[cable_id] = initial_stock
            
            INITIAL_CABLE_STOCK = new_stock_data
            save_stock_data(INITIAL_CABLE_STOCK)

            # Broadcast updated dashboard data immediately
            broadcast_data(datetime.now().strftime('%Y-%m-%d'))

            return jsonify({
                'success': True,
                'message': f"Successfully updated stock for {len(INITIAL_CABLE_STOCK)} cable IDs.",
                'new_stock_data': INITIAL_CABLE_STOCK 
            }), 200

        except ImportError:
            return jsonify({'success': False, 'error': "File processing error: Missing dependency 'openpyxl'. Please install it."}), 500
        except Exception as e:
            print(f"Error processing stock upload: {e}")
            return jsonify({'success': False, 'error': f'File processing error: {str(e)}'}), 500
    else:
        return jsonify({'success': False, 'error': 'Invalid file format. Please upload an .xlsx or .xls file.'}), 400

# -------------------------------------
# NEW: CONSUMPTION UPLOAD ROUTE
# -------------------------------------
@app.route('/upload_consumption', methods=['POST'])
def upload_consumption():
    """
    Placeholder route for consumption file upload. 
    Acknowledges receipt and triggers dashboard update.
    (Actual consumption calculation is handled client-side using SUBMISSION_LOG).
    """
    if 'consume_sheet' not in request.files:
        return jsonify({'success': False, 'error': 'No file part in the request'}), 400
    
    file = request.files['consume_sheet']
    
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No selected file'}), 400

    try:
        # Read file content to discard it (or process it if logic were defined)
        file.read() 
        
        # Trigger a dashboard update to ensure the client-side inventory recalculates
        broadcast_data(datetime.now().strftime('%Y-%m-%d'))

        return jsonify({
            'success': True,
            'message': "Consumption sheet received. Inventory calculation relies on Production Log submissions and Initial Stock data." 
        }), 200

    except Exception as e:
        print(f"Error processing consumption upload: {e}")
        return jsonify({'success': False, 'error': f'File processing error: {str(e)}'}), 500


# -------------------------------------
# EXPORT PRODUCTION DATA FUNCTION (UNCHANGED)
# -------------------------------------
@app.route('/export', methods=['GET'])
def export_data():
    """Exports all stored production data to a CSV file, including terminal data."""
    
    rows = []
    sorted_log = sorted(SUBMISSION_LOG, key=lambda x: x.get('datetime', datetime.min)) 

    for entry in sorted_log:
        if 'datetime' not in entry:
            continue
            
        t1_crimp_height = entry.get('t1_crimp_height_manual') or entry.get('t1_crimp_height_measured')
        t1_insulation_height = entry.get('t1_insulation_height_manual') or entry.get('t1_insulation_height_measured')
        t1_crimp_width = entry.get('t1_crimp_width_manual') or entry.get('t1_crimp_width_measured')
        t1_insulation_width = entry.get('t1_insulation_width_manual') or entry.get('t1_insulation_width_measured')
        t1_pull_force = entry.get('t1_pull_force_manual') or entry.get('t1_pull_force_measured')
        
        t2_crimp_height = entry.get('t2_crimp_height_manual') or entry.get('t2_crimp_height_measured')
        t2_insulation_height = entry.get('t2_insulation_height_manual') or entry.get('t2_insulation_height_measured')
        t2_crimp_width = entry.get('t2_crimp_width_manual') or entry.get('t2_crimp_width_measured')
        t2_insulation_width = entry.get('t2_insulation_width_manual') or entry.get('t2_insulation_width_measured')
        t2_pull_force = entry.get('t2_pull_force_manual') or entry.get('t2_pull_force_measured')


        row = {
            'datetime_obj': entry['datetime'],
            'Shift': entry.get('shift', ''),
            'Worker Name': entry.get('operator_name', ''), 
            'Machine Name': entry.get('machine_name', ''),
            'FG Part Number': entry.get('fg_part_no', ''),
            'Cable Identification': entry.get('cable_id', ''),
            'Produced Qty': entry.get('produced_qty', 0), 
            'Produced Length': entry.get('produced_length', 0.0),
            'QTY PRODUCED HOURS': entry.get('qty_produced_hours', 0.0),
            'T1 Part No': entry.get('t1_terminal_id', ''), 
            'T1 APL NO': entry.get('t1_apl_no', ''), 
            'T1 Crimp H': t1_crimp_height,
            'T1 Insul H': t1_insulation_height,
            'T1 Crimp W': t1_crimp_width,
            'T1 Insul W': t1_insulation_width,
            'T1 Pull F (N)': t1_pull_force,
            'T2 Part No': entry.get('t2_terminal_id', ''), 
            'T2 APL NO': entry.get('t2_apl_no', ''), 
            'T2 Crimp H': t2_crimp_height,
            'T2 Insul H': t2_insulation_height,
            'T2 Crimp W': t2_crimp_width,
            'T2 Insul W': t2_insulation_width,
            'T2 Pull F (N)': t2_pull_force
        }
        rows.append(row)

    if not rows:
        return "No data to export", 204
        
    df = pd.DataFrame(rows)

    if 'datetime_obj' in df.columns:
        df.insert(0, 'Date', df['datetime_obj'].dt.strftime('%Y-%m-%d'))
        df.insert(1, 'Time', df['datetime_obj'].dt.strftime('%H:%M:%S'))
        df = df.drop(columns=['datetime_obj'])

    NEW_FIELD_NAMES = [
        'Date', 'Time', 'Shift', 'Worker Name', 'Machine Name', 
        'FG Part Number', 'Cable Identification', 'Produced Qty', 
        'Produced Length', 'QTY PRODUCED HOURS',
        'T1 Part No', 'T1 APL NO', 'T1 Crimp H', 'T1 Insul H', 'T1 Crimp W', 'T1 Insul W', 'T1 Pull F (N)', 
        'T2 Part No', 'T2 APL NO', 'T2 Crimp H', 'T2 Insul H', 'T2 Crimp W', 'T2 Insul W', 'T2 Pull F (N)' 
    ]
    
    final_cols = [col for col in NEW_FIELD_NAMES if col in df.columns]
    df = df[final_cols]
    
    csv_data = df.to_csv(index=False, encoding='utf-8-sig', quoting=csv.QUOTE_ALL)

    response = Response(
        csv_data,
        mimetype="text/csv",
        headers={
            "Content-disposition": "attachment; filename=production_data_export.csv",
            "Cache-Control": "no-cache"
        }
    )
    return response

# -------------------------------------
# NEW: EXPORT DOWNTIME ROUTE (UPDATED)
# -------------------------------------
@app.route('/export_downtime_all', methods=['GET'])
def export_downtime_all():
    """Exports the entire historical downtime log to a CSV file."""
    global DOWNTIME_LOG
    
    rows = []
    # Sort by start_time
    sorted_log = sorted(DOWNTIME_LOG, key=lambda x: x.get('start_time', datetime.min)) 

    for entry in sorted_log:
        start_dt = entry.get('start_time', datetime.min)
        end_dt = entry.get('end_time', datetime.min)
        
        rows.append({
            'Date': start_dt.strftime('%Y-%m-%d'),
            'Shift': entry.get('shift', ''),
            'Worker Name': entry.get('worker_name', ''), 
            'Machine Name': entry.get('machine_name', ''),
            'FG Part Number': entry.get('fg_part_no', ''),
            'Cable ID': entry.get('cable_id', ''),
            'T1 APL NO': entry.get('t1_apl_no', ''), # ADDED FIELD
            'T2 APL NO': entry.get('t2_apl_no', ''), # ADDED FIELD
            'Start Time': start_dt.strftime('%H:%M:%S'),
            'End Time': end_dt.strftime('%H:%M:%S'),
            'Total Hours': entry.get('total_hours', 0.0),
            'Reason': entry.get('reason', '')
        })

    if not rows:
        return "No downtime data to export", 204
        
    df = pd.DataFrame(rows)

    NEW_FIELD_NAMES = [
        'Date', 'Start Time', 'End Time', 'Total Hours', 'Machine Name', 
        'Shift', 'Worker Name', 'Reason', 'FG Part Number', 'Cable ID',
        'T1 APL NO', 'T2 APL NO' # ADDED FIELD NAMES
    ]
    
    final_cols = [col for col in NEW_FIELD_NAMES if col in df.columns]
    df = df[final_cols]
    
    csv_data = df.to_csv(index=False, encoding='utf-8-sig', quoting=csv.QUOTE_ALL)

    response = Response(
        csv_data,
        mimetype="text/csv",
        headers={
            "Content-disposition": "attachment; filename=downtime_data_export_all.csv",
            "Cache-Control": "no-cache"
        }
    )
    return response


# --- Socket.IO Event Handlers ---

@socketio.on('submit_output')
def handle_submit_output(data):
    try:
        data['datetime'] = datetime.strptime(f"{data['entry_date']} {data['entry_time']}", "%Y-%m-%d %H:%M") 
        SUBMISSION_LOG.append(data)
        save_production_log(SUBMISSION_LOG)
        broadcast_data(data['datetime'].strftime('%Y-%m-%d')) 
        socketio.emit('submission_success', {'success': True}, room=request.sid)

    except KeyError as e:
        print(f"Error processing submission: {e}")
        socketio.emit('submission_success', {'success': False, 'reason': f"Missing data field: {e}"}, room=request.sid)
    except ValueError as e:
        print(f"Date/Time format error or invalid number: {e}")
        socketio.emit('submission_success', {'success': False, 'reason': f"Invalid data format: {e}"}, room=request.sid)

# -------------------------------------
# NEW: SocketIO handler for Downtime Submission (UPDATED)
# -------------------------------------
@socketio.on('submit_downtime')
def handle_submit_downtime(data):
    global DOWNTIME_LOG
    try:
        # CORRECTION: Changed keys from 'start_time_iso' and 'end_time_iso'
        # to the simpler 'start_time' and 'end_time'.
        start_time = datetime.fromisoformat(data['start_time'])
        end_time = datetime.fromisoformat(data['end_time'])
        
        # Calculate total hours
        time_difference = end_time - start_time
        total_hours = time_difference.total_seconds() / 3600

        downtime_entry = {
            'start_time': start_time,
            'end_time': end_time,
            'total_hours': total_hours,
            'worker_name': data.get('operator_name', 'N/A'),
            'shift': data.get('shift', 'N/A'),
            'machine_name': data.get('machine_name', 'N/A'),
            'fg_part_no': data.get('fg_part_no', 'N/A'),
            'cable_id': data.get('cable_id', 'N/A'),
            'reason': data.get('reason', 'No Reason Provided'),
            # ADDED NEW FIELDS
            't1_apl_no': data.get('t1_apl_no', 'N/A'),
            't2_apl_no': data.get('t2_apl_no', 'N/A'),
        }
        
        DOWNTIME_LOG.append(downtime_entry)
        save_downtime_data(DOWNTIME_LOG) # Save to file
        broadcast_data(start_time.strftime('%Y-%m-%d')) # Broadcast data for the relevant day
        socketio.emit('downtime_submission_success', {'success': True}, room=request.sid)

    except KeyError as e:
        # Updated error message to guide debugging
        print(f"Error processing downtime submission (KeyError): {e}. Check that the client (worker.html) sends all required keys.")
        socketio.emit('downtime_submission_success', {'success': False, 'reason': f"Missing data field. Error: {e}"}, room=request.sid)
    except ValueError as e:
        print(f"Date/Time format error or invalid number (ValueError): {e}")
        socketio.emit('downtime_submission_success', {'success': False, 'reason': f"Invalid data format: {e}"}, room=request.sid)
    except Exception as e:
        print(f"General error processing downtime submission: {e}")
        socketio.emit('downtime_submission_success', {'success': False, 'reason': f"Server error: {e}"}, room=request.sid)

    # --- NEW: SCRAP MATERIAL HANDLING ---
@socketio.on('submit_scrap_data')
def handle_scrap_submission(data):
    socketio.emit('submit_scrap_data', data)
    """
    Receives scrap data from Worker and broadcasts it to the Dashboard.
    """
    # 1. Add server-side timestamp if not present
    if 'timestamp' not in data:
        data['timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # 2. Store in the server log (for persistence or later requests)
    SCRAP_LOG.append(data)
    save_scrap_log(SCRAP_LOG)
    
    print(f"Scrap Received from {data.get('machine')}: {data.get('total_meters')} meters")
    
    # 3. Broadcast to everyone (Dashboard)
    # The dashboard is listening for 'submit_scrap_data'
    socketio.emit('submit_scrap_data', data)

@socketio.on('request_scrap_history')
def handle_scrap_history_request():
    """Sends all recorded scrap to the requester (Dashboard) on startup."""
    socketio.emit('initial_scrap_data', SCRAP_LOG, room=request.sid)

@socketio.on('join_machine_room')
def handle_join_machine_room(data):
    machine_name = data.get('machineName')

    if not machine_name:
        socketio.emit('join_confirm', {'success': False, 'reason': 'Missing machine name.'}, room=request.sid)
        return

    # Check if machine is already connected by another client
    for sid, name in SID_TO_MACHINE.items():
        if name == machine_name and sid != request.sid:
            socketio.emit('join_confirm', {'success': False, 'reason': f'Machine {machine_name} is already connected.'}, room=request.sid)
            return

    SID_TO_MACHINE[request.sid] = machine_name
    join_room(machine_name)
    print(f"Client {request.sid} joined room: {machine_name}")
    broadcast_online_status()

    current_plan = MACHINE_PLANS.get(machine_name, [])
    socketio.emit('update_worker_plan', {'plan': current_plan, 'machineName': machine_name}, room=request.sid)
    # Send current queued count for this machine to the connecting client
    queued_count = len(MACHINE_PLAN_QUEUES.get(machine_name, []))
    socketio.emit('queued_plan_count', {'count': queued_count, 'machineName': machine_name}, room=request.sid)

    socketio.emit('join_confirm', {'success': True, 'machineName': machine_name}, room=request.sid)


@socketio.on('mark_plan_complete')
def handle_mark_plan_complete(data):
    line_id = data.get('lineId')
    machine_name = data.get('machineName')

    if not line_id or not machine_name:
        return

    if machine_name in MACHINE_PLANS:
        plan = MACHINE_PLANS[machine_name]
        for item in plan:
            if item.get('line_id') == line_id:
                item['status'] = 'completed'
                break

        # Notify workers of the updated current plan
        socketio.emit('update_worker_plan', {'plan': plan, 'machineName': machine_name}, room=machine_name)

        # If current plan now has no pending rows, and queue has items, dequeue the next plan
        has_pending = any(p.get('status') != 'completed' for p in plan)
        queue = MACHINE_PLAN_QUEUES.get(machine_name, [])
        if not has_pending and queue:
            next_plan = queue.pop(0)
            # Archive the completed plan before swapping
            history = MACHINE_PLAN_HISTORY.setdefault(machine_name, [])
            history.append({'archived_at': datetime.utcnow().isoformat(), 'plan': plan})
            socketio.emit('plan_history_update', {'history': history, 'machineName': machine_name}, room=machine_name)

            MACHINE_PLANS[machine_name] = next_plan
            socketio.emit('update_worker_plan', {'plan': next_plan, 'machineName': machine_name}, room=machine_name)
        
        save_machine_state(MACHINE_PLANS, MACHINE_PLAN_QUEUES, MACHINE_PLAN_HISTORY)

        # Emit updated queue size to workers
        socketio.emit('queued_plan_count', {'count': len(queue), 'machineName': machine_name}, room=machine_name)

@socketio.on('send_live_message')
def handle_send_live_message(data):
    target_machine = data.get('targetMachine')
    message_text = data.get('messageText')

    if not target_machine or not message_text:
        socketio.emit('message_sent_confirm', {'success': False, 'machineName': target_machine, 'reason': 'Missing target machine or message text.'}, room=request.sid)
        return
    
    is_online = target_machine in SID_TO_MACHINE.values()

    if is_online:
        socketio.emit('live_message', {'message': message_text}, room=target_machine)
        socketio.emit('message_sent_confirm', {'success': True, 'machineName': target_machine}, room=request.sid)
    else:
         socketio.emit('message_sent_confirm', {'success': False, 'machineName': target_machine, 'reason': 'Machine is currently offline or not connected.'}, room=request.sid)


@socketio.on('request_dashboard_data')
def handle_request_dashboard_data(data):
    """Sends data to the dashboard based on a date filter request."""
    date_to_filter = data.get('date')
    broadcast_data(date_to_filter)


@socketio.on('request_dequeue_plan')
def handle_request_dequeue_plan(data):
    """Dequeue and activate the next queued plan for a machine on worker request."""
    machine_name = data.get('machineName')
    if not machine_name:
        return

    queue = MACHINE_PLAN_QUEUES.get(machine_name, [])
    if not queue:
        socketio.emit('dequeue_failed', {'reason': 'No queued plans', 'machineName': machine_name}, room=request.sid)
        return

    # Pop the next plan and activate it
    next_plan = queue.pop(0)

    # Archive current plan (so workers can still view it later)
    current = MACHINE_PLANS.get(machine_name)
    if current and isinstance(current, list) and len(current) > 0:
        history = MACHINE_PLAN_HISTORY.setdefault(machine_name, [])
        history.append({'archived_at': datetime.utcnow().isoformat(), 'plan': current})
        socketio.emit('plan_history_update', {'history': history, 'machineName': machine_name}, room=machine_name)

    MACHINE_PLANS[machine_name] = next_plan
    save_machine_state(MACHINE_PLANS, MACHINE_PLAN_QUEUES, MACHINE_PLAN_HISTORY)
    # Broadcast the newly activated plan to the machine room
    socketio.emit('dequeued_plan', {'plan': next_plan, 'machineName': machine_name}, room=machine_name)

    # Inform workers about the updated queue size
    socketio.emit('queued_plan_count', {'count': len(queue), 'machineName': machine_name}, room=machine_name)


@socketio.on('request_plan_history')
def handle_request_plan_history(data):
    """Return archived plan history for a machine to the requesting client."""
    machine_name = data.get('machineName')
    if not machine_name:
        return

    history = MACHINE_PLAN_HISTORY.get(machine_name, [])
    # Send only to the requesting client (not broadcast to room)
    socketio.emit('plan_history', {'history': history, 'machineName': machine_name}, room=request.sid)


@socketio.on('request_queued_plans')
def handle_request_queued_plans(data):
    """Return queued plans for a machine to the requesting client for preview (does not dequeue)."""
    machine_name = data.get('machineName')
    if not machine_name:
        return

    queue = MACHINE_PLAN_QUEUES.get(machine_name, [])
    # Send only to the requesting client (not broadcast to room)
    # Use 'queued_plans' key (client expects this) and do NOT modify the queue or activate any plan here.
    socketio.emit('queued_plans', {'queued_plans': queue, 'machineName': machine_name}, room=request.sid)


@socketio.on('connect')
def handle_connect():
    """Sends the current data when a client connects."""
    if request.path == '/dashboard':
        broadcast_data(date_str=datetime.now().strftime('%Y-%m-%d')) 
        broadcast_online_status()

@socketio.on('disconnect')
def handle_disconnect():
    """Removes the disconnected client from the SID_TO_MACHINE map and updates status."""
    if request.sid in SID_TO_MACHINE:
        machine_name = SID_TO_MACHINE.pop(request.sid)
        print(f"Client {request.sid} disconnected from room: {machine_name}")
        broadcast_online_status()

# --- NEW: Graceful Shutdown Handler ---
def handle_shutdown_signal(sig, frame):
    """Handles Ctrl+C, asks for a password, and shuts down gracefully."""
    print("\n\n[SERVER] Shutdown request received (Ctrl+C).")
    try:
        password = getpass.getpass("[SERVER] Enter the server password to confirm shutdown: ")
        if password == SERVER_PASSWORD:
            print("[SERVER] Password correct. Shutting down gracefully...")
            save_downtime_data(DOWNTIME_LOG)
            save_production_log(SUBMISSION_LOG)
            save_machine_state(MACHINE_PLANS, MACHINE_PLAN_QUEUES, MACHINE_PLAN_HISTORY)
            save_scrap_log(SCRAP_LOG)
            save_stock_data(INITIAL_CABLE_STOCK)
            print("[SERVER] Downtime data saved.")
            print("[SERVER] All production and plan data saved.")
            sys.exit(0)
        else:
            print("[SERVER] Incorrect password. Shutdown aborted. Server continues to run.")
    except (EOFError, KeyboardInterrupt):
        print("\n[SERVER] Shutdown prompt cancelled. Server continues to run.")

# --- NEW: Disable Console Close Button (Windows Only) ---
def disable_close_button():
    """Disables the 'X' close button on the Windows Console to force graceful shutdown via Ctrl+C."""
    if os.name == 'nt':
        try:
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                hmenu = ctypes.windll.user32.GetSystemMenu(hwnd, False)
                # SC_CLOSE (0xF060) -> DeleteMenu (Removes the Close option entirely)
                ctypes.windll.user32.DeleteMenu(hmenu, 0xF060, 0x00000000)
                print("[SERVER] Console 'Close' (X) button disabled. Use Ctrl+C to stop server.")
        except Exception as e:
            print(f"[SERVER] Warning: Could not disable close button: {e}")

# --- Start the Server ---
if __name__ == '__main__':
    # --- NEW: Startup Password Check ---
    print("[SERVER] Starting server...")
    try:
        startup_password = getpass.getpass("[SERVER] Please enter the server password to start: ")
        if startup_password != SERVER_PASSWORD:
            print("[SERVER] Incorrect password. Server will not start.")
            sys.exit(1)
    except (EOFError, KeyboardInterrupt):
        print("\n[SERVER] Startup cancelled.")
        sys.exit(1)

    print("[SERVER] Password accepted. Initializing server...")
    signal.signal(signal.SIGINT, handle_shutdown_signal)
    disable_close_button()

    print("Starting Flask-SocketIO Server...")
    print(f"Worker Input Page: http://0.0.0.0:5000/worker")
    print(f"Dashboard Page: http://0.0.0.0:5000/dashboard")
    print("\nPress Ctrl+C to request shutdown.")

    while True:
        try:
            eventlet.wsgi.server(eventlet.listen(('', 5000)), app)
        except Exception as e:
            print(f"\n[SERVER] CRASH DETECTED: {e}")
            print("[SERVER] Restarting server in 3 seconds...")
            time.sleep(3)
            print("[SERVER] Restarting...")