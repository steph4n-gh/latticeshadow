from __future__ import annotations

import logging
from typing import Union, Callable, Any

import torch
from .bridge import ZkBridge
from .exceptions import ConfigurationError

logger = logging.getLogger("latticeshadow_db.integrations")


def register_zk_hook(
    model: torch.nn.Module,
    bridge: ZkBridge,
    layer_path: str | None = None,
    context_provider: str | Callable[[], str] = ""
) -> torch.utils.hooks.RemovableHandle:
    """
    Registers a forward hook on a PyTorch/Hugging Face model to automatically intercept,
    obfuscate, query the cloud bridge, align, and inject corrections during inference.

    Args:
        model: The PyTorch neural network model (e.g. Hugging Face CausalLM).
        bridge: The calibrated ZkBridge instance.
        layer_path: Optional dot-separated attribute path to the target layer. If None,
            the hook tries to auto-detect the last transformer block (e.g., for Llama,
            Gemma, Mistral, Qwen, GPT-2).
        context_provider: A static string or a zero-argument callable that returns the
            current text context.

    Returns:
        A RemovableHandle that can be used to remove the hook via ``handle.remove()``.
    """
    target_layer = None

    if layer_path is not None:
        # Resolve target layer via dot-attribute path
        curr = model
        for name in layer_path.split("."):
            if not hasattr(curr, name):
                raise ConfigurationError(
                    f"Layer path attribute '{name}' not found on {curr}"
                )
            curr = getattr(curr, name)
        if not isinstance(curr, torch.nn.Module):
            raise ConfigurationError(f"Resolved path '{layer_path}' is not a torch.nn.Module")
        target_layer = curr
    else:
        # Auto-detect last transformer block of common architectures
        if hasattr(model, "model") and hasattr(model.model, "layers") and len(model.model.layers) > 0:
            target_layer = model.model.layers[-1]
            logger.info("Auto-detected Hugging Face model style. Registering hook on model.model.layers[-1]")
        elif hasattr(model, "layers") and len(model.layers) > 0:
            target_layer = model.layers[-1]
            logger.info("Auto-detected flat model style. Registering hook on model.layers[-1]")
        elif hasattr(model, "transformer") and hasattr(model.transformer, "h") and len(model.transformer.h) > 0:
            target_layer = model.transformer.h[-1]
            logger.info("Auto-detected GPT-2 style. Registering hook on model.transformer.h[-1]")
        elif hasattr(model, "transformer") and hasattr(model.transformer, "layers") and len(model.transformer.layers) > 0:
            target_layer = model.transformer.layers[-1]
            logger.info("Auto-detected model style. Registering hook on model.transformer.layers[-1]")
        else:
            raise ConfigurationError(
                "Could not auto-detect target layer. Please specify 'layer_path' explicitly."
            )

    def hook_fn(module: torch.nn.Module, inputs: Any, outputs: Any) -> Any:
        # Determine the text context
        if callable(context_provider):
            text_context = context_provider()
        else:
            text_context = context_provider

        # HF layers typically return a tuple: (hidden_states, optional_self_attns, optional_present_key_value)
        is_tuple = isinstance(outputs, tuple)
        hidden_states = outputs[0] if is_tuple else outputs

        if not isinstance(hidden_states, torch.Tensor):
            logger.warning("Intercepted output is not a torch.Tensor. Skipping ZK query.")
            return outputs

        # Query the bridge (which handles device mismatch, obfuscation, SVD alignment, and blending)
        corrected_states = bridge.query(hidden_states, text_context)

        if is_tuple:
            # Reconstruct the outputs tuple with corrected hidden states
            return (corrected_states,) + outputs[1:]
        return corrected_states

    handle = target_layer.register_forward_hook(hook_fn)
    logger.info("Successfully registered ZK activation hook on module: %s", target_layer.__class__.__name__)
    return handle


# --------------------------------------------------------------------------
# Ecosystem Wrappers
# --------------------------------------------------------------------------
from typing import List

try:
    from langchain_core.embeddings import Embeddings
except ImportError:
    class Embeddings:
        pass

try:
    from llama_index.core.embeddings import BaseEmbedding
except ImportError:
    class BaseEmbedding:
        pass


