import asyncio
import json
import logging
import time
from typing import Optional, Any, Dict
from dataclasses import dataclass, asdict
from config import load_hybrid_config

logger = logging.getLogger(__name__)

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("redis.asyncio not available, Redis caching disabled")


@dataclass
class CacheEntry:
    key: str
    value: Any
    created_at: float
    ttl: int
    hits: int = 0


class RedisCache:
    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.redis_config = self.config.get("redis", {})
        self.enabled = self.redis_config.get("enabled", True) and REDIS_AVAILABLE
        self.host = self.redis_config.get("host", "localhost")
        self.port = self.redis_config.get("port", 6379)
        self.db = self.redis_config.get("db", 0)
        self.password = self.redis_config.get("password", None)
        self.default_ttl = self.redis_config.get("default_ttl_seconds", 3600)
        self.key_prefix = self.redis_config.get("key_prefix", "quant:scraper:")
        self._client: Optional[redis.Redis] = None
        self._local_cache: Dict[str, CacheEntry] = {}
        self._local_max_size = 1000

    async def initialize(self) -> None:
        if not self.enabled:
            logger.info("Redis cache disabled, using local memory cache")
            return

        try:
            self._client = redis.Redis(
                host=self.host,
                port=self.port,
                db=self.db,
                password=self.password,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
            )
            await self._client.ping()
            logger.info(f"Redis cache connected to {self.host}:{self.port}")
        except Exception as e:
            logger.warning(f"Redis connection failed, falling back to local cache: {e}")
            self.enabled = False
            self._client = None

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    async def __aenter__(self) -> "RedisCache":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _make_key(self, key: str) -> str:
        return f"{self.key_prefix}{key}"

    async def get(self, key: str) -> Optional[Any]:
        full_key = self._make_key(key)

        if self.enabled and self._client:
            try:
                data = await self._client.get(full_key)
                if data:
                    entry = json.loads(data)
                    if time.time() < entry["created_at"] + entry["ttl"]:
                        entry["hits"] += 1
                        await self._client.set(full_key, json.dumps(entry), ex=entry["ttl"])
                        logger.debug(f"Cache HIT: {key} (hits: {entry['hits']})")
                        return entry["value"]
                    else:
                        await self._client.delete(full_key)
            except Exception as e:
                logger.warning(f"Redis get error for {key}: {e}")

        # Fallback to local cache
        if full_key in self._local_cache:
            entry = self._local_cache[full_key]
            if time.time() < entry.created_at + entry.ttl:
                entry.hits += 1
                logger.debug(f"Local cache HIT: {key} (hits: {entry.hits})")
                return entry.value
            else:
                del self._local_cache[full_key]

        logger.debug(f"Cache MISS: {key}")
        return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        full_key = self._make_key(key)
        ttl = ttl or self.default_ttl
        entry_data = {
            "key": key,
            "value": value,
            "created_at": time.time(),
            "ttl": ttl,
            "hits": 0,
        }

        if self.enabled and self._client:
            try:
                await self._client.set(full_key, json.dumps(entry_data), ex=ttl)
                logger.debug(f"Cache SET: {key} (TTL: {ttl}s)")
                return True
            except Exception as e:
                logger.warning(f"Redis set error for {key}: {e}")

        # Fallback to local cache
        if len(self._local_cache) >= self._local_max_size:
            # Remove oldest entry
            oldest_key = min(self._local_cache.keys(), key=lambda k: self._local_cache[k].created_at)
            del self._local_cache[oldest_key]

        self._local_cache[full_key] = CacheEntry(
            key=key,
            value=value,
            created_at=time.time(),
            ttl=ttl,
            hits=0,
        )
        logger.debug(f"Local cache SET: {key} (TTL: {ttl}s)")
        return True

    async def delete(self, key: str) -> bool:
        full_key = self._make_key(key)

        if self.enabled and self._client:
            try:
                await self._client.delete(full_key)
            except Exception as e:
                logger.warning(f"Redis delete error for {key}: {e}")

        if full_key in self._local_cache:
            del self._local_cache[full_key]
        return True

    async def clear_pattern(self, pattern: str) -> int:
        full_pattern = self._make_key(pattern)
        count = 0

        if self.enabled and self._client:
            try:
                cursor = 0
                while True:
                    cursor, keys = await self._client.scan(cursor, match=full_pattern, count=100)
                    if keys:
                        await self._client.delete(*keys)
                        count += len(keys)
                    if cursor == 0:
                        break
            except Exception as e:
                logger.warning(f"Redis clear pattern error: {e}")

        # Clear local cache
        keys_to_delete = [k for k in self._local_cache.keys() if pattern in k]
        for k in keys_to_delete:
            del self._local_cache[k]
        count += len(keys_to_delete)

        logger.info(f"Cleared {count} cache entries matching pattern: {pattern}")
        return count

    async def get_stats(self) -> Dict[str, Any]:
        stats = {
            "enabled": self.enabled,
            "local_entries": len(self._local_cache),
            "redis_connected": self._client is not None,
        }

        if self.enabled and self._client:
            try:
                info = await self._client.info("memory")
                stats["redis_memory_used"] = info.get("used_memory_human", "unknown")
                stats["redis_connected_clients"] = (await self._client.info("clients")).get("connected_clients", 0)
            except Exception as e:
                logger.warning(f"Redis stats error: {e}")

        return stats

    async def health_check(self) -> bool:
        if not self.enabled:
            return True
        if not self._client:
            return False
        try:
            await self._client.ping()
            return True
        except Exception:
            return False


async def create_redis_cache(config_dict: dict = None) -> RedisCache:
    cache = RedisCache(config_dict)
    await cache.initialize()
    return cache


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    async def test():
        cache = await create_redis_cache()
        await cache.set("test_key", {"data": "test_value", "number": 42}, ttl=60)
        result = await cache.get("test_key")
        print(f"Retrieved: {result}")
        stats = await cache.get_stats()
        print(f"Stats: {stats}")
        await cache.close()

    asyncio.run(test())