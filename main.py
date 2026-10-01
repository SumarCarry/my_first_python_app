import json
import sqlite3
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


APP_DIR = Path(__file__).resolve().parent
DATABASE = APP_DIR / "tracker.sqlite3"
HOST = "127.0.0.1"
PORT = 8765


def connect_db():
	connection = sqlite3.connect(DATABASE)
	connection.row_factory = sqlite3.Row
	connection.execute("PRAGMA foreign_keys = ON")
	return connection


def initialize_db():
	with connect_db() as connection:
		connection.executescript(
			"""
			CREATE TABLE IF NOT EXISTS habits (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				name TEXT NOT NULL,
				created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
			);
			CREATE TABLE IF NOT EXISTS habit_logs (
				habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
				day TEXT NOT NULL,
				completed INTEGER NOT NULL DEFAULT 0,
				PRIMARY KEY (habit_id, day)
			);
			CREATE TABLE IF NOT EXISTS tasks (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				title TEXT NOT NULL,
				due_date TEXT,
				completed INTEGER NOT NULL DEFAULT 0,
				created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
			);
			"""
		)


class TrackerHandler(BaseHTTPRequestHandler):
	def send_json(self, payload, status=200):
		body = json.dumps(payload).encode("utf-8")
		self.send_response(status)
		self.send_header("Content-Type", "application/json; charset=utf-8")
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def read_json(self):
		length = int(self.headers.get("Content-Length", "0"))
		return json.loads(self.rfile.read(length) or b"{}")

	def do_GET(self):
		parsed = urlparse(self.path)
		if parsed.path == "/api/state":
			selected_day = parse_qs(parsed.query).get("date", [date.today().isoformat()])[0]
			try:
				date.fromisoformat(selected_day)
			except ValueError:
				self.send_json({"error": "Invalid date"}, 400)
				return

			with connect_db() as connection:
				habits = connection.execute(
					"""
					SELECT habits.id, habits.name, COALESCE(habit_logs.completed, 0) AS completed
					FROM habits
					LEFT JOIN habit_logs ON habit_logs.habit_id = habits.id AND habit_logs.day = ?
					ORDER BY habits.id
					""",
					(selected_day,),
				).fetchall()
				tasks = connection.execute(
					"SELECT id, title, due_date, completed FROM tasks ORDER BY completed, due_date IS NULL, due_date, id"
				).fetchall()
			self.send_json(
				{
					"date": selected_day,
					"habits": [dict(row) for row in habits],
					"tasks": [dict(row) for row in tasks],
				}
			)
			return

		if parsed.path == "/":
			page = (APP_DIR / "index.html").read_bytes()
			self.send_response(200)
			self.send_header("Content-Type", "text/html; charset=utf-8")
			self.send_header("Content-Length", str(len(page)))
			self.end_headers()
			self.wfile.write(page)
			return

		self.send_error(404)

	def do_POST(self):
		parsed = urlparse(self.path)
		try:
			payload = self.read_json()
		except (json.JSONDecodeError, UnicodeDecodeError):
			self.send_json({"error": "Invalid JSON"}, 400)
			return

		if parsed.path == "/api/habits":
			name = str(payload.get("name", "")).strip()
			if not name or len(name) > 80:
				self.send_json({"error": "Habit names must be 1 to 80 characters"}, 400)
				return
			with connect_db() as connection:
				cursor = connection.execute("INSERT INTO habits (name) VALUES (?)", (name,))
				habit_id = cursor.lastrowid
			self.send_json({"id": habit_id, "name": name}, 201)
			return

		if parsed.path == "/api/tasks":
			title = str(payload.get("title", "")).strip()
			due_date = payload.get("due_date") or None
			if not title or len(title) > 160:
				self.send_json({"error": "Tasks must be 1 to 160 characters"}, 400)
				return
			if due_date:
				try:
					date.fromisoformat(due_date)
				except (TypeError, ValueError):
					self.send_json({"error": "Invalid due date"}, 400)
					return
			with connect_db() as connection:
				cursor = connection.execute(
					"INSERT INTO tasks (title, due_date) VALUES (?, ?)", (title, due_date)
				)
				task_id = cursor.lastrowid
			self.send_json({"id": task_id, "title": title, "due_date": due_date}, 201)
			return

		parts = parsed.path.strip("/").split("/")
		if len(parts) == 4 and parts[0] == "api" and parts[1] in ("habits", "tasks") and parts[3] == "toggle":
			entity, identifier = parts[1], parts[2]
			table = "habits" if entity == "habits" else "tasks" if entity == "tasks" else None
			try:
				item_id = int(identifier)
			except ValueError:
				item_id = 0
			completed = 1 if payload.get("completed") else 0
			if table == "habits":
				selected_day = payload.get("date", date.today().isoformat())
				try:
					date.fromisoformat(selected_day)
				except (TypeError, ValueError):
					self.send_json({"error": "Invalid date"}, 400)
					return
				with connect_db() as connection:
					connection.execute(
						"INSERT INTO habit_logs (habit_id, day, completed) VALUES (?, ?, ?) "
						"ON CONFLICT(habit_id, day) DO UPDATE SET completed = excluded.completed",
						(item_id, selected_day, completed),
					)
			elif table == "tasks":
				with connect_db() as connection:
					connection.execute("UPDATE tasks SET completed = ? WHERE id = ?", (completed, item_id))
			else:
				self.send_error(404)
				return
			self.send_json({"ok": True})
			return

		self.send_error(404)

	def do_DELETE(self):
		parts = urlparse(self.path).path.strip("/").split("/")
		if len(parts) != 3 or parts[0] != "api" or parts[1] not in ("habits", "tasks"):
			self.send_error(404)
			return
		try:
			item_id = int(parts[2])
		except ValueError:
			self.send_error(404)
			return
		table = "habits" if parts[1] == "habits" else "tasks"
		with connect_db() as connection:
			connection.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
		self.send_json({"ok": True})

	def log_message(self, format_string, *args):
		pass


if __name__ == "__main__":
	initialize_db()
	address = f"http://{HOST}:{PORT}"
	print(f"Daily Habit Tracker is running at {address}")
	webbrowser.open(address)
	try:
		ThreadingHTTPServer((HOST, PORT), TrackerHandler).serve_forever()
	except KeyboardInterrupt:
		print("\nTracker stopped.")