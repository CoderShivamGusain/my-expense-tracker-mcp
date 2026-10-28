
from fastmcp import FastMCP
import os
import json
import aiosqlite
import sqlite3
import tempfile
from typing import Optional

# Database configuration
TEMP_DIR = tempfile.gettempdir()
DB_PATH = os.path.join(TEMP_DIR, "expenses.db")
CATEGORIES_PATH = os.path.join(os.path.dirname(__file__), "categories.json")

mcp = FastMCP("ExpenseTracker")


# -------------------- DATABASE --------------------

def init_db():
    """Initialize database and migrate existing schema."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT DEFAULT '',
                note TEXT DEFAULT '',
                transaction_type TEXT NOT NULL DEFAULT 'expense'
            )
        """)

        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(expenses)")
        }

        if "transaction_type" not in columns:
            conn.execute("""
                ALTER TABLE expenses
                ADD COLUMN transaction_type TEXT NOT NULL DEFAULT 'expense'
            """)

        conn.commit()


init_db()


def validate_amount(amount: float):
    if amount <= 0:
        raise ValueError("Amount must be greater than zero.")


async def fetch_rows(cursor):
    rows = await cursor.fetchall()
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


# -------------------- EXPENSES --------------------

@mcp.tool()
async def add_expense(
    date: str,
    amount: float,
    category: str,
    subcategory: str = "",
    note: str = ""
):
    """Add a new expense."""
    try:
        validate_amount(amount)

        async with aiosqlite.connect(DB_PATH) as conn:
            cur = await conn.execute("""
                INSERT INTO expenses
                (date, amount, category, subcategory, note, transaction_type)
                VALUES (?, ?, ?, ?, ?, 'expense')
            """, (date, amount, category, subcategory, note))

            await conn.commit()
            return {"status": "ok", "id": cur.lastrowid}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
async def edit_expense(
    expense_id: int,
    date: Optional[str] = None,
    amount: Optional[float] = None,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    note: Optional[str] = None
):
    """Update supplied fields of an existing expense."""
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

    try:
        if "amount" in updates:
            validate_amount(updates["amount"])

        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values())

        async with aiosqlite.connect(DB_PATH) as conn:
            cur = await conn.execute(
                f"""UPDATE expenses SET {assignments}
                    WHERE id = ? AND transaction_type = 'expense'""",
                values + [expense_id]
            )
            await conn.commit()

            if cur.rowcount == 0:
                return {"status": "error", "message": "Expense not found."}

        return {"status": "ok", "id": expense_id}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
async def delete_expense(expense_id: int):
    """Permanently delete an expense by ID."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("""
            DELETE FROM expenses
            WHERE id = ? AND transaction_type = 'expense'
        """, (expense_id,))
        await conn.commit()

        if cur.rowcount == 0:
            return {"status": "error", "message": "Expense not found."}

    return {"status": "ok", "deleted_id": expense_id}


@mcp.tool()
async def list_expenses(start_date: str, end_date: str):
    """List expenses within an inclusive ISO date range."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("""
            SELECT id, date, amount, category, subcategory, note
            FROM expenses
            WHERE transaction_type = 'expense'
              AND date BETWEEN ? AND ?
            ORDER BY date, id
        """, (start_date, end_date))
        return await fetch_rows(cur)


# -------------------- CREDITS / INCOME --------------------

@mcp.tool()
async def add_credit(
    date: str,
    amount: float,
    category: str = "Income",
    subcategory: str = "",
    note: str = ""
):
    """Record income, salary, refunds, or other money received."""
    try:
        validate_amount(amount)

        async with aiosqlite.connect(DB_PATH) as conn:
            cur = await conn.execute("""
                INSERT INTO expenses
                (date, amount, category, subcategory, note, transaction_type)
                VALUES (?, ?, ?, ?, ?, 'credit')
            """, (date, amount, category, subcategory, note))
            await conn.commit()

            return {"status": "ok", "id": cur.lastrowid}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
async def list_transactions(
    start_date: str,
    end_date: str,
    transaction_type: Optional[str] = None
):
    """List expenses and credits, optionally filtered by type."""
    if transaction_type not in (None, "expense", "credit"):
        return {
            "status": "error",
            "message": "Type must be 'expense', 'credit', or omitted."
        }

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

    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(query, params)
        return await fetch_rows(cur)


@mcp.tool()
async def delete_credit(credit_id: int):
    """Delete a credit by ID."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("""
            DELETE FROM expenses
            WHERE id = ? AND transaction_type = 'credit'
        """, (credit_id,))
        await conn.commit()

        if cur.rowcount == 0:
            return {"status": "error", "message": "Credit not found."}

    return {"status": "ok", "deleted_id": credit_id}


# -------------------- REPORTS --------------------

@mcp.tool()
async def summarize(
    start_date: str,
    end_date: str,
    category: Optional[str] = None
):
    """Summarize expenses by category."""
    query = """
        SELECT category,
               SUM(amount) AS total_amount,
               COUNT(*) AS count
        FROM expenses
        WHERE transaction_type = 'expense'
          AND date BETWEEN ? AND ?
    """
    params = [start_date, end_date]

    if category:
        query += " AND category = ?"
        params.append(category)

    query += " GROUP BY category ORDER BY total_amount DESC"

    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(query, params)
        return await fetch_rows(cur)


@mcp.tool()
async def financial_summary(start_date: str, end_date: str):
    """Return expenses, credits, and net balance for a date range."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("""
            SELECT
                COALESCE(SUM(CASE
                    WHEN transaction_type = 'expense' THEN amount
                    ELSE 0 END), 0) AS total_expenses,
                COALESCE(SUM(CASE
                    WHEN transaction_type = 'credit' THEN amount
                    ELSE 0 END), 0) AS total_credits
            FROM expenses
            WHERE date BETWEEN ? AND ?
        """, (start_date, end_date))

        row = await cur.fetchone()

    expenses, credits = row
    return {
        "total_expenses": expenses,
        "total_credits": credits,
        "net_balance": credits - expenses
    }


# -------------------- CATEGORIES RESOURCE --------------------

@mcp.resource("expense:///categories", mime_type="application/json")
def categories():
    """Read expense categories from categories.json."""
    defaults = {
        "categories": [
            "Food & Dining",
            "Transportation",
            "Shopping",
            "Entertainment",
            "Bills & Utilities",
            "Healthcare",
            "Travel",
            "Education",
            "Business",
            "Other"
        ]
    }

    try:
        with open(CATEGORIES_PATH, "r", encoding="utf-8") as file:
            return file.read()
    except FileNotFoundError:
        return json.dumps(defaults, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# -------------------- START SERVER --------------------

if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
