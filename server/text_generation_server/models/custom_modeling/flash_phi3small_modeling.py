# coding=utf-8
# Copyright 2022 EleutherAI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import List, Optional, Tuple

import torch
import torch.distributed

from torch import nn
from transformers.activations import ACT2FN

from text_generation_server.utils.import_utils import SYSTEM
from text_generation_server.layers.attention import (
    paged_attention,
    attention,
    reshape_and_cache,
)
from text_generation_server.layers import (
    TensorParallelRowLinear,
    TensorParallelColumnLinear,
    TensorParallelEmbedding,
    SpeculativeHead,
)
from text_generation_server.layers.rotary import PositionRotaryEmbedding
from text_generation_server.layers.layernorm import (
    FastLayerNorm,
)

if SYSTEM == "rocm":
    try:
        from vllm import _custom_C
    except Exception as e:
        raise ImportError(f"Could not load `vllm._custom_C`. Full error: {e}")


def load_attention(config, prefix, weights):
    # Only defined in granite.
    bias = getattr(config, "attention_bias", False)
    bias = True
    return TensorParallelColumnLinear.load_qkv(
        config,
        prefix=f"{prefix}.query_key_value",
        weights=weights,
        bias=bias,
        num_heads=config.num_attention_heads,
        num_key_value_heads=config.num_key_value_heads,
    )


class FlashPhi3SmallAttention(torch.nn.Module):
    def __init__(
        self,
        prefix: str,
        config,
        weights,
    ):
        super().__init__()
        self.num_heads = config.num_attention_heads
        self.hidden_size = config.hidden_size
        self.head_size = self.hidden_size // self.num_heads

        self.rotary_emb = PositionRotaryEmbedding.static(
            config=config,
            dim=self.head_size,
            base=config.rope_embedding_base,
            device=weights.device,
        )

        self.softmax_scale = self.head_size**-0.5

        if self.num_heads % weights.process_group.size() != 0:
            raise ValueError(
                f"`num_heads` must be divisible by `num_shards` (got `num_heads`: {self.num_heads} "
                f"and `num_shards`: {weights.process_group.size()}"
            )
        if config.num_key_value_heads % weights.process_group.size() != 0:
            raise ValueError(
                f"`num_key_value_heads` must be divisible by `num_shards` (got `num_key_value_heads`: {config.num_key_value_heads} "
                f"and `num_shards`: {weights.process_group.size()}"
            )
        self.num_heads = self.num_heads // weights.process_group.size()
        self.num_key_value_heads = (
            config.num_key_value_heads // weights.process_group.size()
        )

        self.query_key_value = load_attention(config, prefix, weights)

        self.o_proj = TensorParallelRowLinear.load(
            config,
            prefix=f"{prefix}.dense",
            weights=weights,
            bias=False,
        )

        self.num_groups = self.num_heads // self.num_key_value_heads
        self.kv_head_mapping = torch.arange(
            0, self.num_key_value_heads, dtype=torch.int32, device=weights.device
        ).repeat_interleave(self.num_groups)

    def forward(
        self,
        hidden_states,
        cos,
        sin,
        cu_seqlen_prefill,
        kv_cache,
        block_tables,
        slots,
        input_lengths,
        max_s,
    ):
        qkv = self.query_key_value(hidden_states)
        query, kv = qkv.split(
            [
                self.head_size * self.num_heads,
                2 * self.head_size * self.num_key_value_heads,
            ],
            dim=1,
        )
        query = query.view(-1, self.num_heads, self.head_size)
        kv = kv.view(-1, 2, self.num_key_value_heads, self.head_size)

        self.rotary_emb(query, torch.select(kv, dim=1, index=0), cos, sin)

        reshape_and_cache(kv[:, 0], kv[:, 1], kv_cache[0], kv_cache[1], slots)

        # output tensor
        attn_output = torch.empty_like(query)

        # Prefill
        if cu_seqlen_prefill is not None:
            # flash attention
            attention(
                query,
                torch.select(kv, dim=1, index=0),
                torch.select(kv, dim=1, index=1),
                attn_output,
                cu_seqlen_prefill,
                max_s,
                self.softmax_scale,
            )
        # Decode
        else:
            paged_attention(
                attn_output,
                query,
                kv_cache[0],
                kv_cache[1],
                self.kv_head_mapping,
                self.softmax_scale,
                block_tables,
                input_lengths,
                max_s,
            )

        return self.o_proj(attn_output.view(-1, self.num_heads * self.head_size))


