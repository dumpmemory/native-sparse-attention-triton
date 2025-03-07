# Copyright 2025 Xunhao Lai.
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
import torch
from flash_attn import flash_attn_varlen_func
from native_sparse_attention.ops import (
    compressed_attention,
    topk_sparse_attention,
    weightedpool_compress,
)
from einops import rearrange
from native_sparse_attention.module.rope import RopeConfig, RotaryEmbedding


class NativeSparseAttentionNoRoPE(torch.nn.Module):
    def __init__(
        self,
        hidden_size: int,
        num_q_heads: int,
        num_kv_heads: int,
        head_dim: int,
        kernel_size: int,
        kernel_stride: int,
        block_size: int,
        topk: int,
        init_blocks: int,
        local_blocks: int,
        window_size: int,
    ):
        super().__init__()
        # configs
        self.hidden_size = hidden_size
        self.num_q_heads = num_q_heads
        self.num_kv_heads = num_kv_heads
        self.head_dim = head_dim
        self.kernel_size = kernel_size
        self.kernel_stride = kernel_stride
        self.block_size = block_size
        self.topk = topk
        self.init_blocks = init_blocks
        self.local_blocks = local_blocks
        self.window_size = window_size

        # qkv proj and o proj
        self.proj_q = torch.nn.Linear(
            self.hidden_size, self.num_q_heads * self.head_dim, bias=False
        )
        self.proj_k = torch.nn.Linear(
            self.hidden_size, self.num_kv_heads * self.head_dim, bias=False
        )
        self.proj_v = torch.nn.Linear(
            self.hidden_size, self.num_kv_heads * self.head_dim, bias=False
        )
        self.proj_o = torch.nn.Linear(
            self.num_q_heads * self.head_dim, self.hidden_size, bias=False
        )

        # nsa parameteres
        self.compress_key = torch.nn.Parameter(
            torch.ones(self.num_kv_heads, self.kernel_size) / self.num_kv_heads
        )
        self.compress_value = torch.nn.Parameter(
            torch.ones(self.num_kv_heads, self.kernel_size) / self.num_kv_heads
        )
        self.intra_block_pe = torch.nn.Parameter(
            torch.zeros(self.num_kv_heads, self.kernel_size, self.head_dim)
        )

        # gate function
        self.gate = torch.nn.Sequential(
            torch.nn.Linear(self.hidden_size, self.num_q_heads * 3, bias=False),
            torch.nn.Sigmoid(),
        )

        # init parameters
        self.init_params()

    def init_params(self):
        for p in self.parameters():
            torch.nn.init.xavier_uniform_(p)

    def forward(
        self,
        x: torch.Tensor,  # shape: [total_len, hidden_size]
        cu_seqlens: torch.Tensor,  # shape: [batch_size + 1]
    ):
        # dtype and shape check
        assert x.dtype == torch.bfloat16 or x.dtype == torch.float16
        assert x.shape[-1] == self.hidden_size
        cu_seqlens = cu_seqlens.to(torch.int32)
        seqlens = cu_seqlens[1:] - cu_seqlens[:-1]

        # qkv proj
        q = self.proj_q(x).view(-1, self.num_q_heads, self.head_dim)
        k = self.proj_k(x).view(-1, self.num_kv_heads, self.head_dim)
        v = self.proj_v(x).view(-1, self.num_kv_heads, self.head_dim)

        # compressed attention
        compressed_k, compressed_cu_seqlens = weightedpool_compress(
            k,
            self.compress_key,
            cu_seqlens,
            self.kernel_size,
            self.kernel_stride,
            self.intra_block_pe,
        )
        compressed_v, _ = weightedpool_compress(
            v,
            self.compress_value,
            cu_seqlens,
            self.kernel_size,
            self.kernel_stride,
            None,
        )
        compressed_seqlens = compressed_cu_seqlens[1:] - compressed_cu_seqlens[:-1]
        compressed_attn_output, topk_idx = compressed_attention(
            q,
            compressed_k,
            compressed_v,
            self.kernel_size,
            self.kernel_stride,
            self.block_size,
            self.topk,
            cu_seqlens,
            compressed_cu_seqlens,
            seqlens.max().item(),
            compressed_seqlens.max().item(),
            None,
            self.init_blocks,
            self.local_blocks,
        )

        # topk sparse attention
        sparse_attn_output = topk_sparse_attention(
            q, k, v, topk_idx, self.block_size, cu_seqlens, None
        )

        # sliding window attention
        sliding_attn_output = flash_attn_varlen_func(
            q,
            k,
            v,
            cu_seqlens,
            cu_seqlens,
            seqlens.max().item(),
            seqlens.max().item(),
            causal=True,
            window_size=(self.window_size, -1),
        )

        # gate average
        gate = self.gate(x)
        gate = rearrange(gate, "n (h g) -> n h g", g=3)
        attn_output = (
            gate[..., 0:1] * compressed_attn_output
            + gate[..., 1:2] * sparse_attn_output
            + gate[..., 2:3] * sliding_attn_output
        )

        # rearrange and output proj
        attn_output = rearrange(attn_output, "n h d -> n (h d)")
        attn_output = self.proj_o(attn_output)

        return attn_output


