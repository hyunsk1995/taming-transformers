import torch
import torch.nn.functional as F
import pytorch_lightning as pl

from main import instantiate_from_config

from taming.modules.diffusionmodules.model import Encoder, Decoder
from taming.modules.vqvae.quantize import VectorQuantizer2 as VectorQuantizer
from taming.modules.vqvae.quantize import GumbelQuantize
from taming.modules.vqvae.quantize import EMAVectorQuantizer

import cv2


class HierarchicalVQModel(pl.LightningModule):
    def __init__(self,
                 ddconfig,
                 lossconfig,
                 n_embed,
                 embed_dim,
                 ckpt_path=None,
                 ignore_keys=[],
                 image_key="image",
                 colorize_nlabels=None,
                 monitor=None,
                 remap=None,
                 sane_index_shape=False,  # tell vector quantizer to return indices as bhw
                 ):
        super().__init__()

        num_latent_layers = ddconfig["num_latent_layers"]
        n_res_block = ddconfig["num_res_blocks"]
        n_res_channel = ddconfig["residual_units"]
        in_channel = ddconfig["in_channels"]
        channel = ddconfig["ch"]

        self.encoder_b = Encoder(in_channel, channel, n_res_block, n_res_channel, stride=4)
        self.encoder_t = Encoder(channel, channel, n_res_block, n_res_channel, stride=2)

        self.quant_conv_t = torch.nn.Conv2d(channel, embed_dim, 1)
        self.quantize_t = VectorQuantizer(embed_dim, n_embed)

        self.decoder_t = Decoder(
            embed_dim, embed_dim, channel, n_res_block, n_res_channel, stride=2
        )

        self.quant_conv_b = torch.nn.Conv2d(channel, embed_dim, 1)
        self.quantize_b = VectorQuantizer(embed_dim, n_embed)
        self.upsample_t = torch.nn.ConvTranspose2d(
            embed_dim, embed_dim, 4, stride=2, padding=1
        )

        self.decoder_b = Decoder(
            embed_dim,
            in_channel,
            channel,
            n_res_block,
            n_res_channel,
            stride=4,
        )

        self.loss = instantiate_from_config(lossconfig)        
        # self.post_quant_conv = torch.nn.Conv2d(embed_dim, ddconfig["z_channels"], 1)
        
        if ckpt_path is not None:
            self.init_from_ckpt(ckpt_path, ignore_keys=ignore_keys)
        
        self.image_key = image_key
        
        if colorize_nlabels is not None:
            assert type(colorize_nlabels)==int
            self.register_buffer("colorize", torch.randn(3, colorize_nlabels, 1, 1))
        if monitor is not None:
            self.monitor = monitor

    def init_from_ckpt(self, path, ignore_keys=list()):
        sd = torch.load(path, map_location="cpu")["state_dict"]
        keys = list(sd.keys())
        for k in keys:
            for ik in ignore_keys:
                if k.startswith(ik):
                    print("Deleting key {} from state_dict.".format(k))
                    del sd[k]
        self.load_state_dict(sd, strict=False)
        print(f"Restored from {path}")

    def forward(self, input):
        quant_t, quant_b, diff_t, diff_b, _, _ = self.encode(input)
        dec, dec_t, dec_b = self.decode(quant_t, quant_b)

        return dec, dec_t, dec_b, diff_t, diff_b

    def encode(self, input):
        enc_b = self.encoder_b(input)
        enc_t = self.encoder_t(enc_b)

        quant_t = self.quant_conv_t(enc_t).permute(0, 2, 3, 1)
        quant_t, diff_t, id_t = self.quantize_t(quant_t)
        quant_t = quant_t.permute(0, 3, 1, 2)
        diff_t = diff_t.unsqueeze(0)

        quant_b = self.quant_conv_b(enc_b).permute(0, 2, 3, 1)
        quant_b, diff_b, id_b = self.quantize_b(quant_b)
        quant_b = quant_b.permute(0, 3, 1, 2)
        diff_b = diff_b.unsqueeze(0)

        return quant_t, quant_b, diff_t, diff_b, id_t, id_b
    
    # Seperation of encoding used in transformer
    def encode_top(self, input):
        enc_b = self.encoder_b(input)

        enc_t = self.encoder_t(enc_b)
        quant_t = self.quant_conv_t(enc_t).permute(0, 2, 3, 1)
        quant_t, diff_t, id_t = self.quantize_t(quant_t)
        quant_t = quant_t.permute(0, 3, 1, 2)
        diff_t = diff_t.unsqueeze(0)

        return quant_t, id_t

    def encode_bottom(self, input):
        enc_b = self.encoder_b(input)
        enc_t = self.encoder_t(enc_b)

        quant_t = self.quant_conv_t(enc_t).permute(0, 2, 3, 1)
        quant_t, diff_t, id_t = self.quantize_t(quant_t)
        quant_t = quant_t.permute(0, 3, 1, 2)
        diff_t = diff_t.unsqueeze(0)

        dec_t = self.decoder_t(quant_t)
        enc_b = torch.cat([dec_t, enc_b], 1)

        quant_b = self.quant_conv_b(enc_b).permute(0, 2, 3, 1)
        quant_b, diff_b, id_b = self.quantize_b(quant_b)
        quant_b = quant_b.permute(0, 3, 1, 2)
        diff_b = diff_b.unsqueeze(0)

        return quant_b, id_b

    def decode(self, quant_t, quant_b):
        dec_t = self.decoder_t(quant_t)
        dec_b = self.decoder_b(quant_b)
    
        dec = cv2.pyrUp(dec_t) + dec_b

        return dec_t, dec_b, dec
    
    def decode_code(self, code_t, code_b):
        quant_t = self.quantize_t.embed_code(code_t)
        quant_t = quant_t.permute(0, 3, 1, 2)
        quant_b = self.quantize_b.embed_code(code_b)
        quant_b = quant_b.permute(0, 3, 1, 2)

        dec = self.decode(quant_t, quant_b)

        return dec

    def get_input(self, batch, k):
        x = batch[k]
        if len(x.shape) == 3:
            x = x[..., None]
        x = x.permute(0, 3, 1, 2).to(memory_format=torch.contiguous_format)
        return x.float()

    def training_step(self, batch, batch_idx):
        x = self.get_input(batch, self.image_key)
        xrec, lf_rec, hf_rec, qloss_t, qloss_b = self(x)

        # autoencode
        aeloss, log_dict_ae = self.loss(qloss_t, qloss_b, x, lf_rec, hf_rec, xrec, split="train")

        self.log("train/aeloss", aeloss, prog_bar=True, logger=True, on_step=True, on_epoch=True)
        self.log_dict(log_dict_ae, prog_bar=False, logger=True, on_step=True, on_epoch=True)
        return aeloss

    def validation_step(self, batch, batch_idx):
        x = self.get_input(batch, self.image_key)
        xrec, lf_rec, hf_rec, qloss_t, qloss_b = self(x)
        aeloss, log_dict_ae = self.loss(qloss_t, qloss_b, x, lf_rec, hf_rec, xrec, split="val")

        rec_loss = log_dict_ae["val/rec_loss"]
        self.log("val/rec_loss", rec_loss,
                   prog_bar=True, logger=True, on_step=True, on_epoch=True, sync_dist=True)
        self.log("val/aeloss", aeloss,
                   prog_bar=True, logger=True, on_step=True, on_epoch=True, sync_dist=True)
        self.log_dict(log_dict_ae)
        return self.log_dict

    def configure_optimizers(self):
        lr = self.learning_rate
        opt_ae = torch.optim.Adam(list(self.encoder_t.parameters())+
                                  list(self.encoder_b.parameters())+
                                  list(self.decoder_t.parameters())+
                                  list(self.decoder_b.parameters())+
                                  list(self.quantize_t.parameters())+
                                  list(self.quantize_b.parameters())+
                                  list(self.quant_conv_t.parameters())+
                                  list(self.quant_conv_b.parameters()),
                                #   list(self.post_quant_conv.parameters()),
                                  lr=lr, betas=(0.5, 0.9))
        return [opt_ae], []

    def log_images(self, batch, **kwargs):
        log = dict()
        x = self.get_input(batch, self.image_key)
        x = x.to(self.device)
        xrec, _ = self(x)
        if x.shape[1] > 3:
            # colorize with random projection
            assert xrec.shape[1] > 3
            x = self.to_rgb(x)
            xrec = self.to_rgb(xrec)
        log["inputs"] = x
        log["reconstructions"] = xrec
        return log

    def to_rgb(self, x):
        assert self.image_key == "segmentation"
        if not hasattr(self, "colorize"):
            self.register_buffer("colorize", torch.randn(3, x.shape[1], 1, 1).to(x))
        x = F.conv2d(x, weight=self.colorize)
        x = 2.*(x-x.min())/(x.max()-x.min()) - 1.
        return x