# Copyright (c) Microsoft Corporation, MIT license
# https://huggingface.co/microsoft/Phi-3-small-8k-instruct/blob/e6adf2a152c4cede5e9b88ede22ce5c0af2861fc/modeling_phi3_small.py#L88
# Remove once this is in transformers.
def gegelu(input: torch.Tensor, limit: Optional[float] = None):
    a_gelu, a_linear = input[..., ::2], input[..., 1::2]
    if limit is not None:
        a_gelu = torch.where(
            torch.isinf(a_gelu), a_gelu, a_gelu.clamp(min=None, max=limit)
        )
        a_linear = torch.where(
            torch.isinf(a_linear), a_linear, a_linear.clamp(min=-limit, max=limit)
        )
    out_gelu = a_gelu * torch.sigmoid(1.702 * a_gelu)
    return out_gelu * (a_linear + 1)


class Phi3SmallMLP(nn.Module):
    def __init__(self, prefix, config, weights):
        super().__init__()

        self.hidden_act = config.hidden_act
        if self.hidden_act == "gegelu":
            self.act = lambda x: gegelu(x, config.gegelu_limit)
        elif "gelu" in self.hidden_act:
            self.act = lambda x: torch.nn.functional.gelu(
                x,
                approximate=(
                    "tanh"
                    if self.hidden_act in ["gelu_fast", "gelu_pytorch_tanh"]
                    else "none"
                ),
            )
        else:
            self.act = ACT2FN[self.hidden_act]

        bias = getattr(config, "mlp_bias", False)
        bias = True
        self.up_proj = TensorParallelColumnLinear.load(
            config, prefix=f"{prefix}.up_proj", weights=weights, bias=bias
        )
        self.down_proj = TensorParallelRowLinear.load(
            config,
            prefix=f"{prefix}.down_proj",
            weights=weights,
            bias=bias,
        )
        self.intermediate_size = (
            config.intermediate_size // weights.process_group.size()
        )

        # TODO: This is a hotfix to be removed & properly refactored.
        self.quantize = config.quantize

    def forward(self, hidden_states):
        if (
            SYSTEM == "rocm"
            and self.hidden_act == "silu"
            and hidden_states.shape[0] == 1
            and not self.quantize
        ):
            out = torch.empty(
                hidden_states.shape[0],
                self.intermediate_size,
                dtype=hidden_states.dtype,
                device="cuda",
            )
            _custom_C.LLMM_Silu(self.gate_up_proj.linear.weight, hidden_states, out, 8)
            return self.down_proj(out)
        else:
            return self.down_proj(self.act(self.up_proj(hidden_states)))


class FlashPhi3SmallLayer(nn.Module):
    def __init__(self, prefix, config, weights):
        super().__init__()
        self.self_attn = FlashPhi3SmallAttention(
            prefix=f"{prefix}.self_attn", config=config, weights=weights
        )
        self.mlp = Phi3SmallMLP(prefix=f"{prefix}.mlp", config=config, weights=weights)

        self.input_layernorm = FastLayerNorm.load(
            prefix=f"{prefix}.input_layernorm",
            weights=weights,
            eps=config.layer_norm_epsilon,
        )
        self.post_attention_layernorm = FastLayerNorm.load(
            prefix=f"{prefix}.post_attention_layernorm",
            weights=weights,
            eps=config.layer_norm_epsilon,
        )

    def forward(
        self,
        hidden_states,
        residual,
        cos,
        sin,
        cu_seqlen_prefill,
        kv_cache,
        block_tables,
        slots,
        input_lengths,
        max_s,
    ):
        normed_hidden_states, res = self.input_layernorm(hidden_states, residual)

        # Self Attention
        attn_output = self.self_attn(
            normed_hidden_states,
            cos,
            sin,
            cu_seqlen_prefill,
            kv_cache,
            block_tables,
            slots,
            input_lengths,
            max_s,
        )

        # faster post attention rms norm
        normed_attn_res_output, attn_res = self.post_attention_layernorm(
            attn_output, res
        )

        mlp_output = self.mlp(normed_attn_res_output)

        return mlp_output, attn_res


