"""
Process-wide KeyBERT model for the gate's key-phrase stage. Importing loads the
model (several seconds), so the engine imports collectors lazily.
"""

import threading

from keybert import KeyBERT
from sentence_transformers import SentenceTransformer

from config import EMBED_MODEL_NAME, EMBED_DEVICE

print("🧠 Loading NLP Model (KeyBERT) for dynamic entity extraction...")
kw_model = KeyBERT(model=SentenceTransformer(EMBED_MODEL_NAME, device=EMBED_DEVICE)) # type: ignore
# Shared by the interactive and background threads; the model and its tokenizer
# are not thread-safe, so calls are serialised.
_KW_LOCK = threading.Lock()
