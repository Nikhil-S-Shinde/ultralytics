# Ultralytics 🚀 AGPL-3.0 License - https://ultralytics.com/license
"""Transformer modules."""

import math

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.init import constant_, xavier_uniform_
from torch import Tensor

from .conv import Conv
from .utils import _get_clones, inverse_sigmoid, multi_scale_deformable_attn_pytorch

__all__ = (
    "TransformerEncoderLayer",
    "TransformerLayer",
    "TransformerBlock",
    "MLPBlock",
    "LayerNorm2d",
    "AIFI",
    "DeformableTransformerDecoder",
    "DeformableTransformerDecoderLayer",
    "MSDeformAttn",
    "MLP",
    "MultiHeadAttention",
)


class TransformerEncoderLayer(nn.Module):
    """Defines a single layer of the transformer encoder."""

    def __init__(self, c1, cm=2048, num_heads=8, dropout=0.0, act=nn.GELU(), normalize_before=False):
        """Initialize the TransformerEncoderLayer with specified parameters."""
        super().__init__()
        from ...utils.torch_utils import TORCH_1_9

        if not TORCH_1_9:
            raise ModuleNotFoundError(
                "TransformerEncoderLayer() requires torch>=1.9 to use nn.MultiheadAttention(batch_first=True)."
            )
        self.ma = nn.MultiheadAttention(c1, num_heads, dropout=dropout, batch_first=True)
        # Implementation of Feedforward model
        self.fc1 = nn.Linear(c1, cm)
        self.fc2 = nn.Linear(cm, c1)

        self.norm1 = nn.LayerNorm(c1)
        self.norm2 = nn.LayerNorm(c1)
        self.dropout = nn.Dropout(dropout)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)

        self.act = act
        self.normalize_before = normalize_before

    @staticmethod
    def with_pos_embed(tensor, pos=None):
        """Add position embeddings to the tensor if provided."""
        return tensor if pos is None else tensor + pos

    def forward_post(self, src, src_mask=None, src_key_padding_mask=None, pos=None):
        """Performs forward pass with post-normalization."""
        q = k = self.with_pos_embed(src, pos)
        src2 = self.ma(q, k, value=src, attn_mask=src_mask, key_padding_mask=src_key_padding_mask)[0]
        src = src + self.dropout1(src2)
        src = self.norm1(src)
        src2 = self.fc2(self.dropout(self.act(self.fc1(src))))
        src = src + self.dropout2(src2)
        return self.norm2(src)

    def forward_pre(self, src, src_mask=None, src_key_padding_mask=None, pos=None):
        """Performs forward pass with pre-normalization."""
        src2 = self.norm1(src)
        q = k = self.with_pos_embed(src2, pos)
        src2 = self.ma(q, k, value=src2, attn_mask=src_mask, key_padding_mask=src_key_padding_mask)[0]
        src = src + self.dropout1(src2)
        src2 = self.norm2(src)
        src2 = self.fc2(self.dropout(self.act(self.fc1(src2))))
        return src + self.dropout2(src2)

    def forward(self, src, src_mask=None, src_key_padding_mask=None, pos=None):
        """Forward propagates the input through the encoder module."""
        if self.normalize_before:
            return self.forward_pre(src, src_mask, src_key_padding_mask, pos)
        return self.forward_post(src, src_mask, src_key_padding_mask, pos)


