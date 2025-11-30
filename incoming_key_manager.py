import sqlite3
import secrets
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)


class IncomingKeyManager:
    """
    Manages incoming API keys using SQLite database.
    Provides methods to create, revoke, and verify API keys.
    """

    def __init__(self, db_path: str = "./data/incoming_keys.db"):
        # Convert to absolute path and ensure it's a Path object
        self.db_path = str(Path(db_path).resolve())
        self._init_database()

    def _init_database(self):
        """Initialize the SQLite database with the api_keys table."""
        # Create directory if it doesn't exist
        db_dir = Path(self.db_path).parent
        if not db_dir.exists():
            logger.info(f"Creating database directory: {db_dir}")
            db_dir.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Create table if it doesn't exist
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revoked INTEGER DEFAULT 0,
                revoked_at TEXT,
                last_used_at TEXT,
                request_count INTEGER DEFAULT 0
            )
        """)

        conn.commit()
        conn.close()
        logger.info(f"Initialized incoming API key database at {self.db_path}")

    def generate_api_key(self, name: str) -> str:
        """
        Generate a new API key with a descriptive name.

        Args:
            name: A descriptive name for this API key

        Returns:
            The generated API key
        """
        # Generate a secure random API key (32 bytes = 64 hex characters)
        api_key = f"sk-{secrets.token_urlsafe(32)}"
        created_at = datetime.utcnow().isoformat()

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                INSERT INTO api_keys (api_key, name, created_at)
                VALUES (?, ?, ?)
            """, (api_key, name, created_at))
            conn.commit()
            logger.info(f"Generated new API key: {name}")
            return api_key
        except sqlite3.IntegrityError:
            # Very unlikely with secure random, but handle it anyway
            logger.error("API key collision detected, regenerating...")
            return self.generate_api_key(name)
        finally:
            conn.close()

    def verify_api_key(self, api_key: str) -> bool:
        """
        Verify if an API key is valid and not revoked.
        Also updates last_used_at and increments request_count.

        Args:
            api_key: The API key to verify

        Returns:
            True if valid and not revoked, False otherwise
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            # Check if key exists and is not revoked
            cursor.execute("""
                SELECT id, revoked FROM api_keys
                WHERE api_key = ?
            """, (api_key,))

            result = cursor.fetchone()

            if result is None:
                logger.warning(f"Invalid API key attempted: {api_key[:10]}...")
                return False

            key_id, revoked = result

            if revoked:
                logger.warning(f"Revoked API key attempted: {api_key[:10]}...")
                return False

            # Update last_used_at and request_count
            cursor.execute("""
                UPDATE api_keys
                SET last_used_at = ?, request_count = request_count + 1
                WHERE id = ?
            """, (datetime.utcnow().isoformat(), key_id))

            conn.commit()
            return True
        finally:
            conn.close()

    def revoke_api_key(self, api_key: str) -> bool:
        """
        Revoke an API key.

        Args:
            api_key: The API key to revoke

        Returns:
            True if successfully revoked, False if key doesn't exist
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                UPDATE api_keys
                SET revoked = 1, revoked_at = ?
                WHERE api_key = ? AND revoked = 0
            """, (datetime.utcnow().isoformat(), api_key))

            conn.commit()

            if cursor.rowcount > 0:
                logger.info(f"Revoked API key: {api_key[:10]}...")
                return True
            else:
                logger.warning(f"Attempted to revoke non-existent or already revoked key")
                return False
        finally:
            conn.close()

    def revoke_by_id(self, key_id: int) -> bool:
        """
        Revoke an API key by its ID.

        Args:
            key_id: The ID of the API key to revoke

        Returns:
            True if successfully revoked, False if key doesn't exist
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                UPDATE api_keys
                SET revoked = 1, revoked_at = ?
                WHERE id = ? AND revoked = 0
            """, (datetime.utcnow().isoformat(), key_id))

            conn.commit()

            if cursor.rowcount > 0:
                logger.info(f"Revoked API key by ID: {key_id}")
                return True
            else:
                logger.warning(f"Attempted to revoke non-existent or already revoked key with ID: {key_id}")
                return False
        finally:
            conn.close()

    def revoke_by_name(self, name: str) -> bool:
        """
        Revoke an API key by its name.

        Args:
            name: The name of the API key to revoke

        Returns:
            True if successfully revoked, False if key doesn't exist
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                UPDATE api_keys
                SET revoked = 1, revoked_at = ?
                WHERE name = ? AND revoked = 0
            """, (datetime.utcnow().isoformat(), name))

            conn.commit()

            if cursor.rowcount > 0:
                logger.info(f"Revoked API key by name: {name}")
                return True
            else:
                logger.warning(f"Attempted to revoke non-existent or already revoked key with name: {name}")
                return False
        finally:
            conn.close()

    def enable_api_key(self, api_key: str) -> bool:
        """
        Re-enable a revoked API key.

        Args:
            api_key: The API key to enable

        Returns:
            True if successfully enabled, False if key doesn't exist or not revoked
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                UPDATE api_keys
                SET revoked = 0, revoked_at = NULL
                WHERE api_key = ? AND revoked = 1
            """, (api_key,))

            conn.commit()

            if cursor.rowcount > 0:
                logger.info(f"Enabled API key: {api_key[:10]}...")
                return True
            else:
                logger.warning(f"Attempted to enable non-existent or already active key")
                return False
        finally:
            conn.close()

    def enable_by_id(self, key_id: int) -> bool:
        """
        Re-enable a revoked API key by its ID.

        Args:
            key_id: The ID of the API key to enable

        Returns:
            True if successfully enabled, False if key doesn't exist or not revoked
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                UPDATE api_keys
                SET revoked = 0, revoked_at = NULL
                WHERE id = ? AND revoked = 1
            """, (key_id,))

            conn.commit()

            if cursor.rowcount > 0:
                logger.info(f"Enabled API key by ID: {key_id}")
                return True
            else:
                logger.warning(f"Attempted to enable non-existent or already active key with ID: {key_id}")
                return False
        finally:
            conn.close()

    def enable_by_name(self, name: str) -> bool:
        """
        Re-enable a revoked API key by its name.

        Args:
            name: The name of the API key to enable

        Returns:
            True if successfully enabled, False if key doesn't exist or not revoked
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                UPDATE api_keys
                SET revoked = 0, revoked_at = NULL
                WHERE name = ? AND revoked = 1
            """, (name,))

            conn.commit()

            if cursor.rowcount > 0:
                logger.info(f"Enabled API key by name: {name}")
                return True
            else:
                logger.warning(f"Attempted to enable non-existent or already active key with name: {name}")
                return False
        finally:
            conn.close()

    def list_api_keys(self) -> List[Dict[str, Any]]:
        """
        List all API keys (both active and revoked).

        Returns:
            List of dictionaries containing API key information
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT id, api_key, name, created_at, revoked, revoked_at,
                       last_used_at, request_count
                FROM api_keys
                ORDER BY created_at DESC
            """)

            rows = cursor.fetchall()

            keys = []
            for row in rows:
                keys.append({
                    "id": row[0],
                    "api_key": row[1],
                    "name": row[2],
                    "created_at": row[3],
                    "revoked": bool(row[4]),
                    "revoked_at": row[5],
                    "last_used_at": row[6],
                    "request_count": row[7]
                })

            return keys
        finally:
            conn.close()

    def get_stats(self) -> Dict[str, int]:
        """
        Get statistics about API keys.

        Returns:
            Dictionary with total, active, and revoked counts
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT COUNT(*) FROM api_keys")
            total = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM api_keys WHERE revoked = 0")
            active = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM api_keys WHERE revoked = 1")
            revoked = cursor.fetchone()[0]

            return {
                "total": total,
                "active": active,
                "revoked": revoked
            }
        finally:
            conn.close()
