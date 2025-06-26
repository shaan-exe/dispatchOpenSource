import sys
import asyncio
import datetime
import logging
from typing import Optional, Tuple, Dict

import discord
from discord.ext import commands
from discord import app_commands
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QLineEdit, QPushButton,
    QMessageBox, QFormLayout, QComboBox, QTextEdit, QTabWidget, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QSizePolicy, QHeaderView
)
from PySide6.QtCore import Qt, QTimer
from qasync import QEventLoop
from pymongo import MongoClient
import aiohttp

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

MONGODB_URI = "mongodb+srv://shaanmakim:BRlYx4onHbCPCryb@dispatch.w3oysrm.mongodb.net/?retryWrites=true&w=majority&appName=Dispatch"
client = MongoClient(MONGODB_URI)
db = client['dispatch']

DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1382943882202976336/DU2iSEJubmKMgXY2lnTtHfMzwX7KEUrjADP-OPHIIzUuRg0bOrA33UioPIPemXrLqkUo"  

async def send_webhook_log(title: str, description: str, color: int = 0x1E90FF, fields: list = None):
    embed = {
        "title": title,    
        "description": description,
        "color": color,
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "fields": fields or []
    }
    data = {
        "embeds": [embed]
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(DISCORD_WEBHOOK_URL, json=data) as resp:
            if resp.status not in (200, 204):
                logging.error(f"Failed to send webhook log: {resp.status} {await resp.text()}")

async def run_blocking(func, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!!', intents=intents)

STATUS_CHOICES = [
    "Available", "Enroute", "On Scene", "Unavailable",
    "Out of Service", "Returning to Station", "Training",
    "Patrolling", "E/R to hospital."
]

PRIORITY_MAP = {
    1: "Priority 1 - Life Threatening",
    2: "Priority 2 - Emergency",
    3: "Priority 3 - Urgent",
    4: "Priority 4 - Routine"
}

CALL_TYPES = [
    "Structure Fire", "Vehicle Fire", "Brush Fire", "Medical Emergency", "Traffic Accident",
    "Hazmat Incident", "Technical Rescue", "Public Assist", "Traffic Stop", "Felony Traffic Stop",
    "Reckless Driver", "Pursuit (Vehicle)", "Hit and Run", "Stolen Vehicle", "Wanted Person",
    "Robbery (Armed)", "Burglary (Residential)", "Burglary (Commercial)", "Assault (Simple)",
    "Assault with a Deadly Weapon", "Shots Fired", "Foot Pursuit", "Vandalism / Property Damage",
    "Officer Needs Assistance (11-99)", "Other"
]

def is_dispatcher_check():
    def predicate(interaction: discord.Interaction) -> bool:
        return (interaction.user.id in dispatch_data['dispatchers']) or interaction.user.guild_permissions.administrator
    return app_commands.check(predicate)

async def load_chat_messages():
    msgs =  db.chat_messages.find().sort("timestamp", 1).limit(100).to_list(length=100)
    return msgs

async def add_chat_message(username: str, message: str):
    doc = {
        "username": username,
        "message": message,
        "timestamp": datetime.datetime.utcnow()
    }
    await run_blocking(db.chat_messages.insert_one, doc)

async def add_unit(user_id: int, unit_name: str, user_display_name: str) -> Tuple[bool, str]:
    unit_name_clean = unit_name.strip()
    if not (1 <= len(unit_name_clean) <= 50):
        return False, "Unit name must be between 1 and 50 characters."
    existing_unit = await run_blocking(db.units_on_duty.find_one, {"user_id": user_id})
    if existing_unit:
        return False, f"User is already on duty as '{existing_unit['name']}'."
    doc = {
        'user_id': user_id,
        'name': unit_name_clean,
        'user_display_name': user_display_name,
        'status': 'Available',
        'call_id': None,
        'timestamp': datetime.datetime.utcnow()
    }
    await run_blocking(db.units_on_duty.insert_one, doc)
    logging.info(f"Unit '{unit_name_clean}' added for user id {user_id}.")
    return True, f"Unit '{unit_name_clean}' is now on duty."

async def remove_unit(user_id: int) -> Tuple[bool, str]:
    result = await run_blocking(db.units_on_duty.delete_one, {"user_id": user_id})
    if result.deleted_count == 0:
        return False, "User is not currently on duty."
    active_calls = await run_blocking(db.active_calls.find)
    for call in active_calls:
        if user_id in call.get('assigned_units', []):
            await run_blocking(db.active_calls.update_one, {"call_id": call['call_id']}, {"$pull": {"assigned_units": user_id}})
    logging.info(f"Unit {user_id} removed from duty.")
    return True, "Unit removed from duty."

async def update_unit_status(user_id: int, new_status: str) -> Tuple[bool, str]:
    if new_status not in STATUS_CHOICES:
        return False, "Invalid status choice."
    result = await run_blocking(db.units_on_duty.update_one, {"user_id": user_id}, {"$set": {"status": new_status}})
    if result.matched_count == 0:
        return False, "User is not currently on duty."
    logging.info(f"Unit {user_id} changed status to {new_status}.")
    return True, f"Status changed to {new_status}."

async def create_call(call_id: str, description: str, location: str, call_type: str, priority: int) -> Tuple[bool, str]:
    call_id = call_id.strip().upper()
    if await run_blocking(db.active_calls.find_one, {"call_id": call_id}):
        return False, f"Call ID '{call_id}' already exists."
    if call_type not in CALL_TYPES:
        return False, f"Invalid call type. Available types: {', '.join(CALL_TYPES)}"
    if priority not in PRIORITY_MAP:
        return False, "Priority must be 1, 2, 3, or 4."
    doc = {
        'call_id': call_id,
        'description': description.strip(),
        'location': location.strip(),
        'type': call_type,
        'priority': PRIORITY_MAP[priority],
        'assigned_units': [],
        'timestamp': datetime.datetime.utcnow(),
        'status': 'Active'
    }
    await run_blocking(db.active_calls.insert_one, doc)
    logging.info(f"Call {call_id} created successfully.")
    return True, f"Call '{call_id}' created."

async def assign_unit_to_call(call_id: str, user_id: int) -> Tuple[bool, str]:
    call = await run_blocking(db.active_calls.find_one, {"call_id": call_id})
    if not call:
        return False, f"Call ID '{call_id}' not found."
    if user_id in call.get('assigned_units', []):
        return False, "Unit is already assigned to this call."
    await run_blocking(db.active_calls.update_one, {"call_id": call_id}, {"$push": {"assigned_units": user_id}})
    await update_unit_status(user_id, 'Enroute')
    logging.info(f"Unit {user_id} assigned to call {call_id}.")
    return True, f"Unit assigned to call '{call_id}'."

async def remove_unit_from_call(call_id: str, user_id: int) -> Tuple[bool, str]:
    call = await run_blocking(db.active_calls.find_one, {"call_id": call_id})
    if not call:
        return False, f"Call ID '{call_id}' not found."
    if user_id not in call.get('assigned_units', []):
        return False, f"Unit is not assigned to call '{call_id}'."
    await run_blocking(db.active_calls.update_one, {"call_id": call_id}, {"$pull": {"assigned_units": user_id}})
    await update_unit_status(user_id, 'Available')
    logging.info(f"Unit {user_id} removed from call {call_id}.")
    return True, f"Unit removed from call '{call_id}'."

async def close_call(call_id: str) -> Tuple[bool, str]:
    call = await run_blocking(db.active_calls.find_one, {"call_id": call_id})
    if not call:
        return False, f"Call ID '{call_id}' not found."
    if call['status'] != 'Active':
        return False, f"Call '{call_id}' is already closed."
    await run_blocking(db.active_calls.update_one, {"call_id": call_id}, {"$set": {"status": "Closed"}})
    for unit_id in call['assigned_units']:
        await update_unit_status(unit_id, 'Returning to Station')
    logging.info(f"Call {call_id} closed.")
    return True, f"Call '{call_id}' closed."

class ChatDialog(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.username = None
        self.layout = QVBoxLayout(self)

        self.username_label = QLabel("Enter your name:")
        self.layout.addWidget(self.username_label)

        self.username_input = QLineEdit()
        self.layout.addWidget(self.username_input)

        self.btn_start = QPushButton("Start Chat")
        self.layout.addWidget(self.btn_start)
        self.btn_start.clicked.connect(self.start_chat)

        self.chat_display = QTextEdit()
        self.chat_display.setReadOnly(True)
        self.chat_display.hide()
        self.layout.addWidget(self.chat_display)

        self.message_input = QLineEdit()
        self.message_input.setPlaceholderText("Type your message and press Enter...")
        self.message_input.returnPressed.connect(self.send_message)
        self.message_input.hide()
        self.layout.addWidget(self.message_input)

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: asyncio.create_task(self.refresh_messages()))

    def start_chat(self):
        name = self.username_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Input error", "Please enter your name to start chat.")
            return
        self.username = name
        self.username_label.hide()
        self.username_input.hide()
        self.btn_start.hide()

        self.chat_display.show()
        self.message_input.show()
        self.timer.start(3000)  
        asyncio.create_task(self.refresh_messages())

    async def refresh_messages(self):
        try:
            msgs = await load_chat_messages()
            self.chat_display.clear()
            for msg in msgs:
                timestamp = msg.get("timestamp")
                timestr = timestamp.strftime('%H:%M:%S') if timestamp else ""
                user = msg.get("username", "Unknown")
                text = msg.get("message", "")
                self.chat_display.append(f"[{timestr}] <b>{user}:</b> {text}")
            self.chat_display.verticalScrollBar().setValue(self.chat_display.verticalScrollBar().maximum())
        except Exception as e:
            self.chat_display.append(f"<i>Error loading chat: {str(e)}</i>")

    def send_message(self):
        msg = self.message_input.text().strip()
        if not msg or not self.username:
            return
        self.message_input.clear()
        asyncio.create_task(add_chat_message(self.username, msg))

class DispatchDashboard(QWidget):
    SPECIFIC_GUILD_ID = 1382497395279003738  

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Dispatch Dashboard")
        self.resize(1000, 700)
        self.users = []
        self.status_bar = QLabel("Initializing...")
        self._build_ui()
        self._init_live_update_timer()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        title_label = QLabel("<h2>Dispatch Operator Dashboard</h2>")
        title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(title_label)

        live_view_container = QHBoxLayout()

        self.calls_table = QTableWidget(0, 7)
        self.calls_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.calls_table.setHorizontalHeaderLabels([
            "Call ID", "Description", "Type", "Priority", "Location", "Status", "Assigned Units"
        ])
        self.calls_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.calls_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.calls_table.setSelectionBehavior(QTableWidget.SelectRows)
        live_view_container.addWidget(self.calls_table, 3)

        self.units_table = QTableWidget(0, 5)
        self.units_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.units_table.setHorizontalHeaderLabels([
            "User  ID", "Calls Sign", "Status", "Assigned Call", "Last Updated"
        ])
        self.units_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.units_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.units_table.setSelectionBehavior(QTableWidget.SelectRows)
        live_view_container.addWidget(self.units_table, 2)

        layout.addLayout(live_view_container)

        layout.addWidget(self.status_bar)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        self._add_tab_add_unit()
        self._add_tab_remove_unit()
        self._add_tab_update_status()
        self._add_tab_create_call()
        self._add_tab_assign_unit()
        self._add_tab_remove_unit_call()
        self._add_tab_close_call()
        self._add_tab_manual()
        self._add_tab_chat()

    def _add_tab_chat(self):
        self.tab_chat = QWidget()
        self.tabs.addTab(self.tab_chat, "Chat")
        layout = QVBoxLayout(self.tab_chat)
        self.chat_widget = ChatDialog(self)
        layout.addWidget(self.chat_widget)

    def _init_live_update_timer(self):
        self.timer = QTimer(self)
        self.timer.setInterval(4000)
        self.timer.timeout.connect(lambda: asyncio.create_task(self.update_live_views()))
        self.timer.start()

    async def update_live_views(self):
        await self._update_calls_table()
        await self._update_units_table()
        await self.populate_call_id_combos()

        calls =  db.active_calls.find().to_list(None)
        units =  db.units_on_duty.find().to_list(None)

        total_calls = len(calls)
        active_calls = sum(1 for c in calls if c.get("status") == "Active")
        total_units = len(units)
        self.status_bar.setText(f"Total Calls: {total_calls} (Active: {active_calls}) | Units On Duty: {total_units}")

    async def _update_calls_table(self):
        calls =  db.active_calls.find().to_list(None)

        self.calls_table.setRowCount(len(calls))
        for row, cdata in enumerate(calls):
            self.calls_table.setItem(row, 0, QTableWidgetItem(cdata.get("call_id", "")))
            self.calls_table.setItem(row, 1, QTableWidgetItem(cdata.get("description", "")))
            self.calls_table.setItem(row, 2, QTableWidgetItem(cdata.get("type", "")))
            self.calls_table.setItem(row, 3, QTableWidgetItem(cdata.get("priority", "")))
            self.calls_table.setItem(row, 4, QTableWidgetItem(cdata.get("location", "")))
            self.calls_table.setItem(row, 5, QTableWidgetItem(cdata.get("status", "")))

            assigned_units = cdata.get("assigned_units", [])
            assigned_names = []
            for uid in assigned_units:
                unit_doc =  db.units_on_duty.find_one({"user_id": uid})
                if unit_doc:
                    assigned_names.append(unit_doc.get("name", str(uid)))
                else:
                    assigned_names.append(str(uid))
            self.calls_table.setItem(row, 6, QTableWidgetItem(", ".join(assigned_names)))

    async def _update_units_table(self):
        units =  db.units_on_duty.find().to_list(None)
        self.units_table.setRowCount(len(units))
        for row, udata in enumerate(units):
            user_id = udata.get("user_id", None)

            user_info = next((u for u in self.users if u['id'] == user_id), None)
            if user_info:
                user_display = f"{user_info['display_name']}#{user_info['discriminator']} ({user_id})"
            else:
                user_display = str(user_id) if user_id is not None else ""
            self.units_table.setItem(row, 0, QTableWidgetItem(user_display))
            self.units_table.setItem(row, 1, QTableWidgetItem(udata.get("name", "")))
            self.units_table.setItem(row, 2, QTableWidgetItem(udata.get("status", "")))
            self.units_table.setItem(row, 3, QTableWidgetItem(str(udata.get("call_id", "None"))))
            timestamp_str = udata.get("timestamp")
            if isinstance(timestamp_str, datetime.datetime):
                timestamp_str = timestamp_str.isoformat()
            self.units_table.setItem(row, 4, QTableWidgetItem(timestamp_str if timestamp_str else ""))

    def _add_tab_add_unit(self):
        self.tab_add_unit = QWidget()
        self.tabs.addTab(self.tab_add_unit, "Add Unit")
        l = QVBoxLayout()
        self.tab_add_unit.setLayout(l)
        form = QFormLayout()
        l.addLayout(form)

        self.add_unit_user_combo = QComboBox()
        form.addRow("Select User:", self.add_unit_user_combo)
        self.add_unit_callsign = QLineEdit()
        self.add_unit_callsign.setPlaceholderText("e.g. 7L-123 or RA-05A")
        form.addRow("Unit Callsign:", self.add_unit_callsign)

        btn = QPushButton("Add Unit")
        btn.clicked.connect(self.handle_add_unit)
        l.addWidget(btn)

        self.add_unit_output = QTextEdit()
        self.add_unit_output.setReadOnly(True)
        l.addWidget(self.add_unit_output)

    def _add_tab_remove_unit(self):
        self.tab_remove_unit = QWidget()
        self.tabs.addTab(self.tab_remove_unit, "Remove Unit")
        l = QVBoxLayout()
        self.tab_remove_unit.setLayout(l)
        form = QFormLayout()
        l.addLayout(form)

        self.remove_unit_user_combo_onduty = QComboBox()
        form.addRow("Select User:", self.remove_unit_user_combo_onduty)

        btn = QPushButton("Remove Unit")
        btn.clicked.connect(self.handle_remove_unit)
        l.addWidget(btn)

        self.remove_unit_output = QTextEdit()
        self.remove_unit_output.setReadOnly(True)
        l.addWidget(self.remove_unit_output)

    def _add_tab_update_status(self):
        self.tab_update_status = QWidget()
        self.tabs.addTab(self.tab_update_status, "Update Status")
        l = QVBoxLayout()
        self.tab_update_status.setLayout(l)
        form = QFormLayout()
        l.addLayout(form)

        self.update_status_user_combo = QComboBox()
        form.addRow("Select User:", self.update_status_user_combo)
        self.update_status_enum = QComboBox()
        for status in STATUS_CHOICES:
            self.update_status_enum.addItem(status)
        form.addRow("Select Status:", self.update_status_enum)

        btn = QPushButton("Update Status")
        btn.clicked.connect(self.handle_update_status)
        l.addWidget(btn)

        self.update_status_output = QTextEdit()
        self.update_status_output.setReadOnly(True)
        l.addWidget(self.update_status_output)

    def _add_tab_create_call(self):
        self.tab_create_call = QWidget()
        self.tabs.addTab(self.tab_create_call, "Create Call")
        l = QVBoxLayout()
        self.tab_create_call.setLayout(l)
        form = QFormLayout()
        l.addLayout(form)

        self.create_call_id = QLineEdit()
        form.addRow("Call ID:", self.create_call_id)

        self.create_call_description = QLineEdit()
        form.addRow("Description:", self.create_call_description)

        self.create_call_location = QLineEdit()
        form.addRow("Location:", self.create_call_location)

        self.create_call_type = QComboBox()
        for ct in CALL_TYPES:
            self.create_call_type.addItem(ct)
        form.addRow("Call Type:", self.create_call_type)

        self.create_call_priority = QComboBox()
        for pr in ["1 - Life Threatening", "2 - Emergency", "3 - Urgent", "4 - Routine"]:
            self.create_call_priority.addItem(pr)
        form.addRow("Priority:", self.create_call_priority)

        btn = QPushButton("Create Call")
        btn.clicked.connect(self.handle_create_call)
        l.addWidget(btn)

        self.create_call_output = QTextEdit()
        self.create_call_output.setReadOnly(True)
        l.addWidget(self.create_call_output)

    def _add_tab_assign_unit(self):
        self.tab_assign_unit = QWidget()
        self.tabs.addTab(self.tab_assign_unit, "Assign Unit")
        l = QVBoxLayout()
        self.tab_assign_unit.setLayout(l)
        form = QFormLayout()
        l.addLayout(form)

        self.assign_call_id_combo = QComboBox()
        form.addRow("Call ID:", self.assign_call_id_combo)

        self.assign_unit_user_combo = QComboBox()
        form.addRow("Select User:", self.assign_unit_user_combo)

        btn = QPushButton("Assign Unit")
        btn.clicked.connect(self.handle_assign_unit)
        l.addWidget(btn)

        self.assign_unit_output = QTextEdit()
        self.assign_unit_output.setReadOnly(True)
        l.addWidget(self.assign_unit_output)

    def _add_tab_remove_unit_call(self):
        self.tab_remove_unit_call = QWidget()
        self.tabs.addTab(self.tab_remove_unit_call, "Remove Unit From Call")
        l = QVBoxLayout()
        self.tab_remove_unit_call.setLayout(l)
        form = QFormLayout()
        l.addLayout(form)

        self.remove_unit_call_id_combo = QComboBox()
        form.addRow("Call ID:", self.remove_unit_call_id_combo)

        self.remove_unit_call_user_combo = QComboBox()
        form.addRow("Select User:", self.remove_unit_call_user_combo)

        btn = QPushButton("Remove Unit From Call")
        btn.clicked.connect(self.handle_remove_unit_from_call)
        l.addWidget(btn)

        self.remove_unit_call_output = QTextEdit()
        self.remove_unit_call_output.setReadOnly(True)
        l.addWidget(self.remove_unit_call_output)

    def _add_tab_close_call(self):
        self.tab_close_call = QWidget()
        self.tabs.addTab(self.tab_close_call, "Close Call")
        l = QVBoxLayout()
        self.tab_close_call.setLayout(l)
        form = QFormLayout()
        l.addLayout(form)

        self.close_call_id_combo = QComboBox()
        form.addRow("Call ID:", self.close_call_id_combo)

        btn = QPushButton("Close Call")
        btn.clicked.connect(self.handle_close_call)
        l.addWidget(btn)

        self.close_call_output = QTextEdit()
        self.close_call_output.setReadOnly(True)
        l.addWidget(self.close_call_output)

    def _add_tab_manual(self):
        self.tab_send_manual = QWidget()
        self.tabs.addTab(self.tab_send_manual, "Send Manual Command")
        l = QVBoxLayout()
        self.tab_send_manual.setLayout(l)

        self.manual_command_input = QLineEdit()
        self.manual_command_input.setPlaceholderText("Enter raw command text, e.g. 'add_unit 12345 RA-01'")
        l.addWidget(self.manual_command_input)

        self.btn_send_manual = QPushButton("Execute Command")
        self.btn_send_manual.clicked.connect(self.handle_send_manual)
        l.addWidget(self.btn_send_manual)

        self.manual_command_output = QTextEdit()
        self.manual_command_output.setReadOnly(True)
        l.addWidget(self.manual_command_output)

    async def populate_user_comboboxes(self):
        on_duty_units =  db.units_on_duty.find().to_list(length=None)

        display_names = []
        for u in on_duty_units:
            matching_user = next((user for user in self.users if user['id'] == u.get('user_id')), None)
            if matching_user:
                display_names.append(f"{matching_user['display_name']}#{matching_user['discriminator']}")
            else:
                display_names.append(str(u.get('user_id', 'Unknown User')))

        combos = []
        if hasattr(self, 'remove_unit_user_combo_onduty'):
            combos.append(self.remove_unit_user_combo_onduty)
        if hasattr(self, 'update_status_user_combo'):
            combos.append(self.update_status_user_combo)
        if hasattr(self, 'assign_unit_user_combo'):
            combos.append(self.assign_unit_user_combo)
        if hasattr(self, 'remove_unit_call_user_combo'):
            combos.append(self.remove_unit_call_user_combo)

        for combo in combos:
            combo.clear()
            combo.addItems(display_names)

    async def populate_user_comboboxes_onduty(self):
        display_names = [f"{u['display_name']}#{u['discriminator']}" for u in self.users]

        if hasattr(self, 'add_unit_user_combo'):
            self.add_unit_user_combo.clear()
            self.add_unit_user_combo.addItems(display_names)

    async def populate_call_id_combos(self):
        calls =  db.active_calls.find({"status": "Active"}).to_list(length=100)
        call_ids = [c.get("call_id", "") for c in calls]

        combos = []
        if hasattr(self, 'assign_call_id_combo'):
            combos.append(self.assign_call_id_combo)
        if hasattr(self, 'remove_unit_call_id_combo'):
            combos.append(self.remove_unit_call_id_combo)
        if hasattr(self, 'close_call_id_combo'):
            combos.append(self.close_call_id_combo)

        for combo in combos:
            combo.clear()
            combo.addItems(call_ids)

    def user_id_by_display(self, display_text):
        for u in self.users:
            full = f"{u['display_name']}#{u['discriminator']}"
            if full == display_text:
                return u['id'], u['display_name']
        return None, None

    def _set_text_threadsafe(self, text_edit: QTextEdit, text: str):
        text_edit.setPlainText(text)

    def _set_status_threadsafe(self, text: str):
        self.status_bar.setText(text)

    def show_message_box(self, title, message):
        QMessageBox.warning(self, title, message)

    def run_async_task(self, coro, output_widget: QTextEdit, success_msg: str = None, on_success: Optional[callable] = None):
        async def wrapper():
            try:
                success, msg = await coro
                out_text = success_msg if success else f"Error: {msg}"
                self._set_text_threadsafe(output_widget, out_text)
                self._set_status_threadsafe(out_text)
                if success and on_success:
                    await on_success()
            except Exception as e:
                self._set_text_threadsafe(output_widget, f"Exception: {str(e)}")
                logging.exception("Error executing command from GUI")
        asyncio.create_task(wrapper())

    def handle_add_unit(self):
        user_display = self.add_unit_user_combo.currentText()
        user_id, display_name = self.user_id_by_display(user_display)
        unit_callsign = self.add_unit_callsign.text().strip()
        if not user_id or not unit_callsign:
            self.show_message_box("Input error", "Please select user and enter unit callsign.")
            return
        self._set_text_threadsafe(self.add_unit_output, "Adding unit...")

        async def refresh_and_log():
            success, msg = await add_unit(user_id, unit_callsign, display_name)
            if success:
                await self.populate_user_comboboxes()
                await send_webhook_log(
                    "Unit Added",
                    f"User **{display_name}** is now on duty as `{unit_callsign}`.",
                    color=0x1E90FF
                )
            return success, msg

        self.run_async_task(
            refresh_and_log(),
            self.add_unit_output,
            f"Added unit '{unit_callsign}' for {user_display}"
        )

    def handle_remove_unit(self):
        user_display = self.remove_unit_user_combo_onduty.currentText()
        user_id, _ = self.user_id_by_display(user_display)
        if not user_id:
            self.show_message_box("Input error", "Please select a user.")
            return
        self._set_text_threadsafe(self.remove_unit_output, "Removing unit...")

        async def refresh_and_log():
            success, msg = await remove_unit(user_id)
            if success:
                await self.populate_user_comboboxes()
                await send_webhook_log(
                    "Unit Removed",
                    f"User `{user_display}` has been removed from duty.",
                    color=0xff0000
                )
            return success, msg

        self.run_async_task(
            refresh_and_log(),
            self.remove_unit_output,
            f"Removed unit for {user_display}"
        )

    def handle_create_call(self):
        callid = self.create_call_id.text().strip()
        desc = self.create_call_description.text().strip()
        loc = self.create_call_location.text().strip()
        ctype = self.create_call_type.currentText()
        prio_map = {"1 - Life Threatening": 1, "2 - Emergency": 2, "3 - Urgent": 3, "4 - Routine": 4}
        prio = prio_map.get(self.create_call_priority.currentText(), 4)
        if not callid or not desc or not loc:
            self.show_message_box("Input error", "Call ID, description, and location are required.")
            return
        self._set_text_threadsafe(self.create_call_output, "Creating call...")

        async def refresh_calls_and_log():
            success, msg = await create_call(callid, desc, loc, ctype, prio)
            if success:
                await self.populate_call_id_combos()
                await send_webhook_log(
                    "Call Created",
                    f"Call `{callid}` created.\n**Description:** {desc}\n**Location:** {loc}\n**Type:** {ctype}\n**Priority:** {PRIORITY_MAP.get(prio, str(prio))}",
                    color=0x00ff00
                )
            return success, msg

        self.run_async_task(
            refresh_calls_and_log(),
            self.create_call_output,
            f"Call '{callid}' created."
        )

    def handle_close_call(self):
        callid = self.close_call_id_combo.currentText()
        if not callid:
            self.show_message_box("Input error", "Call ID required.")
            return
        self._set_text_threadsafe(self.close_call_output, "Closing call...")

        async def refresh_calls_and_log():
            success, msg = await close_call(callid)
            if success:
                await self.populate_call_id_combos()
                await send_webhook_log(
                    "Call Closed",
                    f"Call `{callid}` has been closed.",
                    color=0xff4500
                )
            return success, msg

        self.run_async_task(
            refresh_calls_and_log(),
            self.close_call_output,
            f"Call '{callid}' closed."
        )

    def handle_assign_unit(self):
        callid = self.assign_call_id_combo.currentText()
        user_display = self.assign_unit_user_combo.currentText()
        user_id, _ = self.user_id_by_display(user_display)
        if not callid or not user_id:
            self.show_message_box("Input error", "Call ID and user selection required.")
            return
        self._set_text_threadsafe(self.assign_unit_output, "Assigning unit...")

        async def assign_and_log():
            success, msg = await assign_unit_to_call(callid, user_id)
            if success:
                await send_webhook_log(
                    "Unit Assigned",
                    f"Unit `{user_display}` assigned to call `{callid}`.",
                    color=0x1E90FF
                )
            return success, msg

        self.run_async_task(
            assign_and_log(),
            self.assign_unit_output,
            f"Unit assigned to call '{callid}'."
        )

    def handle_remove_unit_from_call(self):
        callid = self.remove_unit_call_id_combo.currentText()
        user_display = self.remove_unit_call_user_combo.currentText()
        user_id, _ = self.user_id_by_display(user_display)
        if not callid or not user_id:
            self.show_message_box("Input error", "Call ID and user selection required.")
            return
        self._set_text_threadsafe(self.remove_unit_call_output, "Removing unit from call...")

        async def remove_and_log():
            success, msg = await remove_unit_from_call(callid, user_id)
            if success:
                await send_webhook_log(
                    "Unit Removed from Call",
                    f"Unit `{user_display}` removed from call `{callid}`.",
                    color=0xff0000
                )
            return success, msg

        self.run_async_task(
            remove_and_log(),
            self.remove_unit_call_output,
            f"Unit removed from call '{callid}'."
        )

    def handle_update_status(self):
        user_display = self.update_status_user_combo.currentText()
        user_id, _ = self.user_id_by_display(user_display)
        status = self.update_status_enum.currentText()
        if not user_id or not status:
            self.show_message_box("Input error", "Please select user and status.")
            return
        self._set_text_threadsafe(self.update_status_output, "Updating status...")

        async def update_and_log():
            success, msg = await update_unit_status(user_id, status)
            if success:
                await send_webhook_log(
                    "Status Updated",
                    f"Unit `{user_display}` status changed to '{status}'.",
                    color=0x1E90FF
                )
            return success, msg

        self.run_async_task(
            update_and_log(),
            self.update_status_output,
            f"Status updated to '{status}' for {user_display}"
        )

    def handle_send_manual(self):
        cmd_text = self.manual_command_input.text().strip()
        if not cmd_text:
            self.show_message_box("Input error", "Please enter a command.")
            return

        self._set_text_threadsafe(self.manual_command_output, "Executing command...")
        parts = cmd_text.split()
        if not parts:
            self._set_text_threadsafe(self.manual_command_output, "Invalid command syntax.")
            return

        cmd = parts[0].lower()

        async def manual_command_executor():
            try:
                if cmd == 'add_unit':
                    if len(parts) < 3:
                        return False, "Usage: add_unit <user_id> <unit_name>"
                    user_id = int(parts[1])
                    unit_name = ' '.join(parts[2:])
                    display_name = next((u['display_name'] for u in self.users if u['id'] == user_id), f"User -{user_id}")
                    result = await add_unit(user_id, unit_name, display_name)
                    if result[0]:
                        await self.populate_user_comboboxes()
                        await send_webhook_log(
                            "Unit Added",
                            f"User **{display_name}** is now on duty as `{unit_name}`.",
                            color=0x1E90FF
                        )
                    return result
                elif cmd == 'remove_unit':
                    if len(parts) < 2:
                        return False, "Usage: remove_unit <user_id>"
                    user_id = int(parts[1])
                    result = await remove_unit(user_id)
                    if result[0]:
                        await self.populate_user_comboboxes()
                        await send_webhook_log(
                            "Unit Removed",
                            f"User with ID `{user_id}` has been removed from duty.",
                            color=0xff0000
                        )
                    return result
                elif cmd == 'update_status':
                    if len(parts) < 3:
                        return False, "Usage: update_status <user_id> <status>"
                    user_id = int(parts[1])
                    status_val = ' '.join(parts[2:])
                    result = await update_unit_status(user_id, status_val)
                    if result[0]:
                        await send_webhook_log(
                            "Status Updated",
                            f"Unit with ID `{user_id}` status changed to '{status_val}'.",
                            color=0x1E90FF
                        )
                    return result
                elif cmd == 'create_call':
                    if len(parts) < 6:
                        return False, "Usage: create_call <call_id> <description> <location> <call_type> <priority>"
                    call_id = parts[1]
                    description = parts[2]
                    location = parts[3]
                    call_type = parts[4]
                    try:
                        priority = int(parts[5])
                    except ValueError:
                        return False, "Priority must be a number (1-4)."
                    result = await create_call(call_id, description, location, call_type, priority)
                    if result[0]:
                        await self.populate_call_id_combos()
                        await send_webhook_log(
                            "Call Created",
                            f"Call `{call_id}` created.\n**Description:** {description}\n**Location:** {location}\n**Type:** {call_type}\n**Priority:** {PRIORITY_MAP.get(priority, str(priority))}",
                            color=0x00ff00
                        )
                    return result
                elif cmd == 'assign_unit':
                    if len(parts) < 3:
                        return False, "Usage: assign_unit <call_id> <user_id>"
                    call_id = parts[1]
                    user_id = int(parts[2])
                    result = await assign_unit_to_call(call_id, user_id)
                    if result[0]:
                        await send_webhook_log(
                            "Unit Assigned",
                            f"Unit with ID `{user_id}` assigned to call `{call_id}`.",
                            color=0x1E90FF
                        )
                    return result
                elif cmd == 'remove_unit_from_call':
                    if len(parts) < 3:
                        return False, "Usage: remove_unit_from_call <call_id> <user_id>"
                    call_id = parts[1]
                    user_id = int(parts[2])
                    result = await remove_unit_from_call(call_id, user_id)
                    if result[0]:
                        await send_webhook_log(
                            "Unit Removed from Call",
                            f"Unit with ID `{user_id}` removed from call `{call_id}`.",
                            color=0xff0000
                        )
                    return result
                elif cmd == 'close_call':
                    if len(parts) < 2:
                        return False, "Usage: close_call <call_id>"
                    call_id = parts[1]
                    result = await close_call(call_id)
                    if result[0]:
                        await self.populate_call_id_combos()
                        await send_webhook_log(
                            "Call Closed",
                            f"Call `{call_id}` closed.",
                            color=0xff4500
                        )
                    return result
                else:
                    return False, f"Unknown command '{cmd}'."
            except Exception as e:
                logging.exception("Exception in manual command executor")
                return False, f"Exception: {str(e)}"

        async def run_and_report():
            success, msg = await manual_command_executor()
            out_text = f"Command '{cmd}' executed successfully." if success else f"Error: {msg}"
            self._set_text_threadsafe(self.manual_command_output, out_text)
            self._set_status_threadsafe(out_text)

        asyncio.create_task(run_and_report())

    async def async_init(self):
        try:
            guild = bot.get_guild(self.SPECIFIC_GUILD_ID)
            if guild is None:
                self._set_status_threadsafe(f"Guild with ID {self.SPECIFIC_GUILD_ID} not found.")
                logging.error(f"Guild with ID {self.SPECIFIC_GUILD_ID} not found.")
                return

            await guild.chunk()

            self.users = []
            for member in guild.members:
                self.users.append({
                    'id': member.id,
                    'display_name': member.display_name,
                    'discriminator': member.discriminator
                })

            await self.populate_user_comboboxes_onduty()  
            await self.populate_user_comboboxes()         
            await self.populate_call_id_combos()           

            self._set_status_threadsafe(f"Loaded {len(self.users)} users from guild '{guild.name}'.")
            logging.info(f"Loaded {len(self.users)} users from guild '{guild.name}'.")
        except Exception as e:
            logging.exception("Failed to initialize dashboard users.")
            self._set_status_threadsafe(f"Error loading users: {str(e)}")

@bot.event
async def on_ready():
    logging.info(f"Bot connected as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        logging.info(f"Synced {len(synced)} commands")
    except Exception as e:
        logging.error(f"Failed to sync commands: {e}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    dashboard = DispatchDashboard()
    dashboard.show()

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    async def main():
        bot_task = asyncio.create_task(bot.start(''))  

        while not bot.is_ready():
            await asyncio.sleep(0.1)
        await dashboard.async_init()
        await bot_task

    loop.create_task(main())

    with loop:
        loop.run_forever()