class AIFI(TransformerEncoderLayer):
    """Defines the AIFI transformer layer."""

    def __init__(self, c1, cm=2048, num_heads=8, dropout=0, act=nn.GELU(), normalize_before=False):
        """Initialize the AIFI instance with specified parameters."""
        super().__init__(c1, cm, num_heads, dropout, act, normalize_before)

    def forward(self, x):
        """Forward pass for the AIFI transformer layer."""
        c, h, w = x.shape[1:]
        pos_embed = self.build_2d_sincos_position_embedding(w, h, c)
        # Flatten [B, C, H, W] to [B, HxW, C]
        x = super().forward(x.flatten(2).permute(0, 2, 1), pos=pos_embed.to(device=x.device, dtype=x.dtype))
        return x.permute(0, 2, 1).view([-1, c, h, w]).contiguous()

    @staticmethod
    def build_2d_sincos_position_embedding(w, h, embed_dim=256, temperature=10000.0):
        """Builds 2D sine-cosine position embedding."""
        assert embed_dim % 4 == 0, "Embed dimension must be divisible by 4 for 2D sin-cos position embedding"
        grid_w = torch.arange(w, dtype=torch.float32)
        grid_h = torch.arange(h, dtype=torch.float32)
        grid_w, grid_h = torch.meshgrid(grid_w, grid_h, indexing="ij")
        pos_dim = embed_dim // 4
        omega = torch.arange(pos_dim, dtype=torch.float32) / pos_dim
        omega = 1.0 / (temperature**omega)

        out_w = grid_w.flatten()[..., None] @ omega[None]
        out_h = grid_h.flatten()[..., None] @ omega[None]

        return torch.cat([torch.sin(out_w), torch.cos(out_w), torch.sin(out_h), torch.cos(out_h)], 1)[None]


class TransformerLayer(nn.Module):
    """Transformer layer https://arxiv.org/abs/2010.11929 (LayerNorm layers removed for better performance)."""

    def __init__(self, c, num_heads):
        """Initializes a self-attention mechanism using linear transformations and multi-head attention."""
        super().__init__()
        self.q = nn.Linear(c, c, bias=False)
        self.k = nn.Linear(c, c, bias=False)
        self.v = nn.Linear(c, c, bias=False)
        self.ma = nn.MultiheadAttention(embed_dim=c, num_heads=num_heads)
        self.fc1 = nn.Linear(c, c, bias=False)
        self.fc2 = nn.Linear(c, c, bias=False)

    def forward(self, x):
        """Apply a transformer block to the input x and return the output."""
        x = self.ma(self.q(x), self.k(x), self.v(x))[0] + x
        return self.fc2(self.fc1(x)) + x


class TransformerBlock(nn.Module):
    """Vision Transformer https://arxiv.org/abs/2010.11929."""

    def __init__(self, c1, c2, num_heads, num_layers):
        """Initialize a Transformer module with position embedding and specified number of heads and layers."""
        super().__init__()
        self.conv = None
        if c1 != c2:
            self.conv = Conv(c1, c2)
        self.linear = nn.Linear(c2, c2)  # learnable position embedding
        self.tr = nn.Sequential(*(TransformerLayer(c2, num_heads) for _ in range(num_layers)))
        self.c2 = c2

    def forward(self, x):
        """Forward propagates the input through the bottleneck module."""
        if self.conv is not None:
            x = self.conv(x)
        b, _, w, h = x.shape
        p = x.flatten(2).permute(2, 0, 1)
        return self.tr(p + self.linear(p)).permute(1, 2, 0).reshape(b, self.c2, w, h)


