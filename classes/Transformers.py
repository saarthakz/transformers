import torch
import torch.nn as nn
from torch.nn import functional
from typing import Union

device = "cuda" if torch.cuda.is_available() else "cpu"


# Tokenizer functions
class ByteTokenizer:
    def __init__(self, chars: "list[str]") -> None:
        # Map creation
        self.stoi = {}
        self.itos = {}
        for idx, char in enumerate(chars):
            self.stoi[char] = idx
            self.itos[idx] = char
        pass

    def encode(self, text: str):
        output = list(range(len(text)))
        for idx, char in enumerate(text):
            output[idx] = self.stoi[char]

        return output

    def decode(self, arr: list[int]):
        output = list(range(len(arr)))
        for idx in range(len(arr)):
            output[idx] = self.itos[arr[idx]]

        return "".join(output)


# Single Self Attention Head
class SelfAttentionHead(nn.Module):

    def __init__(self, emb_dims, head_size):
        super().__init__()
        self.head_size = head_size
        # Key, Query and Value weights are (D, H)
        self.key = nn.Linear(emb_dims, head_size, bias=False)
        self.query = nn.Linear(emb_dims, head_size, bias=False)
        self.value = nn.Linear(emb_dims, head_size, bias=False)

    # Input is (B, C, D) ; Output is (B, C, H)
    def forward(
        self,
        key_input: torch.Tensor,
        query_input: torch.Tensor,
        value_input: torch.Tensor,
        mask: torch.Tensor,
    ):
        B, C, D = key_input.shape  # Batch, Context, Dimensionality
        query: torch.Tensor = self.query(
            query_input
        )  # (B, C_Q, D) @ (B, D, H) -> (B, C_Q, H)
        key: torch.Tensor = self.key(
            key_input
        )  # (B, C_K, D) @ (B, D, H) -> (B, C_K, H)
        value: torch.Tensor = self.value(
            value_input
        )  # (B, C_K, D) @ (B, D, H) -> (B, C_K, H)
        wei: torch.Tensor = (
            query @ key.transpose(-2, -1) * self.head_size**-0.5
        )  # (B, C_Q, H) @ (B, H, C_K) => (B, C, C)
        # compute attention scores ("affinities")

        wei = wei.masked_fill(mask, float("-inf"))  # (B, C_Q, C_K)
        wei = functional.softmax(wei, dim=-1)  # (B, C_Q, C_K)

        # perform the weighted aggregation of the values

        out = wei @ value  # (B, C_Q, C_K) @ (B, C_K, H) -> (B, C_Q, H)
        return out

# Multiple Self Attention Heads in Parallel
class MultiHeadAttention(nn.Module):

    def __init__(self, num_heads: int, head_size: int):
        super().__init__()

        # Key, Query and Value weights are (D, H)
        self.num_heads = num_heads
        self.head_size = head_size
        self.emb_dim = num_heads * head_size  # Dimensionality
        self.query = nn.Linear(self.emb_dim, self.emb_dim, bias=False)
        self.value = nn.Linear(self.emb_dim, self.emb_dim, bias=False)
        self.key = nn.Linear(self.emb_dim, self.emb_dim, bias=False)
        self.value_proj = nn.Linear(
            self.emb_dim, self.emb_dim
        )  # Additional layer for inter head communication

    def split(self, x: torch.Tensor):
        B, C, D = x.shape
        x = x.view(B, C, self.num_heads, self.head_size)
        return x.permute(0, 2, 1, 3)  # B, N, C, H

    def forward(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        mask: str = "encoder",
        output_attention=False,
    ):

        query = self.split(self.query(query))
        key = self.split(self.key(key))
        value = self.split(self.value(value))

        C_query = query.shape[-2]
        C_key = key.shape[-2]

        if mask == "encoder":
            # Default mask is the encoder mask
            mask = torch.zeros(size=(C_query, C_key)).bool().to(device=value.device)
        else:
            # Decoder lower left tril mask
            mask = (
                torch.triu(
                    torch.full(size=(C_query, C_key), fill_value=-torch.inf), diagonal=1
                )
                .bool()
                .to(device=value.device)
            )

        wei = (query @ key.transpose(-2, -1)) * (
            self.head_size**-0.5
        )  # (B, N, C_Q, H) @ (B, N, H, C_K) => (B, N, C_Q, C_K)

        wei = wei.masked_fill(mask, float("-inf"))  # (B, N, C_Q, C_K)
        wei = functional.softmax(wei, dim=-1)  # (B, N, C_Q, C_K)

        values = wei @ value  # (B, N, C_Q, C_K) @ (B, N, C_K, H) -> (B, N, C_Q, H)
        values = values.permute(0, 2, 1, 3)  # (B, C_Q, N, H)
        B, C_values, N, H = values.shape
        values = values.reshape(B, C_values, N * H)
        values = self.value_proj(values)

        if output_attention:
            return values, wei

        return values

