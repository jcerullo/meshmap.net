import sqlite3
import os
import logging
from fastapi import APIRouter, HTTPException, Response, status, Depends, Path
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

DATABASE_FILE = "tasks.db"
logger = logging.getLogger("meshtastic_dashboard.tasks")

def init_tasks_db():
    """Initializes the tasks database and creates the tasks table if it doesn't exist."""
    logger.info(f"Initializing tasks database at: {DATABASE_FILE}")
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_FILE, timeout=10)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nodeId TEXT NOT NULL,
                taskType TEXT NOT NULL,
                actionPayload TEXT,
                cronString TEXT NOT NULL,
                createdAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS update_tasks_updatedAt
            AFTER UPDATE ON tasks
            FOR EACH ROW
            WHEN OLD.updatedAt = NEW.updatedAt OR OLD.updatedAt IS NULL
            BEGIN
                UPDATE tasks SET updatedAt = CURRENT_TIMESTAMP WHERE id = OLD.id;
            END;
        """)
        conn.commit()
        logger.info("Tasks database initialization complete.")
    except sqlite3.Error as e:
        logger.exception(f"Tasks database initialization failed: {e}")
        raise
    finally:
        if conn:
            conn.close()

class TaskCreate(BaseModel):
    nodeId: str
    taskType: str
    actionPayload: Optional[str] = None
    cronString: str

class TaskUpdate(BaseModel):
    nodeId: Optional[str] = None
    taskType: Optional[str] = None
    actionPayload: Optional[str] = None
    cronString: Optional[str] = None

class TaskInDB(BaseModel):
    id: int
    nodeId: str
    taskType: str
    actionPayload: Optional[str] = None
    cronString: str
    createdAt: str
    updatedAt: str

class ErrorResponse(BaseModel):
    detail: str

tasks_router = APIRouter()

def get_tasks_db_conn():
    """Helper function to get a database connection for the tasks DB."""
    try:
        conn = sqlite3.connect(DATABASE_FILE, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except Exception as e:
            logger.warning(f"Could not set WAL journal mode for tasks DB: {e}")
        return conn
    except sqlite3.Error as e:
        logger.error(f"Tasks database connection error: {e}")
        raise HTTPException(status_code=500, detail=f"Tasks database connection error: {e}")

async def get_db():
    conn = get_tasks_db_conn()
    try:
        yield conn
    finally:
        if conn:
            conn.close()

@tasks_router.post(
    "/",
    response_model=TaskInDB,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new scheduled task",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid input data"},
        500: {"model": ErrorResponse, "description": "Database error"}
    }
)
async def create_task(task: TaskCreate, conn: sqlite3.Connection = Depends(get_db)):
    """Creates a new task entry in the database."""
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO tasks (nodeId, taskType, actionPayload, cronString) VALUES (?, ?, ?, ?)",
            (task.nodeId, task.taskType, task.actionPayload, task.cronString)
        )
        conn.commit()
        new_task_id = cursor.lastrowid
        if new_task_id is None:
            logger.error("Failed to get ID of created task after commit.")
            raise HTTPException(status_code=500, detail="Failed to get ID of created task.")

        cursor.execute("SELECT id, nodeId, taskType, actionPayload, cronString, createdAt, updatedAt FROM tasks WHERE id = ?", (new_task_id,))
        created_task_row = cursor.fetchone()
        if created_task_row:
            created_task_dict = dict(created_task_row)
            logger.info(f"Created task with ID: {new_task_id}")
            return TaskInDB(**created_task_dict)
        else:
            logger.error(f"Failed to retrieve task {new_task_id} immediately after creation.")
            raise HTTPException(status_code=500, detail="Failed to retrieve created task after insert.")
    except sqlite3.IntegrityError as e:
        logger.error(f"Database integrity error creating task: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Database integrity error: {e}")
    except sqlite3.Error as e:
        logger.error(f"Database error creating task: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {e}")

@tasks_router.get(
    "/",
    response_model=List[TaskInDB],
    summary="Get all scheduled tasks",
    responses={500: {"model": ErrorResponse, "description": "Database error"}}
)
async def get_all_tasks(conn: sqlite3.Connection = Depends(get_db)):
    """Retrieves a list of all tasks currently stored in the database."""
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, nodeId, taskType, actionPayload, cronString, createdAt, updatedAt FROM tasks ORDER BY createdAt DESC")
        tasks_rows = cursor.fetchall()
        tasks_list = [TaskInDB(**dict(row)) for row in tasks_rows]
        return tasks_list
    except sqlite3.Error as e:
        logger.error(f"Database error fetching all tasks: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {e}")

@tasks_router.get(
    "/{task_id}",
    response_model=TaskInDB,
    summary="Get a specific task by ID",
    responses={
        404: {"model": ErrorResponse, "description": "Task not found"},
        500: {"model": ErrorResponse, "description": "Database error"}
    }
)
async def get_task(task_id: int, conn: sqlite3.Connection = Depends(get_db)):
    """Retrieves the details of a single task specified by its unique ID."""
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, nodeId, taskType, actionPayload, cronString, createdAt, updatedAt FROM tasks WHERE id = ?", (task_id,))
        task_row = cursor.fetchone()
        if task_row:
            task_dict = dict(task_row)
            return TaskInDB(**task_dict)
        else:
            logger.warning(f"Task with ID {task_id} not found.")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task with ID {task_id} not found")
    except sqlite3.Error as e:
        logger.error(f"Database error fetching task {task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {e}")

@tasks_router.put(
    "/{task_id}",
    response_model=TaskInDB,
    summary="Update an existing task",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid input or no data to update"},
        404: {"model": ErrorResponse, "description": "Task not found"},
        500: {"model": ErrorResponse, "description": "Database error"}
    }
)
async def update_task(task_id: int, task: TaskUpdate, conn: sqlite3.Connection = Depends(get_db)):
    """Updates an existing task specified by its ID. Partial updates allowed."""
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM tasks WHERE id = ?", (task_id,))
        existing_task = cursor.fetchone()
        if not existing_task:
            logger.warning(f"Update failed: Task with ID {task_id} not found.")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task with ID {task_id} not found")

        try:
            update_data = task.model_dump(exclude_unset=True) 
        except AttributeError:
            update_data = task.dict(exclude_unset=True) 

        if not update_data:
              logger.warning(f"Update failed for task {task_id}: No update data provided.")
              raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No update data provided")

        set_clause = ", ".join([f"{key} = ?" for key in update_data.keys()])
        values = list(update_data.values())
        values.append(task_id)

        query = f"UPDATE tasks SET {set_clause} WHERE id = ?"
        cursor.execute(query, tuple(values))
        conn.commit()

        cursor.execute("SELECT id, nodeId, taskType, actionPayload, cronString, createdAt, updatedAt FROM tasks WHERE id = ?", (task_id,))
        updated_task_row = cursor.fetchone()
        if updated_task_row:
            updated_task_dict = dict(updated_task_row)
            logger.info(f"Updated task with ID: {task_id}")
            return TaskInDB(**updated_task_dict)
        else:
            logger.error(f"Failed to retrieve task {task_id} after successful update.")
            raise HTTPException(status_code=500, detail="Failed to retrieve updated task after update.")
    except sqlite3.IntegrityError as e:
        logger.error(f"Database integrity error updating task {task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Database integrity error: {e}")
    except sqlite3.Error as e:
        logger.error(f"Database error updating task {task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {e}")

@tasks_router.delete(
    "/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a task by ID",
    responses={
        404: {"model": ErrorResponse, "description": "Task not found"},
        500: {"model": ErrorResponse, "description": "Database error"}
    }
)
async def delete_task(task_id: int, conn: sqlite3.Connection = Depends(get_db)):
    """Deletes a task specified by its unique ID."""
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM tasks WHERE id = ?", (task_id,))
        existing_task = cursor.fetchone()
        if not existing_task:
            logger.warning(f"Delete failed: Task with ID {task_id} not found.")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task with ID {task_id} not found")

        cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()

        if cursor.rowcount == 0:
            logger.warning(f"Delete operation affected 0 rows for existing task ID {task_id}.")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task with ID {task_id} found but deletion affected 0 rows.")
        else:
            logger.info(f"Deleted task with ID: {task_id}")
            return Response(status_code=status.HTTP_204_NO_CONTENT)
    except sqlite3.Error as e:
        logger.error(f"Database error deleting task {task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {e}")