class MLPBlock(nn.Module):
    """Implements a single block of a multi-layer perceptron."""

    def __init__(self, embedding_dim, mlp_dim, act=nn.GELU):
        """Initialize the MLPBlock with specified embedding dimension, MLP dimension, and activation function."""
        super().__init__()
        self.lin1 = nn.Linear(embedding_dim, mlp_dim)
        self.lin2 = nn.Linear(mlp_dim, embedding_dim)
        self.act = act()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass for the MLPBlock."""
        return self.lin2(self.act(self.lin1(x)))


class MLP(nn.Module):
    """Implements a simple multi-layer perceptron (also called FFN)."""

    def __init__(self, input_dim, hidden_dim, output_dim, num_layers, act=nn.ReLU, sigmoid=False):
        """Initialize the MLP with specified input, hidden, output dimensions and number of layers."""
        super().__init__()
        self.num_layers = num_layers
        h = [hidden_dim] * (num_layers - 1)
        self.layers = nn.ModuleList(nn.Linear(n, k) for n, k in zip([input_dim] + h, h + [output_dim]))
        self.sigmoid = sigmoid
        self.act = act()

    def forward(self, x):
        """Forward pass for the entire MLP."""
        for i, layer in enumerate(self.layers):
            x = getattr(self, "act", nn.ReLU())(layer(x)) if i < self.num_layers - 1 else layer(x)
        return x.sigmoid() if getattr(self, "sigmoid", False) else x


class LayerNorm2d(nn.Module):
    """
    2D Layer Normalization module inspired by Detectron2 and ConvNeXt implementations.

    Original implementations in
    https://github.com/facebookresearch/detectron2/blob/main/detectron2/layers/batch_norm.py
    and
    https://github.com/facebookresearch/ConvNeXt/blob/main/models/convnext.py.
    """

    def __init__(self, num_channels, eps=1e-6):
        """Initialize LayerNorm2d with the given parameters."""
        super().__init__()
        self.weight = nn.Parameter(torch.ones(num_channels))
        self.bias = nn.Parameter(torch.zeros(num_channels))
        self.eps = eps

    def forward(self, x):
        """Perform forward pass for 2D layer normalization."""
        u = x.mean(1, keepdim=True)
        s = (x - u).pow(2).mean(1, keepdim=True)
        x = (x - u) / torch.sqrt(s + self.eps)
        return self.weight[:, None, None] * x + self.bias[:, None, None]


class MSDeformAttn(nn.Module):
    """
    Multiscale Deformable Attention Module based on Deformable-DETR and PaddleDetection implementations.

    https://github.com/fundamentalvision/Deformable-DETR/blob/main/models/ops/modules/ms_deform_attn.py
    """

    def __init__(self, d_model=256, n_levels=4, n_heads=8, n_points=4):
        """Initialize MSDeformAttn with the given parameters."""
        super().__init__()
        if d_model % n_heads != 0:
            raise ValueError(f"d_model must be divisible by n_heads, but got {d_model} and {n_heads}")
        _d_per_head = d_model // n_heads
        # Better to set _d_per_head to a power of 2 which is more efficient in a CUDA implementation
        assert _d_per_head * n_heads == d_model, "`d_model` must be divisible by `n_heads`"

        self.im2col_step = 64

        self.d_model = d_model
        self.n_levels = n_levels
        self.n_heads = n_heads
        self.n_points = n_points

        self.sampling_offsets = nn.Linear(d_model, n_heads * n_levels * n_points * 2)
        self.attention_weights = nn.Linear(d_model, n_heads * n_levels * n_points)
        self.value_proj = nn.Linear(d_model, d_model)
        self.output_proj = nn.Linear(d_model, d_model)

        self._reset_parameters()

    def _reset_parameters(self):
        """Reset module parameters."""
        constant_(self.sampling_offsets.weight.data, 0.0)
        thetas = torch.arange(self.n_heads, dtype=torch.float32) * (2.0 * math.pi / self.n_heads)
        grid_init = torch.stack([thetas.cos(), thetas.sin()], -1)
        grid_init = (
            (grid_init / grid_init.abs().max(-1, keepdim=True)[0])
            .view(self.n_heads, 1, 1, 2)
            .repeat(1, self.n_levels, self.n_points, 1)
        )
        for i in range(self.n_points):
            grid_init[:, :, i, :] *= i + 1
        with torch.no_grad():
            self.sampling_offsets.bias = nn.Parameter(grid_init.view(-1))
        constant_(self.attention_weights.weight.data, 0.0)
        constant_(self.attention_weights.bias.data, 0.0)
        xavier_uniform_(self.value_proj.weight.data)
        constant_(self.value_proj.bias.data, 0.0)
        xavier_uniform_(self.output_proj.weight.data)
        constant_(self.output_proj.bias.data, 0.0)

    def forward(self, query, refer_bbox, value, value_shapes, value_mask=None):
        """
        Perform forward pass for multiscale deformable attention.

        https://github.com/PaddlePaddle/PaddleDetection/blob/develop/ppdet/modeling/transformers/deformable_transformer.py

        Args:
            query (torch.Tensor): [bs, query_length, C]
            refer_bbox (torch.Tensor): [bs, query_length, n_levels, 2], range in [0, 1], top-left (0,0),
                bottom-right (1, 1), including padding area
            value (torch.Tensor): [bs, value_length, C]
            value_shapes (List): [n_levels, 2], [(H_0, W_0), (H_1, W_1), ..., (H_{L-1}, W_{L-1})]
            value_mask (Tensor): [bs, value_length], True for non-padding elements, False for padding elements

        Returns:
            output (Tensor): [bs, Length_{query}, C]
        """
        bs, len_q = query.shape[:2]
        len_v = value.shape[1]
        assert sum(s[0] * s[1] for s in value_shapes) == len_v

        value = self.value_proj(value)
        if value_mask is not None:
            value = value.masked_fill(value_mask[..., None], float(0))
        value = value.view(bs, len_v, self.n_heads, self.d_model // self.n_heads)
        sampling_offsets = self.sampling_offsets(query).view(bs, len_q, self.n_heads, self.n_levels, self.n_points, 2)
        attention_weights = self.attention_weights(query).view(bs, len_q, self.n_heads, self.n_levels * self.n_points)
        attention_weights = F.softmax(attention_weights, -1).view(bs, len_q, self.n_heads, self.n_levels, self.n_points)
        # N, Len_q, n_heads, n_levels, n_points, 2
        num_points = refer_bbox.shape[-1]
        if num_points == 2:
            offset_normalizer = torch.as_tensor(value_shapes, dtype=query.dtype, device=query.device).flip(-1)
            add = sampling_offsets / offset_normalizer[None, None, None, :, None, :]
            sampling_locations = refer_bbox[:, :, None, :, None, :] + add
        elif num_points == 4:
            add = sampling_offsets / self.n_points * refer_bbox[:, :, None, :, None, 2:] * 0.5
            sampling_locations = refer_bbox[:, :, None, :, None, :2] + add
        else:
            raise ValueError(f"Last dim of reference_points must be 2 or 4, but got {num_points}.")
        output = multi_scale_deformable_attn_pytorch(value, value_shapes, sampling_locations, attention_weights)
        return self.output_proj(output)


class DeformableTransformerDecoderLayer(nn.Module):
    """
    Deformable Transformer Decoder Layer inspired by PaddleDetection and Deformable-DETR implementations.

    https://github.com/PaddlePaddle/PaddleDetection/blob/develop/ppdet/modeling/transformers/deformable_transformer.py
    https://github.com/fundamentalvision/Deformable-DETR/blob/main/models/deformable_transformer.py
    """

    def __init__(self, d_model=256, n_heads=8, d_ffn=1024, dropout=0.0, act=nn.ReLU(), n_levels=4, n_points=4):
        """Initialize the DeformableTransformerDecoderLayer with the given parameters."""
        super().__init__()

        # Self attention
        self.self_attn = nn.MultiheadAttention(d_model, n_heads, dropout=dropout)
        self.dropout1 = nn.Dropout(dropout)
        self.norm1 = nn.LayerNorm(d_model)

        # Cross attention
        self.cross_attn = MSDeformAttn(d_model, n_levels, n_heads, n_points)
        self.dropout2 = nn.Dropout(dropout)
        self.norm2 = nn.LayerNorm(d_model)

        # FFN
        self.linear1 = nn.Linear(d_model, d_ffn)
        self.act = act
        self.dropout3 = nn.Dropout(dropout)
        self.linear2 = nn.Linear(d_ffn, d_model)
        self.dropout4 = nn.Dropout(dropout)
        self.norm3 = nn.LayerNorm(d_model)

    @staticmethod
    def with_pos_embed(tensor, pos):
        """Add positional embeddings to the input tensor, if provided."""
        return tensor if pos is None else tensor + pos

    def forward_ffn(self, tgt):
        """Perform forward pass through the Feed-Forward Network part of the layer."""
        tgt2 = self.linear2(self.dropout3(self.act(self.linear1(tgt))))
        tgt = tgt + self.dropout4(tgt2)
        return self.norm3(tgt)

    def forward(self, embed, refer_bbox, feats, shapes, padding_mask=None, attn_mask=None, query_pos=None):
        """Perform the forward pass through the entire decoder layer."""
        # Self attention
        q = k = self.with_pos_embed(embed, query_pos)
        tgt = self.self_attn(q.transpose(0, 1), k.transpose(0, 1), embed.transpose(0, 1), attn_mask=attn_mask)[
            0
        ].transpose(0, 1)
        embed = embed + self.dropout1(tgt)
        embed = self.norm1(embed)

        # Cross attention
        tgt = self.cross_attn(
            self.with_pos_embed(embed, query_pos), refer_bbox.unsqueeze(2), feats, shapes, padding_mask
        )
        embed = embed + self.dropout2(tgt)
        embed = self.norm2(embed)

        # FFN
        return self.forward_ffn(embed)


class DeformableTransformerDecoder(nn.Module):
    """
    Implementation of Deformable Transformer Decoder based on PaddleDetection.

    https://github.com/PaddlePaddle/PaddleDetection/blob/develop/ppdet/modeling/transformers/deformable_transformer.py
    """

    def __init__(self, hidden_dim, decoder_layer, num_layers, eval_idx=-1):
        """Initialize the DeformableTransformerDecoder with the given parameters."""
        super().__init__()
        self.layers = _get_clones(decoder_layer, num_layers)
        self.num_layers = num_layers
        self.hidden_dim = hidden_dim
        self.eval_idx = eval_idx if eval_idx >= 0 else num_layers + eval_idx

    def forward(
        self,
        embed,  # decoder embeddings
        refer_bbox,  # anchor
        feats,  # image features
        shapes,  # feature shapes
        bbox_head,
        score_head,
        pos_mlp,
        attn_mask=None,
        padding_mask=None,
    ):
        """Perform the forward pass through the entire decoder."""
        output = embed
        dec_bboxes = []
        dec_cls = []
        last_refined_bbox = None
        refer_bbox = refer_bbox.sigmoid()
        for i, layer in enumerate(self.layers):
            output = layer(output, refer_bbox, feats, shapes, padding_mask, attn_mask, pos_mlp(refer_bbox))

            bbox = bbox_head[i](output)
            refined_bbox = torch.sigmoid(bbox + inverse_sigmoid(refer_bbox))

            if self.training:
                dec_cls.append(score_head[i](output))
                if i == 0:
                    dec_bboxes.append(refined_bbox)
                else:
                    dec_bboxes.append(torch.sigmoid(bbox + inverse_sigmoid(last_refined_bbox)))
            elif i == self.eval_idx:
                dec_cls.append(score_head[i](output))
                dec_bboxes.append(refined_bbox)
                break

            last_refined_bbox = refined_bbox
            refer_bbox = refined_bbox.detach() if self.training else refined_bbox

        return torch.stack(dec_bboxes), torch.stack(dec_cls)

# class MultiHeadAttention(nn.Module):
#     """
#     An attention layer with downscaling capability for embedding size after projection.

