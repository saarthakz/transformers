import torch
import torch.nn as nn
import os
import sys

sys.path.append(os.path.abspath("."))
from classes.VIT import (
    PatchEmbeddings,
    Block,
    Upscale,
    PoolDownsample,
    Upsample,
)
from classes.VectorQuantizer import VectorQuantizerEMA
from classes.Swin import res_scaler
import math


class ViT_PoolDownsample_BilinearUpsample(nn.Module):
    def __init__(
        self,
        dim=128,
        input_res: list[int] = (64, 64),
        patch_size=4,
        num_channels=3,
        num_codebook_embeddings=1024,
        codebook_dim=32,
        num_layers=2,
        **kwargs
    ):
        super().__init__()

        self.num_layers = num_layers
        self.patch_embedding = PatchEmbeddings(num_channels, dim, patch_size)
        self.encoder = nn.ModuleList()
        self.decoder = nn.ModuleList()

        res = res_scaler(input_res, 1 / patch_size)
        self.init_patch_res = res

        # Encoder Layers
        for _ in range(self.num_layers):
            self.encoder.append(Block(dim, 4))
            self.encoder.append(Block(dim, 4))
            self.encoder.append(PoolDownsample(res))
            dim = dim * 2
            res = res_scaler(res, 0.5)

        self.pre_quant = nn.Linear(dim, codebook_dim)
        self.vq = VectorQuantizerEMA(num_codebook_embeddings, codebook_dim, 0.5, 0.99)
        self.post_quant = nn.Linear(codebook_dim, dim)

        # Decoder Layers
        for _ in range(self.num_layers):
            self.decoder.append(Block(dim, 4))
            self.decoder.append(Block(dim, 4))
            self.decoder.append(Upsample(res, dim))

            dim = dim // 2
            res = res_scaler(res, 2)

        self.upscale = Upscale(num_channels, dim, patch_size)

    def encode(self, x: torch.Tensor):
        x = self.patch_embedding.forward(x)
        for layer in self.encoder:
            x = layer.forward(x)
        return x

    def decode(self, z_q: torch.Tensor):
        for layer in self.decoder:
            z_q = layer.forward(z_q)

        z_q = self.upscale.forward(z_q, *self.init_patch_res)
        return z_q

    def quantize(self, x_enc: torch.Tensor):
        B, C, D = x_enc.shape
        patch_H, patch_W = res_scaler(self.init_patch_res, 1 / (2**self.num_layers))

        assert (
            C == patch_H * patch_W
        ), "Input patch length does not match the patch resolution"
        x_enc = x_enc.transpose(-2, -1).view(B, D, patch_H, patch_W)
        z_q, indices, loss = self.vq.forward(x_enc)  # Vector Quantizer
        z_q = z_q.view(B, D, C).transpose(-2, -1)
        return z_q, indices, loss

    def forward(self, img: torch.Tensor):
        x_enc = self.encode(img)  # Encoder
        z_q, indices, loss = self.quantize(x_enc)  # Vector Quantizer
        recon_imgs = self.decode(z_q)  # Decoder
        return recon_imgs, indices, loss

    def get_recons(self, x: torch.Tensor):
        recon_imgs, _, _ = self.forward(x)
        return recon_imgs