import sqlite3
import logging
import time
import re
import os
import json  

from typing import List, Optional, Dict, Any, Literal, Tuple

try:

    from pydantic import BaseModel, Field, field_validator, model_validator, ValidationError
    PYDANTIC_V2 = True

    try:
        from pydantic import validator as pydantic_validator_v1
    except ImportError:
        pydantic_validator_v1 = None
except ImportError:

    from pydantic import BaseModel, Field, validator as pydantic_validator_v1, ValidationError
    PYDANTIC_V2 = False

    field_validator = None
    model_validator = None

DEFAULT_DB_PATH = "meshtastic_data.db" 
DB_PATH = os.environ.get("DB_PATH", DEFAULT_DB_PATH)
AUTO_REPLY_TABLE_NAME = "auto_reply_rules"

logger = logging.getLogger("meshtastic_dashboard.auto_reply")

cooldown_tracker: Dict[int, Dict[str, float]] = {}

class AutoReplyRuleCreateUpdate(BaseModel):
    trigger_phrase: str = Field(..., description="Phrase or regex pattern to trigger the rule.")
    match_type: Literal['contains', 'exact', 'regex'] = Field(..., description="How to match the trigger phrase.")
    response_message: str = Field(..., description="The message to send back as a reply.")
    cooldown_seconds: int = Field(60, ge=0, description="Minimum time in seconds before this rule triggers again for the same sender.")
    is_enabled: bool = Field(True, description="Whether the rule is active.")

    if PYDANTIC_V2 and field_validator:
        @field_validator('trigger_phrase', 'response_message')
        def field_must_not_be_empty_v2(cls, v):
            if not v or not v.strip():
                raise ValueError('Field cannot be empty')
            return v

        @field_validator('match_type')
        def match_type_must_be_valid_v2(cls, v):
            if v not in ['contains', 'exact', 'regex']:
                raise ValueError("match_type must be 'contains', 'exact', or 'regex'")
            return v
    elif not PYDANTIC_V2 and pydantic_validator_v1:
        @pydantic_validator_v1('trigger_phrase', 'response_message')
        def field_must_not_be_empty_v1(cls, v):
            if not v or not v.strip():
                raise ValueError('Field cannot be empty')
            return v

        @pydantic_validator_v1('match_type')
        def match_type_must_be_valid_v1(cls, v):
            if v not in ['contains', 'exact', 'regex']:
                raise ValueError("match_type must be 'contains', 'exact', or 'regex'")
            return v
    else:

        pass

class AutoReplyRuleResponse(AutoReplyRuleCreateUpdate):
    id: int = Field(..., description="Unique identifier for the rule.")

def _get_db_connection(db_path: str = DB_PATH):
    """Establishes a connection to the SQLite database."""
    logger.debug(f"DB_CONN: Attempting to connect to database at {db_path}")
    try:

        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row 
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 5000;") 
        logger.debug(f"DB_CONN: Successfully connected to database at {db_path}")
        return conn
    except sqlite3.Error as e:

        logger.error(f"DB_CONN: Database connection error to '{db_path}': {e} (Code: {e.sqlite_errorcode} Name: {e.sqlite_errorname})", exc_info=False)
        logger.debug(f"DB_CONN: Full exception details", exc_info=True)
        raise 