#     This class implements a multi-head attention mechanism with the option to downsample the internal
#     dimension of queries, keys, and values.

#     Attributes:
#         embedding_dim (int): Dimensionality of input embeddings.
#         kv_in_dim (int): Dimensionality of key and value inputs.
#         internal_dim (int): Internal dimension after downsampling.
#         num_heads (int): Number of attention heads.
#         q_proj (nn.Linear): Linear projection for queries.
#         k_proj (nn.Linear): Linear projection for keys.
#         v_proj (nn.Linear): Linear projection for values.
#         out_proj (nn.Linear): Linear projection for output.

#     Methods:
#         _separate_heads: Separates input tensor into attention heads.
#         _recombine_heads: Recombines separated attention heads.
#         forward: Computes attention output for given query, key, and value tensors.

#     Examples:
#         >>> attn = Attention(embedding_dim=256, num_heads=8, downsample_rate=2)
#         >>> q = torch.randn(1, 100, 256)
#         >>> k = v = torch.randn(1, 50, 256)
#         >>> output = attn(q, k, v)
#         >>> print(output.shape)
#         torch.Size([1, 100, 256])
#     """

#     def __init__(self, indices, embedding_dim, num_heads, kv_in_dim=None):
#         """
#         Args:
#             indices (list): List of indices for query, key, value tensors
#             embedding_dim (int): Dimension of embeddings
#             num_heads (int): Number of attention heads
#             kv_in_dim (int, optional): Dimension of key/value inputs
#         """
#         super().__init__()                
#         # Type checking
#         assert isinstance(indices, list), f"indices must be list, got {type(indices)}"
#         assert len(indices) == 3, f"indices must contain 3 indices for [query, key, value], got {len(indices)}"
#         assert isinstance(embedding_dim, int), f"embedding_dim must be int, got {type(embedding_dim)}"
#         assert isinstance(num_heads, int), f"num_heads must be int, got {type(num_heads)}"
            
