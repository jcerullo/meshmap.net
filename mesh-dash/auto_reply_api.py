import logging
import asyncio
import sqlite3 

from typing import List, Dict, Optional, Any

from fastapi import APIRouter, HTTPException, Depends, status, Path, Body, Query

try:

    from auto_reply import (
        AutoReplyRuleCreateUpdate,
        AutoReplyRuleResponse,
        db_add_auto_reply_rule,
        db_get_auto_reply_rules,
        db_get_auto_reply_rule_by_id,
        db_update_auto_reply_rule,
        db_delete_auto_reply_rule,
        logger as auto_reply_logger 
    )
    AUTO_REPLY_API_ENABLED = True
except ImportError as import_err:

     logging.basicConfig(level=logging.INFO) 

     if "attempted relative import with no known parent package" in str(import_err):
         logging.critical(f"FATAL: Relative import failed in auto_reply_api.py. Change 'from .auto_reply import ...' to 'from auto_reply import ...'. Error: {import_err}", exc_info=False)
     else:
        logging.critical(f"FATAL: Could not import from auto_reply.py for API. Ensure the file exists and has correct permissions/syntax. Error: {import_err}", exc_info=True)
     AUTO_REPLY_API_ENABLED = False

     auto_reply_router = APIRouter()

     class AutoReplyRuleCreateUpdate: pass
     class AutoReplyRuleResponse: pass
     def db_add_auto_reply_rule(*args, **kwargs): return None
     def db_get_auto_reply_rules(*args, **kwargs): return []
     def db_get_auto_reply_rule_by_id(*args, **kwargs): return None
     def db_update_auto_reply_rule(*args, **kwargs): return None
     def db_delete_auto_reply_rule(*args, **kwargs): return False
     auto_reply_logger = logging.getLogger("meshtastic_dashboard.auto_reply_api_disabled")

logger = auto_reply_logger if AUTO_REPLY_API_ENABLED else logging.getLogger(__name__)

auto_reply_router = APIRouter()

async def run_db_func(func, *args, **kwargs):
    """Runs a synchronous function in the threadpool."""
    try:

        result = await asyncio.to_thread(func, *args, **kwargs)
        return result
    except ValueError as ve: 
         logger.warning(f"Validation error during DB operation '{func.__name__}': {ve}")

         raise HTTPException(
             status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, 
             detail=str(ve)
         ) from ve
    except FileNotFoundError as fnf_err: 
        logger.error(f"Database file not found during '{func.__name__}': {fnf_err}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Auto Reply database file not found: {fnf_err}"
        ) from fnf_err
    except sqlite3.Error as sql_err: 
        logger.error(f"SQLite error during '{func.__name__}': {sql_err} (Code: {sql_err.sqlite_errorcode} Name: {sql_err.sqlite_errorname})", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error during operation: {sql_err}"
        ) from sql_err
    except Exception as e: 
        logger.exception(f"Unexpected error during background DB task '{func.__name__}': {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected server error during database operation."
        ) from e

