"""A tiny random BERT with its own vocabulary, saved like a Hub base model: for `finetune` tests.

Imports torch and transformers inside the function: only tests marked `finetune` call it.
"""

from pathlib import Path

from support.intent_data import FILLERS, KEYWORDS

SPECIAL = ("[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]")


def save_tiny_bert(folder: Path) -> Path:
    from transformers import BertConfig, BertModel, BertTokenizerFast  # noqa: PLC0415

    words = sorted({w for text in (*FILLERS, *KEYWORDS.values()) for w in text.split()})
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "vocab.txt").write_text("\n".join([*SPECIAL, *words]) + "\n", encoding="utf-8")
    tokenizer = BertTokenizerFast(vocab_file=str(folder / "vocab.txt"), do_lower_case=False)
    config = BertConfig(
        vocab_size=len(SPECIAL) + len(words),
        hidden_size=32,
        num_hidden_layers=2,
        num_attention_heads=2,
        intermediate_size=64,
        max_position_embeddings=256,
    )
    BertModel(config).save_pretrained(str(folder))
    tokenizer.save_pretrained(str(folder))
    return folder