class ZkLangChainEmbeddings(Embeddings):
    """
    ZkBridge wrapper for LangChain Embeddings. Intercepts local embeddings
    and runs them through the privacy bridge.
    """
    def __init__(self, embeddings: Any, bridge: ZkBridge):
        self.embeddings = embeddings
        self.bridge = bridge

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        try:
            local_embeds = self.embeddings.embed_documents(texts)
        except Exception as e:
            logger.warning("Local embed_documents failed: %s", e)
            raise e

        results = []
        for emb, text in zip(local_embeds, texts):
            try:
                emb_tensor = torch.tensor(emb, dtype=torch.float32)
                corrected_tensor = self.bridge.query(emb_tensor, text)
                if corrected_tensor is emb_tensor:
                    results.append(emb)
                else:
                    results.append(corrected_tensor.tolist())
            except Exception as e:
                logger.warning("Bridge query failed in embed_documents: %s. Falling back.", e)
                results.append(emb)
        return results

    def embed_query(self, text: str) -> List[float]:
        try:
            local_emb = self.embeddings.embed_query(text)
        except Exception as e:
            logger.warning("Local embed_query failed: %s", e)
            raise e
        try:
            emb_tensor = torch.tensor(local_emb, dtype=torch.float32)
            corrected_tensor = self.bridge.query(emb_tensor, text)
            if corrected_tensor is emb_tensor:
                return local_emb
            return corrected_tensor.tolist()
        except Exception as e:
            logger.warning("Bridge query failed in embed_query: %s. Falling back.", e)
            return local_emb


class ZkLlamaIndexEmbeddings(BaseEmbedding):
    """
    ZkBridge wrapper for LlamaIndex BaseEmbedding. Intercepts local embeddings
    and runs them through the privacy bridge.
    """
    embeddings: Any = None
    bridge: Any = None

    class Config:
        arbitrary_types_allowed = True

    def __init__(self, embeddings: Any, bridge: ZkBridge, **kwargs: Any):
        # Determine if BaseEmbedding subclasses Pydantic BaseModel
        is_pydantic = False
        try:
            from pydantic import BaseModel
            if issubclass(ZkLlamaIndexEmbeddings, BaseModel):
                is_pydantic = True
        except ImportError:
            pass

        if is_pydantic:
            super().__init__(embeddings=embeddings, bridge=bridge, **kwargs)
        else:
            self.embeddings = embeddings
            self.bridge = bridge
            if hasattr(super(), "__init__"):
                try:
                    super().__init__(**kwargs)
                except TypeError:
                    pass

    @classmethod
    def class_name(cls) -> str:
        return "ZkLlamaIndexEmbeddings"

    def dict(self) -> dict:
        return {
            "embeddings": self.embeddings,
            "bridge": self.bridge
        }

    def model_dump(self) -> dict:
        return self.dict()

    def _get_query_embedding(self, query: str) -> List[float]:
        try:
            local_emb = self.embeddings.get_query_embedding(query)
        except Exception as e:
            logger.warning("Local get_query_embedding failed: %s", e)
            raise e
        try:
            emb_tensor = torch.tensor(local_emb, dtype=torch.float32)
            corrected_tensor = self.bridge.query(emb_tensor, query)
            if corrected_tensor is emb_tensor:
                return local_emb
            return corrected_tensor.tolist()
        except Exception as e:
            logger.warning("Bridge query failed in _get_query_embedding: %s. Falling back.", e)
            return local_emb

    def _get_text_embedding(self, text: str) -> List[float]:
        try:
            local_emb = self.embeddings.get_text_embedding(text)
        except Exception as e:
            logger.warning("Local get_text_embedding failed: %s", e)
            raise e
        try:
            emb_tensor = torch.tensor(local_emb, dtype=torch.float32)
            corrected_tensor = self.bridge.query(emb_tensor, text)
            if corrected_tensor is emb_tensor:
                return local_emb
            return corrected_tensor.tolist()
        except Exception as e:
            logger.warning("Bridge query failed in _get_text_embedding: %s. Falling back.", e)
            return local_emb

    def get_query_embedding(self, query: str) -> List[float]:
        return self._get_query_embedding(query)

    def get_text_embedding(self, text: str) -> List[float]:
        return self._get_text_embedding(text)

    async def aget_query_embedding(self, query: str) -> List[float]:
        try:
            local_emb = await self.embeddings.aget_query_embedding(query)
        except Exception as e:
            logger.warning("Local aget_query_embedding failed: %s", e)
            raise e
        try:
            emb_tensor = torch.tensor(local_emb, dtype=torch.float32)
            corrected_tensor = await self.bridge.query_async(emb_tensor, query)
            if corrected_tensor is emb_tensor:
                return local_emb
            return corrected_tensor.tolist()
        except Exception as e:
            logger.warning("Bridge query_async failed in aget_query_embedding: %s. Falling back.", e)
            return local_emb

    async def aget_text_embedding(self, text: str) -> List[float]:
        try:
            local_emb = await self.embeddings.aget_text_embedding(text)
        except Exception as e:
            logger.warning("Local aget_text_embedding failed: %s", e)
            raise e
        try:
            emb_tensor = torch.tensor(local_emb, dtype=torch.float32)
            corrected_tensor = await self.bridge.query_async(emb_tensor, text)
            if corrected_tensor is emb_tensor:
                return local_emb
            return corrected_tensor.tolist()
        except Exception as e:
            logger.warning("Bridge query_async failed in aget_text_embedding: %s. Falling back.", e)
            return local_emb
