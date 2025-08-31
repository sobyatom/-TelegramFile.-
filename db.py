import sqlite3

DB_FILE = "files.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            msg_ids TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_file(name, msg_ids):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO files (name, msg_ids) VALUES (?, ?)", (name, ",".join(map(str,msg_ids))))
    conn.commit()
    conn.close()

def search_files(query=""):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if query:
        c.execute("SELECT id, name, msg_ids FROM files WHERE name LIKE ?", (f"%{query}%",))
    else:
        c.execute("SELECT id, name, msg_ids FROM files")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1], "msg_ids": list(map(int,r[2].split(",")))} for r in rows]