class FlashPhi3SmallModel(torch.nn.Module):
    def __init__(self, prefix, config, weights):
        super().__init__()

        process_group = weights.process_group
        self.tp_rank = process_group.rank()
        self.tp_world_size = process_group.size()
        self.layers = nn.ModuleList(
            [
                FlashPhi3SmallLayer(
                    prefix=(
                        f"model.layers.{layer_id}"
                        if not prefix
                        else f"{prefix}.model.layers.{layer_id}"
                    ),
                    config=config,
                    weights=weights,
                )
                for layer_id in range(config.num_hidden_layers)
            ]
        )
        self.norm = FastLayerNorm.load(
            prefix=(
                "model.final_layernorm"
                if not prefix
                else f"{prefix}.model.final_layernorm"
            ),
            weights=weights,
            eps=config.layer_norm_epsilon,
        )

        self.gradient_checkpointing = False

        self.head_size = self.layers[0].self_attn.head_size
        self.num_heads = self.layers[0].self_attn.num_heads
        self.num_key_value_heads = self.layers[0].self_attn.num_key_value_heads

    def forward(
        self,
        inputs_embeds: torch.Tensor,
        position_ids: torch.Tensor,
        cu_seqlen_prefill: Optional[torch.Tensor],
        kv_cache: List[Tuple[torch.Tensor, torch.Tensor]],
        block_tables: torch.Tensor,
        slots: torch.Tensor,
        input_lengths: torch.Tensor,
        max_s: int,
        true_max_s: int,
        prefill_cache_indices: Optional[torch.Tensor],
    ) -> torch.Tensor:
        hidden_states = inputs_embeds

        # Get rotary cos and sin for this forward
        # Avoid to index in each layer
        cos, sin = self.layers[0].self_attn.rotary_emb.get_cos_sin(
            position_ids, max_s, hidden_states.dtype
        )

        residual = None
        for i, layer in enumerate(self.layers):
            hidden_states, residual = layer(
                hidden_states,
                residual,
                cos,
                sin,
                cu_seqlen_prefill,
                kv_cache[i],
                block_tables,
                slots,
                input_lengths,
                max_s,
            )

        hidden_states, _ = self.norm(hidden_states, residual)

        return hidden_states


class FlashPhi3SmallForCausalLM(torch.nn.Module):
    def __init__(self, prefix, config, weights):
        super().__init__()

        self.embed_tokens = TensorParallelEmbedding(
            prefix=(
                "model.embed_tokens" if not prefix else f"{prefix}.model.embed_tokens"
            ),
            weights=weights,
        )
        self.model = FlashPhi3SmallModel(prefix, config, weights)
        if config.tie_word_embeddings:
            suffix = "model.embed_tokens"
        else:
            suffix = "lm_head"

        self.lm_head = SpeculativeHead.load(
            config,
            prefix=suffix if not prefix else f"{prefix}.{suffix}",
            weights=weights,
        )

    def forward(
        self,
        input_ids: torch.Tensor,
        position_ids: torch.Tensor,
        cu_seqlen_prefill: Optional[torch.Tensor],
        kv_cache: List[Tuple[torch.Tensor, torch.Tensor]],
        block_tables: torch.Tensor,
        slots: torch.Tensor,
        input_lengths: torch.Tensor,
        max_s: int,
        prefill_cache_indices: Optional[torch.Tensor] = None,
        lm_head_indices: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor]]:
        inputs_embeds = self.embed_tokens(input_ids)
        hidden_states = self.model(
            inputs_embeds,
            position_ids,
            cu_seqlen_prefill,
            kv_cache,
            block_tables,
            slots,
            input_lengths,
            max_s,
            true_max_s=max_s,
            prefill_cache_indices=prefill_cache_indices,
        )
        if lm_head_indices is not None:
            hidden_states = hidden_states[lm_head_indices]
        logits, speculative_logits = self.lm_head(hidden_states)
        return logits, speculative_logits