def init_auto_reply_db(db_path: str = DB_PATH):
    """Initializes the auto_reply_rules table in the database if it doesn't exist."""
    logger.info(f"INIT_DB: Initializing Auto-Reply database table '{AUTO_REPLY_TABLE_NAME}' in {db_path}...")
    conn = None 
    try:
        logger.debug(f"INIT_DB: Opening database connection")
        conn = _get_db_connection(db_path)
        cursor = conn.cursor()
        logger.debug(f"INIT_DB: Creating table {AUTO_REPLY_TABLE_NAME} if it doesn't exist")
        cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {AUTO_REPLY_TABLE_NAME} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger_phrase TEXT NOT NULL,
            match_type TEXT NOT NULL CHECK(match_type IN ('contains', 'exact', 'regex')),
            response_message TEXT NOT NULL,
            cooldown_seconds INTEGER NOT NULL DEFAULT 60,
            is_enabled BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        logger.debug(f"INIT_DB: Creating update trigger for {AUTO_REPLY_TABLE_NAME}")

        cursor.execute(f"""
        CREATE TRIGGER IF NOT EXISTS trigger_auto_reply_updated_at
        AFTER UPDATE ON {AUTO_REPLY_TABLE_NAME}
        FOR EACH ROW
        BEGIN
            UPDATE {AUTO_REPLY_TABLE_NAME} SET updated_at = CURRENT_TIMESTAMP WHERE id = OLD.id;
        END;
        """)
        conn.commit()
        logger.debug(f"INIT_DB: Committing database changes")

        cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{AUTO_REPLY_TABLE_NAME}'")
        if cursor.fetchone():
            logger.info(f"INIT_DB: Table '{AUTO_REPLY_TABLE_NAME}' successfully initialized.")
        else:
            logger.error(f"INIT_DB: Table '{AUTO_REPLY_TABLE_NAME}' was not created!")

    except sqlite3.Error as e:
        logger.exception(f"INIT_DB: Database error initializing auto-reply table: {e}")
        logger.debug(f"INIT_DB: Error details - Type: {type(e).__name__}, Args: {e.args}, Code: {e.sqlite_errorcode}, Name: {e.sqlite_errorname}")

    except Exception as e:
        logger.exception(f"INIT_DB: Unexpected error during auto-reply DB init: {e}")
        logger.debug(f"INIT_DB: Error details - Type: {type(e).__name__}, Args: {e.args}")
    finally:
        if conn:
            try:
                conn.close()
                logger.debug(f"INIT_DB: Database connection closed.")
            except sqlite3.Error as e:
                 logger.error(f"INIT_DB: Error closing DB connection: {e}", exc_info=False)

def db_add_auto_reply_rule(
    trigger_phrase: str,
    match_type: Literal['contains', 'exact', 'regex'],
    response_message: str,
    cooldown_seconds: int,
    is_enabled: bool,
    db_path: str = DB_PATH
) -> Optional[int]:
    """Adds a new auto-reply rule to the database."""
    logger.info(f"DB_ADD: Adding auto-reply rule - Trigger: '{trigger_phrase}', Type: {match_type}")
    logger.debug(f"DB_ADD: Full rule details - Response: '{response_message}', Cooldown: {cooldown_seconds}, Enabled: {is_enabled}")

    try:
        logger.debug(f"DB_ADD: Validating input data with Pydantic model")

        model = AutoReplyRuleCreateUpdate(
            trigger_phrase=trigger_phrase,
            match_type=match_type,
            response_message=response_message,
            cooldown_seconds=cooldown_seconds,
            is_enabled=is_enabled
        )
        logger.debug(f"DB_ADD: Validation successful")

        if match_type == 'regex':
            try:
                logger.debug(f"DB_ADD: Testing regex pattern '{trigger_phrase}'")
                re.compile(trigger_phrase)
                logger.debug(f"DB_ADD: Regex pattern valid")
            except re.error as regex_err:
                error_msg = f"Invalid regex pattern '{trigger_phrase}': {regex_err}"
                logger.error(f"DB_ADD: {error_msg}")
                raise ValueError(error_msg) from regex_err

    except ValidationError as ve:

        errors = ve.errors()
        error_summary = ", ".join([f"{err['loc'][0]}: {err['msg']}" for err in errors])
        logger.error(f"DB_ADD: Rule validation error: {error_summary}")
        logger.debug(f"DB_ADD: Full ValidationError details: {errors}")
        raise ValueError(f"Invalid input data: {error_summary}") from ve 
    except ValueError as ve: 
         logger.error(f"DB_ADD: Regex validation error: {ve}")
         raise 

    conn = None
    try:
        logger.debug(f"DB_ADD: Opening database connection")
        conn = _get_db_connection(db_path)
        cursor = conn.cursor()
        logger.debug(f"DB_ADD: Executing INSERT query")
        cursor.execute(
            f"""
            INSERT INTO {AUTO_REPLY_TABLE_NAME} (trigger_phrase, match_type, response_message, cooldown_seconds, is_enabled)
            VALUES (?, ?, ?, ?, ?)
            """,
            (trigger_phrase, match_type, response_message, cooldown_seconds, is_enabled)
        )
        conn.commit()
        rule_id = cursor.lastrowid
        logger.info(f"DB_ADD: Successfully added auto-reply rule with ID: {rule_id}")
        logger.debug(f"DB_ADD: New rule creation complete (ID: {rule_id})")
        return rule_id
    except sqlite3.Error as e:
        logger.exception(f"DB_ADD: Database error adding auto-reply rule: {e}")
        logger.debug(f"DB_ADD: Error details - Type: {type(e).__name__}, Args: {e.args}, Code: {e.sqlite_errorcode}, Name: {e.sqlite_errorname}")
        return None 
    finally:
        if conn:
            try:
                conn.close()
                logger.debug(f"DB_ADD: Database connection closed.")
            except sqlite3.Error as e:
                logger.error(f"DB_ADD: Error closing DB connection: {e}", exc_info=False)

