import os
import asyncio
import hmac
import hashlib
from typing import List, Dict, Any, Optional
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
import uvicorn

from latticeshadow_db.latticedb import connect, Collection
from latticeshadow_db.http_limits import NonlocalTlsGuard, RequestBodyLimit, is_loopback_host

app = FastAPI(
    title="LatticeDB REST Lock Server",
    description="Production-ready REST API for LatticeDB with Bearer Token auth and transaction serialization locks.",
    version="1.0.0"
)
app.add_middleware(RequestBodyLimit, max_bytes=8 * 1024 * 1024)
app.add_middleware(NonlocalTlsGuard)

security = HTTPBearer()

# Dynamic registry to cache Collection instances
# Key: (db_path, collection_name, embedding_dim, privacy, auto_distill, lattice_index, max_entries,
#       drosophila_hash, engine, experimental_index)
_collections_cache: Dict[tuple, Collection] = {}
_cache_lock = asyncio.Lock()

# Global write locks per database file path to prevent SQLite / memmap write race conditions
_db_write_locks: Dict[str, asyncio.Lock] = {}
_db_locks_lock = asyncio.Lock()


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(1, value)


SERVER_DB_ROOT = Path(os.environ.get("LATTICEDB_SERVER_DB_ROOT", "latticedb_data")).resolve()
MAX_SERVER_EMBEDDING_DIM = _int_env("LATTICEDB_MAX_SERVER_EMBEDDING_DIM", 4096)
MAX_SERVER_RESULTS = _int_env("LATTICEDB_MAX_SERVER_RESULTS", 1000)
MAX_SERVER_DOCS_PER_ADD = _int_env("LATTICEDB_MAX_SERVER_DOCS_PER_ADD", 1000)
MAX_SERVER_IDS_PER_DELETE = _int_env("LATTICEDB_MAX_SERVER_IDS_PER_DELETE", 1000)
MAX_SERVER_HNSW_M = _int_env("LATTICEDB_MAX_SERVER_HNSW_M", 128)
MAX_SERVER_HNSW_EF = _int_env("LATTICEDB_MAX_SERVER_HNSW_EF", 4096)
MAX_SERVER_HNSW_CANDIDATES = _int_env("LATTICEDB_MAX_SERVER_HNSW_CANDIDATES", 10000)


def resolve_server_db_path(db_path: str) -> str:
    """Resolve a client database name under the server-owned storage root."""
    if not db_path or "\x00" in db_path:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid db_path.")

    requested = Path(db_path)
    if requested.is_absolute() or any(part == ".." for part in requested.parts):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="db_path must be a relative path inside the server storage root."
        )

    root = SERVER_DB_ROOT.resolve()
    candidate = (root / requested).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="db_path escapes the server storage root."
        )
    if candidate == root:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="db_path must name a database file.")

    candidate.parent.mkdir(parents=True, exist_ok=True)
    return str(candidate)


def validate_collection_limits(
    embedding_dim: int,
    hnsw_m: int,
    hnsw_ef_construction: int,
    hnsw_ef_search: int,
    hnsw_candidate_cap: int,
) -> None:
    if not 1 <= embedding_dim <= MAX_SERVER_EMBEDDING_DIM:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"embedding_dim must be between 1 and {MAX_SERVER_EMBEDDING_DIM}."
        )
    if not 1 <= hnsw_m <= MAX_SERVER_HNSW_M:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="hnsw_m is out of range.")
    if not 1 <= hnsw_ef_construction <= MAX_SERVER_HNSW_EF:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="hnsw_ef_construction is out of range.")
    if not 1 <= hnsw_ef_search <= MAX_SERVER_HNSW_EF:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="hnsw_ef_search is out of range.")
    if not 1 <= hnsw_candidate_cap <= MAX_SERVER_HNSW_CANDIDATES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="hnsw_candidate_cap is out of range.")


async def get_db_write_lock(db_path: str) -> asyncio.Lock:
    """Retrieve or create a unique write lock for a specific SQLite db path."""
    db_path = resolve_server_db_path(db_path)
    async with _db_locks_lock:
        if db_path not in _db_write_locks:
            _db_write_locks[db_path] = asyncio.Lock()
        return _db_write_locks[db_path]