if AUTO_REPLY_API_ENABLED:
    @auto_reply_router.post(
        "/rules",
        response_model=AutoReplyRuleResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Create a new auto-reply rule",
        description="Adds a new rule to the database."
    )
    async def create_rule_api(
        rule_data: AutoReplyRuleCreateUpdate = Body(...) 
    ):
        """
        Creates a new auto-reply rule based on the provided data.

        - **trigger_phrase**: The text or regex pattern that triggers the reply.
        - **match_type**: How to match ('contains', 'exact', 'regex').
        - **response_message**: The message text to send back.
        - **cooldown_seconds**: Minimum time (seconds) before this rule triggers again for the same sender.
        - **is_enabled**: Whether the rule is currently active (true/false).
        """
        logger.info(f"API: Received request to create auto-reply rule. Trigger: '{rule_data.trigger_phrase}'")

        dump_method = getattr(rule_data, "model_dump_json", getattr(rule_data, "dict", None))
        if dump_method: logger.debug(f"API_CREATE: Full request data: {dump_method(indent=2)}")

        new_rule_id = await run_db_func(
            db_add_auto_reply_rule,
            rule_data.trigger_phrase,
            rule_data.match_type,
            rule_data.response_message,
            rule_data.cooldown_seconds,
            rule_data.is_enabled
        )

        if new_rule_id is None:
            logger.error("API_CREATE: Failed to add rule to database (run_db_func returned None).")

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create auto-reply rule in the database."
            )

        logger.info(f"API_CREATE: Rule created successfully with ID: {new_rule_id}")

        logger.debug(f"API_CREATE: Fetching newly created rule ID {new_rule_id}")
        created_rule_data = await run_db_func(db_get_auto_reply_rule_by_id, new_rule_id)

        if not created_rule_data:
            logger.error(f"API_CREATE: Failed to retrieve rule ID {new_rule_id} after creation.")

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Rule created (ID: {new_rule_id}) but failed to retrieve confirmation."
            )

        logger.debug(f"API_CREATE: Successfully retrieved created rule: {created_rule_data}")

        try:
            response_model = AutoReplyRuleResponse(**created_rule_data)

            dump_method_resp = getattr(response_model, "model_dump_json", getattr(response_model, "dict", None))
            if dump_method_resp: logger.debug(f"API_CREATE: Returning response: {dump_method_resp(indent=2)}")
            return response_model
        except Exception as e:
            logger.error(f"API_CREATE: Error creating response model for rule ID {new_rule_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Error formatting created rule response.")

    @auto_reply_router.get(
        "/rules",
        response_model=List[AutoReplyRuleResponse], 
        summary="Get all auto-reply rules",
        description="Retrieves a list of all configured auto-reply rules."
    )
    async def get_rules_api(
        only_enabled: Optional[bool] = Query(None, description="Optionally filter rules by enabled status (true/false).")
    ):
        """
        Retrieves all auto-reply rules. Can optionally filter by enabled status.
        """
        logger.info(f"API: Received request to get auto-reply rules (only_enabled={only_enabled})")

        rules_data = await run_db_func(db_get_auto_reply_rules, only_enabled=only_enabled)

        logger.info(f"API_GET_ALL: Retrieved {len(rules_data)} rules from database.")

        try:
            response_list = [AutoReplyRuleResponse(**rule) for rule in rules_data]
            logger.debug(f"API_GET_ALL: Returning {len(response_list)} rules.")
            return response_list
        except Exception as e:
            logger.error(f"API_GET_ALL: Error creating response model list: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Error formatting rules list response.")

    @auto_reply_router.get(
        "/rules/{rule_id}",
        response_model=AutoReplyRuleResponse,
        summary="Get a specific auto-reply rule by ID",
        description="Retrieves the details of a single auto-reply rule using its unique ID."
    )
    async def get_rule_by_id_api(
        rule_id: int = Path(..., description="The unique ID of the rule to retrieve.", ge=1)
    ):
        """
        Retrieves a specific auto-reply rule by its ID.
        """
        logger.info(f"API: Received request to get auto-reply rule ID: {rule_id}")

        rule_data = await run_db_func(db_get_auto_reply_rule_by_id, rule_id)

        if not rule_data:
            logger.warning(f"API_GET_ID: Rule ID {rule_id} not found.")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Auto-reply rule with ID {rule_id} not found."
            )

        logger.info(f"API_GET_ID: Found rule ID {rule_id}.")

        try:
            response_model = AutoReplyRuleResponse(**rule_data)

            dump_method_resp = getattr(response_model, "model_dump_json", getattr(response_model, "dict", None))
            if dump_method_resp: logger.debug(f"API_GET_ID: Returning rule: {dump_method_resp(indent=2)}")
            return response_model
        except Exception as e:
            logger.error(f"API_GET_ID: Error creating response model for rule ID {rule_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Error formatting rule response.")

    @auto_reply_router.put(
        "/rules/{rule_id}",
        response_model=AutoReplyRuleResponse,
        summary="Update an existing auto-reply rule",
        description="Updates the details of an existing auto-reply rule identified by its ID."
    )
    async def update_rule_api(
        rule_id: int = Path(..., description="The unique ID of the rule to update.", ge=1),
        rule_data: AutoReplyRuleCreateUpdate = Body(...) 
    ):
        """
        Updates an existing auto-reply rule. Uses the same input structure as creating a rule.
        """
        logger.info(f"API: Received request to update auto-reply rule ID: {rule_id}")

        dump_method = getattr(rule_data, "model_dump_json", getattr(rule_data, "dict", None))
        if dump_method: logger.debug(f"API_UPDATE: Update data: {dump_method(indent=2)}")

        updated_rule_data = await run_db_func(
            db_update_auto_reply_rule,
            rule_id,
            rule_data.trigger_phrase,
            rule_data.match_type,
            rule_data.response_message,
            rule_data.cooldown_seconds,
            rule_data.is_enabled
        )

        if not updated_rule_data:

            logger.warning(f"API_UPDATE: Failed to update rule ID {rule_id} (possibly not found or DB error).")

            exists_check = await run_db_func(db_get_auto_reply_rule_by_id, rule_id)
            if not exists_check:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Auto-reply rule with ID {rule_id} not found for update."
                )
            else: 
                 raise HTTPException(
                     status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                     detail=f"Failed to update auto-reply rule {rule_id} in the database."
                 )

        logger.info(f"API_UPDATE: Successfully updated rule ID {rule_id}")

        try:
            response_model = AutoReplyRuleResponse(**updated_rule_data)

            dump_method_resp = getattr(response_model, "model_dump_json", getattr(response_model, "dict", None))
            if dump_method_resp: logger.debug(f"API_UPDATE: Returning updated rule: {dump_method_resp(indent=2)}")
            return response_model
        except Exception as e:
            logger.error(f"API_UPDATE: Error creating response model for updated rule ID {rule_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Error formatting updated rule response.")

    @auto_reply_router.delete(
        "/rules/{rule_id}",
        response_model=Dict[str, str], 
        status_code=status.HTTP_200_OK,
        summary="Delete an auto-reply rule",
        description="Deletes an auto-reply rule identified by its unique ID.",
        responses={
            status.HTTP_404_NOT_FOUND: {"description": "Rule not found"},
            status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Database error during deletion"}
        }
    )
    async def delete_rule_api(
        rule_id: int = Path(..., description="The unique ID of the rule to delete.", ge=1)
    ):
        """
        Deletes a specific auto-reply rule by its ID.
        """
        logger.info(f"API: Received request to delete auto-reply rule ID: {rule_id}")

        deleted = await run_db_func(db_delete_auto_reply_rule, rule_id)

        if not deleted:

            logger.warning(f"API_DELETE: Failed to delete rule ID {rule_id} (possibly not found or DB error).")

            exists_check = await run_db_func(db_get_auto_reply_rule_by_id, rule_id)
            if not exists_check:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Auto-reply rule with ID {rule_id} not found for deletion."
                )
            else: 
                 raise HTTPException(
                     status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                     detail=f"Failed to delete auto-reply rule {rule_id} from the database."
                 )

        logger.info(f"API_DELETE: Successfully deleted rule ID {rule_id}")
        return {"detail": f"Auto-reply rule with ID {rule_id} successfully deleted."}

    @auto_reply_router.put(
        "/rules/bulk/enable",
        response_model=Dict[str, Any], 
        summary="Enable or disable all auto-reply rules",
        description="Sets the 'is_enabled' status for all rules at once."
    )
    async def bulk_enable_disable_rules_api(
        enable: bool = Query(..., description="Set to 'true' to enable all rules, 'false' to disable all rules.")
    ):
        """
        Bulk enables or disables all auto-reply rules.
        """
        action = "enable" if enable else "disable"
        logger.info(f"API: Received request to bulk {action} all auto-reply rules.")

        try:
            logger.debug(f"API_BULK_{action.upper()}: Opening database connection")

            with _get_db_connection() as conn: 
                cursor = conn.cursor()
                query = f"UPDATE {AUTO_REPLY_TABLE_NAME} SET is_enabled = ?"
                params = (1 if enable else 0,)
                logger.debug(f"API_BULK_{action.upper()}: Executing query: '{query}' with param: {params[0]}")

                def sync_bulk_update():
                    cursor.execute(query, params)
                    conn.commit()
                    return cursor.rowcount

                rows_affected = await asyncio.to_thread(sync_bulk_update)

                logger.info(f"API_BULK_{action.upper()}: Bulk {action} operation affected {rows_affected} rules.")
                return {"detail": f"Successfully set 'is_enabled' to {enable} for {rows_affected} rules."}

        except sqlite3.Error as e:
            logger.exception(f"API_BULK_{action.upper()}: Database error during bulk update: {e}")
            logger.debug(f"API_BULK_{action.upper()}: Error details - Type: {type(e).__name__}, Args: {e.args}, Code: {e.sqlite_errorcode}, Name: {e.sqlite_errorname}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Database error during bulk {action} operation."
            ) from e
        except Exception as e:
            logger.exception(f"API_BULK_{action.upper()}: Unexpected error during bulk update: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Unexpected server error during bulk {action} operation."
            ) from e

if __name__ != "__main__": 
    if not AUTO_REPLY_API_ENABLED:
        logger.critical("Auto Reply API is DISABLED due to import errors. Endpoints will not be available.")