def db_get_auto_reply_rules(only_enabled: Optional[bool] = None, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Retrieves auto-reply rules from the database, optionally filtering by enabled status."""
    query = f"SELECT * FROM {AUTO_REPLY_TABLE_NAME}"
    params = []
    if only_enabled is not None:
        query += " WHERE is_enabled = ?"
        params.append(1 if only_enabled else 0)
    query += " ORDER BY id ASC" 

    logger.debug(f"DB_GET_RULES: Getting rules with filter (only_enabled={only_enabled})")
    logger.debug(f"DB_GET_RULES: Query: '{query}', Params: {params}")

    conn = None
    try:
        logger.debug(f"DB_GET_RULES: Opening database connection")
        conn = _get_db_connection(db_path)
        cursor = conn.cursor()
        cursor.execute(query, tuple(params)) 
        rows = cursor.fetchall()
        rule_count = len(rows)
        logger.debug(f"DB_GET_RULES: Retrieved {rule_count} rules from database")

        result = [dict(row) for row in rows]

        if rule_count > 0:
            rule_ids = [row['id'] for row in result]
            logger.debug(f"DB_GET_RULES: Rule IDs retrieved: {rule_ids}")

        return result
    except sqlite3.Error as e:
        logger.exception(f"DB_GET_RULES: Database error retrieving auto-reply rules: {e}")
        logger.debug(f"DB_GET_RULES: Error details - Type: {type(e).__name__}, Args: {e.args}, Code: {e.sqlite_errorcode}, Name: {e.sqlite_errorname}")
        return [] 
    finally:
        if conn:
            try:
                conn.close()
                logger.debug(f"DB_GET_RULES: Database connection closed.")
            except sqlite3.Error as e:
                logger.error(f"DB_GET_RULES: Error closing DB connection: {e}", exc_info=False)

def db_get_auto_reply_rule_by_id(rule_id: int, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    """Retrieves a single auto-reply rule by its ID."""
    logger.debug(f"DB_GET_RULE: Getting rule by ID: {rule_id}")

    conn = None
    try:
        logger.debug(f"DB_GET_RULE: Opening database connection")
        conn = _get_db_connection(db_path)
        cursor = conn.cursor()
        query = f"SELECT * FROM {AUTO_REPLY_TABLE_NAME} WHERE id = ?"
        logger.debug(f"DB_GET_RULE: Executing query: '{query}' with param: {rule_id}")

        cursor.execute(query, (rule_id,))
        row = cursor.fetchone()

        if row:
            logger.debug(f"DB_GET_RULE: Found rule ID {rule_id}")
            result = dict(row)
            logger.debug(f"DB_GET_RULE: Rule details - Trigger: '{result.get('trigger_phrase')}', Type: {result.get('match_type')}, Enabled: {result.get('is_enabled')}")
            return result
        else:
            logger.warning(f"DB_GET_RULE: Rule ID {rule_id} not found in database")
            return None

    except sqlite3.Error as e:
        logger.exception(f"DB_GET_RULE: Database error getting auto-reply rule ID {rule_id}: {e}")
        logger.debug(f"DB_GET_RULE: Error details - Type: {type(e).__name__}, Args: {e.args}, Code: {e.sqlite_errorcode}, Name: {e.sqlite_errorname}")
        return None
    finally:
        if conn:
             try:
                conn.close()
                logger.debug(f"DB_GET_RULE: Database connection closed.")
             except sqlite3.Error as e:
                 logger.error(f"DB_GET_RULE: Error closing DB connection: {e}", exc_info=False)

def db_update_auto_reply_rule(
    rule_id: int,
    trigger_phrase: str,
    match_type: Literal['contains', 'exact', 'regex'],
    response_message: str,
    cooldown_seconds: int,
    is_enabled: bool,
    db_path: str = DB_PATH
) -> Optional[Dict[str, Any]]:
    """Updates an existing auto-reply rule."""
    logger.info(f"DB_UPDATE: Updating rule ID {rule_id} - Trigger: '{trigger_phrase}', Type: {match_type}, Enabled: {is_enabled}")
    logger.debug(f"DB_UPDATE: Full update details - Response: '{response_message}', Cooldown: {cooldown_seconds}")

    try:
        logger.debug(f"DB_UPDATE: Validating input data with Pydantic model")

        model = AutoReplyRuleCreateUpdate(
            trigger_phrase=trigger_phrase,
            match_type=match_type,
            response_message=response_message,
            cooldown_seconds=cooldown_seconds,
            is_enabled=is_enabled
        )
        logger.debug(f"DB_UPDATE: Validation successful")

        if match_type == 'regex':
            try:
                logger.debug(f"DB_UPDATE: Testing regex pattern '{trigger_phrase}'")
                re.compile(trigger_phrase)
                logger.debug(f"DB_UPDATE: Regex pattern valid")
            except re.error as regex_err:
                error_msg = f"Invalid regex pattern '{trigger_phrase}': {regex_err}"
                logger.error(f"DB_UPDATE: {error_msg}")
                raise ValueError(error_msg) from regex_err

    except ValidationError as ve:
        errors = ve.errors()
        error_summary = ", ".join([f"{err['loc'][0]}: {err['msg']}" for err in errors])
        logger.error(f"DB_UPDATE: Rule validation error: {error_summary}")
        logger.debug(f"DB_UPDATE: Full ValidationError details: {errors}")
        raise ValueError(f"Invalid input data: {error_summary}") from ve
    except ValueError as ve: 
         logger.error(f"DB_UPDATE: Regex validation error: {ve}")
         raise 

    conn = None
    try:

        logger.debug(f"DB_UPDATE: Checking if rule ID {rule_id} exists before update")
        existing_rule = db_get_auto_reply_rule_by_id(rule_id, db_path)
        if not existing_rule:
            logger.warning(f"DB_UPDATE: Rule ID {rule_id} not found for update")
            return None

        logger.debug(f"DB_UPDATE: Rule ID {rule_id} exists, proceeding with update")
        logger.debug(f"DB_UPDATE: Opening database connection")
        conn = _get_db_connection(db_path)
        cursor = conn.cursor()
        query = f"""
            UPDATE {AUTO_REPLY_TABLE_NAME} SET
                trigger_phrase = ?,
                match_type = ?,
                response_message = ?,
                cooldown_seconds = ?,
                is_enabled = ?,
                updated_at = CURRENT_TIMESTAMP -- Explicitly set here too, though trigger exists
            WHERE id = ?
            """
        params = (trigger_phrase, match_type, response_message, cooldown_seconds, is_enabled, rule_id)
        logger.debug(f"DB_UPDATE: Executing query: '{query}' with params: {params}")

        cursor.execute(query, params)
        conn.commit()

        if cursor.rowcount == 0:

            logger.warning(f"DB_UPDATE: Rule ID {rule_id} not affected by update (no rows changed - possibly deleted concurrently?)")
            return None 

        logger.info(f"DB_UPDATE: Successfully updated auto-reply rule ID: {rule_id}")

        logger.debug(f"DB_UPDATE: Fetching updated rule data")
        updated_rule = db_get_auto_reply_rule_by_id(rule_id, db_path) 
        if updated_rule:
            logger.debug(f"DB_UPDATE: Successfully retrieved updated rule data")
        else:

            logger.error(f"DB_UPDATE: Failed to retrieve rule after successful update")

        return updated_rule

    except sqlite3.Error as e:
        logger.exception(f"DB_UPDATE: Database error updating auto-reply rule {rule_id}: {e}")
        logger.debug(f"DB_UPDATE: Error details - Type: {type(e).__name__}, Args: {e.args}, Code: {e.sqlite_errorcode}, Name: {e.sqlite_errorname}")
        return None
    finally:
        if conn:
            try:
                conn.close()
                logger.debug(f"DB_UPDATE: Database connection closed.")
            except sqlite3.Error as e:
                logger.error(f"DB_UPDATE: Error closing DB connection: {e}", exc_info=False)

def db_delete_auto_reply_rule(rule_id: int, db_path: str = DB_PATH) -> bool:
    """Deletes an auto-reply rule from the database."""
    logger.info(f"DB_DELETE: Deleting rule ID: {rule_id}")

    conn = None
    try:

        logger.debug(f"DB_DELETE: Checking if rule ID {rule_id} exists before deletion")
        existing_rule = db_get_auto_reply_rule_by_id(rule_id, db_path)
        if not existing_rule:
            logger.warning(f"DB_DELETE: Rule ID {rule_id} not found for deletion")
            return False

        logger.debug(f"DB_DELETE: Rule ID {rule_id} exists, proceeding with deletion")
        logger.debug(f"DB_DELETE: Opening database connection")
        conn = _get_db_connection(db_path)
        cursor = conn.cursor()
        query = f"DELETE FROM {AUTO_REPLY_TABLE_NAME} WHERE id = ?"
        logger.debug(f"DB_DELETE: Executing query: '{query}' with param: {rule_id}")

        cursor.execute(query, (rule_id,))
        conn.commit()

        deleted_count = cursor.rowcount
        logger.debug(f"DB_DELETE: Delete operation affected {deleted_count} rows")

        if deleted_count > 0:
            logger.info(f"DB_DELETE: Successfully deleted auto-reply rule ID: {rule_id}")

            if rule_id in cooldown_tracker:
                logger.debug(f"DB_DELETE: Clearing cooldown tracking data for rule ID {rule_id}")
                try:
                    del cooldown_tracker[rule_id]
                    logger.debug(f"DB_DELETE: Cooldown data cleared")
                except KeyError:
                     logger.warning(f"DB_DELETE: Cooldown entry for rule {rule_id} disappeared before deletion.")

            return True
        else:

            logger.warning(f"DB_DELETE: Rule ID {rule_id} not found for deletion (no rows deleted - possibly deleted concurrently?)")
            return False

    except sqlite3.Error as e:
        logger.exception(f"DB_DELETE: Database error deleting auto-reply rule {rule_id}: {e}")
        logger.debug(f"DB_DELETE: Error details - Type: {type(e).__name__}, Args: {e.args}, Code: {e.sqlite_errorcode}, Name: {e.sqlite_errorname}")
        return False
    finally:
        if conn:
            try:
                conn.close()
                logger.debug(f"DB_DELETE: Database connection closed.")
            except sqlite3.Error as e:
                 logger.error(f"DB_DELETE: Error closing DB connection: {e}", exc_info=False)

def check_message_for_auto_reply(
    incoming_message: str,
    sender_node_id: str,
    channel_index: Optional[int], 
    rules: List[Dict[str, Any]] 
) -> List[Dict[str, Any]]:
    """
    Checks an incoming message against enabled rules and returns replies to send.
    Handles cooldowns.
    """
    logger.debug(f"CHECK_MSG: Checking message '{incoming_message}' from sender {sender_node_id} on channel {channel_index}")
    logger.debug(f"CHECK_MSG: Have {len(rules)} rules to check against")

    replies_to_send = []
    current_time = time.time()
    message_lower = incoming_message.lower() 
    logger.debug(f"CHECK_MSG: Current timestamp: {current_time}")

    for rule in rules:

        if not isinstance(rule, dict) or not all(k in rule for k in ['id', 'trigger_phrase', 'match_type', 'response_message', 'cooldown_seconds', 'is_enabled']):
            logger.warning(f"CHECK_MSG: Skipping invalid rule structure: {rule}")
            continue

        if not rule.get('is_enabled'):
            logger.debug(f"CHECK_MSG: Skipping disabled rule ID {rule.get('id')}")
            continue

        rule_id = rule['id']
        trigger = rule['trigger_phrase']
        match_type = rule['match_type']
        response = rule['response_message']
        cooldown = rule.get('cooldown_seconds', 60) 

        logger.debug(f"CHECK_MSG: Checking rule ID {rule_id} - Trigger: '{trigger}', Type: {match_type}")

        cooldown_active = False
        time_remaining = 0

        if rule_id in cooldown_tracker and sender_node_id in cooldown_tracker[rule_id]:
            last_triggered = cooldown_tracker[rule_id][sender_node_id]
            time_elapsed = current_time - last_triggered
            time_remaining = max(0, cooldown - time_elapsed)

            logger.debug(f"CHECK_MSG: Rule {rule_id} cooldown check - Last triggered: {last_triggered}, Cooldown period: {cooldown}s, Time elapsed: {time_elapsed:.2f}s")

            if time_remaining > 0:
                logger.info(f"CHECK_MSG: Rule {rule_id} on cooldown for sender {sender_node_id}. {time_remaining:.2f}s remaining. Skipping.")
                cooldown_active = True
                continue 

        match_found = False
        try:
            if match_type == 'contains':
                trigger_lower = trigger.lower()
                match_found = trigger_lower in message_lower
                logger.debug(f"CHECK_MSG: 'contains' match check - Trigger: '{trigger_lower}' in '{message_lower}' = {match_found}")

            elif match_type == 'exact':
                trigger_lower = trigger.lower()
                match_found = trigger_lower == message_lower
                logger.debug(f"CHECK_MSG: 'exact' match check - Trigger: '{trigger_lower}' == '{message_lower}' = {match_found}")

            elif match_type == 'regex':

                match_obj = re.search(trigger, incoming_message, re.IGNORECASE)
                match_found = bool(match_obj)
                if match_found:
                    logger.debug(f"CHECK_MSG: 'regex' match found with pattern '{trigger}' - Groups: {match_obj.groups() if match_obj.groups() else 'none'}")
                else:
                    logger.debug(f"CHECK_MSG: 'regex' pattern '{trigger}' did not match message")

        except re.error as re_err:
            logger.error(f"CHECK_MSG: Regex error in rule {rule_id} ('{trigger}'): {re_err}. Skipping rule.")
            logger.debug(f"CHECK_MSG: Regex error details - Type: {type(re_err).__name__}, Args: {re_err.args}")
            continue 

        except Exception as match_err:
            logger.error(f"CHECK_MSG: Unexpected error matching rule {rule_id}: {match_err}")
            logger.debug(f"CHECK_MSG: Error details - Type: {type(match_err).__name__}, Args: {match_err.args}", exc_info=True)
            continue 

        if match_found:
            logger.info(f"CHECK_MSG: Rule {rule_id} triggered by message from {sender_node_id} ('{incoming_message}')")
            reply = {
                "destination": sender_node_id,
                "message": response,
                "channel": channel_index 
            }
            logger.debug(f"CHECK_MSG: Adding reply for rule {rule_id}: {reply}")
            replies_to_send.append(reply)

            logger.debug(f"CHECK_MSG: Updating cooldown for rule {rule_id}, sender {sender_node_id}")
            if rule_id not in cooldown_tracker:
                cooldown_tracker[rule_id] = {}
            cooldown_tracker[rule_id][sender_node_id] = current_time
            logger.debug(f"CHECK_MSG: Cooldown updated to {current_time}")

    logger.debug(f"CHECK_MSG: Found {len(replies_to_send)} replies to send")
    return replies_to_send

if __name__ == "__main__":

    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")

    logger.info("TEST: Running auto_reply.py directly for testing...")

    logger.info("TEST: Initializing database for testing")
    init_auto_reply_db()

    logger.info("TEST: Adding test rules")
    print("\n--- Adding Test Rules ---")
    rule1_id = db_add_auto_reply_rule("status?", "contains", "System OK.", 30, True)
    rule2_id = db_add_auto_reply_rule("^ping$", "regex", "PONG!", 15, True) 
    rule3_id = db_add_auto_reply_rule("weather", "exact", "Weather info not available.", 60, False) 

    logger.info("TEST: Getting all rules")
    print("\n--- Getting All Rules ---")
    all_rules = db_get_auto_reply_rules()
    print(json.dumps(all_rules, indent=2))

    logger.info("TEST: Getting enabled rules")
    print("\n--- Getting Enabled Rules ---")
    enabled_rules = db_get_auto_reply_rules(only_enabled=True)
    print(json.dumps(enabled_rules, indent=2))

    if rule1_id:
        logger.info(f"TEST: Getting specific rule {rule1_id}")
        print(f"\n--- Getting Rule ID {rule1_id} ---")
        rule1 = db_get_auto_reply_rule_by_id(rule1_id)
        print(json.dumps(rule1, indent=2) if rule1 else "Rule not found")

    if rule2_id:
        logger.info(f"TEST: Updating rule {rule2_id}")
        print(f"\n--- Updating Rule ID {rule2_id} ---")
        updated_rule = db_update_auto_reply_rule(rule2_id, "^ping$", "regex", "PONG! v2", 15, True) 
        print("Updated Rule:", json.dumps(updated_rule, indent=2) if updated_rule else "Update failed")

    logger.info("TEST: Testing message checking")
    print("\n--- Checking Messages ---")
    sender = "!1234abcd"
    channel = 0
    messages_to_check = [
        "What is the status?",
        "ping",
        "PING", 
        "weather", 
        "status please",
        "ping again" 
    ]

    logger.info("TEST: Fetching rules for message check test")
    current_enabled_rules = db_get_auto_reply_rules(only_enabled=True)

    for msg in messages_to_check:
        logger.info(f"TEST: Checking message '{msg}'")
        print(f"\nChecking: '{msg}' from {sender}")
        replies = check_message_for_auto_reply(msg, sender, channel, current_enabled_rules)
        if replies:
            print(" -> Replies:", json.dumps(replies))
        else:
            print(" -> No reply triggered.")

    logger.info(f"TEST: Testing cooldown for rule {rule2_id}")
    print(f"\n--- Testing Cooldown for Rule {rule2_id} (ping) ---")
    print("Checking 'ping' again immediately...")
    replies = check_message_for_auto_reply("ping", sender, channel, current_enabled_rules)
    print(" -> Replies:", json.dumps(replies) if replies else "No reply triggered (expected due to cooldown).")

    logger.info("TEST: Waiting 16 seconds for cooldown to expire")
    print("\nWaiting 16 seconds for cooldown...")
    time.sleep(16)
    print("Checking 'ping' after cooldown...")
    replies = check_message_for_auto_reply("ping", sender, channel, current_enabled_rules)
    print(" -> Replies:", json.dumps(replies) if replies else "No reply triggered.")

    logger.info("TEST: Deleting test rules")
    print("\n--- Deleting Test Rules ---")
    if rule1_id: db_delete_auto_reply_rule(rule1_id)
    if rule2_id: db_delete_auto_reply_rule(rule2_id)
    if rule3_id: db_delete_auto_reply_rule(rule3_id)

    logger.info("TEST: Checking final rule state")
    print("\n--- Rules after deletion ---")
    final_rules = db_get_auto_reply_rules()
    print(json.dumps(final_rules, indent=2))

    logger.info("TEST: Test complete")
    print("\n--- Test Complete ---")