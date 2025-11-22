import asyncio
import time
from typing import Dict, List, Optional
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class KeyState:
    """Tracks the state of an API key."""
    key: str
    name: str
    rate_limited_until: float = 0.0  # Unix timestamp when rate limit expires
    error_count: int = 0

    def is_available(self) -> bool:
        """Check if the key is available (not rate-limited)."""
        return time.time() >= self.rate_limited_until


class ApiKeyManager:
    """
    Manages API keys for the proxy server with intelligent rotation.
    - Sticks with one key until it hits rate limits
    - Switches to next available key on 429 errors
    - Waits and retries if all keys are rate-limited
    """
    def __init__(self, keys: Dict[str, str], cooldown_seconds: int = 60):
        """
        Initializes the API key manager with a dictionary of keys.

        Args:
            keys: A dictionary where key names map to API keys.
            cooldown_seconds: How long to wait before retrying a rate-limited key.
        """
        if not keys:
            raise ValueError("No API keys provided.")

        # Create KeyState objects for each key
        self._key_states: List[KeyState] = [
            KeyState(key=key_value, name=key_name)
            for key_name, key_value in keys.items()
        ]

        # Current key index
        self._current_index: int = 0

        # Lock to ensure thread-safe access
        self._lock: asyncio.Lock = asyncio.Lock()

        # Cooldown period after rate limiting
        self._cooldown_seconds: int = cooldown_seconds

        logger.info(f"Initialized ApiKeyManager with {len(self._key_states)} keys, "
                   f"{cooldown_seconds}s cooldown")

    async def get_current_key(self) -> str:
        """
        Gets the current API key without rotating.
        Waits if all keys are rate-limited.

        Returns:
            The current API key.
        """
        async with self._lock:
            # Try to find an available key starting from current index
            for _ in range(len(self._key_states)):
                current_state = self._key_states[self._current_index]

                if current_state.is_available():
                    logger.debug(f"Using key '{current_state.name}'")
                    return current_state.key

                # This key is rate-limited, try next one
                logger.info(f"Key '{current_state.name}' is rate-limited, trying next...")
                self._current_index = (self._current_index + 1) % len(self._key_states)

            # All keys are rate-limited - find the one that will be available soonest
            soonest_available = min(self._key_states,
                                   key=lambda k: k.rate_limited_until)
            wait_time = soonest_available.rate_limited_until - time.time()

            if wait_time > 0:
                logger.warning(f"All keys rate-limited. Waiting {wait_time:.1f}s for "
                             f"key '{soonest_available.name}' to become available...")
                await asyncio.sleep(wait_time)

                # Update current index to the newly available key
                self._current_index = self._key_states.index(soonest_available)

            return self._key_states[self._current_index].key

    async def mark_key_rate_limited(self, api_key: str) -> None:
        """
        Marks a key as rate-limited and rotates to the next key.

        Args:
            api_key: The API key that received a rate limit error.
        """
        async with self._lock:
            # Find the key state
            for state in self._key_states:
                if state.key == api_key:
                    state.rate_limited_until = time.time() + self._cooldown_seconds
                    state.error_count += 1
                    logger.warning(f"Key '{state.name}' rate-limited until "
                                 f"{time.strftime('%H:%M:%S', time.localtime(state.rate_limited_until))} "
                                 f"(error count: {state.error_count})")

                    # Rotate to next key
                    self._current_index = (self._current_index + 1) % len(self._key_states)
                    next_key = self._key_states[self._current_index]
                    logger.info(f"Rotating to key '{next_key.name}'")
                    break

    async def mark_key_success(self, api_key: str) -> None:
        """
        Marks a key as successfully used (resets error count).

        Args:
            api_key: The API key that was used successfully.
        """
        async with self._lock:
            for state in self._key_states:
                if state.key == api_key:
                    if state.error_count > 0:
                        logger.info(f"Key '{state.name}' recovered (was {state.error_count} errors)")
                    state.error_count = 0
                    break

    def get_key_count(self) -> int:
        """
        Gets the total number of API keys.

        Returns:
            The number of API keys.
        """
        return len(self._key_states)

    async def get_status(self) -> Dict[str, any]:
        """
        Gets the current status of all keys.

        Returns:
            Dictionary with key statuses.
        """
        async with self._lock:
            now = time.time()
            return {
                "keys": [
                    {
                        "name": state.name,
                        "available": state.is_available(),
                        "rate_limited_for": max(0, state.rate_limited_until - now),
                        "error_count": state.error_count
                    }
                    for state in self._key_states
                ],
                "current_key": self._key_states[self._current_index].name
            }

    async def all_keys_rate_limited(self) -> bool:
        """
        Check if all keys are currently rate-limited.

        Returns:
            True if all keys are rate-limited, False otherwise.
        """
        async with self._lock:
            return all(not state.is_available() for state in self._key_states)