def get_master_key() -> str:
    """Retrieve and validate the LATTICEDB_MASTER_KEY from environment variables."""
    master_key = os.environ.get("LATTICEDB_MASTER_KEY")
    if not master_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LATTICEDB_MASTER_KEY environment variable is not configured on the server."
        )
    return master_key

def get_api_token() -> str:
    token = os.environ.get("LATTICEDB_API_TOKEN")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LATTICEDB_API_TOKEN is not configured on the server."
        )
    if hmac.compare_digest(token.encode("utf-8"), get_master_key().encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LATTICEDB_API_TOKEN must differ from LATTICEDB_MASTER_KEY."
        )
    return token

def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    """Verify the dedicated REST credential, never the encryption master key."""
    api_token = get_api_token()
    if not hmac.compare_digest(credentials.credentials.encode("utf-8"), api_token.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing authentication token."
        )
    return api_token

async def get_cached_collection(
    collection_name: str,
    db_path: str,
    embedding_dim: int,
    privacy: bool,
    auto_distill: bool,
    lattice_index: bool,
    max_entries: int,
    drosophila_hash: bool,
    engine: str,
    master_key: str,
    experimental_index: Optional[str] = None,
    hnsw_m: int = 32,
    hnsw_ef_construction: int = 200,
    hnsw_ef_search: int = 128,
    hnsw_candidate_cap: int = 1000,
) -> Collection:
    """Thread-safe acquisition of a cached Collection instance."""
    db_path = resolve_server_db_path(db_path)
    validate_collection_limits(
        embedding_dim,
        hnsw_m,
        hnsw_ef_construction,
        hnsw_ef_search,
        hnsw_candidate_cap,
    )
    cache_key = (
        db_path,
        collection_name,
        embedding_dim,
        privacy,
        auto_distill,
        lattice_index,
        max_entries,
        drosophila_hash,
        engine,
        experimental_index,
        hnsw_m,
        hnsw_ef_construction,
        hnsw_ef_search,
        hnsw_candidate_cap,
        hashlib.sha256(master_key.encode("utf-8")).digest(),
    )
    async with _cache_lock:
        if cache_key not in _collections_cache:
            try:
                _collections_cache[cache_key] = connect(
                    db_path=db_path,
                    collection=collection_name,
                    embedding_dim=embedding_dim,
                    privacy=privacy,
                    auto_distill=auto_distill,
                    lattice_index=lattice_index,
                    max_entries=max_entries,
                    drosophila_hash=drosophila_hash,
                    engine=engine,
                    master_key=master_key,
                    experimental_index=experimental_index,
                    hnsw_m=hnsw_m,
                    hnsw_ef_construction=hnsw_ef_construction,
                    hnsw_ef_search=hnsw_ef_search,
                    hnsw_candidate_cap=hnsw_candidate_cap,
                )
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Failed to initialize collection '{collection_name}': {str(e)}"
                )
        return _collections_cache[cache_key]

# ── Pydantic Schemas ───────────────────────────────────────────────────

class CollectionConfig(BaseModel):
    db_path: str = Field(default="latticedb.sqlite", min_length=1, max_length=255, description="SQLite database file path.")
    embedding_dim: int = Field(default=768, ge=1, le=MAX_SERVER_EMBEDDING_DIM, description="Dimension of embedding vectors.")
    privacy: bool = Field(default=False, description="Enable vector privacy (Cayley rotation).")
    auto_distill: bool = Field(default=False, description="Enable cloud-to-local distillation.")
    lattice_index: bool = Field(default=False, description="Enable Leech Lattice coarse index.")
    max_entries: int = Field(default=0, ge=0, le=1_000_000, description="LRU eviction capacity limit (0 = unlimited).")
    drosophila_hash: bool = Field(default=False, description="Enable biological hashing & Hamming search.")
    engine: str = Field(default="default", description="Storage/search engine ('default', 'hyperbolic', 'holographic').")
    experimental_index: Optional[str] = Field(default=None, description="Opt-in experimental index strategy, e.g. 'hnsw_exact_rerank', 'flyhash_exact_rerank', 'pq_exact_rerank', 'diskann_rerank', 'streaming_exact', or 'cascade_auto'.")
    hnsw_m: int = Field(default=32, ge=1, le=MAX_SERVER_HNSW_M, description="Native hnswlib graph degree for hnsw_exact_rerank.")
    hnsw_ef_construction: int = Field(default=200, ge=1, le=MAX_SERVER_HNSW_EF, description="Native hnswlib build efConstruction for hnsw_exact_rerank.")
    hnsw_ef_search: int = Field(default=128, ge=1, le=MAX_SERVER_HNSW_EF, description="Native hnswlib query efSearch for hnsw_exact_rerank.")
    hnsw_candidate_cap: int = Field(default=1000, ge=1, le=MAX_SERVER_HNSW_CANDIDATES, description="Maximum HNSW candidates before exact rerank.")

