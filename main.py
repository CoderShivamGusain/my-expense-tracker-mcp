
from fastmcp import FastMCP
import os
import json
import math
import asyncio
import tempfile
from datetime import date
from typing import Optional

import aiosqlite


# -------------------- CONFIGURATION --------------------

DB_PATH = os.environ.get(
    "EXPENSES_DB_PATH",
    os.path.join(tempfile.gettempdir(), "expenses.db")
)

CATEGORIES_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "categories.json"
)

mcp = FastMCP("ExpenseTracker")

_db_initialized = False
_init_lock = asyncio.Lock()


# -------------------- DATABASE --------------------

async def ensure_db():
    """Initialize the database once per server process and migrate its schema."""
    global _db_initialized

    if _db_initialized:
        return

    async with _init_lock:
        if _db_initialized:
            return

        parent = os.path.dirname(os.path.abspath(DB_PATH))
        os.makedirs(parent, exist_ok=True)

        async with aiosqlite.connect(DB_PATH, timeout=30) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS expenses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    amount REAL NOT NULL,
                    category TEXT NOT NULL,
                    subcategory TEXT DEFAULT '',
                    note TEXT DEFAULT ''
                )
            """)

            cursor = await conn.execute("PRAGMA table_info(expenses)")
            columns = {row[1] for row in await cursor.fetchall()}

            if "transaction_type" not in columns:
                await conn.execute("""
                    ALTER TABLE expenses
                    ADD COLUMN transaction_type TEXT NOT NULL
                    DEFAULT 'expense'
                """)

            await conn.commit()

        _db_initialized = True


async def connect_db():
    """Return an asynchronous SQLite connection with dictionary-like rows."""
    await ensure_db()
    conn = await aiosqlite.connect(DB_PATH, timeout=30)
    conn.row_factory = aiosqlite.Row
    return conn


def rows_to_dicts(rows):
    return [dict(row) for row in rows]


def validate_amount(amount: float):
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise ValueError("Amount must be a number.")

    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("Amount must be greater than zero.")


def validate_date_range(start_date: str, end_date: str):
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except (TypeError, ValueError):
        raise ValueError("Dates must use YYYY-MM-DD format.")

    if start.isoformat() != start_date or end.isoformat() != end_date:
        raise ValueError("Dates must use YYYY-MM-DD format.")

    if start > end:
        raise ValueError("start_date must be on or before end_date.")


def database_error(exc: Exception):
    message = str(exc)
    if "readonly" in message.lower() or "read-only" in message.lower():
        message += (
            " Check DB_PATH and whether the database file and its "
            "parent directory are writable by the server."
        )
    return {"status": "error", "message": message}


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
        validate_date_range(date, date)
        validate_amount(amount)

        async with await connect_db() as conn:
            cursor = await conn.execute("""
                INSERT INTO expenses
                    (date, amount, category, subcategory, note,
                     transaction_type)
                VALUES (?, ?, ?, ?, ?, 'expense')
            """, (date, amount, category, subcategory, note))

            expense_id = cursor.lastrowid
            await conn.commit()

        return {
            "status": "ok",
            "id": expense_id,
            "message": "Expense added successfully."
        }

    except (ValueError, TypeError) as exc:
        return {"status": "error", "message": str(exc)}
    except Exception as exc:
        return database_error(exc)


@mcp.tool()
async def edit_expense(
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

    try:
        if "date" in updates:
            validate_date_range(updates["date"], updates["date"])
        if "amount" in updates:
            validate_amount(updates["amount"])

        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values())

        async with await connect_db() as conn:
            cursor = await conn.execute(
                f"""
                UPDATE expenses
                SET {assignments}
                WHERE id = ? AND transaction_type = 'expense'
                """,
                values + [expense_id]
            )
            await conn.commit()

            if cursor.rowcount == 0:
                return {"status": "error", "message": "Expense not found."}

        return {"status": "ok", "id": expense_id}

    except (ValueError, TypeError) as exc:
        return {"status": "error", "message": str(exc)}
    except Exception as exc:
        return database_error(exc)


@mcp.tool()
async def delete_expense(expense_id: int):
    """Permanently delete an expense by ID."""
    try:
        async with await connect_db() as conn:
            cursor = await conn.execute("""
                DELETE FROM expenses
                WHERE id = ? AND transaction_type = 'expense'
            """, (expense_id,))
            await conn.commit()

            if cursor.rowcount == 0:
                return {"status": "error", "message": "Expense not found."}

        return {"status": "ok", "deleted_id": expense_id}

    except Exception as exc:
        return database_error(exc)


@mcp.tool()
async def list_expenses(start_date: str, end_date: str):
    """List expenses within an inclusive YYYY-MM-DD date range."""
    try:
        validate_date_range(start_date, end_date)

        async with await connect_db() as conn:
            cursor = await conn.execute("""
                SELECT id, date, amount, category, subcategory, note
                FROM expenses
                WHERE transaction_type = 'expense'
                  AND date BETWEEN ? AND ?
                ORDER BY date, id
            """, (start_date, end_date))

            rows = await cursor.fetchall()
            return rows_to_dicts(rows)

    except (ValueError, TypeError) as exc:
        return {"status": "error", "message": str(exc)}
    except Exception as exc:
        return database_error(exc)


# -------------------- CREDITS / INCOME --------------------

@mcp.tool()
async def add_credit(
    date: str,
    amount: float,
    category: str = "Income",
    subcategory: str = "",
    note: str = ""
):
    """Record money received, such as salary, refunds, or other income."""
    try:
        validate_date_range(date, date)
        validate_amount(amount)

        async with await connect_db() as conn:
            cursor = await conn.execute("""
                INSERT INTO expenses
                    (date, amount, category, subcategory, note,
                     transaction_type)
                VALUES (?, ?, ?, ?, ?, 'credit')
            """, (date, amount, category, subcategory, note))

            credit_id = cursor.lastrowid
            await conn.commit()

        return {"status": "ok", "id": credit_id}

    except (ValueError, TypeError) as exc:
        return {"status": "error", "message": str(exc)}
    except Exception as exc:
        return database_error(exc)


@mcp.tool()
async def list_transactions(
    start_date: str,
    end_date: str,
    transaction_type: Optional[str] = None
):
    """List expenses and credits, optionally filtered by transaction type."""
    try:
        validate_date_range(start_date, end_date)

        if transaction_type not in (None, "expense", "credit"):
            raise ValueError(
                "Type must be 'expense', 'credit', or omitted."
            )

        query = """
            SELECT id, date, amount, category, subcategory, note,
                   transaction_type
            FROM expenses
            WHERE date BETWEEN ? AND ?
        """
        params = [start_date, end_date]

        if transaction_type is not None:
            query += " AND transaction_type = ?"
            params.append(transaction_type)

        query += " ORDER BY date, id"

        async with await connect_db() as conn:
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return rows_to_dicts(rows)

    except (ValueError, TypeError) as exc:
        return {"status": "error", "message": str(exc)}
    except Exception as exc:
        return database_error(exc)


@mcp.tool()
async def delete_credit(credit_id: int):
    """Delete a credit by ID."""
    try:
        async with await connect_db() as conn:
            cursor = await conn.execute("""
                DELETE FROM expenses
                WHERE id = ? AND transaction_type = 'credit'
            """, (credit_id,))
            await conn.commit()

            if cursor.rowcount == 0:
                return {"status": "error", "message": "Credit not found."}

        return {"status": "ok", "deleted_id": credit_id}

    except Exception as exc:
        return database_error(exc)


# -------------------- REPORTS --------------------

@mcp.tool()
async def summarize(
    start_date: str,
    end_date: str,
    category: Optional[str] = None
):
    """Summarize expenses by category, including totals and transaction counts."""
    try:
        validate_date_range(start_date, end_date)

        query = """
            SELECT category, SUM(amount) AS total_amount, COUNT(*) AS count
            FROM expenses
            WHERE transaction_type = 'expense'
              AND date BETWEEN ? AND ?
        """
        params = [start_date, end_date]

        if category:
            query += " AND category = ?"
            params.append(category)

        query += " GROUP BY category ORDER BY category"

        async with await connect_db() as conn:
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return rows_to_dicts(rows)

    except (ValueError, TypeError) as exc:
        return {"status": "error", "message": str(exc)}
    except Exception as exc:
        return database_error(exc)


@mcp.tool()
async def financial_summary(start_date: str, end_date: str):
    """Return total expenses, total credits, and net balance for a date range."""
    try:
        validate_date_range(start_date, end_date)

        async with await connect_db() as conn:
            cursor = await conn.execute("""
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

            row = await cursor.fetchone()

        expenses = row["total_expenses"]
        credits = row["total_credits"]

        return {
            "total_expenses": expenses,
            "total_credits": credits,
            "net_balance": credits - expenses
        }

    except (ValueError, TypeError) as exc:
        return {"status": "error", "message": str(exc)}
    except Exception as exc:
        return database_error(exc)


# -------------------- CATEGORIES RESOURCE --------------------

def read_categories():
    """Read categories.json, or return an empty categories list."""
    try:
        with open(CATEGORIES_PATH, "r", encoding="utf-8") as file:
            return file.read()
    except FileNotFoundError:
        return json.dumps({"categories": []})
    except Exception as exc:
        return json.dumps({
            "error": f"Could not load categories: {exc}"
        })


@mcp.resource("expense:///categories", mime_type="application/json")
async def categories():
    """Read expense categories from categories.json."""
    return await asyncio.to_thread(read_categories)


# -------------------- START SERVER --------------------

if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