class NativeSparseAttention(torch.nn.Module):
    def __init__(
        self,
        hidden_size: int,
        num_q_heads: int,
        num_kv_heads: int,
        head_dim: int,
        kernel_size: int,
        kernel_stride: int,
        block_size: int,
        topk: int,
        init_blocks: int,
        local_blocks: int,
        window_size: int,
        rope_config: RopeConfig,
    ):
        super().__init__()
        # configs
        self.hidden_size = hidden_size
        self.num_q_heads = num_q_heads
        self.num_kv_heads = num_kv_heads
        self.head_dim = head_dim
        self.kernel_size = kernel_size
        self.kernel_stride = kernel_stride
        self.block_size = block_size
        self.topk = topk
        self.init_blocks = init_blocks
        self.local_blocks = local_blocks
        self.window_size = window_size
        self.rope_config = rope_config
        assert self.head_dim == self.rope_config.head_dim

        # qkv proj and o proj
        self.proj_q = torch.nn.Linear(
            self.hidden_size, self.num_q_heads * self.head_dim, bias=False
        )
        self.proj_k = torch.nn.Linear(
            self.hidden_size, self.num_kv_heads * self.head_dim, bias=False
        )
        self.proj_v = torch.nn.Linear(
            self.hidden_size, self.num_kv_heads * self.head_dim, bias=False
        )
        self.proj_o = torch.nn.Linear(
            self.num_q_heads * self.head_dim, self.hidden_size, bias=False
        )

        # nsa parameteres
        self.compress_key = torch.nn.Parameter(
            torch.ones(self.num_kv_heads, self.kernel_size) / self.num_kv_heads
        )
        self.compress_value = torch.nn.Parameter(
            torch.ones(self.num_kv_heads, self.kernel_size) / self.num_kv_heads
        )
        self.intra_block_pe = torch.nn.Parameter(
            torch.zeros(self.num_kv_heads, self.kernel_size, self.head_dim)
        )

        # gate function
        self.gate = torch.nn.Sequential(
            torch.nn.Linear(self.hidden_size, self.num_q_heads * 3, bias=False),
            torch.nn.Sigmoid(),
        )

        # rope
        self.rope = RotaryEmbedding(self.rope_config)

        # init parameters
        self.init_params()

    def init_params(self):
        for p in self.parameters():
            torch.nn.init.xavier_uniform_(p)

    def forward(
        self,
        x: torch.Tensor,  # shape: [total_len, hidden_size]
        cu_seqlens: torch.Tensor,  # shape: [batch_size + 1]
    ):
        # dtype and shape check
        assert x.dtype == torch.bfloat16 or x.dtype == torch.float16
        assert x.shape[-1] == self.hidden_size
        cu_seqlens = cu_seqlens.to(torch.int32)
        seqlens = cu_seqlens[1:] - cu_seqlens[:-1]

        # qkv proj
        q = self.proj_q(x).view(-1, self.num_q_heads, self.head_dim)
        k = self.proj_k(x).view(-1, self.num_kv_heads, self.head_dim)
        v = self.proj_v(x).view(-1, self.num_kv_heads, self.head_dim)

        # compressed key and value before rope
        compressed_k, compressed_cu_seqlens = weightedpool_compress(
            k,
            self.compress_key,
            cu_seqlens,
            self.kernel_size,
            self.kernel_stride,
            self.intra_block_pe,
        )
        compressed_v, _ = weightedpool_compress(
            v,
            self.compress_value,
            cu_seqlens,
            self.kernel_size,
            self.kernel_stride,
            None,
        )

        # do rope for query and compressed key
        q = self.rope(q, cu_seqlens)
        compressed_k = self.rope(
            compressed_k, compressed_cu_seqlens, start=0, stride=self.kernel_stride
        )

        # attention between query and compressed key value
        compressed_seqlens = compressed_cu_seqlens[1:] - compressed_cu_seqlens[:-1]
        compressed_attn_output, topk_idx = compressed_attention(
            q,
            compressed_k,
            compressed_v,
            self.kernel_size,
            self.kernel_stride,
            self.block_size,
            self.topk,
            cu_seqlens,
            compressed_cu_seqlens,
            seqlens.max().item(),
            compressed_seqlens.max().item(),
            None,
            self.init_blocks,
            self.local_blocks,
        )

        # do rope for original key
        k = self.rope(k, cu_seqlens)

        # topk sparse attention
        sparse_attn_output = topk_sparse_attention(
            q, k, v, topk_idx, self.block_size, cu_seqlens, None
        )

        # sliding window attention
        sliding_attn_output = flash_attn_varlen_func(
            q,
            k,
            v,
            cu_seqlens,
            cu_seqlens,
            seqlens.max().item(),
            seqlens.max().item(),
            causal=True,
            window_size=(self.window_size, -1),
        )

        # gate average
        gate = self.gate(x)
        gate = rearrange(gate, "n (h g) -> n h g", g=3)
        attn_output = (
            gate[..., 0:1] * compressed_attn_output
            + gate[..., 1:2] * sparse_attn_output
            + gate[..., 2:3] * sliding_attn_output
        )

        # rearrange and output proj
        attn_output = rearrange(attn_output, "n h d -> n (h d)")
        attn_output = self.proj_o(attn_output)

        return attn_output
