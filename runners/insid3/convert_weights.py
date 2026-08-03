"""Invert transformers' DINOv3 conversion: HF safetensors -> the original .pth torch.hub loads.

The forward mapping is transformers' ORIGINAL_TO_CONVERTED_KEY_MAPPING plus a qkv split; the
inverse renames HF keys back and re-fuses q/k/v. torch.hub's STRICT load then validates every
key/shape against the official module — the correctness check.
"""

import re
import sys

import torch
from safetensors.torch import load_file


INVERSE = [
    (r"^embeddings\.cls_token", "cls_token"),
    (r"^embeddings\.mask_token", "mask_token"),
    (r"^embeddings\.register_tokens", "storage_tokens"),
    (r"^embeddings\.patch_embeddings", "patch_embed.proj"),
    (r"^rope_embeddings", "rope_embed"),
    (r"inv_freq", "periods"),
    (r"^layer\.(\d+)\.attention\.o_proj", r"blocks.\1.attn.proj"),
    (r"^layer\.(\d+)\.attention\.", r"blocks.\1.attn."),
    (r"^layer\.(\d+)\.layer_scale(\d+)\.lambda1", r"blocks.\1.ls\2.gamma"),
    (r"^layer\.(\d+)\.mlp\.up_proj", r"blocks.\1.mlp.fc1"),
    (r"^layer\.(\d+)\.mlp\.down_proj", r"blocks.\1.mlp.fc2"),
    (r"^layer\.(\d+)\.mlp", r"blocks.\1.mlp"),
    (r"^layer\.(\d+)\.norm", r"blocks.\1.norm"),
]

src, dst = sys.argv[1], sys.argv[2]
hf = load_file(src)
out = {}
for k, v in hf.items():
    nk = k
    for pat, rep in INVERSE:
        nk = re.sub(pat, rep, nk)
    out[nk] = v

# Re-fuse q/k/v -> qkv (transformers split them; the original module has one fused projection).
fused = {}
for k in list(out.keys()):
    m = re.match(r"^(blocks\.\d+\.attn\.)q_proj\.(weight|bias)$", k)
    if not m:
        continue
    base, kind = m.group(1), m.group(2)
    q = out.pop(f"{base}q_proj.{kind}")
    # DINOv3 forces the K bias to zero; transformers omits it entirely — refuse silently absent
    # WEIGHTS (that would be a real mismatch) but synthesize the zero K bias.
    if f"{base}k_proj.{kind}" in out:
        kk = out.pop(f"{base}k_proj.{kind}")
    elif kind == "bias":
        kk = torch.zeros_like(q)
    else:
        raise KeyError(f"{base}k_proj.{kind}")
    vv = out.pop(f"{base}v_proj.{kind}")
    fused[f"{base}qkv.{kind}"] = torch.cat([q, kk, vv], dim=0)
out.update(fused)
torch.save(out, dst)
print(f"wrote {dst}: {len(out)} tensors")