#         # Store parameters
#         self.indices = indices  # Save indices for forward pass
#         self.embedding_dim = embedding_dim
#         self.kv_in_dim = kv_in_dim if kv_in_dim is not None else embedding_dim
#         self.internal_dim = embedding_dim
#         self.num_heads = num_heads
            
#         # Validate dimensions
#         assert self.internal_dim % num_heads == 0, \
#             f"num_heads ({num_heads}) must divide embedding_dim ({self.internal_dim})"    
#         print(f"Attention Init: indices={indices}, embedding_dim={embedding_dim}, "f"num_heads={num_heads}, kv_in_dim={self.kv_in_dim}")
        
#         # Create projection layers
#         self.q_proj = nn.Linear(embedding_dim, self.internal_dim)
#         self.k_proj = nn.Linear(self.kv_in_dim, self.internal_dim)
#         self.v_proj = nn.Linear(self.kv_in_dim, self.internal_dim)
#         self.out_proj = nn.Linear(self.internal_dim, embedding_dim)
        
#     @staticmethod
#     def _separate_heads(x: Tensor, num_heads: int) -> Tensor:
#         """Separates the input tensor into the specified number of attention heads."""
#         b, n, c = x.shape
#         x = x.reshape(b, n, num_heads, c // num_heads)
#         return x.transpose(1, 2)  # B x N_heads x N_tokens x C_per_head