class AddRequest(BaseModel):
    config: CollectionConfig
    documents: List[str] = Field(..., min_length=1, max_length=MAX_SERVER_DOCS_PER_ADD)
    metadatas: Optional[List[Dict[str, Any]]] = None
    ids: Optional[List[str]] = None

class SearchRequest(BaseModel):
    config: CollectionConfig
    query: str
    n_results: int = Field(default=10, ge=1, le=MAX_SERVER_RESULTS)
    where: Optional[Dict[str, Any]] = None
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    hybrid: bool = Field(default=False, description="Enable hybrid sparse-dense BM25 search.")

class DeleteRequest(BaseModel):
    config: CollectionConfig
    ids: List[str] = Field(..., min_length=1, max_length=MAX_SERVER_IDS_PER_DELETE)

class ClearRequest(BaseModel):
    config: CollectionConfig

class ShredRequest(BaseModel):
    config: CollectionConfig

class ImportRequest(BaseModel):
    config: CollectionConfig
    backup_data: Dict[str, Any]

# ── API Endpoints ──────────────────────────────────────────────────────

@app.post("/api/collections/{name}/add", status_code=status.HTTP_201_CREATED)
async def add_documents(
    name: str,
    req: AddRequest,
    token: str = Depends(verify_token)
):
    """Embed, optionally encrypt, and store documents. Serialized via db write lock."""
    # Ensure master_key is passed to decrypt/encrypt if privacy is enabled
    coll = await get_cached_collection(
        collection_name=name,
        db_path=req.config.db_path,
        embedding_dim=req.config.embedding_dim,
        privacy=req.config.privacy,
        auto_distill=req.config.auto_distill,
        lattice_index=req.config.lattice_index,
        max_entries=req.config.max_entries,
        drosophila_hash=req.config.drosophila_hash,
        engine=req.config.engine,
        experimental_index=req.config.experimental_index,
        hnsw_m=req.config.hnsw_m,
        hnsw_ef_construction=req.config.hnsw_ef_construction,
        hnsw_ef_search=req.config.hnsw_ef_search,
        hnsw_candidate_cap=req.config.hnsw_candidate_cap,
        master_key=get_master_key()
    )
    
    write_lock = await get_db_write_lock(req.config.db_path)
    async with write_lock:
        try:
            # Run in executor to avoid blocking the event loop on disk write/compilation
            loop = asyncio.get_running_loop()
            doc_ids = await loop.run_in_executor(
                None,
                coll.add,
                req.documents,
                req.metadatas,
                req.ids
            )
            return {"status": "success", "inserted_ids": doc_ids}
        except PermissionError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Database is locked or key shredded."
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e)
            )

@app.post("/api/collections/{name}/search")
async def search_collection(
    name: str,
    req: SearchRequest,
    token: str = Depends(verify_token)
):
    """Semantic or hybrid search over stored documents. Concurrent reads allowed."""
    coll = await get_cached_collection(
        collection_name=name,
        db_path=req.config.db_path,
        embedding_dim=req.config.embedding_dim,
        privacy=req.config.privacy,
        auto_distill=req.config.auto_distill,
        lattice_index=req.config.lattice_index,
        max_entries=req.config.max_entries,
        drosophila_hash=req.config.drosophila_hash,
        engine=req.config.engine,
        experimental_index=req.config.experimental_index,
        hnsw_m=req.config.hnsw_m,
        hnsw_ef_construction=req.config.hnsw_ef_construction,
        hnsw_ef_search=req.config.hnsw_ef_search,
        hnsw_candidate_cap=req.config.hnsw_candidate_cap,
        master_key=get_master_key()
    )
    
    try:
        loop = asyncio.get_running_loop()
        # Search is read-only, no lock needed
        result = await loop.run_in_executor(
            None,
            lambda: coll.search(
                query=req.query,
                n_results=req.n_results,
                where=req.where,
                temperature=req.temperature,
                hybrid=req.hybrid
            )
        )
        return {
            "status": "success",
            "results": {
                "ids": result.ids,
                "documents": result.documents,
                "metadatas": result.metadatas,
                "scores": result.scores,
                "distances": result.distances,
                "served_locally": result.served_locally
            }
        }
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Database is locked or key shredded."
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )

@app.post("/api/collections/{name}/delete")
async def delete_documents(
    name: str,
    req: DeleteRequest,
    token: str = Depends(verify_token)
):
    """Delete documents by ID. Serialized via db write lock."""
    coll = await get_cached_collection(
        collection_name=name,
        db_path=req.config.db_path,
        embedding_dim=req.config.embedding_dim,
        privacy=req.config.privacy,
        auto_distill=req.config.auto_distill,
        lattice_index=req.config.lattice_index,
        max_entries=req.config.max_entries,
        drosophila_hash=req.config.drosophila_hash,
        engine=req.config.engine,
        experimental_index=req.config.experimental_index,
        hnsw_m=req.config.hnsw_m,
        hnsw_ef_construction=req.config.hnsw_ef_construction,
        hnsw_ef_search=req.config.hnsw_ef_search,
        hnsw_candidate_cap=req.config.hnsw_candidate_cap,
        master_key=get_master_key()
    )
    
    write_lock = await get_db_write_lock(req.config.db_path)
    async with write_lock:
        try:
            loop = asyncio.get_running_loop()
            deleted_count = await loop.run_in_executor(None, coll.delete, req.ids)
            return {"status": "success", "deleted_count": deleted_count}
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e)
            )

@app.post("/api/collections/{name}/clear")
async def clear_collection(
    name: str,
    req: ClearRequest,
    token: str = Depends(verify_token)
):
    """Clear all documents from collection. Serialized via db write lock."""
    coll = await get_cached_collection(
        collection_name=name,
        db_path=req.config.db_path,
        embedding_dim=req.config.embedding_dim,
        privacy=req.config.privacy,
        auto_distill=req.config.auto_distill,
        lattice_index=req.config.lattice_index,
        max_entries=req.config.max_entries,
        drosophila_hash=req.config.drosophila_hash,
        engine=req.config.engine,
        experimental_index=req.config.experimental_index,
        hnsw_m=req.config.hnsw_m,
        hnsw_ef_construction=req.config.hnsw_ef_construction,
        hnsw_ef_search=req.config.hnsw_ef_search,
        hnsw_candidate_cap=req.config.hnsw_candidate_cap,
        master_key=get_master_key()
    )
    
    write_lock = await get_db_write_lock(req.config.db_path)
    async with write_lock:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, coll.clear)
            return {"status": "success", "message": f"Collection '{name}' cleared successfully."}
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e)
            )

@app.post("/api/collections/{name}/count")
async def count_collection(
    name: str,
    config: CollectionConfig,
    token: str = Depends(verify_token)
):
    """Retrieve document count for collection. Concurrent reads allowed."""
    coll = await get_cached_collection(
        collection_name=name,
        db_path=config.db_path,
        embedding_dim=config.embedding_dim,
        privacy=config.privacy,
        auto_distill=config.auto_distill,
        lattice_index=config.lattice_index,
        max_entries=config.max_entries,
        drosophila_hash=config.drosophila_hash,
        engine=config.engine,
        experimental_index=config.experimental_index,
        hnsw_m=config.hnsw_m,
        hnsw_ef_construction=config.hnsw_ef_construction,
        hnsw_ef_search=config.hnsw_ef_search,
        hnsw_candidate_cap=config.hnsw_candidate_cap,
        master_key=get_master_key()
    )
    
    try:
        loop = asyncio.get_running_loop()
        count = await loop.run_in_executor(None, coll.count)
        return {"status": "success", "count": count}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )

@app.post("/api/collections/{name}/shred")
async def shred_key_blob(
    name: str,
    req: ShredRequest,
    token: str = Depends(verify_token)
):
    """Permanently destroy encryption keys. Serialized via db write lock."""
    coll = await get_cached_collection(
        collection_name=name,
        db_path=req.config.db_path,
        embedding_dim=req.config.embedding_dim,
        privacy=req.config.privacy,
        auto_distill=req.config.auto_distill,
        lattice_index=req.config.lattice_index,
        max_entries=req.config.max_entries,
        drosophila_hash=req.config.drosophila_hash,
        engine=req.config.engine,
        experimental_index=req.config.experimental_index,
        hnsw_m=req.config.hnsw_m,
        hnsw_ef_construction=req.config.hnsw_ef_construction,
        hnsw_ef_search=req.config.hnsw_ef_search,
        hnsw_candidate_cap=req.config.hnsw_candidate_cap,
        master_key=get_master_key()
    )
    
    write_lock = await get_db_write_lock(req.config.db_path)
    async with write_lock:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, coll.crypto_shred)
            # Remove from local collections cache
            resolved_db_path = resolve_server_db_path(req.config.db_path)
            async with _cache_lock:
                for k in list(_collections_cache.keys()):
                    if k[0] == resolved_db_path and k[1] == name:
                        del _collections_cache[k]
            return {"status": "success", "message": f"Collection '{name}' key shredded. Data is permanently lost."}
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e)
            )

@app.post("/api/collections/{name}/rotate")
async def rotate_master_key(
    name: str,
    token: str = Depends(verify_token)
):
    """A per-collection REST rotation would conflict with the server's single master key."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Stop the server and rotate keys with the DB CLI before updating LATTICEDB_MASTER_KEY.",
    )

@app.get("/api/collections/{name}/export")
async def export_data(
    name: str,
    config: CollectionConfig,
    token: str = Depends(verify_token)
):
    """Export all records for backup. Concurrent reads allowed."""
    coll = await get_cached_collection(
        collection_name=name,
        db_path=config.db_path,
        embedding_dim=config.embedding_dim,
        privacy=config.privacy,
        auto_distill=config.auto_distill,
        lattice_index=config.lattice_index,
        max_entries=config.max_entries,
        drosophila_hash=config.drosophila_hash,
        engine=config.engine,
        experimental_index=config.experimental_index,
        hnsw_m=config.hnsw_m,
        hnsw_ef_construction=config.hnsw_ef_construction,
        hnsw_ef_search=config.hnsw_ef_search,
        hnsw_candidate_cap=config.hnsw_candidate_cap,
        master_key=get_master_key()
    )
    
    try:
        loop = asyncio.get_running_loop()
        backup = await loop.run_in_executor(None, coll.export_data)
        return {"status": "success", "backup": backup}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )

@app.post("/api/collections/{name}/import")
async def import_data(
    name: str,
    req: ImportRequest,
    token: str = Depends(verify_token)
):
    """Import records from backup. Serialized via db write lock."""
    coll = await get_cached_collection(
        collection_name=name,
        db_path=req.config.db_path,
        embedding_dim=req.config.embedding_dim,
        privacy=req.config.privacy,
        auto_distill=req.config.auto_distill,
        lattice_index=req.config.lattice_index,
        max_entries=req.config.max_entries,
        drosophila_hash=req.config.drosophila_hash,
        engine=req.config.engine,
        experimental_index=req.config.experimental_index,
        hnsw_m=req.config.hnsw_m,
        hnsw_ef_construction=req.config.hnsw_ef_construction,
        hnsw_ef_search=req.config.hnsw_ef_search,
        hnsw_candidate_cap=req.config.hnsw_candidate_cap,
        master_key=get_master_key()
    )
    
    write_lock = await get_db_write_lock(req.config.db_path)
    async with write_lock:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                coll.import_data,
                req.backup_data,
                True # overwrite
            )
            return {"status": "success", "message": "Database backup imported successfully."}
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e)
            )

def main():
    host = os.environ.get("LATTICEDB_HOST", "127.0.0.1")
    port = int(os.environ.get("LATTICEDB_PORT", "8000"))
    ssl_keyfile = os.environ.get("LATTICEDB_SSL_KEYFILE")
    ssl_certfile = os.environ.get("LATTICEDB_SSL_CERTFILE")

    if not is_loopback_host(host) and not (ssl_keyfile and ssl_certfile):
        raise SystemExit("Nonlocal DB REST binding requires LATTICEDB_SSL_KEYFILE and LATTICEDB_SSL_CERTFILE.")

    uvicorn.run(
        "latticeshadow_db.server:app",
        host=host,
        port=port,
        ssl_keyfile=ssl_keyfile,
        ssl_certfile=ssl_certfile,
        limit_concurrency=32,
    )


if __name__ == "__main__":
    main()
