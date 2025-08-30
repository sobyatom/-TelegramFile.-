import sqlite3, os

DB_PATH = "files.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            name TEXT,
            size INTEGER
        )
    """)
    conn.commit()
    conn.close()

def add_file(tg_id, name, size):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO files (tg_id, name, size) VALUES (?, ?, ?)", (tg_id, name, size))
    conn.commit()
    conn.close()

def search_files(query=""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if query:
        c.execute("SELECT * FROM files WHERE name LIKE ?", ('%' + query + '%',))
    else:
        c.execute("SELECT * FROM files ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return rows