#     @staticmethod
#     def _recombine_heads(x: Tensor) -> Tensor:
#         """Recombines separated attention heads into a single tensor."""
#         b, n_heads, n_tokens, c_per_head = x.shape
#         x = x.transpose(1, 2)
#         return x.reshape(b, n_tokens, n_heads * c_per_head)  # B x N_tokens x C

#     def forward(self, q: Tensor, k: Tensor, v: Tensor) -> Tensor:
#         """Applies multi-head attention to query, key, and value tensors with optional downsampling."""

#        # Fetch q, k, v tensors based on stored indices
#         q = outputs[self.f[0]]  # Query
#         k = outputs[self.f[1]]  # Key
#         v = outputs[self.f[2]]  # Value

#         # Project q, k, v
#         q = self.q_proj(q)
#         k = self.k_proj(k)
#         v = self.v_proj(v)

#         # Separate into attention heads
#         q = self._separate_heads(q, self.num_heads)
#         k = self._separate_heads(k, self.num_heads)
#         v = self._separate_heads(v, self.num_heads)

#         # Compute attention scores
#         d_k = q.shape[-1]  # Dim per head
#         attn_weights = torch.softmax(q @ k.transpose(-2, -1) / math.sqrt(d_k), dim=-1)  # (B, N_heads, Nq, Nk)