class MHAPyTorchScaledDotProduct(nn.Module):
    def __init__(self, embed_dim, num_heads, dropout=0.0, qkv_bias=False):
        super().__init__()

        assert embed_dim % num_heads == 0, "embed_dim is indivisible by num_heads"

        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.head_dim = embed_dim // num_heads

        self.qkv = nn.Linear(embed_dim, 3 * embed_dim, bias=qkv_bias)
        self.proj = nn.Linear(embed_dim, embed_dim)
        self.dropout = dropout

    def forward(self, x: torch.Tensor, attn_mask: Union[None, torch.Tensor]=None, is_causal=False):
        batch_size, num_tokens, embed_dim = x.shape

        # (b, num_tokens, embed_dim) --> (b, num_tokens, 3 * embed_dim)
        qkv = self.qkv(x)

        # (b, num_tokens, 3 * embed_dim) --> (b, num_tokens, 3, num_heads, head_dim)
        qkv = qkv.view(batch_size, num_tokens, 3, self.num_heads, self.head_dim)

        # (b, num_tokens, 3, num_heads, head_dim) --> (3, b, num_heads, num_tokens, head_dim)
        qkv = qkv.permute(2, 0, 3, 1, 4)

        # (3, b, num_heads, num_tokens, head_dim) -> 3 times (b, num_heads, num_tokens, head_dim)
        queries, keys, values = qkv

        use_dropout = 0. if not self.training else self.dropout

        context_vec = nn.functional.scaled_dot_product_attention(
            queries, keys, values, attn_mask=attn_mask, dropout_p=use_dropout, is_causal=is_causal)

        # Combine heads, where self.d_out = self.num_heads * self.head_dim
        context_vec = context_vec.transpose(1, 2).contiguous().view(batch_size, num_tokens, self.embed_dim)

        context_vec = self.proj(context_vec)

        return context_vec


# A Simple Linear Layer with GELU for adding computational abilities
class FeedForward(nn.Module):

    def __init__(self, emb_dims, inner_dims, dropout=0):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(emb_dims, inner_dims),
            nn.SiLU(),
            nn.Dropout(dropout),
            nn.Linear(inner_dims, emb_dims),
        )

    def forward(self, x):
        return self.net(x)


# Transformer Block: Communication followed by Computation
class Block(nn.Module):

    def __init__(self, emb_dims, num_heads, dropout=0):
        # emb_dims: embedding dimension, num_heads: the number of heads we'd like
        super().__init__()        

        # Communication
        # self.self_att = nn.MultiheadAttention(
        #     embed_dim=emb_dims, num_heads=num_heads, dropout=dropout, batch_first=True
        # )
        # self.self_att = MultiHeadAttention(num_heads, emb_dims // num_heads)
        self.self_att = MHAPyTorchScaledDotProduct(emb_dims, num_heads, dropout)

        # Computation
        self.feed_fwd = FeedForward(emb_dims, emb_dims * 4, dropout)

        # Adding Layer Normalization
        self.ln1 = nn.LayerNorm(emb_dims)
        self.ln2 = nn.LayerNorm(emb_dims)

    def forward(self, x, mask: Union[None, torch.Tensor] = None, is_causal = False):
        """
        For a custom mask, use "mask" but for decoder/causal mask use "is_causal".
        """
        # Residual connections allow the network to learn the simplest possible function. No matter how many complex layer we start by learning a linear function and the complex layers add in non linearity as needed to learn true function.
        x = x + self.self_att.forward(self.ln1(x), mask)
        x = x + self.feed_fwd.forward(self.ln2(x))
        return x


class GPT(nn.Module):

    def __init__(
        self,
        context: int,
        emb_dims: int,
        vocab_size: int,
        num_heads: int,
        mask: Union[None, torch.Tensor]=None
    ):
        super().__init__()

        self.mask = mask
        self.context = context
        self.emb_dims = emb_dims
        self.num_heads = num_heads
        self.vocab_size = vocab_size

        # Token embedding table is used for token identification encoding
        # Position embedding table is used for token position (in reference to the current context) encoding
        self.token_embedding_table = nn.Embedding(vocab_size, emb_dims)
        self.position_embedding_table = nn.Embedding(context, emb_dims)

        self.blocks = nn.ModuleList(
            [
                Block(emb_dims=emb_dims, num_heads=num_heads),
                Block(emb_dims=emb_dims, num_heads=num_heads),
                Block(emb_dims=emb_dims, num_heads=num_heads),
            ]
        )

        # Final layer norm
        self.ln_f = nn.LayerNorm(emb_dims)

        # Model head used for output
        self.head = nn.Linear(emb_dims, vocab_size)

    def forward(self, x, targets=None):
        B, C = x.shape

        # x and targets are both (B,C) tensor of integers
        tok_emb = self.token_embedding_table(x)  # (B,C,D)

        # Getting the position embedding for all the positions, starting from 0 -> context - 1
        pos_emb = self.position_embedding_table(torch.arange(C, device="cuda"))  # (C,D)
        x = tok_emb + pos_emb
        for block in self.blocks:
            x = block(x, self.mask)
        x = self.ln_f(x)
        logits = self.head(x)

        if targets is None:
            loss = None
        else:
            B, C, D = logits.shape
            logits = logits.view(B * C, D)
            targets = targets.view(B * C)
            loss = functional.cross_entropy(logits, targets)

        return logits, loss

    def generate(self, idx, max_new_tokens) -> torch.Tensor:
        # idx is (B, C) array of indices in the current context
        for _ in range(max_new_tokens):

            # crop idx to the last context
            idx_cond = idx[:, -self.context :]

            # Get the predictions
            logits, loss = self.forward(x=idx_cond)

            # Focus only on the last step which contains the output considering the entire context window
            # logits are (batch_size, context = full context considered time step, dimensionality) which is essentially the output vector for each batch
            logits = logits[:, -1, :]

            # Apply softmax to get probabilities
            probs = functional.softmax(logits, dim=1)

            # Sample from the distribution
            idx_next = torch.multinomial(probs, num_samples=1)  # (B, 1)

            # Appended along the context_window hence the context keeps building up
            idx = torch.cat((idx, idx_next), dim=1)  # (batch_size, context_window + 1)
        return idx
