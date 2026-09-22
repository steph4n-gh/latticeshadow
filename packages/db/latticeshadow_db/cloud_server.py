from __future__ import annotations

import argparse
import hmac
import logging
import os
from typing import List, Optional

import numpy as np
import torch

try:
    from fastapi import FastAPI, Header, HTTPException, status, Depends
    from pydantic import BaseModel
except ImportError:
    raise ImportError(
        "FastAPI is required to run the cloud server. "
        "Please run 'pip install fastapi uvicorn' to install dependencies."
    )

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("zk_bridge_cloud_server")

def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(1, value)


app = FastAPI(
    title="ZkBridge Split-Layer Cloud Server",
    description="Custom activation hosting proxy endpoint for Pathway 1 (Zero-Knowledge).",
    version="0.1.0"
)

# Configuration parameters (can be overridden by command line arguments)
CONFIG = {
    "api_key": os.getenv("ZK_CLOUD_API_KEY"),
    "local_dim": 256,
    "cloud_dim": 512,
    "device": "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu",
    "model_id": None,
    "max_batch_rows": _int_env("ZK_CLOUD_MAX_BATCH_ROWS", 1024),
    "max_total_floats": _int_env("ZK_CLOUD_MAX_TOTAL_FLOATS", 262_144),
}


class AlignRequest(BaseModel):
    obfuscated_state: List[List[float]]
    text_context: Optional[str] = None


class AlignResponse(BaseModel):
    cloud_state: List[List[float]]


def verify_token(authorization: Optional[str] = Header(None)) -> None:
    """Verifies the bearer authorization token."""
    if not CONFIG["api_key"]:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cloud alignment API key is not configured."
        )

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header"
        )

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format. Must be 'Bearer <token>'"
        )

    token = parts[1]
    if not hmac.compare_digest(token, CONFIG["api_key"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key"
        )


def _validate_obfuscated_state(obfuscated_state: List[List[float]]) -> None:
    """Reject malformed or oversized inputs before torch allocates tensors."""
    if not obfuscated_state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="obfuscated_state must contain at least one row."
        )

    row_count = len(obfuscated_state)
    if row_count > CONFIG["max_batch_rows"]:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Batch row count exceeds limit of {CONFIG['max_batch_rows']}."
        )

    local_dim = int(CONFIG["local_dim"])
    total_floats = 0
    for row in obfuscated_state:
        if len(row) != local_dim:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Input dimension mismatch. Expected last dimension {local_dim}, got {len(row)}"
            )
        total_floats += len(row)
        if total_floats > CONFIG["max_total_floats"]:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Activation payload exceeds limit of {CONFIG['max_total_floats']} floats."
            )


@app.post("/v1/align", response_model=AlignResponse, dependencies=[Depends(verify_token)])
async def align_activations(request: AlignRequest):
    """
    Accepts scrambled local activations, runs the remaining upper layers of the model
    (or a high-fidelity simulator), and returns the cloud representation vector.
    """
    try:
        _validate_obfuscated_state(request.obfuscated_state)

        # Convert input list to PyTorch tensor
        h_rotated = torch.tensor(request.obfuscated_state, dtype=torch.float32, device=CONFIG["device"])

        # --------------------------------------------------------------------------
        # Model Pass vs Simulator Mode
        # --------------------------------------------------------------------------
        if CONFIG["model_id"]:
            # If a model is loaded, we would feed h_rotated directly into the split-layer block here.
            # Example placeholder for actual model slice forward pass:
            # h_cloud = model.forward_remaining(h_rotated)
            logger.info("Executing split-layer pass for model %s", CONFIG["model_id"])
            raise NotImplementedError("Hugging Face split-model loading is placeholder only.")
        else:
            # Simulator Mode:
            # Performs a deterministic linear projection to map the local hidden space (d_local)
            # to the cloud hidden space (d_cloud) + slight noise
            logger.debug("Executing Simulator Mode forward pass...")
            
            # Create a projection matrix matching inputs
            mock_proj = torch.eye(CONFIG["local_dim"], CONFIG["cloud_dim"], device=CONFIG["device"], dtype=torch.float32)
            h_cloud = torch.matmul(h_rotated, mock_proj)

            # Add seeded noise for reproducible mock alignment
            gen = torch.Generator(device=CONFIG["device"])
            gen.manual_seed(0)
            noise = torch.randn(h_cloud.shape, device=CONFIG["device"], dtype=torch.float32, generator=gen) * 0.01
            h_cloud = h_cloud + noise

        # Cast to list for JSON response
        cloud_state_list = h_cloud.detach().cpu().tolist()
        return AlignResponse(cloud_state=cloud_state_list)

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error("Internal processing error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal Server Error: {str(e)}"
        )


@app.get("/health")
async def health_check():
    return {"status": "healthy", "device": CONFIG["device"], "model_id": CONFIG["model_id"]}


def main():
    parser = argparse.ArgumentParser(description="ZkBridge Custom Split-Layer Activation Server")
    parser.add_argument("--host", default="0.0.0.0", help="Binding host address")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    parser.add_argument("--local-dim", type=int, default=CONFIG["local_dim"], help="Local model hidden state dimension")
    parser.add_argument("--cloud-dim", type=int, default=CONFIG["cloud_dim"], help="Cloud model hidden state dimension")
    parser.add_argument("--api-key", default=CONFIG["api_key"], help="Bearer token for incoming requests (or ZK_CLOUD_API_KEY)")
    parser.add_argument("--device", default=CONFIG["device"], help="PyTorch calculation device (cpu, cuda, mps)")
    parser.add_argument("--model-id", default=None, help="Hugging Face model ID for split execution (optional)")
    parser.add_argument("--max-batch-rows", type=int, default=CONFIG["max_batch_rows"], help="Maximum rows accepted per alignment request")
    parser.add_argument("--max-total-floats", type=int, default=CONFIG["max_total_floats"], help="Maximum total floats accepted per alignment request")
    args = parser.parse_args()

    if not args.api_key:
        parser.error("--api-key or ZK_CLOUD_API_KEY is required; no default bearer token is provided.")
    if args.local_dim <= 0 or args.cloud_dim <= 0:
        parser.error("--local-dim and --cloud-dim must be positive.")
    if args.max_batch_rows <= 0 or args.max_total_floats <= 0:
        parser.error("--max-batch-rows and --max-total-floats must be positive.")

    # Update configs
    CONFIG["local_dim"] = args.local_dim
    CONFIG["cloud_dim"] = args.cloud_dim
    CONFIG["api_key"] = args.api_key
    CONFIG["device"] = args.device
    CONFIG["model_id"] = args.model_id
    CONFIG["max_batch_rows"] = args.max_batch_rows
    CONFIG["max_total_floats"] = args.max_total_floats

    try:
        import uvicorn
        logger.info("Starting ZkBridge Cloud Server on http://%s:%d", args.host, args.port)
        logger.info("Configuration: local_dim=%d, cloud_dim=%d, device=%s", args.local_dim, args.cloud_dim, args.device)
        uvicorn.run(app, host=args.host, port=args.port)
    except ImportError:
        logger.error("uvicorn is required to run the server. Run 'pip install uvicorn'")


if __name__ == "__main__":
    main()