#         # Apply attention to values
#         attn_output = attn_weights @ v  # (B, N_heads, Nq, C_per_head)

#         # Recombine attention heads
#         attn_output = self._recombine_heads(attn_output)  # (B, Nq, internal_dim)

#         # Project output back to embedding_dim
#         output = self.out_proj(attn_output)  # (B, Nq, embedding_dim)

#         return output

class MultiHeadAttention(nn.Module):
    """
    Wrapper for torch.nn.MultiheadAttention that works with YOLO architecture.
    Expects inputs to already be in the correct shape (B, N, C) from previous Permute and Flatten ops.
    """
    def __init__(self, indices, embedding_dim, num_heads, kv_in_dim=None):
        super().__init__()
        self.f = indices  # [q_idx, k_idx, v_idx]
        self.embedding_dim = embedding_dim
        self.num_heads = num_heads
        self.kv_in_dim = kv_in_dim if kv_in_dim is not None else embedding_dim

        # Debugging: Ensure values are correct
        print(f"Initializing MultiHeadAttention: indices={indices}, embedding_dim={embedding_dim}, "
              f"num_heads={num_heads}, kv_in_dim={self.kv_in_dim}")

        # Assert that embed_dim is divisible by num_heads
        assert embedding_dim % num_heads == 0, (
            f"embed_dim ({embedding_dim}) must be divisible by num_heads ({num_heads})"
        )
        # Use PyTorch's implementation
        self.attn = nn.MultiheadAttention(
            embed_dim=embedding_dim,
            num_heads=num_heads,
            kdim=self.kv_in_dim,
            vdim=self.kv_in_dim,
            batch_first=True  # Important: expect (B, N, C) format
        )

        # Debugging: Output head_dim calculation
        head_dim = embedding_dim // num_heads
        print(f"MultiHeadAttention initialized successfully: head_dim={head_dim}")

    def forward(self, x, *args):
        """
        Args:
            x: Current input
            args: Will contain the saved outputs list from YOLO
        """
        outputs = args[0] if args else []

        # Debugging: Check saved outputs and indices
        print(f"Forward pass in MultiHeadAttention: indices={self.f}, outputs available={len(outputs)}")
        
        # Get q, k, v tensors - they should already be in (B, N, C) format
        q = outputs[self.f[0]]
        k = outputs[self.f[1]]
        v = outputs[self.f[2]]

        # Debugging: Log shapes of q, k, v
        print(f"Shapes - q: {q.shape}, k: {k.shape}, v: {v.shape}")

        # Apply attention - PyTorch's MultiheadAttention returns attn_output, attn_weights
        attn_output, _ = self.attn(q, k, v)

        # Debugging: Log the output shape
        print(f"Attention output shape: {attn_output.shape}")

        return attn_output
        
        # For YOLO model summary
        self.i = None
        self.type = 'MultiHeadAttention'

    def forward(self, x, *args):
        """
        Args:
            x: Current input
            args: Will contain the saved outputs list from YOLO
        """
        outputs = args[0] if args else []
        
        # Get q, k, v tensors - they should already be in (B, N, C) format
        # from previous Permute and Flatten operations
        q = outputs[self.f[0]]
        k = outputs[self.f[1]]
        v = outputs[self.f[2]]
        
        # Apply attention - PyTorch's MultiheadAttention returns attn_output, attn_weights
        attn_output, _ = self.attn(q, k, v)
        
        return attn_output
