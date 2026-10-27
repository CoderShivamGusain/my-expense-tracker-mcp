
from fastmcp import FastMCP
import os
import sqlite3
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "expenses.db")
CATEGORIES_PATH = os.path.join(os.path.dirname(__file__), "categories.json")

mcp = FastMCP("ExpenseTracker")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT DEFAULT '',
                note TEXT DEFAULT ''
            )
        """)

        # Migration: preserve existing records and add transaction type.
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(expenses)")
        }

        if "transaction_type" not in columns:
            conn.execute("""
                ALTER TABLE expenses
                ADD COLUMN transaction_type TEXT NOT NULL DEFAULT 'expense'
            """)


init_db()


def fetch_rows(cursor):
    return [dict(row) for row in cursor.fetchall()]


def validate_amount(amount):
    if amount <= 0:
        raise ValueError("Amount must be greater than zero.")


# -------------------- EXPENSES --------------------

@mcp.tool()
def add_expense(date: str, amount: float, category: str,
                subcategory: str = "", note: str = ""):
    """Add a new expense."""
    validate_amount(amount)

    with get_connection() as conn:
        cur = conn.execute("""
            INSERT INTO expenses
            (date, amount, category, subcategory, note, transaction_type)
            VALUES (?, ?, ?, ?, ?, 'expense')
        """, (date, amount, category, subcategory, note))

        return {"status": "ok", "id": cur.lastrowid}


@mcp.tool()
def edit_expense(
    expense_id: int,
    date: Optional[str] = None,
    amount: Optional[float] = None,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    note: Optional[str] = None
):
    """Update only the supplied fields of an existing expense."""
    updates = {
        "date": date,
        "amount": amount,
        "category": category,
        "subcategory": subcategory,
        "note": note,
    }

    updates = {key: value for key, value in updates.items()
               if value is not None}

    if not updates:
        return {"status": "error", "message": "No fields supplied to update."}

    if "amount" in updates:
        validate_amount(updates["amount"])

    assignments = ", ".join(f"{key} = ?" for key in updates)
    values = list(updates.values())

    with get_connection() as conn:
        cur = conn.execute(
            f"""UPDATE expenses SET {assignments}
                WHERE id = ? AND transaction_type = 'expense'""",
            values + [expense_id]
        )

        if cur.rowcount == 0:
            return {"status": "error", "message": "Expense not found."}

    return {"status": "ok", "id": expense_id}


@mcp.tool()
def delete_expense(expense_id: int):
    """Permanently delete an expense by ID."""
    with get_connection() as conn:
        cur = conn.execute("""
            DELETE FROM expenses
            WHERE id = ? AND transaction_type = 'expense'
        """, (expense_id,))

        if cur.rowcount == 0:
            return {"status": "error", "message": "Expense not found."}

    return {"status": "ok", "deleted_id": expense_id}


@mcp.tool()
def list_expenses(start_date: str, end_date: str):
    """List expenses within an inclusive ISO date range (YYYY-MM-DD)."""
    with get_connection() as conn:
        cur = conn.execute("""
            SELECT id, date, amount, category, subcategory, note
            FROM expenses
            WHERE transaction_type = 'expense'
              AND date BETWEEN ? AND ?
            ORDER BY date, id
        """, (start_date, end_date))

        return fetch_rows(cur)


# -------------------- CREDITS / INCOME --------------------

@mcp.tool()
def add_credit(date: str, amount: float, category: str = "Income",
               subcategory: str = "", note: str = ""):
    """Record money received, such as salary, refunds, or other income."""
    validate_amount(amount)

    with get_connection() as conn:
        cur = conn.execute("""
            INSERT INTO expenses
            (date, amount, category, subcategory, note, transaction_type)
            VALUES (?, ?, ?, ?, ?, 'credit')
        """, (date, amount, category, subcategory, note))

        return {"status": "ok", "id": cur.lastrowid}


@mcp.tool()
def list_transactions(start_date: str, end_date: str,
                      transaction_type: Optional[str] = None):
    """List expenses and credits in a date range.
    Optionally filter by 'expense' or 'credit'."""
    if transaction_type not in (None, "expense", "credit"):
        raise ValueError("Type must be 'expense', 'credit', or omitted.")

    query = """
        SELECT id, date, amount, category, subcategory, note,
               transaction_type
        FROM expenses
        WHERE date BETWEEN ? AND ?
    """
    params = [start_date, end_date]

    if transaction_type:
        query += " AND transaction_type = ?"
        params.append(transaction_type)

    query += " ORDER BY date, id"

    with get_connection() as conn:
        return fetch_rows(conn.execute(query, params))


@mcp.tool()
def delete_credit(credit_id: int):
    """Delete a credit by ID."""
    with get_connection() as conn:
        cur = conn.execute("""
            DELETE FROM expenses
            WHERE id = ? AND transaction_type = 'credit'
        """, (credit_id,))

        if cur.rowcount == 0:
            return {"status": "error", "message": "Credit not found."}

    return {"status": "ok", "deleted_id": credit_id}


# -------------------- REPORTS --------------------

@mcp.tool()
def summarize(start_date: str, end_date: str, category: Optional[str] = None):
    """Summarize expenses by category within a date range."""
    query = """
        SELECT category, SUM(amount) AS total_amount
        FROM expenses
        WHERE transaction_type = 'expense'
          AND date BETWEEN ? AND ?
    """
    params = [start_date, end_date]

    if category:
        query += " AND category = ?"
        params.append(category)

    query += " GROUP BY category ORDER BY category"

    with get_connection() as conn:
        return fetch_rows(conn.execute(query, params))


@mcp.tool()
def financial_summary(start_date: str, end_date: str):
    """Return total expenses, credits, and net balance for a date range."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT
                COALESCE(SUM(CASE
                    WHEN transaction_type = 'expense' THEN amount
                    ELSE 0 END), 0) AS total_expenses,
                COALESCE(SUM(CASE
                    WHEN transaction_type = 'credit' THEN amount
                    ELSE 0 END), 0) AS total_credits
            FROM expenses
            WHERE date BETWEEN ? AND ?
        """, (start_date, end_date)).fetchone()

    expenses = row["total_expenses"]
    credits = row["total_credits"]

    return {
        "total_expenses": expenses,
        "total_credits": credits,
        "net_balance": credits - expenses
    }


# -------------------- CATEGORIES RESOURCE --------------------

@mcp.resource("expense://categories", mime_type="application/json")
def categories():
    """Read expense categories from categories.json."""
    if not os.path.exists(CATEGORIES_PATH):
        return '{"categories": []}'

    with open(CATEGORIES_PATH, "r", encoding="utf-8") as file:
        return file.read()


# start the server 

if __name__ == "__main__":
    mcp.run(transport= "http", host= "0.0.0.0", port= 8000)
